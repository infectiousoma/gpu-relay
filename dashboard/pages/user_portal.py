"""User Portal — self-service dashboard for bridge users.

Authenticates with an API key (no email/password needed).
All data fetched via bridge REST API — no direct DB access required.
"""

from __future__ import annotations

import os

import httpx
import streamlit as st

_BRIDGE_URL = os.getenv("OPENWEBUI_BRIDGE_URL", "http://bridge:8000").rstrip("/")
_TIMEOUT = 15


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    return st.session_state.get("portal_api_key", "")


def _headers() -> dict:
    key = _api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _fetch_usage() -> dict | None:
    try:
        r = httpx.get(f"{_BRIDGE_URL}/v1/usage", headers=_headers(), timeout=_TIMEOUT)
        if r.status_code == 401:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.error(f"Bridge unreachable: {exc}")
        return None


def _validate_key(api_key: str) -> bool:
    """Try /v1/usage to confirm the key works."""
    try:
        r = httpx.get(
            f"{_BRIDGE_URL}/v1/usage",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_TIMEOUT,
        )
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def render() -> None:
    st.title("User Portal")

    if not _api_key():
        _login_form()
        return

    data = _fetch_usage()
    if data is None:
        st.error("Invalid or expired API key.")
        if st.button("Log out"):
            st.session_state.pop("portal_api_key", None)
            st.rerun()
        return

    _dashboard(data)


def _login_form() -> None:
    st.subheader("Sign in with your API key")
    st.caption("Your bridge API key (starts with `sk-llm-`). Available from your administrator.")
    with st.form("portal_login"):
        key_input = st.text_input("API Key", type="password", placeholder="sk-llm-...")
        submitted = st.form_submit_button("Connect")

    if submitted:
        if not key_input.startswith("sk-llm-"):
            st.error("Key must start with sk-llm-")
            return
        with st.spinner("Verifying…"):
            if _validate_key(key_input):
                st.session_state["portal_api_key"] = key_input
                st.rerun()
            else:
                st.error("Key rejected by bridge (invalid or inactive).")


def _dashboard(data: dict) -> None:
    col_header, col_logout = st.columns([4, 1])
    with col_header:
        st.subheader(f"Welcome, {data.get('email', 'user')}")
    with col_logout:
        if st.button("Log out"):
            st.session_state.pop("portal_api_key", None)
            st.rerun()

    this_month: dict = data.get("this_month", {})
    budget = float(data.get("monthly_budget_usd", 0))
    balance = float(data.get("prepaid_balance_usd", 0))
    billing_mode = data.get("billing_mode", "postpaid")
    spent = float(this_month.get("cost_usd", 0))
    remaining = max(budget - spent, 0) if billing_mode == "postpaid" else balance

    # --- Top metrics ---
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("This month spent", f"${spent:.4f}")
    with col2:
        label = "Remaining budget" if billing_mode == "postpaid" else "Prepaid balance"
        st.metric(label, f"${remaining:.2f}")
    with col3:
        st.metric("Requests", this_month.get("total_requests", 0))
    with col4:
        tokens = (this_month.get("prompt_tokens", 0) or 0) + (this_month.get("completion_tokens", 0) or 0)
        st.metric("Total tokens", f"{tokens:,}")

    if budget > 0 and billing_mode == "postpaid":
        pct = min(spent / budget, 1.0)
        st.progress(pct, text=f"{pct*100:.1f}% of ${budget:.2f} monthly budget used")

    # --- Daily usage chart ---
    st.divider()
    st.subheader("Last 30 days")
    daily: list[dict] = data.get("last_30_days", [])
    if daily:
        try:
            import pandas as pd
            df = pd.DataFrame(daily)
            df["period"] = pd.to_datetime(df["period"])
            df = df.set_index("period")

            tab1, tab2 = st.tabs(["Spending ($)", "Tokens"])
            with tab1:
                st.bar_chart(df[["cost_usd"]], y_label="USD")
            with tab2:
                df["total_tokens"] = df["prompt_tokens"] + df["completion_tokens"]
                st.bar_chart(df[["total_tokens"]], y_label="Tokens")
        except ImportError:
            st.info("Install pandas for charts.")
            for row in daily:
                st.text(f"{row['period']}: ${float(row.get('cost_usd', 0)):.4f} | {row.get('total_requests', 0)} requests")
    else:
        st.info("No usage in the last 30 days.")

    # --- Account info ---
    st.divider()
    with st.expander("Account details"):
        st.json({
            "user_id": data.get("user_id"),
            "email": data.get("email"),
            "billing_mode": billing_mode,
            "monthly_budget_usd": budget,
            "prepaid_balance_usd": balance,
        })


render()
