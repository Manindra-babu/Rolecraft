import uuid
from typing import Any, Optional

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class LearningPlan(Base, TimestampMixin):
    __tablename__ = "learning_plans"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    application_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("applications.id", ondelete="CASCADE"), index=True, nullable=True
    )
    skill_name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    progress: Mapped[Optional["LearningProgress"]] = relationship(
        back_populates="learning_plan", uselist=False, cascade="all, delete-orphan"
    )


class LearningProgress(Base, TimestampMixin):
    __tablename__ = "learning_progress"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    learning_plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("learning_plans.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    completed_steps: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    user_confirmed_learning: Mapped[bool] = mapped_column(default=False, nullable=False)
    confirmation_note: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    learning_plan: Mapped["LearningPlan"] = relationship(back_populates="progress")
