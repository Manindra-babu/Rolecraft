import uuid
from typing import Any, Optional

from sqlalchemy import JSON, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class ResumeFile(Base, TimestampMixin):
    __tablename__ = "resume_files"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)  # docx | pdf
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_bytes: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    layout_type: Mapped[str] = mapped_column(
        String(64), default="single_column", nullable=False
    )
    page_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Structured resume content + exact OOXML location map for in-place editing
    parsed_structure: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    element_map: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)

    user: Mapped["User"] = relationship(back_populates="resumes")
