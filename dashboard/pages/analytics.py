"""Usage analytics page — filterable charts and CSV export."""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from dashboard.components.charts import (
    avg_cost_per_request,
    cost_trend_chart,
    latency_histogram,
    tier_pie_chart,
    usage_hours_bar,
)
from dashboard.components.metrics import format_usd, format_tokens, tier_badge
from dashboard.db import (
    db_session,
    get_all_users_with_stats,
    get_daily_costs,
    get_requests_raw,
    get_tier_distribution,
)


def render() -> None:
    st.title("📈 Usage Analytics")

    # --- Filters sidebar ---
    with st.sidebar:
        st.header("Filters")
        days = st.selectbox("Period", [7, 14, 30, 60, 90], index=2, format_func=lambda d: f"Last {d} days")

        with db_session() as session:
            users = get_all_users_with_stats(session)

        user_options = {"All users": None} | {u["email"]: u["id"] for u in users}
        selected_user_label = st.selectbox("User", list(user_options.keys()))
        selected_user_id = user_options[selected_user_label]

        tier_options = ["All tiers", "simple", "architecture", "maximum", "ultra", "vision"]
        selected_tier_label = st.selectbox("Tier", tier_options)
        selected_tier = None if selected_tier_label == "All tiers" else selected_tier_label

        provider_options = ["All providers", "runpod", "vast", "lambda", "local", "together", "groq", "openai", "mistral", "deepseek"]
        selected_provider_label = st.selectbox("Provider", provider_options)
        selected_provider = None if selected_provider_label == "All providers" else selected_provider_label

        group_by = st.radio("Group by", ["day", "week", "month"], horizontal=True)

    # --- Fetch data ---
    with db_session() as session:
        daily = get_daily_costs(session, days=days, user_id=selected_user_id)
        distribution = get_tier_distribution(session, days=days, user_id=selected_user_id)
        requests = get_requests_raw(session, days=days, user_id=selected_user_id, tier=selected_tier, provider=selected_provider, limit=50_000)

    df = pd.DataFrame(requests) if requests else pd.DataFrame()

    # --- Summary row ---
    st.subheader("Summary")
    if not df.empty:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Requests", f"{len(df):,}")
        col2.metric("Total Cost", format_usd(df["cost_usd"].sum()))
        col3.metric("Total Tokens", format_tokens(int((df["prompt_tokens"] + df["completion_tokens"]).sum())))
        col4.metric("Avg Latency", f"{df['latency_ms'].mean()/1000:.1f}s")
    else:
        st.info("No data for selected filters.")
        return

    st.divider()

    # --- Charts ---
    tab1, tab2, tab3, tab4 = st.tabs(["Cost Trend", "Request Volume", "Distribution", "Latency"])

    with tab1:
        if group_by != "day":
            # Regroup daily data
            ddf = pd.DataFrame(daily) if daily else pd.DataFrame()
            if not ddf.empty:
                ddf["date"] = pd.to_datetime(ddf["date"])
                freq = "W" if group_by == "week" else "ME"
                ddf = ddf.groupby([pd.Grouper(key="date", freq=freq), "tier"]).agg(
                    {"cost_usd": "sum", "requests": "sum"}
                ).reset_index()
                ddf["date"] = ddf["date"].dt.strftime("%Y-%m-%d")
                grouped_daily = ddf.to_dict("records")
            else:
                grouped_daily = []
            st.plotly_chart(cost_trend_chart(grouped_daily), use_container_width=True, key="cost_trend_grouped")
        else:
            st.plotly_chart(cost_trend_chart(daily), use_container_width=True, key="cost_trend_daily")

        st.plotly_chart(avg_cost_per_request(daily), use_container_width=True, key="avg_cost_per_request")

    with tab2:
        st.plotly_chart(usage_hours_bar(daily), use_container_width=True, key="usage_hours_bar")

        # Request timeline with pandas resample
        tdf = df.copy()
        tdf["created_at"] = pd.to_datetime(tdf["created_at"])
        tdf = tdf.set_index("created_at")
        freq = {"day": "D", "week": "W", "month": "ME"}[group_by]
        timeline = tdf["id"].resample(freq).count().reset_index()
        timeline.columns = ["date", "requests"]

        import plotly.express as px
        fig = px.bar(timeline, x="date", y="requests", title="Request Volume Over Time",
                     color_discrete_sequence=["#60a5fa"])
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          font=dict(color="#e5e7eb"), margin=dict(l=40, r=20, t=40, b=40))
        st.plotly_chart(fig, use_container_width=True, key="request_volume_timeline")

    with tab3:
        left, right = st.columns(2)
        with left:
            st.plotly_chart(tier_pie_chart(distribution, "requests"), use_container_width=True, key="tier_pie_requests")
        with right:
            st.plotly_chart(tier_pie_chart(distribution, "cost_usd"), use_container_width=True, key="tier_pie_cost")

        # Tier stats table
        if distribution:
            dist_df = pd.DataFrame(distribution)
            dist_df["cost_usd"] = dist_df["cost_usd"].apply(lambda x: f"${x:.4f}")
            dist_df["requests"] = dist_df["requests"].apply(lambda x: f"{x:,}")
            dist_df.columns = ["Tier", "Requests", "Cost USD", "Total Tokens"]
            st.dataframe(dist_df, use_container_width=True, hide_index=True)

    with tab4:
        st.plotly_chart(latency_histogram(requests), use_container_width=True, key="latency_histogram")

        # P50/P95/P99
        if not df.empty:
            p50 = df["latency_ms"].quantile(0.50) / 1000
            p95 = df["latency_ms"].quantile(0.95) / 1000
            p99 = df["latency_ms"].quantile(0.99) / 1000
            c1, c2, c3 = st.columns(3)
            c1.metric("P50 Latency", f"{p50:.2f}s")
            c2.metric("P95 Latency", f"{p95:.2f}s")
            c3.metric("P99 Latency", f"{p99:.2f}s")

    st.divider()

    # --- Raw data + CSV export ---
    st.subheader("Raw Request Data")
    if not df.empty:
        display_cols = ["created_at", "email", "provider", "tier", "model", "prompt_tokens",
                        "completion_tokens", "latency_ms", "cost_usd", "status", "routing_reason"]
        display_df = df[[c for c in display_cols if c in df.columns]].copy()
        display_df["cost_usd"] = display_df["cost_usd"].apply(lambda x: f"${x:.6f}")

        st.dataframe(display_df, use_container_width=True, height=300)

        csv_buf = io.StringIO()
        df.to_csv(csv_buf, index=False)
        st.download_button(
            label="⬇️ Export CSV",
            data=csv_buf.getvalue(),
            file_name=f"llm_requests_{days}d.csv",
            mime="text/csv",
        )
