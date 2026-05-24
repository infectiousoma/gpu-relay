"""User settings page — tier preferences, provider toggles, personal API keys."""

from __future__ import annotations

import streamlit as st
from sqlalchemy import select

from dashboard.db import (
    db_session,
    delete_user_provider_key,
    get_user_provider_keys,
    update_user_preferences,
    upsert_user_provider_key,
)
from database.models import User

_ALL_PROVIDERS = ["openai", "groq", "together", "together_dedicated", "mistral", "deepseek", "runpod", "vast", "lambda", "local"]


def render() -> None:
    st.title("⚙️ Settings")

    user_id = st.session_state.get("user_id")
    if not user_id:
        st.error("Not logged in.")
        return

    with db_session() as session:
        user = session.get(User, user_id)
        if not user:
            st.error("User not found.")
            return
        admin_ceiling = user.allowed_tiers  # None = all tiers allowed
        current_preferred = user.preferred_tiers or []
        current_disabled = user.disabled_providers or []

    try:
        from bridge.router import get_tiers
        all_tiers = list(get_tiers().keys())
    except Exception:
        all_tiers = ["simple", "architecture", "maximum", "ultra", "vision"]

    # Tiers available to this user (respecting admin ceiling)
    available_tiers = [t for t in all_tiers if not admin_ceiling or t in admin_ceiling]

    # ----------------------------------------------------------------
    # Section 1: Tier preferences
    # ----------------------------------------------------------------
    st.subheader("Tier Preferences")
    st.caption(
        "Choose which tiers the router may use for your requests. "
        "Uncheck tiers you never want routed to. Leave all checked to use all available tiers."
    )

    tier_selections = {}
    tcols = st.columns(max(len(available_tiers), 1))
    for col, tier in zip(tcols, available_tiers):
        tier_selections[tier] = col.checkbox(tier, value=(tier in current_preferred) if current_preferred else True, key=f"tier_{tier}")

    selected_tiers = [t for t, checked in tier_selections.items() if checked]
    all_checked = all(tier_selections.values())

    if st.button("Save tier preferences", type="primary"):
        save_tiers = None if all_checked else selected_tiers
        with db_session() as session:
            update_user_preferences(session, user_id, save_tiers, current_disabled or None)
        st.success("Tier preferences saved.")
        st.rerun()

    st.divider()

    # ----------------------------------------------------------------
    # Section 2: Provider toggles
    # ----------------------------------------------------------------
    st.subheader("Provider Preferences")
    st.caption(
        "Disable providers you don't want used for your requests. "
        "Disabling a provider never affects other users."
    )

    provider_selections = {}
    pcols = st.columns(max(len(_ALL_PROVIDERS), 1))
    for col, provider in zip(pcols, _ALL_PROVIDERS):
        provider_selections[provider] = col.checkbox(
            provider,
            value=(provider not in current_disabled),
            key=f"prov_{provider}",
        )

    disabled_providers = [p for p, enabled in provider_selections.items() if not enabled]

    if st.button("Save provider preferences", type="primary", key="save_providers"):
        with db_session() as session:
            update_user_preferences(session, user_id, current_preferred or None, disabled_providers or None)
        st.success("Provider preferences saved.")
        st.rerun()

    st.divider()

    # ----------------------------------------------------------------
    # Section 3: Personal API keys
    # ----------------------------------------------------------------
    st.subheader("Personal API Keys")
    st.caption(
        "Add your own API keys for cloud providers. When set, your key is used instead of "
        "the shared admin key — no usage is charged to your account for those providers."
    )

    with db_session() as session:
        stored_keys = get_user_provider_keys(session, user_id)
    stored_by_provider = {k["provider"]: k for k in stored_keys}

    api_providers = ["openai", "groq", "together", "mistral", "deepseek"]
    for provider in api_providers:
        has_key = provider in stored_by_provider
        label = f"{'✅' if has_key else '➕'} {provider.title()}"
        if has_key:
            label += f"  *(saved {stored_by_provider[provider]['created_at'].strftime('%Y-%m-%d')})*"

        with st.expander(label, expanded=False):
            with st.form(key=f"key_form_{provider}"):
                new_key = st.text_input(
                    "API key" if not has_key else "Replace API key (leave blank to keep current)",
                    type="password",
                    placeholder=f"Enter {provider} API key...",
                )
                kcol1, kcol2 = st.columns(2)
                save_pressed = kcol1.form_submit_button("Save key", type="primary")
                delete_pressed = kcol2.form_submit_button("Remove key", type="secondary") if has_key else False

                if save_pressed and new_key.strip():
                    from bridge.crypto import encrypt_provider_key
                    encrypted = encrypt_provider_key(new_key.strip())
                    with db_session() as session:
                        upsert_user_provider_key(session, user_id, provider, encrypted)
                    st.success(f"{provider.title()} key saved.")
                    st.rerun()
                elif save_pressed and not new_key.strip():
                    st.warning("Enter a key to save.")

                if delete_pressed:
                    with db_session() as session:
                        delete_user_provider_key(session, user_id, provider)
                    st.success(f"{provider.title()} key removed.")
                    st.rerun()
