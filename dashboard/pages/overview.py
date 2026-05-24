"""Overview page — high-level metrics for the current month."""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from dashboard.api_client import get_bridge_health
from dashboard.components.charts import cost_trend_chart, tier_pie_chart, usage_hours_bar
from dashboard.components.metrics import budget_progress, format_usd, format_tokens, tier_badge
from dashboard.provider_balances import get_provider_balances
from dashboard.db import (
    db_session,
    get_active_pods,
    get_api_provider_stats,
    get_daily_costs,
    get_monthly_spend,
    get_tier_distribution,
    get_today_usage_by_tier,
)


def render() -> None:
    st.title("📊 Overview")

    now = datetime.now(timezone.utc)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    with db_session() as session:
        monthly_spend = float(get_monthly_spend(session, user_id=None, period_start=period_start))
        daily = get_daily_costs(session, days=30)
        distribution = get_tier_distribution(session, days=30)
        active_pods = get_active_pods(session)
        today_tiers = get_today_usage_by_tier(session)
        is_admin = st.session_state.get("user_role") == "admin"
        api_stats = get_api_provider_stats(session)

    # --- Top metrics row ---
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Month Spend", format_usd(monthly_spend))
    with col2:
        active_count = len(active_pods)
        st.metric("Active Pods", active_count, delta=None)
    with col3:
        today_cost = sum(t["cost_usd"] for t in today_tiers)
        st.metric("Today's Cost", format_usd(today_cost))
    with col4:
        today_reqs = sum(t["requests"] for t in today_tiers)
        st.metric("Today's Requests", f"{today_reqs:,}")

    st.divider()

    # --- Provider info ---
    balances = get_provider_balances() if is_admin else []
    usage_by_name = {s["provider"].lower(): s for s in api_stats}
    balance_cards = [b for b in balances if b["provider"].lower().split()[0] not in usage_by_name]
    all_providers = balance_cards + [
        {"provider": s["provider"].title(), "_api_stat": s}
        for s in api_stats
    ]
    if all_providers:
        st.subheader("Providers")
        pcols = st.columns(max(len(all_providers), 1))
        for col, b in zip(pcols, all_providers):
            with col:
                if "_api_stat" in b:
                    s = b["_api_stat"]
                    col.metric(
                        s["provider"].title(),
                        format_usd(s["cost_month"]),
                        delta=f"{format_usd(s['cost_today'])} today · {s['requests_today']} reqs · {s['requests_month']} this month",
                    )
                elif b.get("error"):
                    col.metric(b["provider"], "⚠ Error", delta=b["error"][:60])
                elif b.get("note"):
                    col.metric(b["provider"], "✓ configured", delta=b["note"])
                else:
                    bal = b.get("balance")
                    val = format_usd(bal) if bal is not None else "✓ configured"
                    col.metric(b["provider"], val, delta="credit balance")
        st.divider()

    # --- Bridge health ---
    health = get_bridge_health()
    if health.get("status") == "ok":
        st.success(f"🟢 Bridge healthy — v{health.get('version', '?')}")
    else:
        st.error(f"🔴 Bridge unreachable: {health}")

    st.divider()

    # --- Cost trend chart ---
    st.plotly_chart(cost_trend_chart(daily), use_container_width=True)

    # --- Today by tier ---
    if today_tiers:
        st.subheader("Today's Usage by Tier")
        cols = st.columns(len(today_tiers))
        for col, t in zip(cols, today_tiers):
            with col:
                st.metric(
                    tier_badge(t["tier"]),
                    f"{t['requests']:,} reqs",
                    delta=format_usd(t["cost_usd"]),
                )

    st.divider()

    # --- Two-column charts ---
    left, right = st.columns(2)
    with left:
        st.plotly_chart(
            tier_pie_chart(distribution, value_col="requests"),
            use_container_width=True,
        )
    with right:
        st.plotly_chart(
            tier_pie_chart(distribution, value_col="cost_usd"),
            use_container_width=True,
        )

    st.plotly_chart(usage_hours_bar(daily), use_container_width=True)

    # --- Active instances ---
    if active_pods:
        st.subheader("Active Instances")
        for pod in active_pods:
            uptime = ""
            if pod["started_at"]:
                delta = now - pod["started_at"].replace(tzinfo=timezone.utc)
                uptime = f"up {int(delta.total_seconds()//3600)}h {int((delta.total_seconds()%3600)//60)}m"
            st.info(
                f"**{pod['tier']}** — {pod['provider']} — {pod['gpu'] or 'unknown GPU'} — "
                f"${pod['cost_per_hour_usd']:.2f}/hr — {pod['status']} — {uptime}"
            )
    else:
        st.info("No active pods. Pods spin up on first request to each tier.")
