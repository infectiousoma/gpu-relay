"""Intelligent tier selection.

Priority order (first match wins):
  1. Explicit override — X-Tier header or ?tier= query param.
  2. Budget gate — downgrade or reject if projected cost > remaining.
  3. Context size — prompt_tokens thresholds from tiers.yaml.
  4. File count — files_referenced thresholds from tiers.yaml.
  5. Complexity keywords — detected in last user message.
  6. Default — 'simple'.

All decisions are logged and returned as RoutingDecision for headers/billing.
"""

from __future__ import annotations

import re
from decimal import Decimal

import structlog
import yaml
from fastapi import Request

from bridge.schemas import ChatCompletionRequest, RoutingDecision
from bridge.settings import settings
from database.models import User

log = structlog.get_logger(__name__)

TIER_ORDER = ["simple", "architecture", "maximum", "ultra"]


def _load_tiers() -> dict:
    with open(settings.tiers_config_path) as f:
        return yaml.safe_load(f)["tiers"]


# Cache at module load; bridge restarts on config change.
_TIERS: dict = _load_tiers()


def _projected_cost(tier_name: str, prompt_tokens: int) -> float:
    tier = _TIERS[tier_name]
    idle_sec = tier.get("idle_timeout_sec", 300)
    avg_latency_sec = 5.0 + (prompt_tokens / 1000) * 1.5
    charged_sec = avg_latency_sec + idle_sec
    return (charged_sec / 3600) * tier["cost_per_hour_usd"]


def _prompt_tokens(request: ChatCompletionRequest) -> int:
    # Rough estimate: 4 chars ≈ 1 token across all messages
    total_chars = sum(len(m.content) for m in request.messages if m.content)
    return max(1, total_chars // 4)


def _last_user_text(request: ChatCompletionRequest) -> str:
    for msg in reversed(request.messages):
        if msg.role == "user":
            return msg.content or ""
    return ""


def _keyword_tier(text: str) -> str | None:
    """Return the minimum required tier based on keyword matches."""
    text_lower = text.lower()
    for tier_name in reversed(TIER_ORDER):  # check expensive tiers first
        tier_cfg = _TIERS.get(tier_name, {})
        keywords = tier_cfg.get("routing_signals", {}).get("keywords", [])
        if any(kw in text_lower for kw in keywords):
            return tier_name
    return None


def _min_tier_for_tokens(prompt_tokens: int) -> str:
    for tier_name in TIER_ORDER:
        max_tokens = _TIERS[tier_name].get("routing_signals", {}).get("max_prompt_tokens", 0)
        if prompt_tokens <= max_tokens:
            return tier_name
    return "ultra"


def _min_tier_for_files(file_count: int) -> str:
    for tier_name in TIER_ORDER:
        max_files = _TIERS[tier_name].get("routing_signals", {}).get("max_files", 0)
        if file_count <= max_files:
            return tier_name
    return "ultra"


def _highest(a: str, b: str) -> str:
    return a if TIER_ORDER.index(a) >= TIER_ORDER.index(b) else b


async def select_tier(
    request: ChatCompletionRequest,
    user: User,
    http_request: Request,
    monthly_spent_usd: Decimal,
) -> RoutingDecision:
    model_field = request.model.removeprefix("llm-")
    prompt_tokens = _prompt_tokens(request)
    files = request.files_referenced or 0

    # 1. Explicit override
    override = (
        http_request.headers.get("x-tier")
        or http_request.query_params.get("tier")
        or (model_field if model_field in TIER_ORDER else None)
    )
    if override and override in TIER_ORDER:
        tier = override
        cost = _projected_cost(tier, prompt_tokens)
        return RoutingDecision(tier=tier, reason=f"explicit override: {override}", projected_cost_usd=cost)

    # 2. Signal-based selection
    token_tier = _min_tier_for_tokens(prompt_tokens)
    file_tier = _min_tier_for_files(files)
    kw_tier = _keyword_tier(_last_user_text(request)) or "simple"

    candidate = _highest(_highest(token_tier, file_tier), kw_tier)
    reason_parts = []
    if token_tier != "simple":
        reason_parts.append(f"prompt_tokens={prompt_tokens}")
    if file_tier != "simple":
        reason_parts.append(f"files={files}")
    if kw_tier != "simple":
        reason_parts.append(f"keywords")

    reason = ", ".join(reason_parts) if reason_parts else "default"

    # 3. Budget gate — downgrade if needed
    budget = user.monthly_budget_usd
    remaining = budget - monthly_spent_usd

    for t in [candidate] + TIER_ORDER[TIER_ORDER.index(candidate) - 1::-1]:
        cost = _projected_cost(t, prompt_tokens)
        if Decimal(str(cost)) <= remaining:
            if t != candidate:
                log.warning(
                    "tier_downgraded_budget",
                    user_id=user.id,
                    from_tier=candidate,
                    to_tier=t,
                    remaining_usd=float(remaining),
                )
            return RoutingDecision(
                tier=t,
                reason=reason if t == candidate else f"budget downgrade from {candidate}; {reason}",
                projected_cost_usd=cost,
                downgraded_from=candidate if t != candidate else None,
            )

    # Budget too low for even simple
    from fastapi import HTTPException, status
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=f"Insufficient budget (${float(remaining):.4f} remaining)",
    )
