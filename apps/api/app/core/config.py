from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[4]
ENV_FILE = ROOT_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE.exists() else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    PROJECT_NAME: str = "ResumeIQ"
    ENVIRONMENT: str = "development"
    API_V1_PREFIX: str = "/api/v1"

    # Security
    SECRET_KEY: str = "dev-only-change-in-production-secret-key-at-least-32-chars"
    ENCRYPTION_KEY: str = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    ALGORITHM: str = "HS256"

    # Database & Redis
    DATABASE_URL: str = "sqlite:///./resumeiq_dev.db"
    POSTGRES_URL: str = ""
    REDIS_URL: str = ""
    TASK_RUNNER: str = "inline"  # inline | rq

    # GitHub (server-side public repo token only)
    GITHUB_TOKEN: str = ""

    # LLM settings (user supplies own Gemini key; no global GEMINI_API_KEY required)
    LLM_PROVIDER: str = "gemini"
    GEMINI_FAST_MODEL: str = "gemini-3.8-flash"
    GEMINI_QUALITY_MODEL: str = "gemini-3.8-flash"
    GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-001"
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-20250514"

    PER_USER_DAILY_LLM_LIMIT: int = 100
    LLM_MAX_INPUT_CHARS: int = 24000
    DEMO_MODE: bool = False

    # Optional Google OAuth (disabled unless set)
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # Job extraction
    THIRD_PARTY_SCRAPER_ENABLED: bool = False
    THIRD_PARTY_SCRAPER_URL: str = ""
    THIRD_PARTY_SCRAPER_KEY: str = ""
    JOB_CACHE_TTL_HOURS: int = 24

    # Storage & CORS
    BACKEND_CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    UPLOAD_DIR: str = "./storage/uploads"
    OUTPUT_DIR: str = "./storage/outputs"
    MAX_UPLOAD_MB: int = 10

    @property
    def sqlalchemy_database_url(self) -> str:
        raw = self.DATABASE_URL.strip()
        if (not raw or raw == "sqlite:///./resumeiq_dev.db") and self.POSTGRES_URL.strip():
            raw = self.POSTGRES_URL.strip()
        if raw.startswith("postgres://"):
            return "postgresql+psycopg://" + raw[len("postgres://") :]
        if raw.startswith("postgresql://"):
            return "postgresql+psycopg://" + raw[len("postgresql://") :]
        return raw

    @property
    def cors_origins(self) -> List[str]:
        raw = self.BACKEND_CORS_ORIGINS.strip()
        if raw.startswith("["):
            import json
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except Exception:
                pass
        return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
