from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import applications, auth, extension, github, health, jobs, learning, privacy, profile, resumes
from app.core.config import get_settings
from app.core.logging import logger, scrub_secrets
from app.db.base import Base
from app.db.session import engine

settings = get_settings()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(Exception)
    async def sanitized_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        safe_detail = scrub_secrets(str(exc))
        logger.error("Unhandled error on %s: %s", request.url.path, safe_detail[:200])
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal error occurred."},
        )

    @app.on_event("startup")
    def on_startup() -> None:
        Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
        Path(settings.OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(bind=engine)

    prefix = settings.API_V1_PREFIX
    app.include_router(health.router, prefix=prefix)
    app.include_router(auth.router, prefix=prefix)
    app.include_router(profile.router, prefix=prefix)
    app.include_router(resumes.router, prefix=prefix)
    app.include_router(jobs.router, prefix=prefix)
    app.include_router(github.router, prefix=prefix)
    app.include_router(learning.router, prefix=prefix)
    app.include_router(applications.router, prefix=prefix)
    app.include_router(extension.router, prefix=prefix)
    app.include_router(privacy.router, prefix=prefix)
    return app


app = create_app()
