import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    hashed_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    google_sub: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)

    # Bring-your-own Gemini API key (encrypted at rest; never logged or returned in full)
    encrypted_gemini_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gemini_key_mask: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # missing | valid | needs_attention
    gemini_key_status: Mapped[str] = mapped_column(
        String(32), default="missing", nullable=False
    )
    gemini_key_added_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    gemini_calls_month: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gemini_calls_month_key: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    gemini_calls_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gemini_calls_day_key: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    master_profile: Mapped[Optional["MasterProfile"]] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    resumes: Mapped[list["ResumeFile"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    applications: Mapped[list["Application"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class MasterProfile(Base, TimestampMixin):
    __tablename__ = "master_profiles"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    email: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    phone: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    location: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)

    links: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    education: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    experience: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    projects: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    achievements: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    certifications: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    confirmed_learning: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="master_profile")
