"""Synchronous database helpers for the Streamlit dashboard.

Streamlit runs in a synchronous context. We use psycopg (sync) via SQLAlchemy
instead of asyncpg so there's no event-loop gymnastics.

The sync DATABASE_URL is derived from the env by replacing '+asyncpg' with
'+psycopg' (or using plain 'postgresql+psycopg://...').
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Generator

import streamlit as st
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from database.models import (
    ApiKey,
    AuditLog,
    BillingMode,
    BudgetAlert,
    Invoice,
    InvoiceStatus,
    Pod,
    PodStatus,
    Quota,
    Request,
    RequestStatus,
    TierName,
    User,
    UserProviderKey,
    UserRole,
)


def _sync_url() -> str:
    raw = os.getenv("DATABASE_URL", "postgresql+asyncpg://llm:change-me@postgres:5432/llm_infra")
    return raw.replace("+asyncpg", "+psycopg").replace("postgresql://", "postgresql+psycopg://")


@st.cache_resource
def _engine():
    return create_engine(_sync_url(), pool_pre_ping=True, pool_size=5, max_overflow=10)


def _session_factory() -> sessionmaker:
    return sessionmaker(_engine(), expire_on_commit=False)


@contextmanager
def db_session() -> Generator[Session, None, None]:
    SessionLocal = _session_factory()
    with SessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# Queries used by dashboard pages
# ---------------------------------------------------------------------------

def get_monthly_spend(session: Session, user_id: str | None, period_start: datetime) -> Decimal:
    q = select(func.coalesce(func.sum(Request.cost_usd), 0)).where(
        Request.status == RequestStatus.ok,
        Request.created_at >= period_start,
    )
    if user_id:
        q = q.where(Request.user_id == user_id)
    return session.execute(q).scalar_one()


def get_daily_costs(session: Session, days: int = 30, user_id: str | None = None) -> list[dict]:
    """Return list of {date, tier, cost_usd} for stacked area chart."""
    q = text("""
        SELECT
            DATE(created_at AT TIME ZONE 'UTC') AS day,
            tier,
            SUM(cost_usd)::float AS cost_usd,
            COUNT(*) AS requests
        FROM requests
        WHERE status = 'ok'
          AND created_at >= NOW() - INTERVAL ':days days'
          AND (CAST(:user_id AS UUID) IS NULL OR user_id = CAST(:user_id AS UUID))
        GROUP BY day, tier
        ORDER BY day, tier
    """)
    rows = session.execute(q, {"days": days, "user_id": user_id}).fetchall()
    return [{"date": str(r.day), "tier": r.tier, "cost_usd": r.cost_usd, "requests": r.requests} for r in rows]


def get_tier_distribution(session: Session, days: int = 30, user_id: str | None = None) -> list[dict]:
    q = text("""
        SELECT
            tier,
            COUNT(*) AS requests,
            SUM(cost_usd)::float AS cost_usd,
            SUM(prompt_tokens + completion_tokens) AS total_tokens
        FROM requests
        WHERE status = 'ok'
          AND created_at >= NOW() - INTERVAL ':days days'
          AND (CAST(:user_id AS UUID) IS NULL OR user_id = CAST(:user_id AS UUID))
        GROUP BY tier
    """)
    rows = session.execute(q, {"days": days, "user_id": user_id}).fetchall()
    return [{"tier": r.tier, "requests": r.requests, "cost_usd": r.cost_usd, "total_tokens": r.total_tokens} for r in rows]


def get_all_users_with_stats(session: Session) -> list[dict]:
    """Users + current month spend + request count."""
    now = datetime.now(timezone.utc)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    users = session.execute(select(User).order_by(User.created_at.desc())).scalars().all()
    result = []
    for u in users:
        spend = session.execute(
            select(func.coalesce(func.sum(Request.cost_usd), 0)).where(
                Request.user_id == u.id,
                Request.status == RequestStatus.ok,
                Request.created_at >= period_start,
            )
        ).scalar_one()
        reqs = session.execute(
            select(func.count(Request.id)).where(
                Request.user_id == u.id,
                Request.created_at >= period_start,
            )
        ).scalar_one()
        quota = session.get(Quota, u.id)
        result.append({
            "id": u.id,
            "email": u.email,
            "role": u.role,
            "billing_mode": u.billing_mode,
            "is_active": u.is_active,
            "monthly_budget_usd": float(u.monthly_budget_usd),
            "prepaid_balance_usd": float(u.prepaid_balance_usd),
            "monthly_spend_usd": float(spend),
            "monthly_requests": int(reqs),
            "allowed_tiers": u.allowed_tiers,       # None = unrestricted (admin ceiling)
            "preferred_tiers": u.preferred_tiers,   # user's own subset preference
            "disabled_providers": u.disabled_providers or [],
            "allow_local": getattr(u, "allow_local", False),
            "max_tier": quota.max_tier if quota else "ultra",
            "rpm": quota.requests_per_minute if quota else 60,
            "tpd": quota.tokens_per_day if quota else 1_000_000,
            "created_at": u.created_at,
        })
    return result


def get_active_pods(session: Session) -> list[dict]:
    pods = session.execute(
        select(Pod).where(Pod.status.in_([PodStatus.ready, PodStatus.starting, PodStatus.provisioning]))
        .order_by(Pod.started_at.desc())
    ).scalars().all()
    return [
        {
            "id": p.id,
            "provider": p.provider,
            "tier": p.tier,
            "gpu": p.gpu,
            "status": p.status,
            "cost_per_hour_usd": float(p.cost_per_hour_usd),
            "started_at": p.started_at,
            "last_used_at": p.last_used_at,
            "endpoint_url": p.endpoint_url,
            "health_failures": p.health_failures,
            "external_id": p.external_id,
        }
        for p in pods
    ]


def get_all_pods(session: Session, limit: int = 200) -> list[dict]:
    pods = session.execute(
        select(Pod).order_by(Pod.started_at.desc()).limit(limit)
    ).scalars().all()
    return [
        {
            "id": p.id,
            "provider": p.provider,
            "tier": p.tier,
            "gpu": p.gpu,
            "status": p.status,
            "cost_per_hour_usd": float(p.cost_per_hour_usd),
            "started_at": p.started_at,
            "last_used_at": p.last_used_at,
            "terminated_at": p.terminated_at,
        }
        for p in pods
    ]


def get_invoices(session: Session, user_id: str | None = None) -> list[dict]:
    q = select(Invoice, User.email).join(User).order_by(Invoice.created_at.desc()).limit(200)
    if user_id:
        q = q.where(Invoice.user_id == user_id)
    rows = session.execute(q).all()
    return [
        {
            "id": inv.id,
            "email": email,
            "user_id": inv.user_id,
            "period_start": inv.period_start,
            "period_end": inv.period_end,
            "amount_usd": float(inv.amount_usd),
            "request_count": inv.request_count,
            "status": inv.status,
            "paid_at": inv.paid_at,
            "created_at": inv.created_at,
        }
        for inv, email in rows
    ]


def get_budget_alerts(session: Session, days: int = 30) -> list[dict]:
    q = (
        select(BudgetAlert, User.email)
        .join(User)
        .where(BudgetAlert.fired_at >= text(f"NOW() - INTERVAL '{days} days'"))
        .order_by(BudgetAlert.fired_at.desc())
    )
    rows = session.execute(q).all()
    return [
        {
            "email": email,
            "threshold_pct": a.threshold_pct,
            "fired_at": a.fired_at,
            "period_start": a.period_start,
        }
        for a, email in rows
    ]


def get_requests_raw(
    session: Session,
    *,
    days: int = 30,
    user_id: str | None = None,
    tier: str | None = None,
    provider: str | None = None,
    limit: int = 10_000,
) -> list[dict]:
    q = (
        select(Request, User.email, Pod.provider)
        .join(User)
        .outerjoin(Pod, Request.pod_id == Pod.id)
        .where(
            Request.created_at >= text(f"NOW() - INTERVAL '{days} days'"),
        )
        .order_by(Request.created_at.desc())
        .limit(limit)
    )
    if user_id:
        q = q.where(Request.user_id == user_id)
    if tier:
        q = q.where(Request.tier == tier)
    if provider:
        q = q.where(Pod.provider == provider)
    rows = session.execute(q).all()
    return [
        {
            "id": r.id,
            "email": email,
            "provider": pod_provider or "unknown",
            "tier": r.tier,
            "model": r.model,
            "pipeline": r.pipeline,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "files_referenced": r.files_referenced,
            "latency_ms": r.latency_ms,
            "cost_usd": float(r.cost_usd),
            "status": r.status,
            "routing_reason": r.routing_reason,
            "created_at": r.created_at,
        }
        for r, email, pod_provider in rows
    ]


def get_api_provider_stats(session: Session) -> list[dict]:
    """Token usage + cost grouped by API provider for today and this month."""
    q = text("""
        SELECT
            p.provider,
            COUNT(r.id) FILTER (
                WHERE r.created_at >= DATE_TRUNC('day', NOW() AT TIME ZONE 'UTC')
            ) AS requests_today,
            COALESCE(SUM(r.prompt_tokens + r.completion_tokens) FILTER (
                WHERE r.created_at >= DATE_TRUNC('day', NOW() AT TIME ZONE 'UTC')
            ), 0) AS tokens_today,
            COALESCE(SUM(r.cost_usd) FILTER (
                WHERE r.created_at >= DATE_TRUNC('day', NOW() AT TIME ZONE 'UTC')
            ), 0)::float AS cost_today,
            COALESCE(SUM(r.cost_usd), 0)::float AS cost_month,
            COUNT(r.id) AS requests_month
        FROM requests r
        JOIN pods p ON p.id = r.pod_id
        WHERE r.status = 'ok'
          AND p.provider NOT IN ('runpod', 'vast', 'lambda', 'local', 'mock')
          AND r.created_at >= DATE_TRUNC('month', NOW() AT TIME ZONE 'UTC')
        GROUP BY p.provider
    """)
    rows = session.execute(q).fetchall()
    return [
        {
            "provider": r.provider,
            "requests_today": int(r.requests_today or 0),
            "tokens_today": int(r.tokens_today or 0),
            "cost_today": float(r.cost_today or 0),
            "cost_month": float(r.cost_month or 0),
            "requests_month": int(r.requests_month or 0),
        }
        for r in rows
    ]


def get_today_usage_by_tier(session: Session) -> list[dict]:
    q = text("""
        SELECT
            tier,
            COUNT(*) AS requests,
            SUM(latency_ms)::float / 3600000.0 AS hours,
            SUM(cost_usd)::float AS cost_usd
        FROM requests
        WHERE status = 'ok'
          AND created_at >= DATE_TRUNC('day', NOW() AT TIME ZONE 'UTC')
        GROUP BY tier
    """)
    rows = session.execute(q).fetchall()
    return [{"tier": r.tier, "requests": r.requests, "hours": r.hours, "cost_usd": r.cost_usd} for r in rows]


def get_user_provider_keys(session: Session, user_id: str) -> list[dict]:
    """Return list of provider names that have a stored key for this user (no plaintext)."""
    rows = session.execute(
        select(UserProviderKey).where(UserProviderKey.user_id == user_id)
    ).scalars().all()
    return [{"provider": r.provider, "created_at": r.created_at} for r in rows]


def upsert_user_provider_key(session: Session, user_id: str, provider: str, encrypted_key: str) -> None:
    existing = session.execute(
        select(UserProviderKey).where(
            UserProviderKey.user_id == user_id,
            UserProviderKey.provider == provider,
        )
    ).scalar_one_or_none()
    if existing:
        existing.encrypted_key = encrypted_key
    else:
        session.add(UserProviderKey(user_id=user_id, provider=provider, encrypted_key=encrypted_key))
    session.commit()


def delete_user_provider_key(session: Session, user_id: str, provider: str) -> None:
    existing = session.execute(
        select(UserProviderKey).where(
            UserProviderKey.user_id == user_id,
            UserProviderKey.provider == provider,
        )
    ).scalar_one_or_none()
    if existing:
        session.delete(existing)
        session.commit()


def update_user_preferences(
    session: Session,
    user_id: str,
    preferred_tiers: list[str] | None,
    disabled_providers: list[str] | None,
    provider_order: list[str] | None = None,
) -> None:
    user = session.get(User, user_id)
    if user:
        user.preferred_tiers = preferred_tiers or None
        user.disabled_providers = disabled_providers or None
        user.provider_order = provider_order or None
        session.commit()
