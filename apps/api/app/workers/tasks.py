from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger, scrub_secrets
from app.db.session import SessionLocal
from app.models.application import Application, Change, GitHubConfirmJob, ResumeVersion
from app.models.github import GitHubAnalysis
from app.models.job import JobPosting
from app.models.resume import ResumeFile
from app.models.user import MasterProfile, User
from app.schemas.github import AnalyzedRepo, RepoRecommendation
from app.schemas.job import StructuredJobFields
from app.schemas.resume import ParsedResumeStructure
from app.services.generation.optimizer import (
    build_added_from_github_change,
    generate_tailored_changes,
)
from app.services.github.client import generate_repo_bullets_with_evidence
from app.services.llm.base import LLMKeyInvalidError, LLMRateLimitError, LLMServiceError
from app.services.llm.factory import get_llm_client_for_user


def enqueue_task(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """
    Dispatch a background task via Redis/RQ when TASK_RUNNER=rq and REDIS_URL is configured,
    or execute inline when Redis/RQ is disabled.
    Important: Never pass raw API keys in queue payloads; pass job_id so the worker decrypts in memory.
    """
    settings = get_settings()
    if settings.TASK_RUNNER == "rq" and settings.REDIS_URL:
        try:
            from redis import Redis
            from rq import Queue

            conn = Redis.from_url(settings.REDIS_URL)
            q = Queue("resumeiq", connection=conn)
            job = q.enqueue(func, *args, **kwargs, job_timeout=180)
            return {"mode": "rq", "job_id": job.id}
        except Exception as exc:
            logger.warning("RQ enqueue failed (%s); falling back to inline execution", type(exc).__name__)
    return {"mode": "inline", "result": func(*args, **kwargs)}


def _minimal_analyzed_repo(rec: RepoRecommendation) -> AnalyzedRepo:
    return AnalyzedRepo(
        name=rec.repo_name,
        full_name=rec.full_name,
        html_url=rec.html_url,
        description=rec.reason,
        inferred_skills=list(rec.matched_skills),
        quality_score=rec.quality_score,
    )


def run_github_confirm_job(job_id: str, db: Session | None = None) -> dict[str, Any]:
    """
    Worker entrypoint to generate evidence-backed project bullets for confirmed GitHub repos
    and add/update pending 'added_from_github' changes in the application's latest ResumeVersion.
    Decrypts user's saved Gemini key in memory inside the worker; never exposes secrets or PII in logs.
    """
    owns_session = db is None
    session = db if db is not None else SessionLocal()

    def _fail(job_row: GitHubConfirmJob, *, code: str, message: str) -> dict[str, Any]:
        job_row.status = "failed"
        job_row.error_code = code
        job_row.error_message = scrub_secrets(message)[:500]
        job_row.stage_message = "Failed to add GitHub projects."
        session.add(job_row)
        session.commit()
        return {"status": "failed", "job_id": job_row.id, "error_code": code}

    try:
        job_row = session.query(GitHubConfirmJob).filter(GitHubConfirmJob.id == job_id).first()
        if not job_row:
            return {"status": "missing", "job_id": job_id}

        app_row = (
            session.query(Application)
            .filter(Application.id == job_row.application_id, Application.user_id == job_row.user_id)
            .first()
        )
        user = session.query(User).filter(User.id == job_row.user_id).first()
        if not app_row or not user:
            return _fail(
                job_row,
                code="NOT_FOUND",
                message="Application or user account could not be found.",
            )

        resume = (
            session.query(ResumeFile).filter(ResumeFile.id == app_row.resume_file_id).first()
            if app_row.resume_file_id
            else None
        )
        job_posting = (
            session.query(JobPosting).filter(JobPosting.id == app_row.job_posting_id).first()
            if app_row.job_posting_id
            else None
        )
        if not resume or not job_posting:
            return _fail(
                job_row,
                code="MISSING_CONTEXT",
                message="Resume or job posting context is missing for this application.",
            )

        # Stage 1: Generating
        job_row.status = "generating"
        job_row.progress_percent = 35
        job_row.stage_message = "Generating resume-ready project descriptions..."
        session.add(job_row)
        session.commit()

        gh_repos: list[AnalyzedRepo] = []
        if app_row.github_analysis_id:
            gh = (
                session.query(GitHubAnalysis)
                .filter(GitHubAnalysis.id == app_row.github_analysis_id)
                .first()
            )
            if gh:
                gh_repos = [AnalyzedRepo.model_validate(r) for r in gh.repos_data]
        repos_by_name = {r.name: r for r in gh_repos}

        # Validate user's saved LLM key upfront (decrypts in memory)
        try:
            llm = get_llm_client_for_user(session, user)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            return _fail(
                job_row,
                code=str(detail.get("code") or "GEMINI_KEY_REQUIRED"),
                message="Your Gemini key needs attention. Add or re-verify your Gemini key in Profile -> API Key.",
            )

        confirmed_set = set(job_row.repo_names or [])
        updated_recs: list[RepoRecommendation] = []
        confirmed_recs: list[RepoRecommendation] = []

        try:
            for raw_rec in app_row.github_recommendations or []:
                rec = RepoRecommendation.model_validate(raw_rec)
                if rec.repo_name in confirmed_set:
                    rec.decision = "approved"
                    analyzed_repo = repos_by_name.get(rec.repo_name) or _minimal_analyzed_repo(rec)
                    bullets, bullet_evidence = generate_repo_bullets_with_evidence(
                        analyzed_repo,
                        db=session,
                        user=user,
                        raise_on_llm_error=True,
                        llm=llm,
                    )
                    rec.draft_bullets = bullets
                    rec.bullet_evidence = bullet_evidence
                    confirmed_recs.append(rec)
                updated_recs.append(rec)
        except LLMKeyInvalidError:
            user.gemini_key_status = "needs_attention"
            session.add(user)
            return _fail(
                job_row,
                code="GEMINI_KEY_INVALID",
                message="Your Gemini key needs attention. Please check Profile -> API Key and retry.",
            )
        except LLMRateLimitError as exc:
            return _fail(
                job_row,
                code="RATE_LIMIT",
                message=str(exc) or "Rate limit hit while contacting Gemini. Please wait a moment and retry.",
            )
        except TimeoutError:
            return _fail(
                job_row,
                code="TIMEOUT",
                message="Timed out waiting for project bullet generation. Please retry.",
            )
        except (LLMServiceError, Exception) as exc:
            logger.warning("GitHub confirm generation error job_id=%s err=%s", job_id, type(exc).__name__)
            return _fail(
                job_row,
                code="LLM_ERROR",
                message="Something went wrong generating project descriptions. Please try again.",
            )

        if not confirmed_recs:
            return _fail(
                job_row,
                code="NO_MATCHING_REPOS",
                message="None of the selected repositories were found on this application.",
            )

        # Stage 2: Validating against repository evidence
        job_row.status = "validating"
        job_row.progress_percent = 75
        job_row.stage_message = "Validating project bullets against repository evidence..."
        app_row.github_recommendations = [r.model_dump(mode="json") for r in updated_recs]
        session.add(app_row)
        session.add(job_row)
        session.commit()

        parsed_resume = ParsedResumeStructure.model_validate(resume.parsed_structure)
        profile = session.query(MasterProfile).filter(MasterProfile.user_id == user.id).first()
        confirmed_learning = list(profile.confirmed_learning or []) if profile else []

        latest_version = (
            session.query(ResumeVersion)
            .filter(ResumeVersion.application_id == app_row.id)
            .order_by(ResumeVersion.version_number.desc())
            .first()
        )

        added_count = 0
        if latest_version is None:
            job_fields = StructuredJobFields(
                title=job_posting.title,
                company=job_posting.company,
                location=job_posting.location,
                experience_level=job_posting.experience_level,
                responsibilities=list(job_posting.responsibilities),
                required_skills=list(job_posting.required_skills),
                preferred_skills=list(job_posting.preferred_skills),
                or_groups=list(job_posting.or_groups or []),
                keywords=list(job_posting.keywords),
            )
            proposed_changes = generate_tailored_changes(
                resume=parsed_resume,
                raw_resume_text=resume.raw_text,
                element_map=resume.element_map or {},
                job=job_fields,
                github_repos=gh_repos,
                approved_github_recs=confirmed_recs,
                confirmed_learning=confirmed_learning,
                llm=llm,
            )
            latest_version = ResumeVersion(
                application_id=app_row.id,
                version_number=1,
                page_count_original=resume.page_count,
            )
            session.add(latest_version)
            session.flush()
            for ch in proposed_changes:
                if ch.get("change_type") == "added_from_github":
                    added_count += 1
                session.add(Change(resume_version_id=latest_version.id, **ch))
        else:
            existing_gh_changes = (
                session.query(Change)
                .filter(
                    Change.resume_version_id == latest_version.id,
                    Change.change_type == "added_from_github",
                )
                .all()
            )
            existing_by_repo = {
                str((c.target_location or {}).get("repo_name") or ""): c
                for c in existing_gh_changes
                if (c.target_location or {}).get("repo_name")
            }
            for rec in confirmed_recs:
                payload_dict = build_added_from_github_change(
                    rec=rec,
                    resume=parsed_resume,
                    raw_resume_text=resume.raw_text,
                    github_repos=gh_repos,
                    confirmed_learning=confirmed_learning,
                )
                existing_row = existing_by_repo.get(rec.repo_name)
                if existing_row is not None:
                    existing_row.new_text = payload_dict["new_text"]
                    existing_row.reason = payload_dict["reason"]
                    existing_row.evidence_refs = payload_dict["evidence_refs"]
                    existing_row.validation_status = payload_dict["validation_status"]
                    existing_row.validation_warnings = payload_dict["validation_warnings"]
                    existing_row.status = "pending"
                    existing_row.edited_text = None
                    session.add(existing_row)
                else:
                    session.add(Change(resume_version_id=latest_version.id, **payload_dict))
                added_count += 1

        # Stage 3: Invalidate stale export cache and mark Done
        latest_version.export_cache_key = None
        latest_version.output_pdf_path = None
        latest_version.output_pdf_bytes = None
        latest_version.output_docx_path = None
        latest_version.output_docx_bytes = None
        latest_version.preview_image_path = None
        latest_version.preview_image_bytes = None
        latest_version.export_blocked = False
        session.add(latest_version)

        job_row.status = "done"
        job_row.progress_percent = 100
        job_row.stage_message = f"Added {added_count} GitHub project{'s' if added_count != 1 else ''} to Review Changes."
        job_row.version_id = latest_version.id
        job_row.added_count = added_count
        session.add(job_row)
        session.commit()
        return {
            "status": "done",
            "job_id": job_row.id,
            "version_id": latest_version.id,
            "added_count": added_count,
        }
    except Exception as exc:
        logger.error("Unexpected failure in run_github_confirm_job job_id=%s err=%s", job_id, type(exc).__name__)
        try:
            job_row = session.query(GitHubConfirmJob).filter(GitHubConfirmJob.id == job_id).first()
            if job_row and job_row.status != "failed":
                return _fail(
                    job_row,
                    code="INTERNAL_ERROR",
                    message="Something went wrong while confirming GitHub projects. Please try again.",
                )
        except Exception:
            pass
        return {"status": "failed", "job_id": job_id}
    finally:
        if owns_session:
            session.close()
