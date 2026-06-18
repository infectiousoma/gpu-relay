"""Add user_tier_overrides table

Revision ID: 0008_user_tier_overrides
Revises: 0007_allow_local_false
Create Date: 2026-06-18 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0008_user_tier_overrides"
down_revision: Union[str, None] = "0007_allow_local_false"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_tier_overrides",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tier", sa.String(32), nullable=False),
        sa.Column("provider_order", sa.JSON, nullable=True),
        sa.Column("disabled_providers", sa.JSON, nullable=True),
        sa.Column("provider_models", sa.JSON, nullable=True),
        sa.Column("model_override", sa.String(256), nullable=True),
        sa.Column("model_no_tools_override", sa.String(256), nullable=True),
        sa.Column("idle_timeout_sec", sa.Integer, nullable=True),
        sa.Column("max_context_tokens", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_tier_overrides_user_id", "user_tier_overrides", ["user_id"])
    op.create_unique_constraint(
        "uq_user_tier_override", "user_tier_overrides", ["user_id", "tier"]
    )


def downgrade() -> None:
    op.drop_table("user_tier_overrides")
