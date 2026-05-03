"""Metric card and progress bar helpers for Streamlit dashboard."""

from __future__ import annotations

from decimal import Decimal

import streamlit as st


def metric_card(label: str, value: str, delta: str | None = None, color: str = "#60a5fa") -> None:
    """Render a styled metric card using st.metric."""
    st.metric(label=label, value=value, delta=delta)


def budget_progress(spent: float, budget: float) -> None:
    """Render budget progress bar with alert colors."""
    if budget <= 0:
        st.warning("No budget set")
        return

    pct = min(spent / budget, 1.0)
    remaining = max(budget - spent, 0.0)

    if pct >= 1.0:
        color = "🔴"
        label = "BUDGET EXCEEDED"
    elif pct >= 0.8:
        color = "🟠"
        label = "Budget critical"
    elif pct >= 0.5:
        color = "🟡"
        label = "Budget moderate"
    else:
        color = "🟢"
        label = "Budget healthy"

    st.progress(pct, text=f"{color} {label} — ${spent:.4f} / ${budget:.2f} ({pct*100:.1f}%)")
    st.caption(f"${remaining:.4f} remaining this month")


def status_badge(status: str) -> str:
    icons = {
        "ready":        "🟢",
        "starting":     "🔵",
        "provisioning": "🟣",
        "draining":     "🟡",
        "terminated":   "⚫",
        "failed":       "🔴",
        "ok":           "✅",
        "error":        "❌",
        "timeout":      "⏱️",
        "rejected_quota": "🚫",
        "rejected_budget": "💰",
        "active":       "🟢",
        "open":         "📄",
        "paid":         "✅",
        "void":         "🗑️",
        "overdue":      "⚠️",
    }
    return f"{icons.get(status, '❓')} {status}"


def tier_badge(tier: str) -> str:
    icons = {"simple": "⚡", "architecture": "🏗️", "maximum": "🚀", "ultra": "💎"}
    return f"{icons.get(tier, '❓')} {tier}"


def format_usd(v: float) -> str:
    if v >= 1:
        return f"${v:.4f}"
    return f"${v:.6f}"


def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def alert_box(msg: str, level: str = "warning") -> None:
    fn = {"info": st.info, "warning": st.warning, "error": st.error, "success": st.success}
    fn.get(level, st.info)(msg)
