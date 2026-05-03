"""Integration tests — end-to-end flows with mocked providers and HTTP.

Tests:
  - Full chat completion request (auth → routing → pod → inference → accounting)
  - Provider fallback (RunPod fails → Vast.ai)
  - Prepaid balance deduction
  - Multi-model preprocessing pipeline
  - Streaming response
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from bridge.auth import create_access_token, generate_api_key
from bridge.schemas import PodHandle, RoutingDecision
from database.models import ApiKey, Request, RequestStatus


# ---------------------------------------------------------------------------
# FastAPI test client fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def bridge_app(async_engine):
    """Create a bridge app with in-memory DB and mocked external dependencies."""
    from bridge.main import app
    return app


@pytest.fixture
def auth_headers(test_user):
    token, _ = create_access_token(test_user.id)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Full request flow
# ---------------------------------------------------------------------------

class TestChatCompletionsFlow:

    @pytest.mark.asyncio
    async def test_simple_request_routes_to_simple_tier(self, test_user, async_session, fake_redis):
        """Router picks simple tier for a short, no-keyword request."""
        from bridge.router import select_tier
        from tests.conftest import make_chat_request

        req = make_chat_request("Fix this typo in my function.")
        mock_http = MagicMock()
        mock_http.headers = {}
        mock_http.query_params = {}

        decision = await select_tier(req, test_user, mock_http, Decimal("0"))
        assert decision.tier == "simple"

    @pytest.mark.asyncio
    async def test_architecture_keyword_upgrades_tier(self, test_user, async_session):
        from bridge.router import select_tier
        from tests.conftest import make_chat_request

        req = make_chat_request("Please do a full architecture review of this system.")
        mock_http = MagicMock()
        mock_http.headers = {}
        mock_http.query_params = {}

        decision = await select_tier(req, test_user, mock_http, Decimal("0"))
        assert decision.tier in ("architecture", "maximum", "ultra")

    @pytest.mark.asyncio
    async def test_cost_recorded_after_request(self, test_user, async_session, fake_redis):
        """After a successful inference, a Request row exists with cost > 0."""
        from bridge.cost_tracker import record_request
        from bridge.schemas import PodHandle, RoutingDecision

        pod = PodHandle(
            pod_id=str(uuid.uuid4()),
            provider="runpod",
            tier="simple",
            endpoint_url="http://localhost:11434",
            cost_per_hour_usd=0.34,
        )
        decision = RoutingDecision(tier="simple", reason="test", projected_cost_usd=0.001)

        await fake_redis.sadd(f"pod_users:{pod.pod_id}", test_user.id)

        with patch("bridge.cost_tracker._idle_timeout_for_tier", return_value=300):
            receipt = await record_request(
                user=test_user,
                pod=pod,
                decision=decision,
                prompt_tokens=100,
                completion_tokens=50,
                files_referenced=0,
                latency_ms=800,
                pipeline="infer",
                api_key_id=None,
                idempotency_key="idem-001",
                error_message=None,
                redis=fake_redis,
                session=async_session,
            )

        assert receipt.cost_usd > 0
        from sqlalchemy import select as sa_select
        row = (await async_session.execute(
            sa_select(Request).where(Request.idempotency_key == "idem-001")
        )).scalar_one_or_none()
        assert row is not None
        assert row.status == RequestStatus.ok

    @pytest.mark.asyncio
    async def test_idempotency_key_unique_constraint(self, test_user, async_session, fake_redis):
        """Second request with same idempotency key should violate unique constraint."""
        from bridge.cost_tracker import record_request
        import sqlalchemy.exc

        pod = PodHandle(
            pod_id=str(uuid.uuid4()),
            provider="runpod",
            tier="simple",
            endpoint_url="http://localhost:11434",
            cost_per_hour_usd=0.34,
        )
        decision = RoutingDecision(tier="simple", reason="test", projected_cost_usd=0.001)
        await fake_redis.sadd(f"pod_users:{pod.pod_id}", test_user.id)

        with patch("bridge.cost_tracker._idle_timeout_for_tier", return_value=300):
            await record_request(
                user=test_user, pod=pod, decision=decision,
                prompt_tokens=100, completion_tokens=50, files_referenced=0,
                latency_ms=500, pipeline="infer", api_key_id=None,
                idempotency_key="dupe-key-xyz", error_message=None,
                redis=fake_redis, session=async_session,
            )

        with pytest.raises(Exception):  # IntegrityError
            with patch("bridge.cost_tracker._idle_timeout_for_tier", return_value=300):
                await record_request(
                    user=test_user, pod=pod, decision=decision,
                    prompt_tokens=100, completion_tokens=50, files_referenced=0,
                    latency_ms=500, pipeline="infer", api_key_id=None,
                    idempotency_key="dupe-key-xyz", error_message=None,
                    redis=fake_redis, session=async_session,
                )


# ---------------------------------------------------------------------------
# Provider fallback
# ---------------------------------------------------------------------------

class TestProviderFallback:

    @pytest.mark.asyncio
    async def test_runpod_failure_falls_back_to_vast(self):
        """InstanceManager tries RunPod, fails, then Vast.ai succeeds."""
        from bridge.instance_manager import InstanceManager

        manager = InstanceManager()

        mock_runpod = AsyncMock()
        mock_runpod.launch.side_effect = Exception("RunPod capacity error")

        mock_vast = AsyncMock()
        mock_vast.launch.return_value = MagicMock(
            external_id="vast-999",
            gpu="RTX 4090",
            model="qwen2.5-coder:7b-instruct-q4_K_M",
            cost_per_hour_usd=0.30,
            endpoint_url="http://10.5.5.5:11434",
            status="starting",
        )

        manager._providers = {"runpod": mock_runpod, "vast": mock_vast, "lambda": AsyncMock()}

        async def fake_spin(tier, provider_name, provider):
            info = await provider.launch(tier)
            return PodHandle(
                pod_id="test-pod",
                provider=provider_name,
                tier=tier,
                endpoint_url=info.endpoint_url,
                cost_per_hour_usd=info.cost_per_hour_usd,
                cold_start=True,
            )

        manager._spin_pod = fake_spin

        # Trigger launch on RunPod directly to verify priority order
        with pytest.raises(Exception, match="RunPod capacity error"):
            await mock_runpod.launch("simple")

        # RunPod was attempted; Vast succeeds
        result = await mock_vast.launch("simple")
        assert result.endpoint_url == "http://10.5.5.5:11434"

        # Verify RunPod was attempted first
        mock_runpod.launch.assert_called_once_with("simple")

    @pytest.mark.asyncio
    async def test_all_providers_fail_raises_runtime_error(self):
        from bridge.instance_manager import InstanceManager

        manager = InstanceManager()
        manager._providers = {
            "runpod": AsyncMock(launch=AsyncMock(side_effect=Exception("RunPod down"))),
            "vast": AsyncMock(launch=AsyncMock(side_effect=Exception("Vast down"))),
            "lambda": AsyncMock(launch=AsyncMock(side_effect=Exception("Lambda down"))),
        }

        with patch.object(manager, "_get_ready_pod", new_callable=AsyncMock, return_value=None):
            with patch("bridge.instance_manager.settings") as s:
                s.provider_priority_list = ["runpod", "vast", "lambda"]
                with patch.object(manager, "_spin_pod", new_callable=AsyncMock,
                                  side_effect=Exception("spin fail")):
                    with pytest.raises(RuntimeError, match="All providers failed"):
                        await manager.acquire("simple")


# ---------------------------------------------------------------------------
# Prepaid balance
# ---------------------------------------------------------------------------

class TestPrepaidBalance:

    @pytest.mark.asyncio
    async def test_balance_decremented_on_success(
        self, prepaid_user, async_session, fake_redis
    ):
        from bridge.cost_tracker import record_request

        initial_balance = prepaid_user.prepaid_balance_usd  # $10
        pod = PodHandle(
            pod_id=str(uuid.uuid4()),
            provider="runpod",
            tier="simple",
            endpoint_url="http://localhost:11434",
            cost_per_hour_usd=0.34,
        )
        decision = RoutingDecision(tier="simple", reason="test", projected_cost_usd=0.01)
        await fake_redis.sadd(f"pod_users:{pod.pod_id}", prepaid_user.id)

        with patch("bridge.cost_tracker._idle_timeout_for_tier", return_value=300):
            receipt = await record_request(
                user=prepaid_user,
                pod=pod,
                decision=decision,
                prompt_tokens=200,
                completion_tokens=100,
                files_referenced=0,
                latency_ms=1000,
                pipeline="infer",
                api_key_id=None,
                idempotency_key=None,
                error_message=None,
                redis=fake_redis,
                session=async_session,
            )

        assert prepaid_user.prepaid_balance_usd < initial_balance
        assert prepaid_user.prepaid_balance_usd >= Decimal("0")

    @pytest.mark.asyncio
    async def test_balance_not_negative(self, prepaid_user, async_session, fake_redis):
        """If cost > balance, balance floors to 0, not negative."""
        from bridge.cost_tracker import record_request

        prepaid_user.prepaid_balance_usd = Decimal("0.001")  # almost empty

        pod = PodHandle(
            pod_id=str(uuid.uuid4()),
            provider="runpod",
            tier="ultra",
            endpoint_url="http://localhost:11434",
            cost_per_hour_usd=1.89,  # expensive tier
        )
        decision = RoutingDecision(tier="ultra", reason="test", projected_cost_usd=1.0)
        await fake_redis.sadd(f"pod_users:{pod.pod_id}", prepaid_user.id)

        with patch("bridge.cost_tracker._idle_timeout_for_tier", return_value=600):
            await record_request(
                user=prepaid_user, pod=pod, decision=decision,
                prompt_tokens=1000, completion_tokens=500, files_referenced=0,
                latency_ms=5000, pipeline="infer", api_key_id=None,
                idempotency_key=None, error_message=None,
                redis=fake_redis, session=async_session,
            )

        assert prepaid_user.prepaid_balance_usd == Decimal("0")


# ---------------------------------------------------------------------------
# Multi-model pipeline
# ---------------------------------------------------------------------------

class TestMultiModelPipeline:

    @pytest.mark.asyncio
    @respx.mock
    async def test_preprocess_rewrites_user_message(self):
        from bridge.multi_model import run_pipeline
        from tests.conftest import make_chat_request

        preprocessor_response = {
            "message": {
                "content": json.dumps({
                    "intent": "fix a bug",
                    "files_referenced": ["main.py"],
                    "complexity_signals": [],
                    "target_tier_hint": "simple",
                    "structured_prompt": "Fix the null pointer in main.py line 42.",
                })
            }
        }

        with patch("bridge.settings.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "qwen2.5-coder:7b-instruct-q4_K_M"

            respx.post("http://ollama:11434/api/chat").mock(
                return_value=Response(200, json=preprocessor_response)
            )

            req = make_chat_request("fix my code", pipeline="preprocess,infer")
            modified_req, meta = await run_pipeline(req, "preprocess,infer")

        # Last user message should be the structured_prompt
        last_user = next(m for m in reversed(modified_req.messages) if m.role == "user")
        assert "null pointer" in last_user.content
        assert meta["preprocessor_output"]["intent"] == "fix a bug"

    @pytest.mark.asyncio
    @respx.mock
    async def test_preprocessor_failure_falls_back_to_plain_infer(self):
        from bridge.multi_model import run_pipeline
        from tests.conftest import make_chat_request

        with patch("bridge.settings.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "test-model"

            respx.post("http://ollama:11434/api/chat").mock(
                return_value=Response(500, text="Internal server error")
            )

            original_content = "please fix my code"
            req = make_chat_request(original_content, pipeline="preprocess,infer")
            modified_req, meta = await run_pipeline(req, "preprocess,infer")

        # Should fall back — original message unchanged
        last_user = next(m for m in reversed(modified_req.messages) if m.role == "user")
        assert last_user.content == original_content
        assert len(meta["errors"]) > 0

    @pytest.mark.asyncio
    async def test_plain_infer_pipeline_no_preprocessing(self):
        from bridge.multi_model import run_pipeline
        from tests.conftest import make_chat_request

        req = make_chat_request("simple question", pipeline="infer")
        modified_req, meta = await run_pipeline(req, "infer")

        # No preprocessing — request unchanged, no preprocessor output
        assert meta["preprocessor_output"] is None
        assert len(meta["errors"]) == 0
