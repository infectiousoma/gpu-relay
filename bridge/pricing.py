"""Pricing engine: base GPU cost + per-user-type markups and discounts.

Usage:
    from bridge.pricing import calculate_price, effective_hourly_rate, PRICING_CONFIG

User types:
    personal  — no markup, no discount (cost-pass-through)
    family    — 15% markup; prepaid: 10% discount; higher tiers: higher markup
    business  — 30-40% markup; volume discounts at 100 h and 200 h/month
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

PRICING_CONFIG: dict[str, dict[str, dict]] = {
    "personal": {
        "simple": {
            "cost_per_hour_usd": 0.34,
            "markup": 1.0,
        },
        "architecture": {
            "cost_per_hour_usd": 0.34,
            "markup": 1.0,
        },
        "maximum": {
            "cost_per_hour_usd": 1.10,
            "markup": 1.0,
        },
        "ultra": {
            "cost_per_hour_usd": 1.89,
            "markup": 1.0,
        },
    },
    "family": {
        "simple": {
            "cost_per_hour_usd": 0.34,
            "markup": 1.15,
            "prepaid_discount": 0.90,
        },
        "architecture": {
            "cost_per_hour_usd": 0.34,
            "markup": 1.15,
            "prepaid_discount": 0.90,
        },
        "maximum": {
            "cost_per_hour_usd": 1.10,
            "markup": 1.20,
            "prepaid_discount": 0.85,
        },
        "ultra": {
            "cost_per_hour_usd": 1.89,
            "markup": 1.20,
            "prepaid_discount": 0.85,
        },
    },
    "business": {
        "simple": {
            "cost_per_hour_usd": 0.34,
            "markup": 1.30,
            "volume_discount_100hrs": 0.95,
            "volume_discount_200hrs": 0.90,
        },
        "architecture": {
            "cost_per_hour_usd": 0.34,
            "markup": 1.30,
            "volume_discount_100hrs": 0.95,
            "volume_discount_200hrs": 0.90,
        },
        "maximum": {
            "cost_per_hour_usd": 1.10,
            "markup": 1.40,
            "volume_discount_100hrs": 0.93,
            "volume_discount_200hrs": 0.87,
        },
        "ultra": {
            "cost_per_hour_usd": 1.89,
            "markup": 1.40,
            "volume_discount_100hrs": 0.93,
            "volume_discount_200hrs": 0.87,
        },
    },
}


def calculate_price(
    tier: str,
    duration_seconds: float,
    user_type: str = "personal",
    is_prepaid: bool = False,
    monthly_hours: float = 0.0,
) -> float:
    """Return the billed price in USD for one inference session.

    Args:
        tier: one of simple / architecture / maximum / ultra
        duration_seconds: wall-clock seconds charged (latency + idle window)
        user_type: personal | family | business
        is_prepaid: apply prepaid discount if available for this user_type/tier
        monthly_hours: cumulative GPU-hours used this month (for volume discounts)

    Returns:
        Rounded USD amount (4 decimal places).
    """
    if user_type not in PRICING_CONFIG:
        raise ValueError(f"Unknown user_type: {user_type!r}. Must be one of {list(PRICING_CONFIG)}")
    tier_cfg = PRICING_CONFIG[user_type].get(tier)
    if tier_cfg is None:
        raise ValueError(f"Unknown tier: {tier!r}")

    base_rate = Decimal(str(tier_cfg["cost_per_hour_usd"]))
    markup = Decimal(str(tier_cfg["markup"]))
    duration_hours = Decimal(str(duration_seconds)) / Decimal("3600")

    price = base_rate * markup * duration_hours

    # Prepaid discount
    if is_prepaid and "prepaid_discount" in tier_cfg:
        price *= Decimal(str(tier_cfg["prepaid_discount"]))

    # Volume discounts (business tier only; applied after prepaid discount)
    if user_type == "business":
        if monthly_hours >= 200 and "volume_discount_200hrs" in tier_cfg:
            price *= Decimal(str(tier_cfg["volume_discount_200hrs"]))
        elif monthly_hours >= 100 and "volume_discount_100hrs" in tier_cfg:
            price *= Decimal(str(tier_cfg["volume_discount_100hrs"]))

    return float(price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def effective_hourly_rate(
    tier: str,
    user_type: str = "personal",
    is_prepaid: bool = False,
    monthly_hours: float = 0.0,
) -> float:
    """Return the effective hourly rate in USD (one hour, for display)."""
    return calculate_price(tier, 3600, user_type=user_type, is_prepaid=is_prepaid, monthly_hours=monthly_hours)


def markup_summary(user_type: str) -> dict[str, float]:
    """Return {tier: effective_$/hr} for the given user type at zero monthly hours."""
    tiers = PRICING_CONFIG.get(user_type, {})
    return {tier: effective_hourly_rate(tier, user_type=user_type) for tier in tiers}
