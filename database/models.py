"""SQLAlchemy 2.x models for the self-hosted LLM infrastructure.

Schema overview
---------------
users          - tenant identity, auth, billing mode
quotas         - per-user limits (rate, daily tokens, monthly USD)
api_keys       - hashed API keys (one user → many keys, rotatable)
pods           - GPU instance lifecycle (pool entries)
requests       - per-inference accounting (tokens, latency, cost)
invoices       - monthly billing rollup
audit_log      - append-only operator/security log
budget_alerts  - de-dupe ledger so 50/80/100 alerts fire once per period
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
import sqlalchemy as _sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"
    service = "service"


class BillingMode(str, enum.Enum):
    prepaid = "prepaid"
    postpaid = "postpaid"


class PodStatus(str, enum.Enum):
    provisioning = "provisioning"
    starting = "starting"
    ready = "ready"
    draining = "draining"
    terminated = "terminated"
    failed = "failed"


class TierName(str, enum.Enum):
    simple = "simple"
    architecture = "architecture"
    maximum = "maximum"
    ultra = "ultra"
    vision = "vision"


class InvoiceStatus(str, enum.Enum):
    open = "open"
    paid = "paid"
    void = "void"
    overdue = "overdue"


class RequestStatus(str, enum.Enum):
    ok = "ok"
    error = "error"
    timeout = "timeout"
    rejected_quota = "rejected_quota"
    rejected_budget = "rejected_budget"


# ---------------------------------------------------------------------------
# Users / auth
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), default=UserRole.user, nullable=False)
    billing_mode: Mapped[BillingMode] = mapped_column(
        Enum(BillingMode, name="billing_mode"), default=BillingMode.postpaid, nullable=False
    )
    monthly_budget_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("25.0000"), nullable=False)
    prepaid_balance_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0.0000"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pipeline_default: Mapped[str] = mapped_column(String(64), default="infer", nullable=False)
    # null = unrestricted; list of TierName values = whitelist (admin-set ceiling)
    allowed_tiers: Mapped[list[str] | None] = mapped_column(_sa.JSON, nullable=True, default=None)
    # user's own preference — subset of allowed_tiers; null = use all allowed
    preferred_tiers: Mapped[list[str] | None] = mapped_column(_sa.JSON, nullable=True, default=None)
    # providers user has disabled for their requests; null = use all
    disabled_providers: Mapped[list[str] | None] = mapped_column(_sa.JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    quota: Mapped["Quota"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")
    requests: Mapped[list["Request"]] = relationship(back_populates="user")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="user")
    provider_keys: Mapped[list["UserProviderKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("monthly_budget_usd >= 0", name="ck_users_budget_nonneg"),
        CheckConstraint("prepaid_balance_usd >= 0", name="ck_users_prepaid_nonneg"),
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)  # for UI display, e.g. "sk-llm-abcd"
    label: Mapped[str | None] = mapped_column(String(120))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="api_keys")


class UserProviderKey(Base):
    """User-supplied API key for a specific provider (encrypted at rest)."""

    __tablename__ = "user_provider_keys"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="provider_keys")

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_provider_key"),
    )


class Quota(Base):
    __tablename__ = "quotas"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    requests_per_minute: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    tokens_per_day: Mapped[int] = mapped_column(BigInteger, default=1_000_000, nullable=False)
    usd_per_month: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("25.0000"), nullable=False)
    max_tier: Mapped[TierName] = mapped_column(Enum(TierName, name="tier_name"), default=TierName.ultra, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="quota")


# ---------------------------------------------------------------------------
# Pods (GPU instance lifecycle)
# ---------------------------------------------------------------------------

class Pod(Base):
    __tablename__ = "pods"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)            # runpod | vast | lambda
    tier: Mapped[TierName] = mapped_column(Enum(TierName, name="tier_name"), nullable=False)
    gpu: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)        # provider-side pod/instance id
    endpoint_url: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[PodStatus] = mapped_column(Enum(PodStatus, name="pod_status"), nullable=False)
    cost_per_hour_usd: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    health_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict | None] = mapped_column(_sa.JSON)

    requests: Mapped[list["Request"]] = relationship(back_populates="pod")

    __table_args__ = (
        Index("ix_pods_tier_status", "tier", "status"),
        Index("ix_pods_provider_status", "provider", "status"),
        UniqueConstraint("provider", "external_id", name="uq_pods_provider_external"),
    )


# ---------------------------------------------------------------------------
# Requests (per-inference accounting)
# ---------------------------------------------------------------------------

class Request(Base):
    __tablename__ = "requests"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    pod_id: Mapped[str | None] = mapped_column(ForeignKey("pods.id", ondelete="SET NULL"))
    api_key_id: Mapped[str | None] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"))
    tier: Mapped[TierName] = mapped_column(Enum(TierName, name="tier_name"), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    pipeline: Mapped[str] = mapped_column(String(64), default="infer", nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    files_referenced: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    routing_reason: Mapped[str | None] = mapped_column(String(255))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"), nullable=False)
    status: Mapped[RequestStatus] = mapped_column(Enum(RequestStatus, name="request_status"), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="requests")
    pod: Mapped[Pod | None] = relationship(back_populates="requests")

    __table_args__ = (
        Index("ix_requests_user_created", "user_id", "created_at"),
        Index("ix_requests_pod_created", "pod_id", "created_at"),
        Index("ix_requests_tier_created", "tier", "created_at"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_requests_user_idempotency"),
    )


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(Enum(InvoiceStatus, name="invoice_status"), default=InvoiceStatus.open, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    line_items_json: Mapped[dict | None] = mapped_column(_sa.JSON)

    user: Mapped[User] = relationship(back_populates="invoices")

    __table_args__ = (
        UniqueConstraint("user_id", "period_start", "period_end", name="uq_invoices_user_period"),
    )


# ---------------------------------------------------------------------------
# Budget alerts (one row per (user, period, threshold) — dedupes notifications)
# ---------------------------------------------------------------------------

class BudgetAlert(Base):
    __tablename__ = "budget_alerts"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False)
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "period_start", "threshold_pct", name="uq_budget_alerts_unique"),
    )


# ---------------------------------------------------------------------------
# Audit log (append-only — application must not UPDATE/DELETE)
# ---------------------------------------------------------------------------

class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource: Mapped[str | None] = mapped_column(String(128))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    details_json: Mapped[dict | None] = mapped_column(_sa.JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_audit_user_created", "user_id", "created_at"),
        Index("ix_audit_action_created", "action", "created_at"),
    )
