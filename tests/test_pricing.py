"""Unit tests for bridge/pricing.py."""

from __future__ import annotations

import pytest

from bridge.pricing import (
    PRICING_CONFIG,
    calculate_price,
    effective_hourly_rate,
    markup_summary,
)


class TestPricingConfig:
    def test_all_user_types_present(self):
        assert set(PRICING_CONFIG.keys()) == {"personal", "family", "business"}

    def test_all_tiers_present_per_user_type(self):
        expected_tiers = {"simple", "architecture", "maximum", "ultra"}
        for user_type, tiers in PRICING_CONFIG.items():
            assert set(tiers.keys()) == expected_tiers, f"{user_type} missing tiers"

    def test_personal_markup_is_1(self):
        for tier, cfg in PRICING_CONFIG["personal"].items():
            assert cfg["markup"] == 1.0, f"personal/{tier} should have no markup"

    def test_business_markup_greater_than_family(self):
        for tier in ("simple", "architecture", "maximum", "ultra"):
            family_markup = PRICING_CONFIG["family"][tier]["markup"]
            business_markup = PRICING_CONFIG["business"][tier]["markup"]
            assert business_markup > family_markup

    def test_volume_discounts_only_in_business(self):
        for tier in ("simple", "architecture"):
            assert "volume_discount_100hrs" in PRICING_CONFIG["business"][tier]
            assert "volume_discount_100hrs" not in PRICING_CONFIG["personal"][tier]
            assert "volume_discount_100hrs" not in PRICING_CONFIG["family"][tier]


class TestCalculatePrice:
    def test_personal_no_markup(self):
        # 1 hour simple at personal = 0.34 exactly
        price = calculate_price("simple", 3600, user_type="personal")
        assert abs(price - 0.34) < 0.0001

    def test_family_markup_applied(self):
        personal = calculate_price("simple", 3600, user_type="personal")
        family = calculate_price("simple", 3600, user_type="family")
        assert family > personal
        assert abs(family - personal * 1.15) < 0.001

    def test_business_markup_applied(self):
        personal = calculate_price("simple", 3600, user_type="personal")
        business = calculate_price("simple", 3600, user_type="business")
        assert abs(business - personal * 1.30) < 0.001

    def test_prepaid_discount_family(self):
        full = calculate_price("simple", 3600, user_type="family", is_prepaid=False)
        discounted = calculate_price("simple", 3600, user_type="family", is_prepaid=True)
        # 10% discount → discounted = full * 0.90
        assert discounted < full
        assert abs(discounted - full * 0.90) < 0.001

    def test_prepaid_discount_personal_no_effect(self):
        # personal has no prepaid_discount key → no change
        full = calculate_price("simple", 3600, user_type="personal", is_prepaid=False)
        discounted = calculate_price("simple", 3600, user_type="personal", is_prepaid=True)
        assert full == discounted

    def test_volume_discount_100hrs_business(self):
        no_vol = calculate_price("simple", 3600, user_type="business", monthly_hours=50)
        with_vol = calculate_price("simple", 3600, user_type="business", monthly_hours=100)
        assert with_vol < no_vol
        assert abs(with_vol / no_vol - 0.95) < 0.001

    def test_volume_discount_200hrs_beats_100hrs(self):
        vol100 = calculate_price("simple", 3600, user_type="business", monthly_hours=100)
        vol200 = calculate_price("simple", 3600, user_type="business", monthly_hours=200)
        assert vol200 < vol100

    def test_zero_duration_is_zero(self):
        assert calculate_price("simple", 0, user_type="business") == 0.0

    def test_ultra_costs_more_than_simple_all_user_types(self):
        for user_type in ("personal", "family", "business"):
            simple = calculate_price("simple", 3600, user_type=user_type)
            ultra = calculate_price("ultra", 3600, user_type=user_type)
            assert ultra > simple

    def test_short_duration(self):
        # 60 seconds of simple personal = 0.34 / 60 ≈ 0.0057
        price = calculate_price("simple", 60, user_type="personal")
        assert abs(price - 0.34 / 60) < 0.0001

    def test_unknown_user_type_raises(self):
        with pytest.raises(ValueError, match="Unknown user_type"):
            calculate_price("simple", 3600, user_type="enterprise")

    def test_unknown_tier_raises(self):
        with pytest.raises(ValueError, match="Unknown tier"):
            calculate_price("mega", 3600, user_type="personal")

    def test_result_rounded_to_4_decimal_places(self):
        price = calculate_price("simple", 7200, user_type="family")
        # Check it has at most 4 decimal places
        assert price == round(price, 4)


class TestEffectiveHourlyRate:
    def test_personal_simple(self):
        assert abs(effective_hourly_rate("simple", user_type="personal") - 0.34) < 0.0001

    def test_business_with_volume_discount(self):
        base = effective_hourly_rate("simple", user_type="business", monthly_hours=0)
        discounted = effective_hourly_rate("simple", user_type="business", monthly_hours=200)
        assert discounted < base


class TestMarkupSummary:
    def test_returns_all_tiers(self):
        summary = markup_summary("personal")
        assert set(summary.keys()) == {"simple", "architecture", "maximum", "ultra"}

    def test_values_are_floats(self):
        summary = markup_summary("business")
        for v in summary.values():
            assert isinstance(v, float)

    def test_business_rates_higher_than_personal(self):
        personal = markup_summary("personal")
        business = markup_summary("business")
        for tier in personal:
            assert business[tier] > personal[tier]
