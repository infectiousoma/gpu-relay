"""Streamlit dashboard main entry point.

Navigation:
  - Login gate: uses bridge API JWT auth
  - Sidebar: page selector + logout
  - Pages: overview, analytics, users, billing, monitoring

Run: streamlit run dashboard/app.py
"""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="LLM Infrastructure Dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={"About": "Self-hosted LLM Infrastructure v0.1.0"},
)

# Inject mobile-friendly CSS
st.markdown("""
<style>
    /* Tighten sidebar */
    [data-testid="stSidebar"] { min-width: 220px; }
    /* Metric cards */
    [data-testid="stMetricValue"] { font-size: 1.4rem !important; }
    /* Full-width on mobile */
    @media (max-width: 768px) {
        [data-testid="stSidebar"] { display: none; }
        .block-container { padding: 0.5rem 1rem; }
    }
    /* Status badges */
    code { font-size: 0.8rem; }
</style>
""", unsafe_allow_html=True)


def _login_page() -> None:
    st.title("🤖 LLM Infrastructure")
    st.subheader("Sign in")

    col, _ = st.columns([1, 1])
    with col:
        with st.form("login"):
            email = st.text_input("Email", placeholder="admin@example.com")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

        if submitted:
            from dashboard.api_client import login
            if login(email, password):
                st.session_state["user_email"] = email
                st.success("Logged in.")
                st.rerun()
            else:
                st.error("Invalid credentials.")


def _is_admin() -> bool:
    return st.session_state.get("user_role") == "admin"


def _sidebar_nav() -> str:
    with st.sidebar:
        st.markdown("## 🤖 LLM Dashboard")
        role_label = " 👑 Admin" if _is_admin() else ""
        st.caption(f"Signed in as **{st.session_state.get('user_email', '?')}**{role_label}")
        st.divider()

        pages = ["📊 Overview", "📈 Analytics", "💳 Billing", "⚙️ Settings"]
        if _is_admin():
            pages += ["👥 Users", "🖥️ Monitoring"]

        page = st.radio("Navigate", pages, label_visibility="collapsed")

        st.divider()
        if st.button("Sign out", use_container_width=True):
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.rerun()

    return page


def main() -> None:
    if "bridge_token" not in st.session_state:
        _login_page()
        return

    page = _sidebar_nav()

    if page == "📊 Overview":
        from dashboard.pages.overview import render
        render()
    elif page == "📈 Analytics":
        from dashboard.pages.analytics import render
        render()
    elif page == "👥 Users":
        from dashboard.pages.users import render
        render()
    elif page == "💳 Billing":
        from dashboard.pages.billing import render
        render()
    elif page == "🖥️ Monitoring":
        from dashboard.pages.monitoring import render
        render()
    elif page == "⚙️ Settings":
        from dashboard.pages.settings import render
        render()


if __name__ == "__main__":
    main()
