"""Intelligent tier selection.

Priority order (first match wins):
  1. Explicit override — X-Tier header or ?tier= query param.
  2. Prompt tier hint — user typed @architecture, "use maximum tier", etc.
  3. Budget gate — downgrade or reject if projected cost > remaining.
  4. Context size — prompt_tokens thresholds from tiers.yaml.
  5. File count — files_referenced thresholds from tiers.yaml.
  6. Complexity keywords — detected in last user message.
  7. Default — 'simple'.

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

_POD_PROVIDERS = {"runpod", "vast", "lambda"}

# Matches: @architecture  |  tier: maximum  |  use simple tier  |  [ultra]
_TIER_HINT_RE = re.compile(
    r"(?:^|[\s,;])"
    r"(?:"
    r"@(simple|architecture|maximum|ultra)"
    r"|tier[:\s]+(simple|architecture|maximum|ultra)"
    r"|use\s+(simple|architecture|maximum|ultra)(?:\s+tier)?"
    r"|\[(simple|architecture|maximum|ultra)\]"
    r")",
    re.IGNORECASE,
)


def _pod_providers_active() -> bool:
    return bool(set(settings.provider_priority_list) & _POD_PROVIDERS)


def _prompt_tier_hint(text: str) -> str | None:
    m = _TIER_HINT_RE.search(text)
    if not m:
        return None
    return next(g for g in m.groups() if g is not None).lower()


def refresh_tier_prices(offers_by_provider: dict) -> None:
    """Update in-memory cost estimates from live provider GPU pricing.

    Called at startup so budget-gate projections use real market prices
    instead of the static values in tiers.yaml.
    """
    from providers.base import TIER_GPU_PREFERENCE, TIER_VRAM_REQUIRED

    for tier_name, tier_cfg in _TIERS.items():
        min_vram = TIER_VRAM_REQUIRED.get(tier_name, 8)
        prefs = TIER_GPU_PREFERENCE.get(tier_name, [])
        best_price: float | None = None

        for offers in offers_by_provider.values():
            candidates = [o for o in offers if o.available and o.vram_gb >= min_vram]
            for pref in prefs:
                match = next((o for o in candidates if pref.lower() in o.gpu.lower()), None)
                if match and (best_price is None or match.cost_per_hour_usd < best_price):
                    best_price = match.cost_per_hour_usd
                    break

        if best_price is not None:
            old = tier_cfg.get("cost_per_hour_usd")
            tier_cfg["cost_per_hour_usd"] = best_price
            if old != best_price:
                log.info("tier_price_synced", tier=tier_name, old=old, new=best_price)


def _load_tiers() -> dict:
    with open(settings.tiers_config_path) as f:
        return yaml.safe_load(f)["tiers"]


# Cache at module load; bridge restarts on config change.
_TIERS: dict = _load_tiers()


def _projected_cost(tier_name: str, prompt_tokens: int) -> float:
    if not _pod_providers_active():
        return 0.0
    tier = _TIERS[tier_name]
    idle_sec = tier.get("idle_timeout_sec", 300)
    avg_latency_sec = 5.0 + (prompt_tokens / 1000) * 1.5
    charged_sec = avg_latency_sec + idle_sec
    return (charged_sec / 3600) * tier["cost_per_hour_usd"]


def _prompt_tokens(request: ChatCompletionRequest) -> int:
    # Rough estimate: 4 chars ≈ 1 token across all messages
    total_chars = sum(len(m.text_content()) for m in request.messages)
    return max(1, total_chars // 4)


def _last_user_text(request: ChatCompletionRequest) -> str:
    for msg in reversed(request.messages):
        if msg.role == "user":
            return msg.text_content()
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


def _allowed_tiers(user: User) -> list[str] | None:
    """Effective tier whitelist = intersection of admin ceiling and user preference."""
    admin_ceiling = getattr(user, "allowed_tiers", None) or None
    preferred = getattr(user, "preferred_tiers", None) or None
    if admin_ceiling and preferred:
        merged = [t for t in preferred if t in admin_ceiling]
        return merged or admin_ceiling  # fallback to ceiling if preference empties the list
    return preferred or admin_ceiling


async def select_tier(
    request: ChatCompletionRequest,
    user: User,
    http_request: Request,
    monthly_spent_usd: Decimal,
) -> RoutingDecision:
    from fastapi import HTTPException, status

    model_field = request.model.removeprefix("llm-")
    prompt_tokens = _prompt_tokens(request)
    files = request.files_referenced or 0
    allowed = _allowed_tiers(user)

    # 0. Vision routing — image content always routes to vision tier before other signals
    if has_image_content(request.messages) and not model_supports_vision(model_field):
        vision_cfg = _TIERS.get("vision")
        if vision_cfg and (not allowed or "vision" in allowed):
            cost = _projected_cost("vision", prompt_tokens)
            log.info("tier_vision_routing", original_model=model_field)
            return RoutingDecision(tier="vision", reason="vision_content_detected", projected_cost_usd=cost)
        # Vision tier not configured or user lacks access — fall through to normal routing;
        # main.py strips images on pod acquisition failure per the vision tier's fallback setting.

    # 1. Explicit override
    override = (
        http_request.headers.get("x-tier")
        or http_request.query_params.get("tier")
        or (model_field if model_field in TIER_ORDER else None)
    )
    if override and override in TIER_ORDER:
        if allowed and override not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Tier '{override}' not in your allowed tiers: {allowed}",
            )
        tier = override
        cost = _projected_cost(tier, prompt_tokens)
        return RoutingDecision(tier=tier, reason=f"explicit override: {override}", projected_cost_usd=cost)

    # 2. Prompt tier hint — @architecture, "use maximum tier", [simple], tier:ultra
    hint = _prompt_tier_hint(_last_user_text(request))
    if hint and hint in TIER_ORDER:
        if allowed and hint not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Tier '{hint}' not in your allowed tiers: {allowed}",
            )
        cost = _projected_cost(hint, prompt_tokens)
        log.info("tier_from_prompt_hint", tier=hint)
        return RoutingDecision(tier=hint, reason="prompt tier hint", projected_cost_usd=cost)

    # 3. Signal-based selection
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

    # 4. Budget gate — downgrade if needed, respecting allowed_tiers whitelist
    budget = user.monthly_budget_usd
    remaining = budget - monthly_spent_usd

    candidates = [candidate] + TIER_ORDER[TIER_ORDER.index(candidate) - 1::-1]
    if allowed:
        candidates = [t for t in candidates if t in allowed]
        if not candidates:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"No allowed tier available for this request (allowed: {allowed})",
            )

    for t in candidates:
        cost = _projected_cost(t, prompt_tokens)
        if Decimal(str(cost)) <= remaining:
            downgraded = t != candidate
            if downgraded:
                log.warning(
                    "tier_downgraded",
                    user_id=user.id,
                    from_tier=candidate,
                    to_tier=t,
                    remaining_usd=float(remaining),
                    allowed_tiers=allowed,
                )
            return RoutingDecision(
                tier=t,
                reason=reason if not downgraded else f"downgrade from {candidate}; {reason}",
                projected_cost_usd=cost,
                downgraded_from=candidate if downgraded else None,
            )

    # Budget too low for any candidate
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=f"Insufficient budget (${float(remaining):.4f} remaining)",
    )


def get_tiers() -> dict:
    """Return the cached tier config dict (loaded from tiers.yaml at startup)."""
    return _TIERS


# ---------------------------------------------------------------------------
# Vision helpers
# ---------------------------------------------------------------------------

_VISION_MODELS = {"llava", "bakllava", "moondream", "llava-llama3", "minicpm-v",
                  "gpt-4o", "gpt-4-vision", "claude-3", "gemini",
                  "llama-3.2", "vision-instruct",
                  "qwen3-vl", "qwen2.5-vl", "qwen2-vl"}


def has_image_content(messages: list) -> bool:
    for msg in messages:
        content = msg.content if hasattr(msg, "content") else msg.get("content")
        if isinstance(content, list):
            for part in content:
                part_type = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
                if part_type == "image_url":
                    return True
    return False


def model_supports_vision(model_name: str) -> bool:
    return any(v in model_name.lower() for v in _VISION_MODELS)


def strip_images_from_messages(messages: list) -> list:
    cleaned = []
    for msg in messages:
        content = msg.content if hasattr(msg, "content") else msg.get("content")
        if isinstance(content, list):
            text_parts = [
                p for p in content
                if (p.get("type") if isinstance(p, dict) else getattr(p, "type", None)) == "text"
            ]
            text = " ".join(
                p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "")
                for p in text_parts
            ).strip()
            replacement = text or "[image removed — model does not support vision]"
            if hasattr(msg, "content"):
                msg.content = replacement
            else:
                msg = dict(msg)
                msg["content"] = replacement
        cleaned.append(msg)
    return cleaned
