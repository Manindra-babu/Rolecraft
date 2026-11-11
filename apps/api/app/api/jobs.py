from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_valid_gemini_key
from app.core.rate_limit import rate_limiter
from app.db.session import get_db
from app.models.job import JobPosting
from app.models.user import User
from app.schemas.job import (
    JobExtractTextRequest,
    JobExtractUrlRequest,
    JobPostingResponse,
    JobPostingUpdateRequest,
)
from app.services.jobs.extractor import (
    _build_skill_items,
    _sanitize_raw_skill_items,
    extract_job_from_text,
    extract_job_from_url,
)
from app.services.jobs.ssrf import SSRFError
from app.services.jobs.vision import extract_job_from_screenshot

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _to_job_response(job: JobPosting) -> JobPostingResponse:
    return JobPostingResponse(
        id=job.id,
        status="extracted",
        url=job.url,
        normalized_url=job.normalized_url,
        extraction_method=job.extraction_method,
        confidence=job.confidence,
        title=job.title,
        company=job.company,
        location=job.location,
        experience_level=job.experience_level,
        responsibilities=job.responsibilities,
        responsibilities_or_soft_requirements=list(job.responsibilities),
        required_skills=job.required_skills,
        preferred_skills=job.preferred_skills,
        skills=_build_skill_items(job.required_skills, job.preferred_skills, job.or_groups or []),
        or_groups=job.or_groups or [],
        keywords=job.keywords,
        raw_text=job.raw_text,
        fetched_at=job.fetched_at,
    )


@router.post("/extract-url", response_model=JobPostingResponse)
def extract_from_url_endpoint(
    payload: JobExtractUrlRequest,
    current_user: User = Depends(require_valid_gemini_key),
    db: Session = Depends(get_db),
) -> JobPostingResponse:
    rate_limiter.check(f"job_extract:{current_user.id}", max_requests=20, window_seconds=60)
    try:
        return extract_job_from_url(payload.url, db=db, user=current_user)
    except SSRFError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.post("/extract-text", response_model=JobPostingResponse)
def extract_from_text_endpoint(
    payload: JobExtractTextRequest,
    current_user: User = Depends(require_valid_gemini_key),
    db: Session = Depends(get_db),
) -> JobPostingResponse:
    rate_limiter.check(f"job_extract:{current_user.id}", max_requests=20, window_seconds=60)
    return extract_job_from_text(
        payload.raw_text,
        db=db,
        user=current_user,
        url=payload.url,
        title_hint=payload.title_hint or "",
        company_hint=payload.company_hint or "",
    )


@router.post("/extract-screenshot", response_model=JobPostingResponse)
async def extract_from_screenshot_endpoint(
    file: UploadFile = File(...),
    current_user: User = Depends(require_valid_gemini_key),
    db: Session = Depends(get_db),
) -> JobPostingResponse:
    rate_limiter.check(f"job_extract:{current_user.id}", max_requests=10, window_seconds=60)
    mime = (file.content_type or "").lower()
    if mime not in {"image/png", "image/jpeg", "image/webp"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PNG, JPEG, or WebP screenshots are supported.",
        )
    content = await file.read()
    if not content or len(content) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Screenshot must be non-empty and under 5 MB.",
        )
    return extract_job_from_screenshot(content, mime, db=db, user=current_user)


@router.get("/{job_id}", response_model=JobPostingResponse)
def get_job_posting(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobPostingResponse:
    job = (
        db.query(JobPosting)
        .filter(JobPosting.id == job_id, JobPosting.user_id == current_user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")
    return _to_job_response(job)


@router.put("/{job_id}", response_model=JobPostingResponse)
def update_job_posting(
    job_id: str,
    payload: JobPostingUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobPostingResponse:
    job = (
        db.query(JobPosting)
        .filter(JobPosting.id == job_id, JobPosting.user_id == current_user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")

    job.title = payload.title.strip()
    job.company = payload.company.strip()
    job.location = payload.location.strip()
    job.experience_level = payload.experience_level.strip()
    job.responsibilities = payload.responsibilities
    job.required_skills = _sanitize_raw_skill_items(payload.required_skills) or payload.required_skills
    job.preferred_skills = _sanitize_raw_skill_items(payload.preferred_skills) or payload.preferred_skills
    if payload.or_groups:
        job.or_groups = [_sanitize_raw_skill_items(g) for g in payload.or_groups if g]
    job.keywords = payload.keywords
    db.add(job)
    db.commit()
    db.refresh(job)
    return _to_job_response(job)
