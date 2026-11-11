from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.rate_limit import rate_limiter
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.user import MasterProfile, User
from app.schemas.auth import TokenResponse, UserLoginRequest, UserRegisterRequest, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def _to_user_response(user: User) -> UserResponse:
    settings = get_settings()
    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        gemini_key_mask=user.gemini_key_mask,
        gemini_key_status=user.gemini_key_status,
        gemini_key_added_at=user.gemini_key_added_at,
        gemini_calls_month=user.gemini_calls_month,
        gemini_calls_today=user.gemini_calls_today,
        daily_limit=settings.PER_USER_DAILY_LLM_LIMIT,
        demo_mode=settings.DEMO_MODE,
        google_oauth_enabled=bool(settings.GOOGLE_CLIENT_ID),
        created_at=user.created_at,
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(
    payload: UserRegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TokenResponse:
    client_ip = request.client.host if request.client else "unknown"
    rate_limiter.check(f"register:{client_ip}", max_requests=20, window_seconds=60)

    normalized_email = payload.email.strip().lower()
    existing = db.query(User).filter(User.email == normalized_email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    user = User(
        email=normalized_email,
        full_name=payload.full_name.strip(),
        hashed_password=hash_password(payload.password),
        gemini_key_status="missing",
    )
    db.add(user)
    db.flush()

    profile = MasterProfile(
        user_id=user.id,
        full_name=user.full_name,
        email=user.email,
    )
    db.add(profile)
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id)
    return TokenResponse(access_token=token, user=_to_user_response(user))


@router.post("/login", response_model=TokenResponse)
def login(
    payload: UserLoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TokenResponse:
    client_ip = request.client.host if request.client else "unknown"
    rate_limiter.check(f"login:{client_ip}", max_requests=30, window_seconds=60)

    normalized_email = payload.email.strip().lower()
    user = db.query(User).filter(User.email == normalized_email).first()
    if not user or not user.hashed_password or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    token = create_access_token(user.id)
    return TokenResponse(access_token=token, user=_to_user_response(user))


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    return _to_user_response(current_user)


@router.post("/logout")
def logout() -> dict:
    return {"message": "Signed out."}
