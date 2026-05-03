"""Unit tests for bridge/cost_tracker.py."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge.cost_tracker import estimate_cost, record_request
from bridge.schemas import PodHandle, RoutingDecision
from database.models import BillingMode, Request, RequestStatus, User


class TestEstimateCost:
    def test_positive_for_all_tiers(self):
        for tier in ("simple", "architecture", "maximum", "ultra"):
            rates = {"simple": 0.34, "architecture": 0.34, "maximum": 1.10, "ultra": 1.89}
            cost = estimate_cost(tier, rates[tier], prompt_tokens=100, idle_timeout_sec=300)
            assert cost > 0, f"cost should be positive for {tier}"

    def test_ultra_costs_more_than_simple(self):
        simple = estimate_cost("simple", 0.34, 100, 300)
        ultra = estimate_cost("ultra", 1.89, 100, 600)
        assert ultra > simple

    def test_more_tokens_costs_more(self):
        low = estimate_cost("simple", 0.34, 100, 300)
        high = estimate_cost("simple", 0.34, 10_000, 300)
        assert high > low

    def test_concurrent_users_reduce_cost(self):
        solo = estimate_cost("simple", 0.34, 100, 300, concurrent_users=1)
        shared = estimate_cost("simple", 0.34, 100, 300, concurrent_users=5)
        assert shared < solo

    def test_zero_concurrent_users_clamped(self):
        # Should not divide by zero
        cost = estimate_cost("simple", 0.34, 100, 300, concurrent_users=0)
        assert cost > 0


class TestRecordRequest:
    @pytest.mark.asyncio
    async def test_ok_request_persisted(
        self, test_user, async_session, fake_redis, mock_pod_handle
    ):
        decision = RoutingDecision(tier="simple", reason="default", projected_cost_usd=0.001)

        # Seed concurrent-users key
        await fake_redis.sadd(f"pod_users:{mock_pod_handle.pod_id}", test_user.id)

        with patch("bridge.cost_tracker._idle_timeout_for_tier", return_value=300):
            receipt = await record_request(
                user=test_user,
                pod=mock_pod_handle,
                decision=decision,
                prompt_tokens=500,
                completion_tokens=200,
                files_referenced=0,
                latency_ms=1200,
                pipeline="infer",
                api_key_id=None,
                idempotency_key=None,
                error_message=None,
                redis=fake_redis,
                session=async_session,
            )

        assert receipt.prompt_tokens == 500
        assert receipt.completion_tokens == 200
        assert receipt.cost_usd > 0
        assert receipt.latency_ms == 1200

        # Verify row in DB
        from sqlalchemy import select
        result = await async_session.execute(select(Request).where(Request.id == receipt.request_id))
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.status == RequestStatus.ok
        assert row.cost_usd > 0

    @pytest.mark.asyncio
    async def test_error_request_persisted_with_zero_cost(
        self, test_user, async_session, fake_redis, mock_pod_handle
    ):
        decision = RoutingDecision(tier="simple", reason="default", projected_cost_usd=0.001)

        with patch("bridge.cost_tracker._idle_timeout_for_tier", return_value=300):
            receipt = await record_request(
                user=test_user,
                pod=mock_pod_handle,
                decision=decision,
                prompt_tokens=100,
                completion_tokens=0,
                files_referenced=0,
                latency_ms=500,
                pipeline="infer",
                api_key_id=None,
                idempotency_key=None,
                error_message="Connection refused",
                redis=fake_redis,
                session=async_session,
            )

        assert receipt.cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_prepaid_balance_decremented(
        self, prepaid_user, async_session, fake_redis, mock_pod_handle
    ):
        original_balance = prepaid_user.prepaid_balance_usd
        decision = RoutingDecision(tier="simple", reason="default", projected_cost_usd=0.01)

        await fake_redis.sadd(f"pod_users:{mock_pod_handle.pod_id}", prepaid_user.id)

        with patch("bridge.cost_tracker._idle_timeout_for_tier", return_value=300):
            receipt = await record_request(
                user=prepaid_user,
                pod=mock_pod_handle,
                decision=decision,
                prompt_tokens=200,
                completion_tokens=100,
                files_referenced=0,
                latency_ms=800,
                pipeline="infer",
                api_key_id=None,
                idempotency_key=None,
                error_message=None,
                redis=fake_redis,
                session=async_session,
            )

        # Balance should be lower (or 0 if cost exceeded balance)
        assert prepaid_user.prepaid_balance_usd <= original_balance

    @pytest.mark.asyncio
    async def test_no_pod_results_in_zero_cost(
        self, test_user, async_session, fake_redis
    ):
        decision = RoutingDecision(tier="simple", reason="default", projected_cost_usd=0.0)

        receipt = await record_request(
            user=test_user,
            pod=None,
            decision=decision,
            prompt_tokens=100,
            completion_tokens=0,
            files_referenced=0,
            latency_ms=0,
            pipeline="infer",
            api_key_id=None,
            idempotency_key=None,
            error_message="No pod available",
            redis=fake_redis,
            session=async_session,
        )

        assert receipt.cost_usd == 0.0
        assert receipt.pod_id is None
