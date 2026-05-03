"""Quota enforcement: rate limits, daily token budget, monthly spend gate.

Checks (in order):
  1. RPM — Redis sliding-window token bucket keyed by user_id.
  2. Daily tokens — Redis counter reset at UTC midnight.
  3. Monthly USD — DB aggregate vs quota.usd_per_month.

Raises HTTP 429 on any breach. The monthly USD check also consults
budget_alerts to fire 50/80/100% notifications (once per threshold per period).
"""

from __future__ import annotations

import calendar
import uuid as _uuid
from datetime import datetime, timezone
from decimal import Decimal

import structlog
from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bridge.settings import settings
from database.models import BudgetAlert, Quota, Request, RequestStatus, User

log = structlog.get_logger(__name__)

_MONTH_SECONDS = 31 * 24 * 3600  # conservative TTL for monthly keys


def _period_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _period_seconds_remaining(now: datetime) -> int:
    year, month = now.year, now.month
    last_day = calendar.monthrange(year, month)[1]
    period_end = now.replace(day=last_day, hour=23, minute=59, second=59, microsecond=999999)
    return max(1, int((period_end - now).total_seconds()))


async def check_rpm(user_id: str, redis: Redis) -> None:
    """Sliding-window token bucket: max requests_per_minute per user."""
    quota_rpm = settings.rate_limit_rpm_default
    key = f"rpm:{user_id}"
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    window_ms = 60_000

    pipe = redis.pipeline()
    pipe.zremrangebyscore(key, "-inf", now_ms - window_ms)
    pipe.zadd(key, {f"{now_ms}:{_uuid.uuid4().hex}": now_ms})
    pipe.zcard(key)
    pipe.expire(key, 120)
    results = await pipe.execute()
    count = results[2]

    if count > quota_rpm:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit: {quota_rpm} requests/minute",
            headers={"Retry-After": "60"},
        )


async def check_daily_tokens(user_id: str, prompt_tokens: int, redis: Redis) -> None:
    """Increment daily token counter; reject if it would exceed quota."""
    quota = settings.tokens_per_day_default
    now = datetime.now(timezone.utc)
    key = f"tpd:{user_id}:{now.strftime('%Y%m%d')}"

    current = await redis.get(key)
    current_val = int(current) if current else 0

    if current_val + prompt_tokens > quota:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily token quota exceeded ({quota:,} tokens/day)",
            headers={"Retry-After": str(_seconds_until_midnight(now))},
        )

    pipe = redis.pipeline()
    pipe.incrby(key, prompt_tokens)
    pipe.expireat(key, _next_midnight_ts(now))
    await pipe.execute()


async def check_monthly_budget(
    user: User,
    projected_cost_usd: float,
    session: AsyncSession,
    redis: Redis,
) -> None:
    """Reject if user would exceed monthly budget; fire tiered alerts."""
    now = datetime.now(timezone.utc)
    period_start = _period_start(now)

    result = await session.execute(
        select(func.coalesce(func.sum(Request.cost_usd), Decimal("0")))
        .where(
            Request.user_id == user.id,
            Request.status == RequestStatus.ok,
            Request.created_at >= period_start,
        )
    )
    spent: Decimal = result.scalar_one()
    budget: Decimal = user.monthly_budget_usd

    if spent + Decimal(str(projected_cost_usd)) > budget:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Monthly budget ${budget:.2f} would be exceeded (spent ${spent:.4f})",
        )

    await _maybe_fire_alerts(user, spent, budget, period_start, session, redis)


async def _maybe_fire_alerts(
    user: User,
    spent: Decimal,
    budget: Decimal,
    period_start: datetime,
    session: AsyncSession,
    redis: Redis,
) -> None:
    if budget <= 0:
        return

    pct_used = float(spent / budget * 100)
    for threshold in settings.alert_percents_list:
        if pct_used < threshold:
            continue

        dedup_key = f"budget_alert:{user.id}:{period_start.strftime('%Y%m')}:{threshold}"
        already_sent = await redis.get(dedup_key)
        if already_sent:
            continue

        # Persist + mark sent
        session.add(
            BudgetAlert(
                user_id=user.id,
                period_start=period_start,
                threshold_pct=threshold,
            )
        )
        await session.commit()
        await redis.setex(dedup_key, _MONTH_SECONDS, "1")

        log.warning(
            "budget_alert_fired",
            user_id=user.id,
            email=user.email,
            threshold_pct=threshold,
            spent_usd=float(spent),
            budget_usd=float(budget),
        )
        # TODO: send email notification via background task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seconds_until_midnight(now: datetime) -> int:
    next_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    import datetime as dt
    next_midnight = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((next_midnight - now).total_seconds()))


def _next_midnight_ts(now: datetime) -> int:
    import datetime as dt
    next_midnight = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(next_midnight.timestamp())
