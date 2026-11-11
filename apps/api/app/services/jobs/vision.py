from google import genai
from google.genai import types
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import scrub_secrets
from app.core.security import decrypt_api_key
from app.models.user import User
from app.schemas.job import JobPostingResponse, StructuredJobFields
from app.services.jobs.extractor import _enrich_fields_deterministically, _save_job_posting
from app.services.jobs.validator import validate_extracted_job


def extract_job_from_screenshot(
    image_bytes: bytes,
    mime_type: str,
    *,
    db: Session,
    user: User,
) -> JobPostingResponse:
    settings = get_settings()
    if settings.DEMO_MODE:
        demo_text = (
            "Software Engineer Intern at DemoCorp\n"
            "Responsibilities:\n- Build REST APIs in Python and FastAPI\n"
            "Requirements:\n- Python, SQL, PostgreSQL, Git"
        )
        fields = _enrich_fields_deterministically(
            StructuredJobFields(title="Software Engineer Intern", company="DemoCorp"),
            demo_text,
        )
        return _save_job_posting(
            db,
            user,
            url=None,
            normalized_url=None,
            method="screenshot_vision",
            confidence=0.85,
            fields=fields,
            raw_text=demo_text,
        )

    api_key = decrypt_api_key(user.encrypted_gemini_key or "")
    try:
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=settings.GEMINI_FAST_MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                "Extract the full job posting text and structured fields from this screenshot. Use only text visible in the image.",
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StructuredJobFields,
                temperature=0.1,
            ),
        )
        fields = StructuredJobFields.model_validate_json((resp.text or "").strip())
        raw_text = "\n".join(
            [fields.title, fields.company, *fields.responsibilities, *fields.required_skills]
        ).strip()
        fields = _enrich_fields_deterministically(fields, raw_text)
        ok, conf, reason = validate_extracted_job(raw_text, fields)
        if not ok:
            return JobPostingResponse(
                status="fallback_required",
                fallback_reason=reason or "Could not read a valid job posting from this screenshot.",
                extraction_method="screenshot_vision",
                confidence=conf,
            )
        return _save_job_posting(
            db,
            user,
            url=None,
            normalized_url=None,
            method="screenshot_vision",
            confidence=conf,
            fields=fields,
            raw_text=raw_text,
        )
    except Exception as exc:
        safe_msg = scrub_secrets(str(exc), [api_key])
        return JobPostingResponse(
            status="fallback_required",
            fallback_reason=f"Screenshot extraction failed: {safe_msg[:140]}",
            extraction_method="screenshot_vision",
        )
