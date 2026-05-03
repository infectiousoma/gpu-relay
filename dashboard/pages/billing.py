"""Billing page — invoices, generation, payment tracking, alerts log."""

from __future__ import annotations

import calendar
import io
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import streamlit as st
from sqlalchemy import func, select, text

from dashboard.components.metrics import format_usd, status_badge
from dashboard.db import db_session, get_all_users_with_stats, get_budget_alerts, get_invoices
from database.models import Invoice, InvoiceStatus, Request, RequestStatus, TierName, User


def render() -> None:
    st.title("💳 Billing")

    tab1, tab2, tab3 = st.tabs(["Invoices", "Generate Invoice", "Budget Alerts"])

    with tab1:
        _invoices_tab()

    with tab2:
        _generate_tab()

    with tab3:
        _alerts_tab()


def _invoices_tab() -> None:
    st.subheader("All Invoices")

    with db_session() as session:
        users = get_all_users_with_stats(session)
        user_options = {"All users": None} | {u["email"]: u["id"] for u in users}
        selected_email = st.selectbox("Filter by user", list(user_options.keys()))
        user_id = user_options[selected_email]
        invoices = get_invoices(session, user_id=user_id)

    if not invoices:
        st.info("No invoices found.")
        return

    # Summary
    total_open = sum(i["amount_usd"] for i in invoices if i["status"] == "open")
    total_paid = sum(i["amount_usd"] for i in invoices if i["status"] == "paid")
    c1, c2, c3 = st.columns(3)
    c1.metric("Open", format_usd(total_open))
    c2.metric("Paid", format_usd(total_paid))
    c3.metric("Total invoices", len(invoices))

    # Table
    for inv in invoices:
        period = f"{inv['period_start'].strftime('%Y-%m-%d')} → {inv['period_end'].strftime('%Y-%m-%d')}"
        status_icon = {"open": "📄", "paid": "✅", "void": "🗑️", "overdue": "⚠️"}.get(inv["status"], "❓")
        with st.expander(
            f"{status_icon} {inv['email']} — {period} — {format_usd(inv['amount_usd'])}",
            expanded=False,
        ):
            col1, col2 = st.columns(2)
            col1.markdown(f"**Status:** {status_badge(inv['status'])}")
            col1.markdown(f"**Requests:** {inv['request_count']:,}")
            col2.markdown(f"**Amount:** {format_usd(inv['amount_usd'])}")
            col2.markdown(f"**Created:** {inv['created_at'].strftime('%Y-%m-%d %H:%M')}")
            if inv["paid_at"]:
                col2.markdown(f"**Paid:** {inv['paid_at'].strftime('%Y-%m-%d')}")

            bcol1, bcol2, bcol3 = st.columns(3)

            if inv["status"] == "open" and bcol1.button("Mark paid", key=f"pay_{inv['id']}"):
                with db_session() as session:
                    row = session.get(Invoice, inv["id"])
                    if row:
                        row.status = InvoiceStatus.paid
                        row.paid_at = datetime.now(timezone.utc)
                        session.commit()
                st.rerun()

            if inv["status"] == "open" and bcol2.button("Void", key=f"void_{inv['id']}"):
                with db_session() as session:
                    row = session.get(Invoice, inv["id"])
                    if row:
                        row.status = InvoiceStatus.void
                        session.commit()
                st.rerun()

            # CSV download as "PDF" substitute (true PDF requires WeasyPrint/ReportLab)
            csv_data = _invoice_csv(inv)
            bcol3.download_button(
                "⬇️ Download CSV",
                data=csv_data,
                file_name=f"invoice_{inv['id'][:8]}.csv",
                mime="text/csv",
                key=f"dl_{inv['id']}",
            )


def _generate_tab() -> None:
    st.subheader("Generate Invoices")
    st.markdown("Rolls up all `ok` requests for postpaid users in the selected month into Invoice rows.")

    now = datetime.now(timezone.utc)
    month_str = st.text_input("Month (YYYY-MM)", value=now.strftime("%Y-%m"))

    if st.button("Generate", type="primary"):
        try:
            year, month = map(int, month_str.split("-"))
        except ValueError:
            st.error("Invalid format — use YYYY-MM")
            return

        last_day = calendar.monthrange(year, month)[1]
        period_start = datetime(year, month, 1, tzinfo=timezone.utc)
        period_end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)

        created = 0
        skipped = 0
        with db_session() as session:
            from database.models import BillingMode
            users = session.execute(
                select(User).where(User.billing_mode == BillingMode.postpaid, User.is_active.is_(True))
            ).scalars().all()

            for user in users:
                existing = session.execute(
                    select(Invoice).where(
                        Invoice.user_id == user.id,
                        Invoice.period_start == period_start,
                    )
                ).scalar_one_or_none()

                if existing:
                    skipped += 1
                    continue

                agg = session.execute(
                    select(
                        func.sum(Request.cost_usd).label("total"),
                        func.count(Request.id).label("count"),
                    ).where(
                        Request.user_id == user.id,
                        Request.status == RequestStatus.ok,
                        Request.created_at >= period_start,
                        Request.created_at <= period_end,
                    )
                ).one()

                if not agg.count:
                    continue

                session.add(Invoice(
                    user_id=user.id,
                    period_start=period_start,
                    period_end=period_end,
                    amount_usd=Decimal(str(agg.total or 0)),
                    request_count=int(agg.count or 0),
                    status=InvoiceStatus.open,
                ))
                created += 1

            session.commit()

        st.success(f"Generated {created} invoices ({skipped} already existed).")


def _alerts_tab() -> None:
    st.subheader("Budget Alert History")

    days = st.slider("Lookback (days)", 7, 90, 30)

    with db_session() as session:
        alerts = get_budget_alerts(session, days=days)

    if not alerts:
        st.success("No budget alerts fired in this period.")
        return

    st.warning(f"{len(alerts)} alert(s) fired in last {days} days.")
    df = pd.DataFrame(alerts)
    df["fired_at"] = df["fired_at"].apply(lambda x: x.strftime("%Y-%m-%d %H:%M") if x else "")
    df["period_start"] = df["period_start"].apply(lambda x: x.strftime("%Y-%m") if x else "")
    df.columns = ["Email", "Threshold %", "Fired At", "Period"]
    st.dataframe(df, use_container_width=True, hide_index=True)


def _invoice_csv(inv: dict) -> str:
    buf = io.StringIO()
    buf.write(f"Invoice\n")
    buf.write(f"User,{inv['email']}\n")
    buf.write(f"Period,{inv['period_start'].strftime('%Y-%m-%d')} to {inv['period_end'].strftime('%Y-%m-%d')}\n")
    buf.write(f"Amount USD,{inv['amount_usd']:.4f}\n")
    buf.write(f"Requests,{inv['request_count']}\n")
    buf.write(f"Status,{inv['status']}\n")
    return buf.getvalue()
