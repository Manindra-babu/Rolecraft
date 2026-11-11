import uuid
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class Application(Base, TimestampMixin):
    __tablename__ = "applications"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    resume_file_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("resume_files.id", ondelete="SET NULL"), nullable=True
    )
    job_posting_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("job_postings.id", ondelete="SET NULL"), nullable=True
    )
    github_analysis_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("github_analyses.id", ondelete="SET NULL"), nullable=True
    )

    # Draft | Applied | Interview | Offer | Rejected
    status: Mapped[str] = mapped_column(String(32), default="Draft", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    compatibility_before: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    compatibility_after: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    github_recommendations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="applications")
    versions: Mapped[list["ResumeVersion"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    skill_assessments: Mapped[list["SkillAssessment"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class ResumeVersion(Base, TimestampMixin):
    __tablename__ = "resume_versions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    application_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("applications.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    output_docx_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    output_pdf_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    preview_image_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    output_docx_bytes: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    output_pdf_bytes: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    preview_image_bytes: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    page_count_original: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_count_output: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    overflow_detected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    overflow_suggestions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    application: Mapped["Application"] = relationship(back_populates="versions")
    changes: Mapped[list["Change"]] = relationship(
        back_populates="resume_version", cascade="all, delete-orphan"
    )


class Change(Base, TimestampMixin):
    __tablename__ = "changes"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    resume_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("resume_versions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    section: Mapped[str] = mapped_column(String(64), nullable=False)
    # reworded | added_from_resume | added_from_github | reordered | removed | user_confirmed_skill
    change_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_location: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    new_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    edited_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )

    # Validation layer results
    validation_status: Mapped[str] = mapped_column(
        String(32), default="passed", nullable=False
    )  # passed | flagged | blocked
    validation_warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    # pending | accepted | rejected | edited
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)

    resume_version: Mapped["ResumeVersion"] = relationship(back_populates="changes")


class SkillAssessment(Base, TimestampMixin):
    __tablename__ = "skill_assessments"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    application_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("applications.id", ondelete="CASCADE"), index=True, nullable=False
    )
    skill_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # demonstrated | under_represented | github_only | needs_development | missing
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    importance: Mapped[str] = mapped_column(
        String(32), default="required", nullable=False
    )  # required | preferred
    evidence_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    application: Mapped["Application"] = relationship(back_populates="skill_assessments")
