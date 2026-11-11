import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, utcnow


class JobPosting(Base, TimestampMixin):
    __tablename__ = "job_postings"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    normalized_url: Mapped[Optional[str]] = mapped_column(String(1000), index=True, nullable=True)
    extraction_method: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    company: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    location: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    experience_level: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    responsibilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    required_skills: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    preferred_skills: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
