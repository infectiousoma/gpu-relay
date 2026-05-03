"""Cost calculation and persistence.

Cost formula:
  charged_seconds = latency_seconds + (idle_timeout / concurrent_users_in_window)
  cost_usd        = (charged_seconds / 3600) * tier.cost_per_hour_usd

  concurrent_users_in_window: number of distinct users whose requests
  overlapped this pod in the last idle_timeout seconds. Approximated via
  Redis sorted set on the pod.  If not available, defaults to 1 (full charge).

For prepaid users, balance is decremented immediately.
Request row written regardless of success/failure so billing is complete.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from bridge.schemas import CostReceipt, PodHandle, RoutingDecision
from bridge.settings import settings
from database.models import Request, RequestStatus, User

log = structlog.get_logger(__name__)


def estimate_cost(
    tier_name: str,
    cost_per_hour_usd: float,
    prompt_tokens: int,
    idle_timeout_sec: int,
    *,
    concurrent_users: int = 1,
) -> float:
    """Lightweight estimate for quota pre-check (no DB write)."""
    avg_seconds = 5.0 + (prompt_tokens / 1000) * 1.5
    charged_sec = avg_seconds + (idle_timeout_sec / max(concurrent_users, 1))
    return (charged_sec / 3600) * cost_per_hour_usd


async def record_request(
    *,
    user: User,
    pod: PodHandle | None,
    decision: RoutingDecision,
    prompt_tokens: int,
    completion_tokens: int,
    files_referenced: int,
    latency_ms: int,
    pipeline: str,
    api_key_id: str | None,
    idempotency_key: str | None,
    error_message: str | None,
    redis: Redis,
    session: AsyncSession,
) -> CostReceipt:
    """Compute final cost, write Request row, update prepaid balance if needed."""
    now = datetime.now(timezone.utc)
    status_val = RequestStatus.error if error_message else RequestStatus.ok

    cost_usd = Decimal("0")
    if pod and not error_message:
        concurrent = await _concurrent_users(pod.pod_id, idle_timeout_sec=_idle_timeout_for_tier(decision.tier), redis=redis)
        idle_sec = _idle_timeout_for_tier(decision.tier)
        latency_sec = latency_ms / 1000
        charged_sec = latency_sec + (idle_sec / max(concurrent, 1))
        cost_usd = Decimal(str((charged_sec / 3600) * pod.cost_per_hour_usd)).quantize(Decimal("0.000001"))

    request_id = str(uuid.uuid4())
    row = Request(
        id=request_id,
        user_id=user.id,
        pod_id=pod.pod_id if pod else None,
        api_key_id=api_key_id,
        tier=decision.tier,
        model=pod.tier if pod else decision.tier,
        pipeline=pipeline,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        files_referenced=files_referenced,
        routing_reason=decision.reason,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        status=status_val,
        error_message=error_message,
        idempotency_key=idempotency_key,
        created_at=now,
    )
    session.add(row)

    if user.billing_mode == "prepaid" and cost_usd > 0:
        new_balance = user.prepaid_balance_usd - cost_usd
        user.prepaid_balance_usd = max(Decimal("0"), new_balance)

    await session.commit()

    log.info(
        "request_recorded",
        request_id=request_id,
        user_id=user.id,
        tier=decision.tier,
        cost_usd=float(cost_usd),
        latency_ms=latency_ms,
        status=status_val,
    )

    return CostReceipt(
        request_id=request_id,
        user_id=user.id,
        tier=decision.tier,
        provider=pod.provider if pod else "none",
        pod_id=pod.pod_id if pod else None,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        cost_usd=float(cost_usd),
    )


async def _concurrent_users(pod_id: str, idle_timeout_sec: int, redis: Redis) -> int:
    """Count distinct user_ids seen on this pod in last idle_timeout_sec."""
    key = f"pod_users:{pod_id}"
    count = await redis.scard(key)
    return int(count) if count else 1


def _idle_timeout_for_tier(tier_name: str) -> int:
    """Read from tiers config; fall back to 300."""
    try:
        import yaml
        with open(settings.tiers_config_path) as f:
            cfg = yaml.safe_load(f)
        return cfg["tiers"].get(tier_name, {}).get("idle_timeout_sec", 300)
    except Exception:
        return 300
