"""User settings page — tier preferences, provider toggles, personal API keys, storage volumes."""

from __future__ import annotations

import streamlit as st
from sqlalchemy import select

from dashboard.db import (
    db_session,
    delete_user_provider_key,
    delete_user_volume_key,
    get_user_provider_keys,
    get_user_volume_keys,
    update_user_preferences,
    upsert_user_provider_key,
    upsert_user_volume_key,
)
from database.models import User

_ALL_PROVIDERS = ["openai", "groq", "cerebras", "sambanova", "together", "together_dedicated", "mistral", "deepseek", "runpod", "vast", "lambda", "local"]
_API_PROVIDERS = ["openai", "groq", "cerebras", "sambanova", "together", "mistral", "deepseek"]
_GPU_PROVIDERS = ["runpod", "vast", "lambda"]


def render() -> None:
    st.title("⚙️ Settings")

    if msg := st.session_state.pop("settings_flash", None):
        st.success(msg)

    user_id = st.session_state.get("user_id")
    if not user_id:
        st.error("Not logged in.")
        return

    with db_session() as session:
        user = session.get(User, user_id)
        if not user:
            st.error("User not found.")
            return
        admin_ceiling = user.allowed_tiers
        current_preferred = user.preferred_tiers or []
        current_disabled = user.disabled_providers or []
        current_provider_order = user.provider_order or []

    try:
        from bridge.router import get_tiers
        all_tiers = list(get_tiers().keys())
    except Exception:
        all_tiers = ["simple", "mid", "architecture", "maximum", "ultra", "vision"]

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
        st.session_state["settings_flash"] = "Tier preferences saved."
        st.rerun()

    st.divider()

    # ----------------------------------------------------------------
    # Section 2: Provider order + toggles
    # ----------------------------------------------------------------
    st.subheader("Provider Preferences")
    st.caption(
        "Set your provider priority order and disable providers you never want used. "
        "Changes affect only your requests."
    )

    if current_provider_order:
        default_selection = [p for p in current_provider_order if p in _ALL_PROVIDERS]
    else:
        default_selection = [p for p in _ALL_PROVIDERS if p not in current_disabled]

    st.caption("**Priority order** — select providers in the order you want them tried (first = highest priority). Unselected providers are disabled.")
    selected_ordered = st.multiselect(
        "Provider order",
        options=_ALL_PROVIDERS,
        default=default_selection,
        key="prov_order",
        label_visibility="collapsed",
    )

    disabled_providers = [p for p in _ALL_PROVIDERS if p not in selected_ordered]

    if st.button("Save provider preferences", type="primary", key="save_providers"):
        with db_session() as session:
            update_user_preferences(
                session, user_id,
                current_preferred or None,
                disabled_providers or None,
                selected_ordered if selected_ordered != _ALL_PROVIDERS else None,
            )
        st.session_state["settings_flash"] = "Provider preferences saved."
        st.rerun()

    st.divider()

    # ----------------------------------------------------------------
    # Section 3: Commercial API keys (OpenAI, Groq, etc.)
    # ----------------------------------------------------------------
    st.subheader("Commercial API Keys")
    st.caption(
        "Your own API key for commercial providers (OpenAI, Groq, etc.). "
        "When set, your key is used for your requests instead of the shared admin key."
    )

    with db_session() as session:
        stored_keys = get_user_provider_keys(session, user_id)
    stored_by_provider = {k["provider"]: k for k in stored_keys}

    for provider in _API_PROVIDERS:
        _render_provider_key_form(provider, provider.title(), user_id, stored_by_provider)

    st.divider()

    # ----------------------------------------------------------------
    # Section 4: GPU Provider API Keys (RunPod, Vast, Lambda)
    # ----------------------------------------------------------------
    st.subheader("GPU Provider API Keys")
    st.caption(
        "Your own API key for GPU cloud providers. "
        "When set, pods are launched using your account — GPU costs are billed to you directly. "
        "If you also configure a storage volume below, this key is used for pod launch "
        "unless the volume key specifies its own."
    )

    with db_session() as session:
        stored_keys = get_user_provider_keys(session, user_id)
    stored_by_provider = {k["provider"]: k for k in stored_keys}

    for provider in _GPU_PROVIDERS:
        _render_provider_key_form(provider, provider.title(), user_id, stored_by_provider)

    st.divider()

    # ----------------------------------------------------------------
    # Section 5: Storage Volumes
    # ----------------------------------------------------------------
    st.subheader("Storage Volumes")
    st.caption(
        "Register a persistent network volume for GPU providers. "
        "Speeds up cold starts by caching model weights across pod launches (~30 s vs 5–15 min). "
        "The volume API key overrides the GPU provider key above for volume validation and pod launch."
    )

    with db_session() as session:
        stored_volumes = get_user_volume_keys(session, user_id)
    stored_vol_by_provider = {v["provider"]: v for v in stored_volumes}

    for provider in _GPU_PROVIDERS:
        _render_volume_key_form(provider, user_id, stored_vol_by_provider)


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------

def _render_provider_key_form(provider: str, label: str, user_id: str, stored_by_provider: dict) -> None:
    has_key = provider in stored_by_provider
    expander_label = f"{'✅' if has_key else '➕'} {label}"
    if has_key:
        expander_label += f"  *(saved {stored_by_provider[provider]['created_at'].strftime('%Y-%m-%d')})*"

    with st.expander(expander_label, expanded=False):
        with st.form(key=f"key_form_{provider}"):
            new_key = st.text_input(
                "API key" if not has_key else "Replace API key (leave blank to keep current)",
                type="password",
                placeholder=f"Enter {label} API key...",
            )
            kcol1, kcol2 = st.columns(2)
            save_pressed = kcol1.form_submit_button("Save key", type="primary")
            delete_pressed = kcol2.form_submit_button("Remove key", type="secondary") if has_key else False

            if save_pressed and new_key.strip():
                from bridge.crypto import encrypt_provider_key
                encrypted = encrypt_provider_key(new_key.strip())
                with db_session() as session:
                    upsert_user_provider_key(session, user_id, provider, encrypted)
                st.session_state["settings_flash"] = f"{label} key saved."
                st.rerun()
            elif save_pressed and not new_key.strip():
                st.warning("Enter a key to save.")

            if delete_pressed:
                with db_session() as session:
                    delete_user_provider_key(session, user_id, provider)
                st.session_state["settings_flash"] = f"{label} key removed."
                st.rerun()


def _render_volume_key_form(provider: str, user_id: str, stored_vol_by_provider: dict) -> None:
    has_vol = provider in stored_vol_by_provider
    existing = stored_vol_by_provider.get(provider, {})
    label = f"{'✅' if has_vol else '➕'} {provider.title()}"
    if has_vol:
        vid = existing["volume_id"]
        vid_short = vid[:12] + "…" if len(vid) > 12 else vid
        dc = existing.get("datacenter") or "any DC"
        label += f"  *({vid_short}, {dc}, saved {existing['created_at'].strftime('%Y-%m-%d')})*"

    with st.expander(label, expanded=False):
        with st.form(key=f"vol_form_{provider}"):
            vol_id = st.text_input(
                "Volume ID",
                value=existing.get("volume_id", ""),
                placeholder="e.g. abc12345",
            )
            api_key_input = st.text_input(
                "API key" if not has_vol else "API key (leave blank to keep current)",
                type="password",
                placeholder=f"Enter {provider.title()} API key for this volume…",
            )
            datacenter = st.text_input(
                "Datacenter (optional)",
                value=existing.get("datacenter") or "",
                placeholder="e.g. EU-RO-1 — constrains pod to this DC, enables DC validation",
            )

            vc1, vc2 = st.columns(2)
            save = vc1.form_submit_button("Save volume", type="primary")
            remove = vc2.form_submit_button("Remove", type="secondary") if has_vol else False

            if save:
                if not vol_id.strip():
                    st.warning("Volume ID is required.")
                elif not api_key_input.strip() and not has_vol:
                    st.warning("API key is required for a new volume key.")
                else:
                    from bridge.crypto import decrypt_provider_key, encrypt_provider_key

                    validation_err = None
                    if provider == "runpod":
                        plaintext_key = api_key_input.strip()
                        if not plaintext_key and has_vol:
                            with db_session() as _s:
                                from database.models import UserVolumeKey as _UVK
                                _row = _s.execute(
                                    select(_UVK).where(
                                        _UVK.user_id == user_id,
                                        _UVK.provider == provider,
                                    )
                                ).scalar_one_or_none()
                                if _row:
                                    plaintext_key = decrypt_provider_key(_row.api_key_encrypted)

                        if plaintext_key:
                            from bridge.runpod_validate import validate_runpod_volume
                            try:
                                validation_err = validate_runpod_volume(
                                    vol_id.strip(),
                                    plaintext_key,
                                    datacenter.strip() or None,
                                )
                            except Exception as e:
                                validation_err = f"Validation error: {e}"

                    if validation_err:
                        st.error(validation_err)
                    else:
                        encrypted = encrypt_provider_key(api_key_input.strip()) if api_key_input.strip() else None
                        with db_session() as session:
                            upsert_user_volume_key(
                                session, user_id, provider,
                                vol_id.strip(), encrypted,
                                datacenter.strip() or None,
                            )
                        st.session_state["settings_flash"] = f"{provider.title()} volume key saved."
                        st.rerun()

            if remove:
                with db_session() as session:
                    delete_user_volume_key(session, existing["id"], user_id)
                st.session_state["settings_flash"] = f"{provider.title()} volume key removed."
                st.rerun()
