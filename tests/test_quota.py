"""Unit tests for bridge/quota.py — rate limits and budget enforcement."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from bridge.quota import check_daily_tokens, check_monthly_budget, check_rpm
from database.models import BudgetAlert


class TestRPM:
    @pytest.mark.asyncio
    async def test_first_request_passes(self, test_user, fake_redis):
        with patch("bridge.quota.settings") as s:
            s.rate_limit_rpm_default = 60
            await check_rpm(test_user.id, fake_redis)  # should not raise

    @pytest.mark.asyncio
    async def test_exceeds_rpm_raises_429(self, test_user, fake_redis):
        with patch("bridge.quota.settings") as s:
            s.rate_limit_rpm_default = 3
            # Fill the bucket
            for _ in range(3):
                await check_rpm(test_user.id, fake_redis)
            # 4th should fail
            with pytest.raises(HTTPException) as exc:
                await check_rpm(test_user.id, fake_redis)
            assert exc.value.status_code == 429
            assert "Rate limit" in exc.value.detail

    @pytest.mark.asyncio
    async def test_different_users_independent(self, test_user, prepaid_user, fake_redis):
        with patch("bridge.quota.settings") as s:
            s.rate_limit_rpm_default = 2
            # Exhaust user1
            for _ in range(2):
                await check_rpm(test_user.id, fake_redis)
            with pytest.raises(HTTPException):
                await check_rpm(test_user.id, fake_redis)
            # user2 still fine
            await check_rpm(prepaid_user.id, fake_redis)


class TestDailyTokens:
    @pytest.mark.asyncio
    async def test_small_request_passes(self, test_user, fake_redis):
        with patch("bridge.quota.settings") as s:
            s.tokens_per_day_default = 100_000
            await check_daily_tokens(test_user.id, 1_000, fake_redis)  # no raise

    @pytest.mark.asyncio
    async def test_exceeds_daily_limit_raises_429(self, test_user, fake_redis):
        with patch("bridge.quota.settings") as s:
            s.tokens_per_day_default = 1_000
            # Pre-fill counter
            await fake_redis.set(
                f"tpd:{test_user.id}:{datetime.now(timezone.utc).strftime('%Y%m%d')}",
                "900",
            )
            with pytest.raises(HTTPException) as exc:
                await check_daily_tokens(test_user.id, 200, fake_redis)
            assert exc.value.status_code == 429

    @pytest.mark.asyncio
    async def test_counter_incremented(self, test_user, fake_redis):
        with patch("bridge.quota.settings") as s:
            s.tokens_per_day_default = 100_000
            key = f"tpd:{test_user.id}:{datetime.now(timezone.utc).strftime('%Y%m%d')}"
            await check_daily_tokens(test_user.id, 500, fake_redis)
            val = await fake_redis.get(key)
            assert int(val) == 500


class TestMonthlyBudget:
    @pytest.mark.asyncio
    async def test_within_budget_passes(self, test_user, async_session, fake_redis):
        with patch("bridge.quota.settings") as s:
            s.alert_percents_list = [50, 80, 100]
            await check_monthly_budget(test_user, 0.001, async_session, fake_redis)

    @pytest.mark.asyncio
    async def test_exceeds_budget_raises_429(self, test_user, async_session, fake_redis):
        from database.models import Request, RequestStatus
        import uuid

        # Inject spend that eats the whole budget
        now = datetime.now(timezone.utc)
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        async_session.add(Request(
            id=str(uuid.uuid4()),
            user_id=test_user.id,
            tier="ultra",
            model="test",
            pipeline="infer",
            prompt_tokens=1000,
            completion_tokens=500,
            files_referenced=0,
            latency_ms=1000,
            cost_usd=Decimal("24.99"),  # almost all of $25 budget
            status=RequestStatus.ok,
            created_at=now,
        ))
        await async_session.commit()

        with patch("bridge.quota.settings") as s:
            s.alert_percents_list = [50, 80, 100]
            with pytest.raises(HTTPException) as exc:
                await check_monthly_budget(test_user, 5.0, async_session, fake_redis)
            assert exc.value.status_code == 429

    @pytest.mark.asyncio
    async def test_alert_fires_at_80_pct(self, test_user, async_session, fake_redis):
        from database.models import Request, RequestStatus
        import uuid

        now = datetime.now(timezone.utc)

        # $20 spent of $25 budget = 80%
        async_session.add(Request(
            id=str(uuid.uuid4()),
            user_id=test_user.id,
            tier="simple",
            model="test",
            pipeline="infer",
            prompt_tokens=100,
            completion_tokens=50,
            files_referenced=0,
            latency_ms=500,
            cost_usd=Decimal("20.00"),
            status=RequestStatus.ok,
            created_at=now,
        ))
        await async_session.commit()

        with patch("bridge.quota.settings") as s:
            s.alert_percents_list = [50, 80, 100]
            await check_monthly_budget(test_user, 0.001, async_session, fake_redis)

        # BudgetAlert rows for 50% and 80% thresholds should exist
        from sqlalchemy import select
        alerts = (await async_session.execute(
            select(BudgetAlert).where(BudgetAlert.user_id == test_user.id)
        )).scalars().all()
        fired_thresholds = {a.threshold_pct for a in alerts}
        assert 80 in fired_thresholds

    @pytest.mark.asyncio
    async def test_alert_deduped(self, test_user, async_session, fake_redis):
        now = datetime.now(timezone.utc)
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Pre-seed dedup key in Redis (alert already sent)
        dedup_key = f"budget_alert:{test_user.id}:{period_start.strftime('%Y%m')}:80"
        await fake_redis.setex(dedup_key, 3600, "1")

        from database.models import Request, RequestStatus
        import uuid

        async_session.add(Request(
            id=str(uuid.uuid4()),
            user_id=test_user.id,
            tier="simple",
            model="test",
            pipeline="infer",
            prompt_tokens=100,
            completion_tokens=50,
            files_referenced=0,
            latency_ms=500,
            cost_usd=Decimal("20.00"),
            status=RequestStatus.ok,
            created_at=now,
        ))
        await async_session.commit()

        with patch("bridge.quota.settings") as s:
            s.alert_percents_list = [50, 80, 100]
            await check_monthly_budget(test_user, 0.001, async_session, fake_redis)

        from sqlalchemy import select
        alerts = (await async_session.execute(
            select(BudgetAlert).where(
                BudgetAlert.user_id == test_user.id,
                BudgetAlert.threshold_pct == 80,
            )
        )).scalars().all()
        # Should NOT have created a new row (deduped)
        assert len(alerts) == 0
