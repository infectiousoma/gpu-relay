"""Add allowed_tiers to users table

Revision ID: 0002_user_allowed_tiers
Revises: 0001_initial_schema
Create Date: 2026-05-05 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_user_allowed_tiers"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("allowed_tiers", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "allowed_tiers")
