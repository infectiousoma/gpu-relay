"""Unit tests for bridge/router.py — tier selection logic."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from bridge.router import (
    TIER_ORDER,
    _highest,
    _keyword_tier,
    _min_tier_for_files,
    _min_tier_for_tokens,
    _projected_cost,
    select_tier,
)
from bridge.schemas import ChatCompletionRequest, ChatMessage
from tests.conftest import TEST_TIERS, make_chat_request


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_highest_prefers_later_tier(self):
        assert _highest("simple", "architecture") == "architecture"
        assert _highest("maximum", "ultra") == "ultra"
        assert _highest("ultra", "simple") == "ultra"
        assert _highest("simple", "simple") == "simple"

    def test_min_tier_for_tokens_small(self):
        # 100 tokens → simple
        assert _min_tier_for_tokens(100) == "simple"

    def test_min_tier_for_tokens_medium(self):
        # 10k tokens → architecture (> simple's 8000 limit)
        assert _min_tier_for_tokens(10_000) == "architecture"

    def test_min_tier_for_tokens_large(self):
        # 30k tokens → maximum (> architecture's 24000 limit)
        assert _min_tier_for_tokens(30_000) == "maximum"

    def test_min_tier_for_tokens_huge(self):
        # 65k tokens → ultra
        assert _min_tier_for_tokens(65_000) == "ultra"

    def test_min_tier_for_files_few(self):
        assert _min_tier_for_files(2) == "simple"

    def test_min_tier_for_files_many(self):
        # 25 files → maximum (> architecture's 20 limit)
        assert _min_tier_for_files(25) == "maximum"

    def test_min_tier_for_files_massive(self):
        # 100 files → ultra
        assert _min_tier_for_files(100) == "ultra"

    def test_keyword_tier_architecture(self):
        tier = _keyword_tier("please do a full architecture review")
        assert tier == "architecture"

    def test_keyword_tier_audit(self):
        tier = _keyword_tier("security audit of this codebase")
        assert tier == "maximum"

    def test_keyword_tier_no_match(self):
        tier = _keyword_tier("fix this bug in my code")
        assert tier is None

    def test_keyword_tier_mission_critical(self):
        tier = _keyword_tier("this is mission-critical infrastructure design")
        assert tier == "ultra"

    def test_projected_cost_positive(self):
        cost = _projected_cost("simple", 100)
        assert cost > 0
        # simple: 0.34/hr. Even 5s latency + 300s idle = 305s = 0.085hr * 0.34 ≈ 0.029
        assert cost < 1.0

    def test_projected_cost_ultra_higher(self):
        cost_simple = _projected_cost("simple", 100)
        cost_ultra = _projected_cost("ultra", 100)
        assert cost_ultra > cost_simple


# ---------------------------------------------------------------------------
# select_tier — main routing function
# ---------------------------------------------------------------------------

class TestSelectTier:

    @pytest.mark.asyncio
    async def test_explicit_override_header(self, test_user, mock_http_request):
        mock_http_request.headers = {"x-tier": "ultra"}
        req = make_chat_request("Hello")
        decision = await select_tier(req, test_user, mock_http_request, Decimal("0"))
        assert decision.tier == "ultra"
        assert "override" in decision.reason

    @pytest.mark.asyncio
    async def test_explicit_override_query_param(self, test_user, mock_http_request):
        mock_http_request.query_params = {"tier": "maximum"}
        req = make_chat_request("Hello")
        decision = await select_tier(req, test_user, mock_http_request, Decimal("0"))
        assert decision.tier == "maximum"

    @pytest.mark.asyncio
    async def test_model_field_as_tier(self, test_user, mock_http_request):
        req = make_chat_request("Hello", model="llm-architecture")
        decision = await select_tier(req, test_user, mock_http_request, Decimal("0"))
        assert decision.tier == "architecture"
        assert "override" in decision.reason

    @pytest.mark.asyncio
    async def test_default_simple(self, test_user, mock_http_request, simple_request):
        decision = await select_tier(simple_request, test_user, mock_http_request, Decimal("0"))
        assert decision.tier == "simple"

    @pytest.mark.asyncio
    async def test_keyword_triggers_architecture(self, test_user, mock_http_request, architecture_request):
        decision = await select_tier(architecture_request, test_user, mock_http_request, Decimal("0"))
        assert decision.tier == "architecture"
        assert "keywords" in decision.reason

    @pytest.mark.asyncio
    async def test_large_context_upgrades_tier(self, test_user, mock_http_request, large_context_request):
        decision = await select_tier(large_context_request, test_user, mock_http_request, Decimal("0"))
        # 9k tokens > simple's 8k limit → at least architecture
        assert decision.tier in ("architecture", "maximum", "ultra")

    @pytest.mark.asyncio
    async def test_many_files_triggers_architecture(self, test_user, mock_http_request):
        req = make_chat_request("refactor this", files_referenced=15)
        decision = await select_tier(req, test_user, mock_http_request, Decimal("0"))
        # 15 files > simple's 3-file limit
        assert decision.tier in ("architecture", "maximum", "ultra")

    @pytest.mark.asyncio
    async def test_budget_downgrade(self, test_user, mock_http_request):
        # $0.05 remaining: architecture (~$0.057) rejected, simple (~$0.029) fits
        test_user.monthly_budget_usd = Decimal("25.00")
        spent = Decimal("24.95")

        req = make_chat_request("architecture review")  # would want architecture tier
        decision = await select_tier(req, test_user, mock_http_request, spent)
        # Should be downgraded to simple (cheapest that fits in $0.05 remaining)
        assert decision.tier == "simple"
        assert decision.downgraded_from == "architecture"

    @pytest.mark.asyncio
    async def test_budget_exhausted_raises_402(self, test_user, mock_http_request):
        from fastapi import HTTPException
        test_user.monthly_budget_usd = Decimal("0.001")
        req = make_chat_request("Hello")
        with pytest.raises(HTTPException) as exc_info:
            await select_tier(req, test_user, mock_http_request, Decimal("0.001"))
        assert exc_info.value.status_code == 402

    @pytest.mark.asyncio
    async def test_projected_cost_returned(self, test_user, mock_http_request, simple_request):
        decision = await select_tier(simple_request, test_user, mock_http_request, Decimal("0"))
        assert decision.projected_cost_usd > 0

    @pytest.mark.asyncio
    async def test_downgraded_from_populated(self, test_user, mock_http_request):
        # Force downgrade by leaving almost no budget
        test_user.monthly_budget_usd = Decimal("25.00")
        req = make_chat_request("security audit of entire system analyze everything")
        # Near-exhausted budget
        decision = await select_tier(req, test_user, mock_http_request, Decimal("24.95"))
        if decision.downgraded_from:
            assert TIER_ORDER.index(decision.downgraded_from) > TIER_ORDER.index(decision.tier)
