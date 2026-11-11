from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_valid_gemini_key
from app.core.config import get_settings
from app.core.rate_limit import rate_limiter
from app.core.security import encrypt_api_key, mask_api_key
from app.db.session import get_db
from app.models.user import MasterProfile, User
from app.schemas.profile import (
    GeminiKeySaveRequest,
    GeminiKeyStatusResponse,
    MasterProfileResponse,
    MasterProfileUpdate,
)
from app.services.llm.gemini_client import validate_gemini_key

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("", response_model=MasterProfileResponse)
def get_master_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MasterProfile:
    profile = db.query(MasterProfile).filter(MasterProfile.user_id == current_user.id).first()
    if not profile:
        profile = MasterProfile(
            user_id=current_user.id,
            full_name=current_user.full_name,
            email=current_user.email,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


@router.put("", response_model=MasterProfileResponse)
def update_master_profile(
    payload: MasterProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MasterProfile:
    profile = db.query(MasterProfile).filter(MasterProfile.user_id == current_user.id).first()
    if not profile:
        profile = MasterProfile(user_id=current_user.id)
        db.add(profile)

    for field, value in payload.model_dump().items():
        setattr(profile, field, value)

    db.commit()
    db.refresh(profile)
    return profile


@router.get("/api-key", response_model=GeminiKeyStatusResponse)
def get_gemini_key_status(
    current_user: User = Depends(get_current_user),
) -> GeminiKeyStatusResponse:
    settings = get_settings()
    return GeminiKeyStatusResponse(
        has_key=bool(current_user.encrypted_gemini_key),
        status=current_user.gemini_key_status,
        masked_key=current_user.gemini_key_mask,
        added_at=current_user.gemini_key_added_at,
        calls_month=current_user.gemini_calls_month,
        calls_today=current_user.gemini_calls_today,
        daily_limit=settings.PER_USER_DAILY_LLM_LIMIT,
        demo_mode=settings.DEMO_MODE,
    )


@router.put("/api-key", response_model=GeminiKeyStatusResponse)
def save_gemini_key(
    payload: GeminiKeySaveRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GeminiKeyStatusResponse:
    rate_limiter.check(f"save_key:{current_user.id}", max_requests=10, window_seconds=60)
    settings = get_settings()

    raw_key = payload.api_key.strip()
    is_valid, err_msg = validate_gemini_key(raw_key)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=err_msg
            or "This key was rejected by Google. Check that you copied it fully and that the Gemini API is enabled.",
        )

    current_user.encrypted_gemini_key = encrypt_api_key(raw_key)
    current_user.gemini_key_mask = mask_api_key(raw_key)
    current_user.gemini_key_status = "valid"
    current_user.gemini_key_added_at = datetime.now(timezone.utc)
    db.add(current_user)
    db.commit()
    db.refresh(current_user)

    return GeminiKeyStatusResponse(
        has_key=True,
        status=current_user.gemini_key_status,
        masked_key=current_user.gemini_key_mask,
        added_at=current_user.gemini_key_added_at,
        calls_month=current_user.gemini_calls_month,
        calls_today=current_user.gemini_calls_today,
        daily_limit=settings.PER_USER_DAILY_LLM_LIMIT,
        demo_mode=settings.DEMO_MODE,
    )


@router.delete("/api-key", response_model=GeminiKeyStatusResponse)
def delete_gemini_key(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GeminiKeyStatusResponse:
    settings = get_settings()
    current_user.encrypted_gemini_key = None
    current_user.gemini_key_mask = None
    current_user.gemini_key_status = "missing"
    current_user.gemini_key_added_at = None
    db.add(current_user)
    db.commit()
    db.refresh(current_user)

    return GeminiKeyStatusResponse(
        has_key=False,
        status="missing",
        masked_key=None,
        added_at=None,
        calls_month=current_user.gemini_calls_month,
        calls_today=current_user.gemini_calls_today,
        daily_limit=settings.PER_USER_DAILY_LLM_LIMIT,
        demo_mode=settings.DEMO_MODE,
    )


@router.get("/analysis-gate-check")
def analysis_gate_check(current_user: User = Depends(require_valid_gemini_key)) -> dict:
    return {"allowed": True, "user_id": current_user.id}
