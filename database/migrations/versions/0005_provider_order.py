"""Add provider_order to users

Revision ID: 0005_provider_order
Revises: 0004_user_prefs
Create Date: 2026-05-24 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_provider_order"
down_revision: Union[str, None] = "0004_user_prefs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("provider_order", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "provider_order")
