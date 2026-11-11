from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, Response
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
from app.services.github.client import analyze_github_profile
from app.services.github.scorer import rank_repos_for_job
from app.services.learning.roadmap import build_learning_plan
from app.services.llm.factory import get_embedding_client_for_user, get_llm_client_for_user
from app.services.matching.scorer import assess_skills_and_score
from app.services.resume.docx_editor import (
    apply_changes_to_docx_in_place,
    build_ats_docx_from_structure,
    convert_docx_to_pdf_and_preview,
)
from app.services.resume.pdf_editor import apply_changes_to_pdf_in_place
from app.services.resume.theme_verifier import verify_pdf_theme_match

router = APIRouter(prefix="/applications", tags=["applications"])


def _to_version_response(v: ResumeVersion) -> ResumeVersionResponse:
    return ResumeVersionResponse(
        id=v.id,
        version_number=v.version_number,
        has_docx=bool(v.output_docx_bytes or (v.output_docx_path and Path(v.output_docx_path).exists())),
        has_pdf=bool(v.output_pdf_bytes or (v.output_pdf_path and Path(v.output_pdf_path).exists())),
        has_preview=bool(
            v.preview_image_bytes or (v.preview_image_path and Path(v.preview_image_path).exists())
        ),
        has_orig_preview=bool(
            v.orig_preview_image_bytes
            or (v.orig_preview_image_path and Path(v.orig_preview_image_path).exists())
        ),
        page_count_original=v.page_count_original,
        page_count_output=v.page_count_output,
        overflow_detected=v.overflow_detected,
        overflow_suggestions=list(v.overflow_suggestions or []),
        similarity_score=v.similarity_score,
        unchanged_similarity_score=v.unchanged_similarity_score,
        theme_warnings=list(v.theme_warnings or []),
        export_blocked=bool(v.export_blocked),
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
    gh = (
        db.query(GitHubAnalysis).filter(GitHubAnalysis.id == app_row.github_analysis_id).first()
        if app_row.github_analysis_id
        else None
    )

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
        github_status=app_row.github_status or ("analyzed" if gh else "not_provided"),
        github_username=gh.github_username if gh else None,
        github_repo_count=len(gh.repos_data or []) if gh else 0,
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

    parsed_resume = ParsedResumeStructure.model_validate(resume.parsed_structure)
    gh_analysis = None
    gh_repos: list[AnalyzedRepo] = []
    github_status = "not_provided"
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
            github_status = f"analyzed (@{gh_analysis.github_username}, {len(gh_repos)} repos)"
    else:
        # Auto-detect GitHub profile/repo URL from resume links, projects, or master profile
        detected_gh = ""
        for lnk in parsed_resume.links or []:
            l_url = lnk.get("url", "") if isinstance(lnk, dict) else getattr(lnk, "url", "")
            if "github.com/" in (l_url or "").lower():
                detected_gh = l_url
                break
        if not detected_gh:
            for proj in parsed_resume.projects or []:
                if "github.com/" in (proj.url or "").lower():
                    detected_gh = proj.url
                    break
        if not detected_gh:
            prof = db.query(MasterProfile).filter(MasterProfile.user_id == current_user.id).first()
            if prof and prof.github_url and "github.com/" in prof.github_url.lower():
                detected_gh = prof.github_url
        if detected_gh:
            try:
                gh_res = analyze_github_profile(detected_gh, db=db, user=current_user)
                if not gh_res.skipped and gh_res.id:
                    gh_analysis = (
                        db.query(GitHubAnalysis)
                        .filter(GitHubAnalysis.id == gh_res.id)
                        .first()
                    )
                    gh_repos = list(gh_res.repos)
                    github_status = f"analyzed (@{gh_res.github_username}, {len(gh_repos)} repos)"
                else:
                    github_status = "skipped"
            except Exception as exc:
                github_status = f"unavailable ({type(exc).__name__})"
        else:
            github_status = "no_github_link"

    embedder = get_embedding_client_for_user(db, current_user)
    llm = get_llm_client_for_user(db, current_user)

    # Initial compatibility score (resume only before GitHub/optimization additions)
    breakdown_before, _ = assess_skills_and_score(
        resume=parsed_resume,
        job_required_skills=list(job.required_skills),
        job_preferred_skills=list(job.preferred_skills),
        job_responsibilities=list(job.responsibilities),
        job_keywords=list(job.keywords),
        job_or_groups=list(job.or_groups or []),
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
        job_or_groups=list(job.or_groups or []),
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
        github_status=github_status,
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
        or_groups=list(job.or_groups or []),
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
            section=change.section,
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
            "status": ch.status,
            "validation_status": ch.validation_status,
        }
        for ch in version.changes
        if ch.status in {"accepted", "edited"} and ch.validation_status != "blocked"
    ]

    out_dir = Path(settings.OUTPUT_DIR) / current_user.id / version.id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_docx = out_dir / "ResumeIQ_Optimized.docx"
    out_pdf = out_dir / "ResumeIQ_Optimized.pdf"
    out_png = out_dir / "preview_page1.png"
    orig_png = out_dir / "orig_page1.png"

    source_path = Path(resume.storage_path)
    if not source_path.exists() and resume.file_bytes:
        source_path = out_dir / f"_source.{resume.file_type}"
        source_path.write_bytes(resume.file_bytes)
    orig_pages = resume.page_count
    parsed = ParsedResumeStructure.model_validate(resume.parsed_structure)
    elem_map = resume.element_map or {}

    theme_warnings: list[str] = []
    similarity_score: float | None = None
    unchanged_similarity_score: float | None = None
    export_blocked = False
    orig_png_bytes: bytes | None = None
    out_png_bytes: bytes | None = None

    if resume.file_type == "docx" and source_path.exists():
        orig_pdf = out_dir / "_orig.pdf"
        orig_pages_calc, used_lo_orig, _ = convert_docx_to_pdf_and_preview(source_path, orig_pdf, orig_png)
        if orig_pages is None:
            orig_pages = orig_pages_calc
            resume.page_count = orig_pages
            db.add(resume)

        docx_warnings = apply_changes_to_docx_in_place(
            source_docx_path=source_path,
            output_docx_path=out_docx,
            approved_changes=approved_changes,
        )
        theme_warnings.extend(docx_warnings)
        out_pages, used_lo_out, lo_note = convert_docx_to_pdf_and_preview(out_docx, out_pdf, out_png)
        if lo_note:
            theme_warnings.append(lo_note)
        if orig_pdf.exists() and out_pdf.exists() and used_lo_orig and used_lo_out:
            verify = verify_pdf_theme_match(
                original_pdf_path=orig_pdf,
                output_pdf_path=out_pdf,
                strategy_used="inplace_pdf",
            )
            similarity_score = verify.overall_similarity_score
            unchanged_similarity_score = verify.unchanged_similarity_score
            theme_warnings.extend(verify.warnings)
            orig_png_bytes = verify.orig_preview_png or (orig_png.read_bytes() if orig_png.exists() else None)
            out_png_bytes = verify.out_preview_png or (out_png.read_bytes() if out_png.exists() else None)
            if not verify.passed:
                export_blocked = True
                if verify.blocked_reason:
                    theme_warnings.insert(0, verify.blocked_reason)
    elif resume.file_type == "pdf" and source_path.exists():
        # Strategy B: in-place PyMuPDF edit first (falls back to Strategy C style-clone if needed)
        pdf_res = apply_changes_to_pdf_in_place(
            source_pdf_path=source_path,
            output_pdf_path=out_pdf,
            approved_changes=approved_changes,
            resume_structure=parsed,
        )
        orig_pages = pdf_res.page_count_original
        out_pages = pdf_res.page_count_output
        if pdf_res.notice:
            theme_warnings.append(pdf_res.notice)
        theme_warnings.extend(pdf_res.font_substitutions)
        theme_warnings.extend(pdf_res.warnings)

        # Strategy D: Theme-match pixel & layout verification
        verify = verify_pdf_theme_match(
            original_pdf_path=source_path,
            output_pdf_path=out_pdf,
            changed_bboxes_by_page=pdf_res.changed_bboxes_by_page,
            strategy_used=pdf_res.strategy_used,
        )
        similarity_score = verify.overall_similarity_score
        unchanged_similarity_score = verify.unchanged_similarity_score
        theme_warnings.extend(verify.warnings)
        orig_png_bytes = verify.orig_preview_png
        out_png_bytes = verify.out_preview_png
        if orig_png_bytes:
            orig_png.write_bytes(orig_png_bytes)
        if out_png_bytes:
            out_png.write_bytes(out_png_bytes)
        if not verify.passed:
            export_blocked = True
            if verify.blocked_reason:
                theme_warnings.insert(0, verify.blocked_reason)

        # Also generate clean style-matched DOCX sibling (with all sections & hyperlinks)
        build_ats_docx_from_structure(
            structure=parsed,
            approved_changes=approved_changes,
            output_docx_path=out_docx,
            style_tokens=elem_map.get("style_tokens"),
        )
    else:
        build_ats_docx_from_structure(
            structure=parsed,
            approved_changes=approved_changes,
            output_docx_path=out_docx,
            style_tokens=elem_map.get("style_tokens"),
        )
        out_pages, _, _ = convert_docx_to_pdf_and_preview(out_docx, out_pdf, out_png)

    if orig_png_bytes is None and orig_png.exists():
        orig_png_bytes = orig_png.read_bytes()
    if out_png_bytes is None and out_png.exists():
        out_png_bytes = out_png.read_bytes()

    overflow = bool(orig_pages and out_pages > orig_pages)
    suggestions: list[str] = []
    if overflow:
        suggestions.append(
            f"Optimized resume is {out_pages} pages (original was {orig_pages}). "
            "Consider rejecting lower-relevance project bullets or older experience lines rather than shrinking font sizes."
        )

    version.output_docx_path = str(out_docx) if out_docx.exists() else None
    version.output_pdf_path = str(out_pdf) if out_pdf.exists() else None
    version.preview_image_path = str(out_png) if out_png.exists() else None
    version.orig_preview_image_path = str(orig_png) if orig_png.exists() else None
    version.output_docx_bytes = out_docx.read_bytes() if out_docx.exists() else None
    version.output_pdf_bytes = out_pdf.read_bytes() if out_pdf.exists() else None
    version.preview_image_bytes = out_png_bytes
    version.orig_preview_image_bytes = orig_png_bytes
    version.page_count_original = orig_pages
    version.page_count_output = out_pages
    version.overflow_detected = overflow
    version.overflow_suggestions = suggestions
    version.similarity_score = similarity_score
    version.unchanged_similarity_score = unchanged_similarity_score
    version.theme_warnings = list(dict.fromkeys(w for w in theme_warnings if w))
    version.export_blocked = export_blocked
    db.add(version)
    db.commit()
    db.refresh(version)

    if export_blocked:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": version.theme_warnings[0] if version.theme_warnings else "Layout similarity verification failed.",
                "similarity_score": similarity_score,
                "unchanged_similarity_score": unchanged_similarity_score,
                "warnings": version.theme_warnings,
                "version_id": version.id,
            },
        )
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

    if kind == "docx":
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if version.output_docx_path and Path(version.output_docx_path).exists():
            return FileResponse(version.output_docx_path, media_type=mime, filename="ResumeIQ_Optimized.docx")
        if version.output_docx_bytes:
            return Response(
                content=version.output_docx_bytes,
                media_type=mime,
                headers={"Content-Disposition": 'attachment; filename="ResumeIQ_Optimized.docx"'},
            )
    if kind == "pdf":
        if version.output_pdf_path and Path(version.output_pdf_path).exists():
            return FileResponse(version.output_pdf_path, media_type="application/pdf", filename="ResumeIQ_Optimized.pdf")
        if version.output_pdf_bytes:
            return Response(
                content=version.output_pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": 'attachment; filename="ResumeIQ_Optimized.pdf"'},
            )
    if kind == "preview":
        if version.preview_image_path and Path(version.preview_image_path).exists():
            return FileResponse(version.preview_image_path, media_type="image/png")
        if version.preview_image_bytes:
            return Response(content=version.preview_image_bytes, media_type="image/png")
    if kind == "orig_preview":
        if version.orig_preview_image_path and Path(version.orig_preview_image_path).exists():
            return FileResponse(version.orig_preview_image_path, media_type="image/png")
        if version.orig_preview_image_bytes:
            return Response(content=version.orig_preview_image_bytes, media_type="image/png")
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export artifact not generated yet.")
