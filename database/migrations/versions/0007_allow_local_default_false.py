"""Change allow_local default to false and reset existing users

Revision ID: 0007_allow_local_false
Revises: 0006_allow_local
Create Date: 2026-05-24 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_allow_local_false"
down_revision: Union[str, None] = "0006_allow_local"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE users SET allow_local = false")
    op.alter_column("users", "allow_local", server_default="false", existing_type=sa.Boolean(), existing_nullable=False)


def downgrade() -> None:
    op.execute("UPDATE users SET allow_local = true")
    op.alter_column("users", "allow_local", server_default="true", existing_type=sa.Boolean(), existing_nullable=False)
