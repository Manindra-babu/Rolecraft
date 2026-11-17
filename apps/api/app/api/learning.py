from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.application import Application, Change, ResumeVersion
from app.models.learning import LearningPlan, LearningProgress
from app.models.user import MasterProfile, User
from app.schemas.matching import LearningPlanDetail

router = APIRouter(prefix="/learning", tags=["learning"])


class UpdateLearningProgressRequest(BaseModel):
    completed_steps: list[str] = Field(default_factory=list)
    user_confirmed_learning: bool = False
    confirmation_note: str = Field(default="", max_length=300)
    application_id: str | None = None
    status: str | None = None


def _to_plan_detail(plan: LearningPlan) -> LearningPlanDetail:
    data = dict(plan.plan_data or {})
    prog = plan.progress
    return LearningPlanDetail(
        id=plan.id,
        skill_name=plan.skill_name,
        category=plan.category,
        technologies=data.get("technologies", [plan.skill_name]),
        ordered_steps=data.get("ordered_steps", []),
        resources=data.get("resources", []),
        project_ideas=data.get("project_ideas", []),
        certifications=data.get("certifications", []),
        how_to_demonstrate=data.get("how_to_demonstrate", ""),
        completed_steps=prog.completed_steps if prog else [],
        user_confirmed_learning=prog.user_confirmed_learning if prog else False,
        confirmation_note=prog.confirmation_note if prog else "",
    )


@router.get("", response_model=list[LearningPlanDetail])
def list_learning_plans(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[LearningPlanDetail]:
    plans = (
        db.query(LearningPlan)
        .filter(LearningPlan.user_id == current_user.id)
        .order_by(LearningPlan.created_at.desc())
        .all()
    )
    return [_to_plan_detail(p) for p in plans]


@router.put("/{plan_id}/progress", response_model=LearningPlanDetail)
def update_learning_progress(
    plan_id: str,
    payload: UpdateLearningProgressRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LearningPlanDetail:
    plan = (
        db.query(LearningPlan)
        .filter(LearningPlan.id == plan_id, LearningPlan.user_id == current_user.id)
        .first()
    )
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning plan not found.")

    prog = plan.progress
    if not prog:
        prog = LearningProgress(learning_plan_id=plan.id)
        db.add(prog)

    prog.completed_steps = payload.completed_steps
    prog.user_confirmed_learning = payload.user_confirmed_learning
    prog.confirmation_note = payload.confirmation_note.strip()

    # Sync honest "Currently learning" or "Learned" confirmation into MasterProfile.confirmed_learning
    profile = db.query(MasterProfile).filter(MasterProfile.user_id == current_user.id).first()
    confirmed_items: list[dict[str, str]] = []
    p_steps = (getattr(plan, "plan_data", {}) or {}).get("steps") or getattr(plan, "ordered_steps", None) or []
    req_status = (getattr(payload, "status", None) or "").lower()
    is_learned = bool(
        payload.user_confirmed_learning
        and (
            req_status in {"learned", "completed", "proficient"}
            or "learned" in prog.confirmation_note.lower()
            or (bool(p_steps) and len(prog.completed_steps) >= len(p_steps))
        )
    )
    status_str = "learned" if is_learned else "in_progress"
    if profile:
        existing = [
            item
            for item in (profile.confirmed_learning or [])
            if str(item.get("skill", "")).lower() != plan.skill_name.lower()
        ]
        if payload.user_confirmed_learning:
            source = prog.confirmation_note or "active learning plan"
            existing.append(
                {
                    "skill": plan.skill_name,
                    "status": status_str,
                    "source": source,
                    "confirmation_note": prog.confirmation_note,
                    "label": plan.skill_name,
                }
            )
        profile.confirmed_learning = existing
        confirmed_items = list(existing)
        if is_learned:
            cur_skills = list(profile.skills or [])
            if plan.skill_name not in cur_skills:
                cur_skills.append(plan.skill_name)
                profile.skills = cur_skills
        db.add(profile)
    elif payload.user_confirmed_learning:
        confirmed_items = [
            {
                "skill": plan.skill_name,
                "status": status_str,
                "source": prog.confirmation_note or "active learning plan",
                "confirmation_note": prog.confirmation_note,
                "label": plan.skill_name,
            }
        ]

    # Also keep ONE consolidated pending user_confirmed_skill change in the latest draft ResumeVersion
    target_apps = (
        db.query(Application)
        .filter(Application.user_id == current_user.id)
        .filter(Application.id == payload.application_id if payload.application_id else True)
        .all()
    )
    active_learning_items = [it for it in confirmed_items if it.get("status") != "learned"]
    skills_list = [str(it.get("skill") or it.get("label") or "").strip() for it in active_learning_items if str(it.get("skill") or it.get("label") or "").strip()]
    learning_refs = [
        {
            "source": "user_confirmed",
            "detail": (
                f"{it.get('skill')}: {it.get('source')}"
                if it.get("source")
                else f"Confirmed Currently Learning: {it.get('skill')}"
            ),
        }
        for it in active_learning_items
    ]
    for app_obj in target_apps:
        latest_ver = (
            db.query(ResumeVersion)
            .filter(ResumeVersion.application_id == app_obj.id)
            .order_by(ResumeVersion.version_number.desc())
            .first()
        )
        if not latest_ver:
            continue
        existing_uc = [c for c in latest_ver.changes if c.change_type == "user_confirmed_skill"]
        if skills_list:
            consolidated = f"Currently Learning: {', '.join(dict.fromkeys(skills_list))}"
            if existing_uc:
                primary_uc = existing_uc[0]
                primary_uc.section = "skills"
                primary_uc.target_location = {"insert_section": "skills", "skills": skills_list}
                primary_uc.new_text = consolidated
                primary_uc.evidence_refs = learning_refs
                db.add(primary_uc)
                for extra_uc in existing_uc[1:]:
                    db.delete(extra_uc)
            else:
                db.add(
                    Change(
                        resume_version_id=latest_ver.id,
                        section="skills",
                        change_type="user_confirmed_skill",
                        target_location={"insert_section": "skills", "skills": skills_list},
                        original_text="",
                        new_text=consolidated,
                        reason="Explicitly confirmed active learning (added as a separate Currently Learning line in Skills).",
                        evidence_refs=learning_refs,
                        validation_status="passed",
                        validation_warnings=[],
                        status="pending",
                    )
                )
        else:
            for extra_uc in existing_uc:
                db.delete(extra_uc)

    db.add(prog)
    db.commit()
    db.refresh(plan)
    return _to_plan_detail(plan)
