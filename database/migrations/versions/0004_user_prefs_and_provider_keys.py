"""Add preferred_tiers, disabled_providers to users; add user_provider_keys table

Revision ID: 0004_user_prefs_and_provider_keys
Revises: 0003_vision_tier
Create Date: 2026-05-24 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_user_prefs_and_provider_keys"
down_revision: Union[str, None] = "0003_vision_tier"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("preferred_tiers", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("disabled_providers", sa.JSON(), nullable=True))

    op.create_table(
        "user_provider_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("encrypted_key", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_provider_key"),
    )
    op.create_index("ix_user_provider_keys_user_id", "user_provider_keys", ["user_id"])


def downgrade() -> None:
    op.drop_table("user_provider_keys")
    op.drop_column("users", "disabled_providers")
    op.drop_column("users", "preferred_tiers")
