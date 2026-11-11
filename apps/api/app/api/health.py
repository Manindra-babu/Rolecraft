from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "demo_mode": settings.DEMO_MODE,
        "google_oauth_enabled": bool(settings.GOOGLE_CLIENT_ID),
    }
