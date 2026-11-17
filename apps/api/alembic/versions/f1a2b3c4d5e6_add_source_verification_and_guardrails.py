"""add_source_verification_and_guardrails

Revision ID: f1a2b3c4d5e6
Revises: d4f8a21c9b30
Create Date: 2026-11-17 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "d4f8a21c9b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Applications table
    try:
        op.add_column("applications", sa.Column("confirmed_learning", sa.JSON(), nullable=False, server_default="[]"))
    except Exception:
        pass

    # Resume versions table
    for col_name, col_type, default in [
        ("overlap_passed", sa.Boolean(), None),
        ("overlap_details", sa.JSON(), "[]"),
        ("page_fit", sa.JSON(), "{}"),
        ("source_verification", sa.JSON(), "{}"),
    ]:
        try:
            op.add_column("resume_versions", sa.Column(col_name, col_type, nullable=True if default is None else False, server_default=default))
        except Exception:
            pass


def downgrade() -> None:
    try:
        op.drop_column("resume_versions", "source_verification")
    except Exception:
        pass
    try:
        op.drop_column("resume_versions", "page_fit")
    except Exception:
        pass
    try:
        op.drop_column("resume_versions", "overlap_details")
    except Exception:
        pass
    try:
        op.drop_column("resume_versions", "overlap_passed")
    except Exception:
        pass
    try:
        op.drop_column("applications", "confirmed_learning")
    except Exception:
        pass
