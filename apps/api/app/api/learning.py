from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.learning import LearningPlan, LearningProgress
from app.models.user import MasterProfile, User
from app.schemas.matching import LearningPlanDetail

router = APIRouter(prefix="/learning", tags=["learning"])


class UpdateLearningProgressRequest(BaseModel):
    completed_steps: list[str] = Field(default_factory=list)
    user_confirmed_learning: bool = False
    confirmation_note: str = Field(default="", max_length=300)


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

    # Sync honest "Currently learning" confirmation into MasterProfile.confirmed_learning
    # NEVER merge into MasterProfile.skills until demonstrated by project/credential (Section 5.7)
    profile = db.query(MasterProfile).filter(MasterProfile.user_id == current_user.id).first()
    if profile:
        existing = [
            item
            for item in (profile.confirmed_learning or [])
            if str(item.get("skill", "")).lower() != plan.skill_name.lower()
        ]
        if payload.user_confirmed_learning:
            source = prog.confirmation_note or "official documentation"
            existing.append(
                {
                    "skill": plan.skill_name,
                    "source": source,
                    "label": f"Currently learning: {plan.skill_name}, via {source}",
                }
            )
        profile.confirmed_learning = existing
        db.add(profile)

    db.add(prog)
    db.commit()
    db.refresh(plan)
    return _to_plan_detail(plan)
