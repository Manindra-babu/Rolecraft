import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.rate_limit import rate_limiter
from app.db.session import get_db
from app.models.resume import ResumeFile
from app.models.user import MasterProfile, User
from app.schemas.profile import MasterProfileResponse
from app.schemas.resume import (
    ParsedResumeStructure,
    ResumeConfirmRequest,
    ResumeUploadResponse,
)
from app.services.resume.docx_parser import parse_docx_resume
from app.services.resume.pdf_parser import PDF_FORMAT_NOTICE, parse_pdf_resume

router = APIRouter(prefix="/resumes", tags=["resumes"])


def _to_upload_response(resume: ResumeFile) -> ResumeUploadResponse:
    layout_support = "full"
    notice: str | None = None
    if resume.file_type == "pdf":
        layout_support = "analysis_only"
        notice = PDF_FORMAT_NOTICE
    elif resume.layout_type == "two_column":
        layout_support = "partial"
        notice = (
            "Two-column section layout detected. Formatting is preserved in place; "
            "check the rendered PDF preview for column breaks."
        )
    elif resume.layout_type == "textbox_based":
        layout_support = "partial"
        notice = (
            "This DOCX uses floating text boxes. Text is extracted, but in-place edits "
            "work best on standard body or table paragraphs."
        )

    return ResumeUploadResponse(
        id=resume.id,
        filename=resume.filename,
        file_type=resume.file_type,
        file_size=resume.file_size,
        layout_type=resume.layout_type,
        layout_support=layout_support,
        format_notice=notice,
        page_count=resume.page_count,
        parsed_structure=ParsedResumeStructure.model_validate(resume.parsed_structure),
        element_count=len((resume.element_map or {}).get("order", [])),
        created_at=resume.created_at,
    )


@router.post("/upload", response_model=ResumeUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_resume(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ResumeUploadResponse:
    rate_limiter.check(f"upload:{current_user.id}", max_requests=20, window_seconds=60)
    settings = get_settings()

    original_name = Path(file.filename or "resume.docx").name
    ext = Path(original_name).suffix.lower()
    if ext not in {".docx", ".pdf"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .docx (recommended) and .pdf files are supported.",
        )

    content = await file.read()
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {settings.MAX_UPLOAD_MB} MB upload limit.",
        )

    # Validate magic bytes
    if ext == ".docx" and not content.startswith(b"PK\x03\x04"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid DOCX file signature. Please upload a valid Word (.docx) document.",
        )
    if ext == ".pdf" and not content.startswith(b"%PDF"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid PDF file signature.",
        )

    user_dir = Path(settings.UPLOAD_DIR) / current_user.id
    user_dir.mkdir(parents=True, exist_ok=True)
    safe_filename = f"{uuid.uuid4()}{ext}"
    dest_path = user_dir / safe_filename
    dest_path.write_bytes(content)

    try:
        if ext == ".docx":
            parsed, element_map, raw_text, layout_type, _, _ = parse_docx_resume(dest_path)
            page_count = None
            file_type = "docx"
        else:
            parsed, element_map, raw_text, page_count = parse_pdf_resume(dest_path)
            layout_type = "pdf"
            file_type = "pdf"
    except Exception:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to parse this document. Ensure it is not password-protected or corrupted.",
        )

    resume_row = ResumeFile(
        user_id=current_user.id,
        filename=original_name[:255],
        file_type=file_type,
        file_size=len(content),
        storage_path=str(dest_path),
        layout_type=layout_type,
        page_count=page_count,
        parsed_structure=parsed.model_dump(mode="json"),
        element_map=element_map,
        raw_text=raw_text,
    )
    db.add(resume_row)
    db.commit()
    db.refresh(resume_row)
    return _to_upload_response(resume_row)


@router.get("", response_model=list[ResumeUploadResponse])
def list_resumes(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ResumeUploadResponse]:
    rows = (
        db.query(ResumeFile)
        .filter(ResumeFile.user_id == current_user.id)
        .order_by(ResumeFile.created_at.desc())
        .all()
    )
    return [_to_upload_response(r) for r in rows]


@router.get("/{resume_id}", response_model=ResumeUploadResponse)
def get_resume(
    resume_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ResumeUploadResponse:
    row = (
        db.query(ResumeFile)
        .filter(ResumeFile.id == resume_id, ResumeFile.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")
    return _to_upload_response(row)


@router.post("/{resume_id}/confirm", response_model=MasterProfileResponse)
def confirm_resume_to_master_profile(
    resume_id: str,
    payload: ResumeConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MasterProfile:
    resume = (
        db.query(ResumeFile)
        .filter(ResumeFile.id == resume_id, ResumeFile.user_id == current_user.id)
        .first()
    )
    if not resume:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")

    parsed = payload.parsed_structure
    resume.parsed_structure = parsed.model_dump(mode="json")

    profile = db.query(MasterProfile).filter(MasterProfile.user_id == current_user.id).first()
    if not profile:
        profile = MasterProfile(user_id=current_user.id)
        db.add(profile)

    if parsed.full_name:
        profile.full_name = parsed.full_name
    if parsed.email:
        profile.email = parsed.email
    if parsed.phone:
        profile.phone = parsed.phone
    if parsed.location:
        profile.location = parsed.location
    if parsed.summary:
        profile.summary = parsed.summary
    profile.links = parsed.links
    profile.education = [e.model_dump(mode="json") for e in parsed.education]
    profile.experience = [e.model_dump(mode="json") for e in parsed.experience]
    profile.projects = [p.model_dump(mode="json") for p in parsed.projects]
    profile.skills = parsed.skills
    profile.achievements = parsed.achievements
    profile.certifications = parsed.certifications

    db.add(resume)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_resume(
    resume_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    row = (
        db.query(ResumeFile)
        .filter(ResumeFile.id == resume_id, ResumeFile.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")
    Path(row.storage_path).unlink(missing_ok=True)
    db.delete(row)
    db.commit()
