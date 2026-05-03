"""Real-time monitoring page — live pods, in-flight requests, cost ticker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import streamlit as st

from dashboard.api_client import get_bridge_health, kill_pod
from dashboard.components.charts import hourly_cost_ticker, pod_status_gauge
from dashboard.components.metrics import format_usd, status_badge, tier_badge
from dashboard.db import db_session, get_active_pods, get_all_pods, get_requests_raw


_REFRESH_INTERVALS = {"5s": 5, "15s": 15, "30s": 30, "1m": 60, "Off": 0}


def render() -> None:
    st.title("🖥️ Real-Time Monitoring")

    # --- Auto-refresh control ---
    col1, col2 = st.columns([3, 1])
    with col2:
        refresh_label = st.selectbox("Auto-refresh", list(_REFRESH_INTERVALS.keys()), index=1)
        refresh_sec = _REFRESH_INTERVALS[refresh_label]
    with col1:
        if st.button("🔄 Refresh now"):
            st.rerun()

    if refresh_sec > 0:
        st.caption(f"Auto-refreshing every {refresh_label}")

    # --- Bridge health ---
    health = get_bridge_health()
    status_txt = health.get("status", "unknown")
    if status_txt == "ok":
        st.success(f"🟢 Bridge online — v{health.get('version', '?')}")
    else:
        st.error(f"🔴 Bridge: {health}")

    st.divider()

    # --- Active pods ---
    with db_session() as session:
        active_pods = get_active_pods(session)
        recent_requests = get_requests_raw(session, days=1, limit=5000)

    now = datetime.now(timezone.utc)

    st.subheader(f"Active Pods ({len(active_pods)})")

    if active_pods:
        for pod in active_pods:
            _render_pod_card(pod, now)
    else:
        st.info("No active pods. Pods spin up automatically on first request.")

    st.divider()

    # --- Cost ticker ---
    st.subheader("Hourly Cost (last 24h)")
    today_cost = sum(r["cost_usd"] for r in recent_requests)
    this_hour = sum(
        r["cost_usd"] for r in recent_requests
        if r["created_at"] and r["created_at"].replace(tzinfo=timezone.utc) >= now - timedelta(hours=1)
    )
    c1, c2, c3 = st.columns(3)
    c1.metric("Today total", format_usd(today_cost))
    c2.metric("Last hour", format_usd(this_hour))
    c3.metric("Requests (24h)", f"{len(recent_requests):,}")

    st.plotly_chart(hourly_cost_ticker(recent_requests), use_container_width=True)

    st.divider()

    # --- Recent requests in-flight / last 50 ---
    st.subheader("Recent Requests (last 50)")
    last50 = recent_requests[:50]
    if last50:
        for r in last50:
            ts = r["created_at"].strftime("%H:%M:%S") if r["created_at"] else "?"
            status_icon = "✅" if r["status"] == "ok" else "❌"
            st.text(
                f"{ts} {status_icon} {tier_badge(r['tier'])} "
                f"{'@' + r['email'][:20]:<22} "
                f"{r['prompt_tokens']:>6}+{r['completion_tokens']:<6} tok  "
                f"{r['latency_ms']/1000:>5.1f}s  "
                f"{format_usd(r['cost_usd'])}"
            )
    else:
        st.info("No requests in the last 24h.")

    st.divider()

    # --- All pods history ---
    st.subheader("Pod History (last 200)")
    with db_session() as session:
        all_pods = get_all_pods(session, limit=200)

    if all_pods:
        import pandas as pd
        df = pd.DataFrame(all_pods)
        df["started_at"] = df["started_at"].apply(lambda x: x.strftime("%Y-%m-%d %H:%M") if x else "")
        df["terminated_at"] = df["terminated_at"].apply(lambda x: x.strftime("%Y-%m-%d %H:%M") if x else "")
        df["cost_per_hour_usd"] = df["cost_per_hour_usd"].apply(lambda x: f"${x:.2f}")
        st.dataframe(df, use_container_width=True, height=300)

    # --- Auto-refresh via st.rerun after sleep ---
    if refresh_sec > 0:
        import time
        time.sleep(refresh_sec)
        st.rerun()


def _render_pod_card(pod: dict, now: datetime) -> None:
    uptime_str = "—"
    if pod["started_at"]:
        started = pod["started_at"]
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        delta = now - started
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m = rem // 60
        uptime_str = f"{h}h {m}m"

    last_used_str = "never"
    if pod["last_used_at"]:
        lu = pod["last_used_at"]
        if lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
        idle_sec = int((now - lu).total_seconds())
        if idle_sec < 60:
            last_used_str = f"{idle_sec}s ago"
        elif idle_sec < 3600:
            last_used_str = f"{idle_sec//60}m ago"
        else:
            last_used_str = f"{idle_sec//3600}h ago"

    health_icon = "🟢" if pod["health_failures"] == 0 else ("🟡" if pod["health_failures"] < 3 else "🔴")

    with st.container(border=True):
        col1, col2, col3, col4, col5 = st.columns([2, 2, 1, 1, 1])

        col1.markdown(
            f"**{tier_badge(pod['tier'])}**  \n"
            f"`{pod['id'][:12]}…`  \n"
            f"{pod['provider']} · {pod['gpu'] or 'GPU?'}"
        )
        col2.markdown(
            f"**Status:** {status_badge(pod['status'])}  \n"
            f"**Health:** {health_icon} {pod['health_failures']} fail(s)  \n"
            f"**Endpoint:** `{(pod['endpoint_url'] or '')[:40]}`"
        )
        col3.metric("Cost/hr", f"${pod['cost_per_hour_usd']:.2f}")
        col4.metric("Uptime", uptime_str)
        col4.caption(f"Idle: {last_used_str}")

        if col5.button("💀 Kill", key=f"kill_{pod['id']}", type="secondary"):
            if kill_pod(pod["id"]):
                st.success(f"Terminated {pod['id'][:12]}")
                st.rerun()
            else:
                st.error("Kill failed — check bridge logs")
