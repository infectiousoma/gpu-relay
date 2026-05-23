"""Add 'vision' value to tier_name enum

Revision ID: 0003_vision_tier
Revises: 0002_user_allowed_tiers
Create Date: 2026-05-23 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003_vision_tier"
down_revision: Union[str, None] = "0002_user_allowed_tiers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE tier_name ADD VALUE IF NOT EXISTS 'vision'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values without recreating the type.
    # To fully revert: recreate tier_name without 'vision' and retype all three columns
    # (pods.tier, requests.tier, quotas.max_tier). Safe to leave in place if rolling back.
    pass
