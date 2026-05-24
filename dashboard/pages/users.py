"""User management page — list, budget, suspend, prepaid credit."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import streamlit as st
from sqlalchemy import select

from dashboard.components.charts import user_spend_bar
from dashboard.components.metrics import budget_progress, format_usd
from dashboard.db import db_session, get_all_users_with_stats
from database.models import BillingMode, Quota, TierName, User, UserRole


def render() -> None:
    st.title("👥 User Management")

    with db_session() as session:
        users = get_all_users_with_stats(session)

    if not users:
        st.info("No users found.")
        return

    # --- Summary metrics ---
    active = sum(1 for u in users if u["is_active"])
    total_spend = sum(u["monthly_spend_usd"] for u in users)
    col1, col2, col3 = st.columns(3)
    col1.metric("Total Users", len(users))
    col2.metric("Active", active)
    col3.metric("Month Spend (all)", format_usd(total_spend))

    st.plotly_chart(user_spend_bar(users), use_container_width=True)

    st.divider()

    # --- User table ---
    st.subheader("All Users")
    search = st.text_input("Search by email", placeholder="filter...")

    filtered = [u for u in users if search.lower() in u["email"].lower()] if search else users

    for u in filtered:
        with st.expander(
            f"{'🟢' if u['is_active'] else '⚫'} {u['email']} — "
            f"{u['role']} — {format_usd(u['monthly_spend_usd'])} / ${u['monthly_budget_usd']:.2f}",
            expanded=False,
        ):
            _render_user_detail(u)

    st.divider()
    with st.expander("➕ Add New User", expanded=False):
        _render_add_user_form()


def _render_user_detail(u: dict) -> None:
    col1, col2 = st.columns(2)

    with col1:
        st.markdown(f"**ID:** `{u['id']}`")
        st.markdown(f"**Role:** {u['role']}")
        st.markdown(f"**Billing:** {u['billing_mode']}")
        _allowed = u.get("allowed_tiers")
        tiers_display = ", ".join(_allowed) if _allowed else "all"
        st.markdown(f"**Allowed tiers:** {tiers_display}")
        st.markdown(f"**RPM limit:** {u['rpm']}")
        st.markdown(f"**Tokens/day:** {u['tpd']:,}")
        if u["billing_mode"] == "prepaid":
            st.markdown(f"**Prepaid balance:** {format_usd(u['prepaid_balance_usd'])}")

    with col2:
        st.markdown(f"**This month:** {u['monthly_requests']:,} requests — {format_usd(u['monthly_spend_usd'])}")
        budget_progress(u["monthly_spend_usd"], u["monthly_budget_usd"])
        st.markdown(f"**Created:** {u['created_at'].strftime('%Y-%m-%d') if u['created_at'] else '—'}")

    st.markdown("---")

    # --- Edit form ---
    with st.form(key=f"edit_{u['id']}"):
        st.markdown("**Edit**")
        fcol1, fcol2, fcol3 = st.columns(3)

        try:
            from bridge.router import get_tiers
            _all_tiers = list(get_tiers().keys())
        except Exception:
            _all_tiers = ["simple", "architecture", "maximum", "ultra", "vision"]
        new_budget = fcol1.number_input(
            "Budget USD/month", value=u["monthly_budget_usd"], min_value=0.0, step=5.0, key=f"budget_{u['id']}"
        )
        new_allowed_tiers = fcol2.multiselect(
            "Allowed tiers (empty = all)",
            _all_tiers,
            default=u.get("allowed_tiers") or [],
            key=f"tiers_{u['id']}",
        )
        new_rpm = fcol3.number_input(
            "RPM limit", value=u["rpm"], min_value=1, max_value=1000, step=10, key=f"rpm_{u['id']}"
        )

        if u["billing_mode"] == "prepaid":
            add_credit = st.number_input(
                "Add prepaid credit (USD)", value=0.0, min_value=0.0, step=5.0, key=f"credit_{u['id']}"
            )
        else:
            add_credit = 0.0

        new_allow_local = st.checkbox(
            "Allow local provider",
            value=u.get("allow_local", True),
            key=f"allow_local_{u['id']}",
            help="Uncheck to prevent this user from routing requests to the local GPU.",
        )

        bcol1, bcol2 = st.columns(2)
        submitted = bcol1.form_submit_button("💾 Save changes", type="primary")
        suspend = bcol2.form_submit_button(
            "⏸️ Suspend" if u["is_active"] else "▶️ Activate", type="secondary"
        )

        if submitted:
            with db_session() as session:
                user = session.get(User, u["id"])
                if user:
                    user.monthly_budget_usd = Decimal(str(new_budget))
                    if add_credit > 0:
                        user.prepaid_balance_usd += Decimal(str(add_credit))
                    # None = unrestricted; non-empty list = whitelist
                    user.allowed_tiers = new_allowed_tiers if new_allowed_tiers else None
                    user.allow_local = new_allow_local
                    quota = session.get(Quota, u["id"])
                    if quota:
                        quota.usd_per_month = Decimal(str(new_budget))
                        # Keep max_tier in sync with highest allowed tier
                        ordered = [t for t in _all_tiers if t in new_allowed_tiers]
                        quota.max_tier = TierName(ordered[-1] if ordered else "ultra")
                        quota.requests_per_minute = new_rpm
                    session.commit()
            st.success("Saved.")
            st.rerun()

        if suspend:
            with db_session() as session:
                user = session.get(User, u["id"])
                if user:
                    user.is_active = not user.is_active
                    session.commit()
            action = "Suspended" if u["is_active"] else "Activated"
            st.success(f"{action} {u['email']}.")
            st.rerun()



def _render_add_user_form() -> None:
    with st.form("add_user"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        role = st.selectbox("Role", ["user", "admin", "service"])
        billing = st.selectbox("Billing mode", ["postpaid", "prepaid"])
        budget = st.number_input("Monthly budget USD", value=25.0, min_value=0.0, step=5.0)

        if st.form_submit_button("Create user", type="primary"):
            if not email or not password:
                st.error("Email and password required.")
                return
            from bridge.auth import hash_password
            with db_session() as session:
                existing = session.execute(
                    select(User).where(User.email == email)
                ).scalar_one_or_none()
                if existing:
                    st.error(f"User {email} already exists.")
                    return

                user = User(
                    email=email,
                    password_hash=hash_password(password),
                    role=UserRole(role),
                    billing_mode=BillingMode(billing),
                    monthly_budget_usd=Decimal(str(budget)),
                )
                session.add(user)
                session.flush()
                session.add(Quota(
                    user_id=user.id,
                    usd_per_month=Decimal(str(budget)),
                ))
                session.commit()

            st.success(f"Created {email}")
            st.rerun()
