"""Add allow_local to users

Revision ID: 0006_allow_local
Revises: 0005_provider_order
Create Date: 2026-05-24 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_allow_local"
down_revision: Union[str, None] = "0005_provider_order"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("allow_local", sa.Boolean(), nullable=False, server_default="true"))


def downgrade() -> None:
    op.drop_column("users", "allow_local")
