import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, utcnow


class GitHubAnalysis(Base, TimestampMixin):
    __tablename__ = "github_analyses"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    github_username: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    profile_url: Mapped[str] = mapped_column(String(500), nullable=False)
    repos_data: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    detected_skills: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    etag: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
