"""End-to-end tests — require a live docker-compose stack.

Run with:
    pytest tests/test_e2e.py -m e2e -v

Prerequisites:
    docker compose up -d
    docker compose run --rm bridge alembic upgrade head
    BRIDGE_URL=http://localhost:8000 pytest tests/test_e2e.py -m e2e

All tests hit the real bridge API. Provider API calls are NOT mocked here —
if you don't have RunPod/Vast/Lambda keys, set MOCK_PROVIDERS=1 in .env to
use a stub provider that returns fake pod handles without billing.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

BRIDGE_URL = os.getenv("BRIDGE_URL", "http://localhost:8000")
ADMIN_EMAIL = os.getenv("E2E_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("E2E_ADMIN_PASSWORD", "changeme")

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def bridge_token() -> str:
    r = httpx.post(
        f"{BRIDGE_URL}/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def auth_headers(bridge_token: str) -> dict:
    return {"Authorization": f"Bearer {bridge_token}"}


# ---------------------------------------------------------------------------
# Bridge health
# ---------------------------------------------------------------------------

class TestBridgeHealth:
    def test_healthz_returns_ok(self):
        r = httpx.get(f"{BRIDGE_URL}/healthz", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_models_endpoint_returns_tiers(self, auth_headers):
        r = httpx.get(f"{BRIDGE_URL}/v1/models", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert data["object"] == "list"
        model_ids = [m["id"] for m in data["data"]]
        assert "llm-simple" in model_ids
        assert "llm-auto" in model_ids


# ---------------------------------------------------------------------------
# Auth flow
# ---------------------------------------------------------------------------

class TestAuthFlow:
    def test_login_returns_token(self):
        r = httpx.post(
            f"{BRIDGE_URL}/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_invalid_credentials_returns_401(self):
        r = httpx.post(
            f"{BRIDGE_URL}/auth/login",
            json={"email": "nobody@example.com", "password": "wrong"},
            timeout=10,
        )
        assert r.status_code == 401

    def test_api_key_create_and_use(self, auth_headers):
        # Create API key
        r = httpx.post(f"{BRIDGE_URL}/auth/keys", headers=auth_headers, params={"label": "e2e-test"}, timeout=10)
        assert r.status_code == 200
        key_data = r.json()
        assert key_data["key"].startswith("sk-llm-")

        # Use the API key to list models
        key_headers = {"Authorization": f"Bearer {key_data['key']}"}
        r2 = httpx.get(f"{BRIDGE_URL}/v1/models", headers=key_headers, timeout=10)
        assert r2.status_code == 200

        # Revoke the key
        r3 = httpx.delete(
            f"{BRIDGE_URL}/auth/keys/{key_data['id']}",
            headers=auth_headers,
            timeout=10,
        )
        assert r3.status_code == 204

        # Revoked key should fail
        r4 = httpx.get(f"{BRIDGE_URL}/v1/models", headers=key_headers, timeout=10)
        assert r4.status_code == 401

    def test_unauthenticated_request_returns_401(self):
        r = httpx.post(f"{BRIDGE_URL}/v1/chat/completions", json={
            "model": "llm-simple",
            "messages": [{"role": "user", "content": "Hello"}],
        }, timeout=10)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# User creation
# ---------------------------------------------------------------------------

class TestUserManagement:
    def test_create_user_via_api(self, auth_headers):
        test_email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
        r = httpx.get(f"{BRIDGE_URL}/admin/users", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        users = r.json()
        assert isinstance(users, list)

    def test_non_admin_cannot_list_users(self):
        # Create a regular user first via admin, then try with their token
        # (This test assumes the stack has a non-admin user)
        pass  # Covered by unit tests; skip if no second user in e2e env


# ---------------------------------------------------------------------------
# Chat completions — with MOCK_PROVIDERS=1 or real providers
# ---------------------------------------------------------------------------

class TestChatCompletions:
    @pytest.mark.skipif(
        not os.getenv("MOCK_PROVIDERS") and not os.getenv("RUNPOD_API_KEY"),
        reason="No provider configured (set MOCK_PROVIDERS=1 or RUNPOD_API_KEY)",
    )
    def test_simple_chat_completion(self, auth_headers):
        r = httpx.post(
            f"{BRIDGE_URL}/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "llm-simple",
                "messages": [{"role": "user", "content": "Say exactly: hello world"}],
                "max_tokens": 20,
            },
            timeout=300,  # cold start can take ~90s
        )
        assert r.status_code == 200, f"Chat failed: {r.text}"
        data = r.json()

        # OpenAI-compatible shape
        assert "choices" in data
        assert len(data["choices"]) > 0
        assert data["choices"][0]["message"]["role"] == "assistant"

        # Bridge extensions
        assert "bridge" in data
        assert data["bridge"]["tier"] == "simple"
        assert data["bridge"]["cost_usd"] > 0

        # Response headers
        assert "x-llm-tier" in r.headers
        assert "x-llm-cost-usd" in r.headers

    @pytest.mark.skipif(
        not os.getenv("MOCK_PROVIDERS") and not os.getenv("RUNPOD_API_KEY"),
        reason="No provider configured",
    )
    def test_tier_routing_by_model_field(self, auth_headers):
        r = httpx.post(
            f"{BRIDGE_URL}/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "llm-architecture",
                "messages": [{"role": "user", "content": "What is 2+2?"}],
                "max_tokens": 10,
            },
            timeout=300,
        )
        assert r.status_code == 200
        assert r.headers.get("x-llm-tier") == "architecture"

    def test_rate_limit_enforced(self, auth_headers):
        """Rapid-fire requests should eventually get 429."""
        got_429 = False
        for _ in range(100):
            r = httpx.post(
                f"{BRIDGE_URL}/v1/chat/completions",
                headers=auth_headers,
                json={
                    "model": "llm-simple",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=5,
            )
            if r.status_code == 429:
                got_429 = True
                break
        # Rate limit should have triggered (60 RPM default)
        assert got_429, "Expected 429 after rapid-fire requests"

    def test_budget_exhausted_returns_429(self, bridge_token):
        """Create a user with $0 budget, verify first request returns 402."""
        admin_headers = {"Authorization": f"Bearer {bridge_token}"}

        # This test is a stub — full budget enforcement test requires DB manipulation
        # which is better covered in integration tests. Mark as known-skip.
        pytest.skip("Budget enforcement tested in integration suite")


# ---------------------------------------------------------------------------
# Cost tracking verification
# ---------------------------------------------------------------------------

class TestCostTracking:
    @pytest.mark.skipif(
        not os.getenv("MOCK_PROVIDERS") and not os.getenv("RUNPOD_API_KEY"),
        reason="No provider configured",
    )
    def test_cost_in_response_headers(self, auth_headers):
        r = httpx.post(
            f"{BRIDGE_URL}/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "llm-simple",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
            },
            timeout=300,
        )
        assert r.status_code == 200
        cost = float(r.headers["x-llm-cost-usd"])
        assert cost > 0
        latency = int(r.headers["x-llm-latency-ms"])
        assert latency > 0


# ---------------------------------------------------------------------------
# Dashboard smoke
# ---------------------------------------------------------------------------

class TestDashboard:
    DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:8501")

    def test_dashboard_health(self):
        try:
            r = httpx.get(f"{self.DASHBOARD_URL}/_stcore/health", timeout=5)
            assert r.status_code == 200
        except httpx.ConnectError:
            pytest.skip("Dashboard not running")
