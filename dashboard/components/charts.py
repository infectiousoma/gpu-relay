"""Reusable Plotly chart builders for the dashboard.

All functions return plotly.graph_objects.Figure ready for st.plotly_chart().
Config: responsive=True, margin tight, dark-friendly color palette.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

TIER_COLORS = {
    "simple":       "#4ade80",   # green
    "architecture": "#60a5fa",   # blue
    "maximum":      "#f59e0b",   # amber
    "ultra":        "#f43f5e",   # rose
}
STATUS_COLORS = {
    "ready":        "#4ade80",
    "starting":     "#60a5fa",
    "provisioning": "#a78bfa",
    "draining":     "#f59e0b",
    "terminated":   "#6b7280",
    "failed":       "#f43f5e",
}

_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#e5e7eb"),
    margin=dict(l=40, r=20, t=40, b=40),
    legend=dict(bgcolor="rgba(0,0,0,0)"),
)


def cost_trend_chart(daily: list[dict]) -> go.Figure:
    """Stacked area chart: daily cost by tier over last N days."""
    if not daily:
        return _empty("No cost data")

    df = pd.DataFrame(daily)
    df["date"] = pd.to_datetime(df["date"])

    fig = go.Figure()
    for tier in ["simple", "architecture", "maximum", "ultra"]:
        tier_df = df[df["tier"] == tier].sort_values("date")
        if tier_df.empty:
            continue
        fig.add_trace(go.Scatter(
            x=tier_df["date"],
            y=tier_df["cost_usd"],
            name=tier,
            mode="lines",
            stackgroup="cost",
            line=dict(width=0.5),
            fillcolor=TIER_COLORS.get(tier, "#94a3b8"),
            line_color=TIER_COLORS.get(tier, "#94a3b8"),
            hovertemplate="%{x|%Y-%m-%d}<br>$%{y:.4f}<extra>" + tier + "</extra>",
        ))

    fig.update_layout(
        title="Cost Trend (last 30 days)",
        xaxis_title=None,
        yaxis_title="USD",
        hovermode="x unified",
        **_LAYOUT,
    )
    return fig


def tier_pie_chart(distribution: list[dict], value_col: str = "requests") -> go.Figure:
    """Pie chart: tier distribution by requests or cost."""
    if not distribution:
        return _empty("No data")

    df = pd.DataFrame(distribution)
    label = "Requests" if value_col == "requests" else "Cost (USD)"

    fig = go.Figure(go.Pie(
        labels=df["tier"],
        values=df[value_col],
        marker=dict(colors=[TIER_COLORS.get(t, "#94a3b8") for t in df["tier"]]),
        textinfo="label+percent",
        hovertemplate="%{label}<br>" + label + ": %{value:.4f}<extra></extra>" if value_col == "cost_usd"
                      else "%{label}<br>" + label + ": %{value:,}<extra></extra>",
    ))
    fig.update_layout(
        title=f"Tier Distribution by {label}",
        showlegend=True,
        **_LAYOUT,
    )
    return fig


def usage_hours_bar(daily: list[dict]) -> go.Figure:
    """Grouped bar: request counts per day, stacked by tier."""
    if not daily:
        return _empty("No usage data")

    df = pd.DataFrame(daily)
    df["date"] = pd.to_datetime(df["date"])

    fig = go.Figure()
    for tier in ["simple", "architecture", "maximum", "ultra"]:
        tier_df = df[df["tier"] == tier].sort_values("date")
        if tier_df.empty:
            continue
        fig.add_trace(go.Bar(
            x=tier_df["date"],
            y=tier_df["requests"],
            name=tier,
            marker_color=TIER_COLORS.get(tier, "#94a3b8"),
            hovertemplate="%{x|%Y-%m-%d}<br>%{y:,} requests<extra>" + tier + "</extra>",
        ))

    fig.update_layout(
        title="Daily Request Count by Tier",
        barmode="stack",
        xaxis_title=None,
        yaxis_title="Requests",
        **_LAYOUT,
    )
    return fig


def avg_cost_per_request(daily: list[dict]) -> go.Figure:
    """Line chart: average cost per request per day."""
    if not daily:
        return _empty("No data")

    df = pd.DataFrame(daily)
    df["date"] = pd.to_datetime(df["date"])
    agg = df.groupby("date").agg({"cost_usd": "sum", "requests": "sum"}).reset_index()
    agg["avg_cost"] = agg["cost_usd"] / agg["requests"].replace(0, 1)

    fig = go.Figure(go.Scatter(
        x=agg["date"],
        y=agg["avg_cost"],
        mode="lines+markers",
        line=dict(color="#60a5fa", width=2),
        marker=dict(size=4),
        hovertemplate="%{x|%Y-%m-%d}<br>Avg: $%{y:.5f}<extra></extra>",
    ))
    fig.update_layout(
        title="Average Cost per Request",
        xaxis_title=None,
        yaxis_title="USD / request",
        **_LAYOUT,
    )
    return fig


def latency_histogram(requests: list[dict]) -> go.Figure:
    """Histogram of request latencies split by tier."""
    if not requests:
        return _empty("No request data")

    df = pd.DataFrame(requests)
    df["latency_s"] = df["latency_ms"] / 1000

    fig = go.Figure()
    for tier in ["simple", "architecture", "maximum", "ultra"]:
        t = df[df["tier"] == tier]
        if t.empty:
            continue
        fig.add_trace(go.Histogram(
            x=t["latency_s"],
            name=tier,
            nbinsx=30,
            opacity=0.75,
            marker_color=TIER_COLORS.get(tier, "#94a3b8"),
            hovertemplate="Latency: %{x:.1f}s<br>Count: %{y}<extra>" + tier + "</extra>",
        ))

    fig.update_layout(
        title="Request Latency Distribution",
        barmode="overlay",
        xaxis_title="Seconds",
        yaxis_title="Count",
        **_LAYOUT,
    )
    return fig


def pod_status_gauge(active: int, total_today: int) -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="number+delta",
        value=active,
        delta={"reference": 0, "valueformat": "+d"},
        title={"text": "Active Pods"},
        number={"font": {"color": "#4ade80" if active > 0 else "#6b7280"}},
    ))
    fig.update_layout(height=150, **_LAYOUT)
    return fig


def hourly_cost_ticker(requests: list[dict]) -> go.Figure:
    """Bar chart: cost per hour over the last 24h."""
    if not requests:
        return _empty("No recent data")

    df = pd.DataFrame(requests)
    df["hour"] = pd.to_datetime(df["created_at"]).dt.floor("h")
    agg = df.groupby("hour")["cost_usd"].sum().reset_index()

    fig = go.Figure(go.Bar(
        x=agg["hour"],
        y=agg["cost_usd"],
        marker_color="#60a5fa",
        hovertemplate="%{x|%H:%M}<br>$%{y:.4f}<extra></extra>",
    ))
    fig.update_layout(
        title="Cost per Hour (last 24h)",
        xaxis_title=None,
        yaxis_title="USD",
        **_LAYOUT,
    )
    return fig


def user_spend_bar(users: list[dict]) -> go.Figure:
    """Horizontal bar: per-user month spend vs budget."""
    if not users:
        return _empty("No user data")

    df = pd.DataFrame(users).sort_values("monthly_spend_usd", ascending=True).tail(20)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=df["email"],
        x=df["monthly_budget_usd"],
        name="Budget",
        orientation="h",
        marker_color="rgba(100,116,139,0.3)",
        hovertemplate="%{y}<br>Budget: $%{x:.2f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        y=df["email"],
        x=df["monthly_spend_usd"],
        name="Spent",
        orientation="h",
        marker_color="#60a5fa",
        hovertemplate="%{y}<br>Spent: $%{x:.4f}<extra></extra>",
    ))
    fig.update_layout(
        title="User Spend vs Budget (current month, top 20)",
        barmode="overlay",
        xaxis_title="USD",
        yaxis_title=None,
        height=max(300, len(df) * 28),
        **_LAYOUT,
    )
    return fig


def _empty(msg: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=msg, xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
                       font=dict(size=14, color="#6b7280"))
    fig.update_layout(**_LAYOUT)
    return fig
