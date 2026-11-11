"""add_github_confirm_jobs

Revision ID: c2b4a8f10d9e
Revises: 87e3865cdb9c
Create Date: 2026-11-11 12:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2b4a8f10d9e"
down_revision: Union[str, None] = "87e3865cdb9c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "github_confirm_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("repo_names", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("stage_message", sa.String(length=255), nullable=False, server_default="Queued..."),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("version_id", sa.String(length=36), nullable=True),
        sa.Column("added_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_github_confirm_jobs_application_id", "github_confirm_jobs", ["application_id"])
    op.create_index("ix_github_confirm_jobs_user_id", "github_confirm_jobs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_github_confirm_jobs_user_id", table_name="github_confirm_jobs")
    op.drop_index("ix_github_confirm_jobs_application_id", table_name="github_confirm_jobs")
    op.drop_table("github_confirm_jobs")
