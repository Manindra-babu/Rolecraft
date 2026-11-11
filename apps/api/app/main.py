from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text

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
        from app.services.resume.pdf_editor import EXPORT_CODE_VERSION

        Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
        Path(settings.OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(bind=engine)
        # Ensure export_cache_key column exists on legacy DBs and purge stale pre-v4 export artifacts
        try:
            insp = inspect(engine)
            cols = {c["name"] for c in insp.get_columns("resume_versions")}
            with engine.begin() as conn:
                if "export_cache_key" not in cols:
                    conn.execute(text("ALTER TABLE resume_versions ADD COLUMN export_cache_key VARCHAR(128)"))
                conn.execute(
                    text(
                        "UPDATE resume_versions "
                        "SET output_docx_path = NULL, output_pdf_path = NULL, preview_image_path = NULL, "
                        "    output_docx_bytes = NULL, output_pdf_bytes = NULL, preview_image_bytes = NULL, "
                        "    similarity_score = NULL, unchanged_similarity_score = NULL, "
                        "    export_blocked = FALSE, theme_warnings = '[]', export_cache_key = NULL "
                        "WHERE export_cache_key IS NULL OR export_cache_key NOT LIKE :prefix"
                    ),
                    {"prefix": f"{EXPORT_CODE_VERSION}:%"},
                )
        except Exception as exc:
            logger.warning("Startup export cache cleanup skipped: %s", type(exc).__name__)

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
