"""Add preferred_tiers, disabled_providers to users; add user_provider_keys table

Revision ID: 0004_user_prefs
Revises: 0003_vision_tier
Create Date: 2026-05-24 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0004_user_prefs"
down_revision: Union[str, None] = "0003_vision_tier"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("preferred_tiers", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("disabled_providers", sa.JSON(), nullable=True))

    op.execute("""
        CREATE TABLE user_provider_keys (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider    VARCHAR(64) NOT NULL,
            encrypted_key TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_user_provider_key UNIQUE (user_id, provider)
        )
    """)
    op.execute("CREATE INDEX ix_user_provider_keys_user_id ON user_provider_keys (user_id)")


def downgrade() -> None:
    op.drop_table("user_provider_keys")
    op.drop_column("users", "disabled_providers")
    op.drop_column("users", "preferred_tiers")
