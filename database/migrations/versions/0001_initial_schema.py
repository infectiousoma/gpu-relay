"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-03 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Enums (created once, reused by multiple tables)
# ---------------------------------------------------------------------------
USER_ROLE = postgresql.ENUM("admin", "user", "service", name="user_role", create_type=False)
BILLING_MODE = postgresql.ENUM("prepaid", "postpaid", name="billing_mode", create_type=False)
POD_STATUS = postgresql.ENUM(
    "provisioning", "starting", "ready", "draining", "terminated", "failed",
    name="pod_status", create_type=False,
)
TIER_NAME = postgresql.ENUM("simple", "architecture", "maximum", "ultra", name="tier_name", create_type=False)
INVOICE_STATUS = postgresql.ENUM("open", "paid", "void", "overdue", name="invoice_status", create_type=False)
REQUEST_STATUS = postgresql.ENUM(
    "ok", "error", "timeout", "rejected_quota", "rejected_budget",
    name="request_status", create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    USER_ROLE.create(bind, checkfirst=True)
    BILLING_MODE.create(bind, checkfirst=True)
    POD_STATUS.create(bind, checkfirst=True)
    TIER_NAME.create(bind, checkfirst=True)
    INVOICE_STATUS.create(bind, checkfirst=True)
    REQUEST_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("role", USER_ROLE, nullable=False, server_default="user"),
        sa.Column("billing_mode", BILLING_MODE, nullable=False, server_default="postpaid"),
        sa.Column("monthly_budget_usd", sa.Numeric(12, 4), nullable=False, server_default="25.0000"),
        sa.Column("prepaid_balance_usd", sa.Numeric(12, 4), nullable=False, server_default="0.0000"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("pipeline_default", sa.String(64), nullable=False, server_default="infer"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("monthly_budget_usd >= 0", name="ck_users_budget_nonneg"),
        sa.CheckConstraint("prepaid_balance_usd >= 0", name="ck_users_prepaid_nonneg"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key_hash", sa.String(255), nullable=False, unique=True),
        sa.Column("key_prefix", sa.String(12), nullable=False),
        sa.Column("label", sa.String(120)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])

    op.create_table(
        "quotas",
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("requests_per_minute", sa.Integer, nullable=False, server_default="60"),
        sa.Column("tokens_per_day", sa.BigInteger, nullable=False, server_default="1000000"),
        sa.Column("usd_per_month", sa.Numeric(12, 4), nullable=False, server_default="25.0000"),
        sa.Column("max_tier", TIER_NAME, nullable=False, server_default="ultra"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "pods",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("tier", TIER_NAME, nullable=False),
        sa.Column("gpu", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("endpoint_url", sa.String(512)),
        sa.Column("status", POD_STATUS, nullable=False),
        sa.Column("cost_per_hour_usd", sa.Numeric(8, 4), nullable=False),
        sa.Column("health_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ready_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("terminated_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", postgresql.JSONB),
        sa.UniqueConstraint("provider", "external_id", name="uq_pods_provider_external"),
    )
    op.create_index("ix_pods_tier_status", "pods", ["tier", "status"])
    op.create_index("ix_pods_provider_status", "pods", ["provider", "status"])

    op.create_table(
        "requests",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("pod_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("pods.id", ondelete="SET NULL")),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("api_keys.id", ondelete="SET NULL")),
        sa.Column("tier", TIER_NAME, nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("pipeline", sa.String(64), nullable=False, server_default="infer"),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("files_referenced", sa.Integer, nullable=False, server_default="0"),
        sa.Column("routing_reason", sa.String(255)),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("status", REQUEST_STATUS, nullable=False),
        sa.Column("error_message", sa.Text),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_requests_user_idempotency"),
    )
    op.create_index("ix_requests_user_created", "requests", ["user_id", "created_at"])
    op.create_index("ix_requests_pod_created", "requests", ["pod_id", "created_at"])
    op.create_index("ix_requests_tier_created", "requests", ["tier", "created_at"])

    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount_usd", sa.Numeric(12, 4), nullable=False),
        sa.Column("request_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", INVOICE_STATUS, nullable=False, server_default="open"),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("line_items_json", postgresql.JSONB),
        sa.UniqueConstraint("user_id", "period_start", "period_end", name="uq_invoices_user_period"),
    )
    op.create_index("ix_invoices_user_id", "invoices", ["user_id"])

    op.create_table(
        "budget_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("threshold_pct", sa.Integer, nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "period_start", "threshold_pct", name="uq_budget_alerts_unique"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource", sa.String(128)),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("details_json", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_user_created", "audit_log", ["user_id", "created_at"])
    op.create_index("ix_audit_action_created", "audit_log", ["action", "created_at"])

    # Enforce append-only on audit_log via revoke at application role level (handled by DBA), and a trigger:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_no_mutate() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_update
            BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION audit_log_no_mutate();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;")
    op.execute("DROP FUNCTION IF EXISTS audit_log_no_mutate();")

    op.drop_index("ix_audit_action_created", table_name="audit_log")
    op.drop_index("ix_audit_user_created", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_table("budget_alerts")

    op.drop_index("ix_invoices_user_id", table_name="invoices")
    op.drop_table("invoices")

    op.drop_index("ix_requests_tier_created", table_name="requests")
    op.drop_index("ix_requests_pod_created", table_name="requests")
    op.drop_index("ix_requests_user_created", table_name="requests")
    op.drop_table("requests")

    op.drop_index("ix_pods_provider_status", table_name="pods")
    op.drop_index("ix_pods_tier_status", table_name="pods")
    op.drop_table("pods")

    op.drop_table("quotas")

    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_table("api_keys")

    op.drop_table("users")

    bind = op.get_bind()
    for enum_name in ("request_status", "invoice_status", "tier_name", "pod_status", "billing_mode", "user_role"):
        op.execute(f"DROP TYPE IF EXISTS {enum_name};")
