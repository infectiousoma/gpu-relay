"""Shared pytest fixtures for all test suites."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bridge.settings import settings
from database.models import Base, BillingMode, Quota, TierName, User, UserRole

# ---------------------------------------------------------------------------
# Tiers config (in-memory, no disk I/O in unit tests)
# ---------------------------------------------------------------------------

TEST_TIERS = {
    "simple": {
        "gpu": "RTX_4090",
        "model": "qwen2.5-coder:7b-instruct-q4_K_M",
        "vram_gb": 4,
        "cost_per_hour_usd": 0.34,
        "idle_timeout_sec": 300,
        "max_context_tokens": 32768,
        "routing_signals": {
            "max_prompt_tokens": 8000,
            "max_files": 3,
            "keywords": [],
        },
    },
    "architecture": {
        "gpu": "RTX_4090",
        "model": "qwen2.5-coder:32b-instruct-q4_K_M",
        "vram_gb": 18,
        "cost_per_hour_usd": 0.34,
        "idle_timeout_sec": 600,
        "max_context_tokens": 32768,
        "routing_signals": {
            "max_prompt_tokens": 24000,
            "max_files": 20,
            "keywords": ["architecture", "design", "refactor", "review"],
        },
    },
    "maximum": {
        "gpu": "A100_40GB",
        "model": "deepseek-v3:latest-q4_K_M",
        "vram_gb": 36,
        "cost_per_hour_usd": 1.10,
        "idle_timeout_sec": 600,
        "max_context_tokens": 65536,
        "routing_signals": {
            "max_prompt_tokens": 60000,
            "max_files": 75,
            "keywords": ["audit", "analyze", "security", "system design"],
        },
    },
    "ultra": {
        "gpu": "A100_80GB",
        "model": "qwen2.5:72b-instruct-q4_K_M",
        "vram_gb": 48,
        "cost_per_hour_usd": 1.89,
        "idle_timeout_sec": 600,
        "max_context_tokens": 131072,
        "routing_signals": {
            "max_prompt_tokens": 130000,
            "max_files": 999,
            "keywords": ["mission-critical", "full audit"],
        },
    },
}


@pytest.fixture(autouse=True)
def patch_tiers():
    """Patch bridge.router._TIERS so tests don't read disk."""
    with patch("bridge.router._TIERS", TEST_TIERS):
        yield


# ---------------------------------------------------------------------------
# Async DB (SQLite in-memory)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def async_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def async_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Test users
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def test_user(async_session: AsyncSession) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email="test@example.com",
        password_hash="$2b$12$placeholder",
        role=UserRole.user,
        billing_mode=BillingMode.postpaid,
        monthly_budget_usd=Decimal("25.00"),
        prepaid_balance_usd=Decimal("0.00"),
        is_active=True,
        pipeline_default="infer",
        created_at=datetime.now(timezone.utc),
    )
    async_session.add(user)
    quota = Quota(
        user_id=user.id,
        requests_per_minute=60,
        tokens_per_day=1_000_000,
        usd_per_month=Decimal("25.00"),
        max_tier=TierName.ultra,
        updated_at=datetime.now(timezone.utc),
    )
    async_session.add(quota)
    await async_session.commit()
    return user


@pytest_asyncio.fixture
async def prepaid_user(async_session: AsyncSession) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email="prepaid@example.com",
        password_hash="$2b$12$placeholder",
        role=UserRole.user,
        billing_mode=BillingMode.prepaid,
        monthly_budget_usd=Decimal("50.00"),
        prepaid_balance_usd=Decimal("10.00"),
        is_active=True,
        pipeline_default="infer",
        created_at=datetime.now(timezone.utc),
    )
    async_session.add(user)
    async_session.add(Quota(
        user_id=user.id,
        requests_per_minute=60,
        tokens_per_day=1_000_000,
        usd_per_month=Decimal("50.00"),
        max_tier=TierName.ultra,
        updated_at=datetime.now(timezone.utc),
    ))
    await async_session.commit()
    return user


@pytest_asyncio.fixture
async def admin_user(async_session: AsyncSession) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email="admin@example.com",
        password_hash="$2b$12$placeholder",
        role=UserRole.admin,
        billing_mode=BillingMode.postpaid,
        monthly_budget_usd=Decimal("10000.00"),
        prepaid_balance_usd=Decimal("0.00"),
        is_active=True,
        pipeline_default="infer",
        created_at=datetime.now(timezone.utc),
    )
    async_session.add(user)
    async_session.add(Quota(
        user_id=user.id,
        requests_per_minute=1000,
        tokens_per_day=100_000_000,
        usd_per_month=Decimal("10000.00"),
        max_tier=TierName.ultra,
        updated_at=datetime.now(timezone.utc),
    ))
    await async_session.commit()
    return user


# ---------------------------------------------------------------------------
# Fake Redis
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def fake_redis() -> FakeRedis:
    redis = FakeRedis()
    yield redis
    await redis.aclose()


# ---------------------------------------------------------------------------
# Mock FastAPI Request
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_http_request():
    req = MagicMock()
    req.headers = {}
    req.query_params = {}
    req.state = MagicMock()
    req.state.auth_method = "api_key"
    return req


# ---------------------------------------------------------------------------
# Chat request factory
# ---------------------------------------------------------------------------

def make_chat_request(
    content: str = "Hello, fix this code.",
    model: str = "llm-auto",
    files_referenced: int = 0,
    pipeline: str | None = None,
) -> "ChatCompletionRequest":
    from bridge.schemas import ChatCompletionRequest, ChatMessage
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content=content)],
        files_referenced=files_referenced,
        pipeline=pipeline,
    )


@pytest.fixture
def simple_request():
    return make_chat_request("Fix this typo in my code.")


@pytest.fixture
def architecture_request():
    return make_chat_request("Please do a full architecture review of this design.")


@pytest.fixture
def large_context_request():
    # 9k+ tokens → should exceed simple threshold
    return make_chat_request("x " * 36000)  # ~9k tokens


# ---------------------------------------------------------------------------
# Mock pod handle
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_pod_handle():
    from bridge.schemas import PodHandle
    return PodHandle(
        pod_id=str(uuid.uuid4()),
        provider="runpod",
        tier="simple",
        endpoint_url="http://10.0.0.1:11434",
        cost_per_hour_usd=0.34,
        cold_start=False,
    )
