"""add_export_cache_key_to_resume_versions

Revision ID: d4f8a21c9b30
Revises: c2b4a8f10d9e
Create Date: 2026-11-11 16:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4f8a21c9b30"
down_revision: Union[str, None] = "c2b4a8f10d9e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("resume_versions", sa.Column("export_cache_key", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("resume_versions", "export_cache_key")
