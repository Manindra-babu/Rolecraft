from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_valid_gemini_key
from app.core.rate_limit import rate_limiter
from app.db.session import get_db
from app.models.user import User
from app.schemas.job import JobPostingResponse
from app.services.jobs.extractor import extract_job_from_text

router = APIRouter(prefix="/extension", tags=["extension"])


class ExtensionCaptureJobRequest(BaseModel):
    url: str = Field(min_length=8, max_length=1000)
    page_title: str = Field(default="", max_length=300)
    raw_text: str = Field(min_length=40, max_length=60000)


@router.post("/capture-job", response_model=JobPostingResponse)
def capture_job_from_extension(
    payload: ExtensionCaptureJobRequest,
    current_user: User = Depends(require_valid_gemini_key),
    db: Session = Depends(get_db),
) -> JobPostingResponse:
    """Accept job page text & URL captured inside the user's own authenticated browser session."""
    rate_limiter.check(f"ext_capture:{current_user.id}", max_requests=20, window_seconds=60)
    return extract_job_from_text(
        payload.raw_text,
        db=db,
        user=current_user,
        url=payload.url,
        title_hint=payload.page_title,
        method="browser_extension",
    )
