from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_valid_gemini_key
from app.core.config import get_settings
from app.core.rate_limit import rate_limiter
from app.db.session import get_db
from app.models.application import Application, Change, ResumeVersion, SkillAssessment
from app.models.github import GitHubAnalysis
from app.models.job import JobPosting
from app.models.learning import LearningPlan, LearningProgress
from app.models.resume import ResumeFile
from app.models.user import MasterProfile, User
from app.schemas.application import (
    ApplicationCreateRequest,
    ApplicationDetailResponse,
    ApplicationStatusUpdateRequest,
    ApplicationSummaryResponse,
    ChangeDecisionRequest,
    ChangeResponse,
    GitHubDecisionsRequest,
    ResumeVersionResponse,
)
from app.schemas.github import AnalyzedRepo, RepoRecommendation
from app.schemas.job import StructuredJobFields
from app.schemas.matching import CompatibilityBreakdown, LearningPlanDetail, SkillAssessmentItem
from app.schemas.resume import ParsedResumeStructure
from app.services.generation.optimizer import generate_tailored_changes
from app.services.generation.validator import validate_claim_against_evidence
from app.services.github.scorer import rank_repos_for_job
from app.services.learning.roadmap import build_learning_plan
from app.services.llm.factory import get_embedding_client_for_user, get_llm_client_for_user
from app.services.matching.scorer import assess_skills_and_score
from app.services.resume.docx_editor import (
    apply_changes_to_docx_in_place,
    build_ats_docx_from_structure,
    convert_docx_to_pdf_and_preview,
)

router = APIRouter(prefix="/applications", tags=["applications"])


def _to_version_response(v: ResumeVersion) -> ResumeVersionResponse:
    return ResumeVersionResponse(
        id=v.id,
        version_number=v.version_number,
        has_docx=bool(v.output_docx_path and Path(v.output_docx_path).exists()),
        has_pdf=bool(v.output_pdf_path and Path(v.output_pdf_path).exists()),
        has_preview=bool(v.preview_image_path and Path(v.preview_image_path).exists()),
        page_count_original=v.page_count_original,
        page_count_output=v.page_count_output,
        overflow_detected=v.overflow_detected,
        overflow_suggestions=list(v.overflow_suggestions or []),
        changes=[ChangeResponse.model_validate(c) for c in v.changes],
        created_at=v.created_at,
    )


def _to_detail_response(db: Session, app_row: Application) -> ApplicationDetailResponse:
    resume = db.query(ResumeFile).filter(ResumeFile.id == app_row.resume_file_id).first() if app_row.resume_file_id else None
    job = db.query(JobPosting).filter(JobPosting.id == app_row.job_posting_id).first() if app_row.job_posting_id else None
    plans = (
        db.query(LearningPlan)
        .filter(LearningPlan.application_id == app_row.id)
        .all()
    )
    plan_details: list[LearningPlanDetail] = []
    for p in plans:
        data = dict(p.plan_data or {})
        prog = p.progress
        plan_details.append(
            LearningPlanDetail(
                id=p.id,
                skill_name=p.skill_name,
                category=p.category,
                technologies=data.get("technologies", [p.skill_name]),
                ordered_steps=data.get("ordered_steps", []),
                resources=data.get("resources", []),
                project_ideas=data.get("project_ideas", []),
                certifications=data.get("certifications", []),
                how_to_demonstrate=data.get("how_to_demonstrate", ""),
                completed_steps=prog.completed_steps if prog else [],
                user_confirmed_learning=prog.user_confirmed_learning if prog else False,
                confirmation_note=prog.confirmation_note if prog else "",
            )
        )

    breakdown = (
        CompatibilityBreakdown.model_validate(app_row.score_breakdown)
        if app_row.score_breakdown
        else None
    )
    versions = sorted(app_row.versions, key=lambda v: v.version_number, reverse=True)

    return ApplicationDetailResponse(
        id=app_row.id,
        status=app_row.status,
        notes=app_row.notes,
        resume_file_id=app_row.resume_file_id,
        resume_filename=resume.filename if resume else "",
        resume_file_type=resume.file_type if resume else "docx",
        job_posting_id=app_row.job_posting_id,
        job_title=job.title if job else "",
        company=job.company if job else "",
        github_analysis_id=app_row.github_analysis_id,
        compatibility_before=app_row.compatibility_before,
        compatibility_after=app_row.compatibility_after,
        score_breakdown=breakdown,
        skill_assessments=[
            SkillAssessmentItem(
                skill_name=s.skill_name,
                category=s.category,
                importance=s.importance,
                evidence_summary=s.evidence_summary,
                confidence=s.confidence,
            )
            for s in app_row.skill_assessments
        ],
        github_recommendations=[
            RepoRecommendation.model_validate(r) for r in (app_row.github_recommendations or [])
        ],
        learning_plans=plan_details,
        versions=[_to_version_response(v) for v in versions],
        created_at=app_row.created_at,
        updated_at=app_row.updated_at,
    )


@router.get("", response_model=list[ApplicationSummaryResponse])
def list_applications(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ApplicationSummaryResponse]:
    apps = (
        db.query(Application)
        .filter(Application.user_id == current_user.id)
        .order_by(Application.updated_at.desc())
        .all()
    )
    results: list[ApplicationSummaryResponse] = []
    for a in apps:
        job = db.query(JobPosting).filter(JobPosting.id == a.job_posting_id).first() if a.job_posting_id else None
        rf = db.query(ResumeFile).filter(ResumeFile.id == a.resume_file_id).first() if a.resume_file_id else None
        results.append(
            ApplicationSummaryResponse(
                id=a.id,
                status=a.status,
                job_title=job.title if job else "Untitled Role",
                company=job.company if job else "",
                resume_filename=rf.filename if rf else "",
                compatibility_before=a.compatibility_before,
                compatibility_after=a.compatibility_after,
                created_at=a.created_at,
                updated_at=a.updated_at,
            )
        )
    return results


@router.post("", response_model=ApplicationDetailResponse, status_code=status.HTTP_201_CREATED)
def create_application(
    payload: ApplicationCreateRequest,
    current_user: User = Depends(require_valid_gemini_key),
    db: Session = Depends(get_db),
) -> ApplicationDetailResponse:
    rate_limiter.check(f"app_create:{current_user.id}", max_requests=20, window_seconds=60)

    resume = (
        db.query(ResumeFile)
        .filter(ResumeFile.id == payload.resume_file_id, ResumeFile.user_id == current_user.id)
        .first()
    )
    if not resume:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")

    job = (
        db.query(JobPosting)
        .filter(JobPosting.id == payload.job_posting_id, JobPosting.user_id == current_user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")

    gh_analysis = None
    gh_repos: list[AnalyzedRepo] = []
    if payload.github_analysis_id:
        gh_analysis = (
            db.query(GitHubAnalysis)
            .filter(
                GitHubAnalysis.id == payload.github_analysis_id,
                GitHubAnalysis.user_id == current_user.id,
            )
            .first()
        )
        if gh_analysis:
            gh_repos = [AnalyzedRepo.model_validate(r) for r in gh_analysis.repos_data]

    parsed_resume = ParsedResumeStructure.model_validate(resume.parsed_structure)
    embedder = get_embedding_client_for_user(db, current_user)
    llm = get_llm_client_for_user(db, current_user)

    # Initial compatibility score (resume only before GitHub/optimization additions)
    breakdown_before, _ = assess_skills_and_score(
        resume=parsed_resume,
        job_required_skills=list(job.required_skills),
        job_preferred_skills=list(job.preferred_skills),
        job_responsibilities=list(job.responsibilities),
        job_keywords=list(job.keywords),
        github_repos=[],
        embedder=embedder,
    )

    # Full 5-category assessment including GitHub evidence
    breakdown_full, assessments = assess_skills_and_score(
        resume=parsed_resume,
        job_required_skills=list(job.required_skills),
        job_preferred_skills=list(job.preferred_skills),
        job_responsibilities=list(job.responsibilities),
        job_keywords=list(job.keywords),
        github_repos=gh_repos,
        embedder=embedder,
    )

    recs = (
        rank_repos_for_job(
            gh_repos,
            job_skills=list(job.required_skills) + list(job.preferred_skills),
            job_keywords=list(job.keywords),
            resume_text=resume.raw_text,
        )
        if gh_repos
        else []
    )

    app_row = Application(
        user_id=current_user.id,
        resume_file_id=resume.id,
        job_posting_id=job.id,
        github_analysis_id=gh_analysis.id if gh_analysis else None,
        status="Draft",
        compatibility_before=breakdown_before.overall_score,
        compatibility_after=breakdown_full.overall_score,
        score_breakdown=breakdown_full.model_dump(mode="json"),
        github_recommendations=[r.model_dump(mode="json") for r in recs],
    )
    db.add(app_row)
    db.flush()

    for item in assessments:
        db.add(
            SkillAssessment(
                application_id=app_row.id,
                skill_name=item.skill_name,
                category=item.category,
                importance=item.importance,
                evidence_summary=item.evidence_summary,
                confidence=item.confidence,
            )
        )
        if item.category in {"needs_development", "missing"}:
            plan_detail = build_learning_plan(
                skill_name=item.skill_name,
                category=item.category,
                job_title=job.title,
                llm=llm,
            )
            plan_row = LearningPlan(
                user_id=current_user.id,
                application_id=app_row.id,
                skill_name=item.skill_name,
                category=item.category,
                plan_data=plan_detail.model_dump(mode="json"),
            )
            db.add(plan_row)
            db.flush()
            db.add(LearningProgress(learning_plan_id=plan_row.id, completed_steps=[]))

    db.commit()
    db.refresh(app_row)
    return _to_detail_response(db, app_row)


@router.get("/{app_id}", response_model=ApplicationDetailResponse)
def get_application(
    app_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApplicationDetailResponse:
    app_row = (
        db.query(Application)
        .filter(Application.id == app_id, Application.user_id == current_user.id)
        .first()
    )
    if not app_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")
    return _to_detail_response(db, app_row)


@router.patch("/{app_id}/status", response_model=ApplicationDetailResponse)
def update_application_status(
    app_id: str,
    payload: ApplicationStatusUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApplicationDetailResponse:
    app_row = (
        db.query(Application)
        .filter(Application.id == app_id, Application.user_id == current_user.id)
        .first()
    )
    if not app_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")
    app_row.status = payload.status
    app_row.notes = payload.notes
    db.add(app_row)
    db.commit()
    db.refresh(app_row)
    return _to_detail_response(db, app_row)


@router.post("/{app_id}/github-decisions", response_model=ApplicationDetailResponse)
def update_github_decisions(
    app_id: str,
    payload: GitHubDecisionsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApplicationDetailResponse:
    app_row = (
        db.query(Application)
        .filter(Application.id == app_id, Application.user_id == current_user.id)
        .first()
    )
    if not app_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")
    decision_map = {d.repo_name: d.decision for d in payload.decisions}
    updated = []
    for rec in app_row.github_recommendations or []:
        rec_copy = dict(rec)
        if rec_copy.get("repo_name") in decision_map:
            rec_copy["decision"] = decision_map[rec_copy["repo_name"]]
        updated.append(rec_copy)
    app_row.github_recommendations = updated
    db.add(app_row)
    db.commit()
    db.refresh(app_row)
    return _to_detail_response(db, app_row)


@router.post("/{app_id}/generate", response_model=ApplicationDetailResponse)
def generate_resume_version(
    app_id: str,
    current_user: User = Depends(require_valid_gemini_key),
    db: Session = Depends(get_db),
) -> ApplicationDetailResponse:
    rate_limiter.check(f"generate:{current_user.id}", max_requests=15, window_seconds=60)
    app_row = (
        db.query(Application)
        .filter(Application.id == app_id, Application.user_id == current_user.id)
        .first()
    )
    if not app_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")

    resume = db.query(ResumeFile).filter(ResumeFile.id == app_row.resume_file_id).first()
    job = db.query(JobPosting).filter(JobPosting.id == app_row.job_posting_id).first()
    if not resume or not job:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Resume or job missing.")

    gh_repos: list[AnalyzedRepo] = []
    if app_row.github_analysis_id:
        gh = db.query(GitHubAnalysis).filter(GitHubAnalysis.id == app_row.github_analysis_id).first()
        if gh:
            gh_repos = [AnalyzedRepo.model_validate(r) for r in gh.repos_data]

    profile = db.query(MasterProfile).filter(MasterProfile.user_id == current_user.id).first()
    confirmed_learning = list(profile.confirmed_learning or []) if profile else []

    parsed_resume = ParsedResumeStructure.model_validate(resume.parsed_structure)
    job_fields = StructuredJobFields(
        title=job.title,
        company=job.company,
        location=job.location,
        experience_level=job.experience_level,
        responsibilities=list(job.responsibilities),
        required_skills=list(job.required_skills),
        preferred_skills=list(job.preferred_skills),
        keywords=list(job.keywords),
    )
    recs = [RepoRecommendation.model_validate(r) for r in (app_row.github_recommendations or [])]
    llm = get_llm_client_for_user(db, current_user)

    proposed_changes = generate_tailored_changes(
        resume=parsed_resume,
        raw_resume_text=resume.raw_text,
        element_map=resume.element_map or {},
        job=job_fields,
        github_repos=gh_repos,
        approved_github_recs=recs,
        confirmed_learning=confirmed_learning,
        llm=llm,
    )

    next_ver_num = max((v.version_number for v in app_row.versions), default=0) + 1
    version = ResumeVersion(
        application_id=app_row.id,
        version_number=next_ver_num,
        page_count_original=resume.page_count,
    )
    db.add(version)
    db.flush()

    for ch in proposed_changes:
        db.add(Change(resume_version_id=version.id, **ch))

    db.commit()
    db.refresh(app_row)
    return _to_detail_response(db, app_row)


@router.patch("/{app_id}/changes/{change_id}", response_model=ChangeResponse)
def update_change_decision(
    app_id: str,
    change_id: str,
    payload: ChangeDecisionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Change:
    app_row = (
        db.query(Application)
        .filter(Application.id == app_id, Application.user_id == current_user.id)
        .first()
    )
    if not app_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")

    change = (
        db.query(Change)
        .join(ResumeVersion)
        .filter(Change.id == change_id, ResumeVersion.application_id == app_row.id)
        .first()
    )
    if not change:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Change not found.")

    if payload.status == "accepted" and change.validation_status == "blocked":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Blocked changes contain unverified claims. Edit the text to match your evidence before accepting.",
        )

    if payload.status == "edited" or payload.edited_text is not None:
        edited = (payload.edited_text or "").strip()
        if not edited:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Edited text cannot be empty.")
        resume = db.query(ResumeFile).filter(ResumeFile.id == app_row.resume_file_id).first()
        gh_repos: list[AnalyzedRepo] = []
        if app_row.github_analysis_id:
            gh = db.query(GitHubAnalysis).filter(GitHubAnalysis.id == app_row.github_analysis_id).first()
            if gh:
                gh_repos = [AnalyzedRepo.model_validate(r) for r in gh.repos_data]
        profile = db.query(MasterProfile).filter(MasterProfile.user_id == current_user.id).first()
        val = validate_claim_against_evidence(
            new_text=edited,
            original_text=change.original_text,
            resume=ParsedResumeStructure.model_validate(resume.parsed_structure) if resume else ParsedResumeStructure(),
            raw_resume_text=resume.raw_text if resume else "",
            github_repos=gh_repos,
            confirmed_learning=list(profile.confirmed_learning or []) if profile else [],
        )
        change.edited_text = edited
        change.validation_status = val.status
        change.validation_warnings = val.warnings
        if val.status == "blocked":
            change.status = "pending"
        else:
            change.status = "edited"
    else:
        change.status = payload.status

    db.add(change)
    db.commit()
    db.refresh(change)
    return change


@router.post("/{app_id}/versions/{version_id}/bulk-accept", response_model=ResumeVersionResponse)
def bulk_accept_low_risk_changes(
    app_id: str,
    version_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ResumeVersionResponse:
    """Bulk-accept ONLY low-risk reordering changes. Changes adding new info always require individual decisions."""
    version = (
        db.query(ResumeVersion)
        .join(Application)
        .filter(
            ResumeVersion.id == version_id,
            ResumeVersion.application_id == app_id,
            Application.user_id == current_user.id,
        )
        .first()
    )
    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume version not found.")

    for ch in version.changes:
        if ch.change_type == "reordered" and ch.validation_status != "blocked" and ch.status == "pending":
            ch.status = "accepted"
            db.add(ch)
    db.commit()
    db.refresh(version)
    return _to_version_response(version)


@router.post("/{app_id}/versions/{version_id}/export", response_model=ResumeVersionResponse)
def export_resume_version(
    app_id: str,
    version_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ResumeVersionResponse:
    settings = get_settings()
    app_row = (
        db.query(Application)
        .filter(Application.id == app_id, Application.user_id == current_user.id)
        .first()
    )
    if not app_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")

    version = (
        db.query(ResumeVersion)
        .filter(ResumeVersion.id == version_id, ResumeVersion.application_id == app_id)
        .first()
    )
    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")

    resume = db.query(ResumeFile).filter(ResumeFile.id == app_row.resume_file_id).first()
    if not resume:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source resume not found.")

    approved_changes = [
        {
            "section": ch.section,
            "change_type": ch.change_type,
            "target_location": ch.target_location,
            "original_text": ch.original_text,
            "new_text": ch.new_text,
            "edited_text": ch.edited_text,
        }
        for ch in version.changes
        if ch.status in {"accepted", "edited"} and ch.validation_status != "blocked"
    ]

    out_dir = Path(settings.OUTPUT_DIR) / current_user.id / version.id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_docx = out_dir / "ResumeIQ_Optimized.docx"
    out_pdf = out_dir / "ResumeIQ_Optimized.pdf"
    out_png = out_dir / "preview_page1.png"

    source_path = Path(resume.storage_path)
    orig_pages = resume.page_count

    if resume.file_type == "docx" and source_path.exists():
        # Count original pages if not recorded yet
        if orig_pages is None:
            orig_pdf = out_dir / "_orig.pdf"
            orig_png = out_dir / "_orig.png"
            orig_pages, _, _ = convert_docx_to_pdf_and_preview(source_path, orig_pdf, orig_png)
            resume.page_count = orig_pages
            db.add(resume)

        apply_changes_to_docx_in_place(
            source_docx_path=source_path,
            output_docx_path=out_docx,
            approved_changes=approved_changes,
        )
    else:
        parsed = ParsedResumeStructure.model_validate(resume.parsed_structure)
        build_ats_docx_from_structure(
            structure=parsed,
            approved_changes=approved_changes,
            output_docx_path=out_docx,
        )

    out_pages, _, _ = convert_docx_to_pdf_and_preview(out_docx, out_pdf, out_png)
    overflow = bool(orig_pages and out_pages > orig_pages)
    suggestions: list[str] = []
    if overflow:
        suggestions.append(
            f"Optimized resume is {out_pages} pages (original was {orig_pages}). "
            "Consider rejecting lower-relevance project bullets or older experience lines rather than shrinking font sizes."
        )

    version.output_docx_path = str(out_docx)
    version.output_pdf_path = str(out_pdf)
    version.preview_image_path = str(out_png)
    version.page_count_original = orig_pages
    version.page_count_output = out_pages
    version.overflow_detected = overflow
    version.overflow_suggestions = suggestions
    db.add(version)
    db.commit()
    db.refresh(version)
    return _to_version_response(version)


@router.get("/{app_id}/versions/{version_id}/download/{kind}")
def download_version_artifact(
    app_id: str,
    version_id: str,
    kind: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    version = (
        db.query(ResumeVersion)
        .join(Application)
        .filter(
            ResumeVersion.id == version_id,
            ResumeVersion.application_id == app_id,
            Application.user_id == current_user.id,
        )
        .first()
    )
    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")

    if kind == "docx" and version.output_docx_path and Path(version.output_docx_path).exists():
        return FileResponse(
            version.output_docx_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="ResumeIQ_Optimized.docx",
        )
    if kind == "pdf" and version.output_pdf_path and Path(version.output_pdf_path).exists():
        return FileResponse(
            version.output_pdf_path,
            media_type="application/pdf",
            filename="ResumeIQ_Optimized.pdf",
        )
    if kind == "preview" and version.preview_image_path and Path(version.preview_image_path).exists():
        return FileResponse(
            version.preview_image_path,
            media_type="image/png",
        )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export artifact not generated yet.")
