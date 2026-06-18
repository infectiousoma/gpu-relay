"""API key management page — create, view, and revoke sk-llm- keys."""

from __future__ import annotations

import httpx
import streamlit as st

_BRIDGE_URL = __import__("os").getenv("OPENWEBUI_BRIDGE_URL", "http://bridge:8000").rstrip("/")
_TIMEOUT = 15


def _headers() -> dict:
    tok = st.session_state.get("bridge_token")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _list_keys() -> list[dict]:
    try:
        r = httpx.get(f"{_BRIDGE_URL}/auth/keys", headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.error(f"Failed to load keys: {exc}")
        return []


def _create_key(label: str) -> dict | None:
    try:
        params = {"label": label} if label.strip() else {}
        r = httpx.post(f"{_BRIDGE_URL}/auth/keys", headers=_headers(), params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.error(f"Failed to create key: {exc}")
        return None


def _revoke_key(key_id: str) -> bool:
    try:
        r = httpx.delete(
            f"{_BRIDGE_URL}/auth/keys/{key_id}",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        return r.status_code in (200, 204)
    except Exception as exc:
        st.error(f"Failed to revoke key: {exc}")
        return False


def render() -> None:
    st.title("🔑 API Keys")
    st.caption("Create and manage `sk-llm-` API keys for bridge access.")

    if flash := st.session_state.pop("keys_flash", None):
        st.success(flash)

    # ----------------------------------------------------------------
    # Create new key
    # ----------------------------------------------------------------
    st.subheader("Create New Key")
    with st.form("create_key_form"):
        label_input = st.text_input("Label (optional)", placeholder="e.g. cursor, local-dev")
        submitted = st.form_submit_button("Create key", type="primary")

    if submitted:
        new_key = _create_key(label_input)
        if new_key:
            st.session_state["new_key_plaintext"] = new_key["key"]
            st.session_state["new_key_prefix"] = new_key["prefix"]
            st.rerun()

    if plaintext := st.session_state.pop("new_key_plaintext", None):
        prefix = st.session_state.pop("new_key_prefix", "")
        st.success("Key created. **Copy it now — it will not be shown again.**")
        st.code(plaintext, language=None)
        st.caption(f"Prefix: `{prefix}`")

    st.divider()

    # ----------------------------------------------------------------
    # Existing keys
    # ----------------------------------------------------------------
    st.subheader("Active Keys")
    keys = _list_keys()
    active = [k for k in keys if not k.get("revoked")]
    revoked = [k for k in keys if k.get("revoked")]

    if not active:
        st.info("No active keys.")
    else:
        for key in active:
            col1, col2, col3, col4 = st.columns([3, 2, 3, 1])
            col1.code(key["prefix"] + "…", language=None)
            col2.write(key.get("label") or "*(no label)*")
            col3.caption(f"Created {key['created_at'][:10]}" + (f" · Last used {key['last_used_at'][:10]}" if key.get("last_used_at") else ""))
            if col4.button("Revoke", key=f"revoke_{key['id']}", type="secondary"):
                if _revoke_key(key["id"]):
                    st.session_state["keys_flash"] = f"Key `{key['prefix']}…` revoked."
                    st.rerun()

    if revoked:
        with st.expander(f"Revoked keys ({len(revoked)})"):
            for key in revoked:
                st.caption(f"`{key['prefix']}…`  {key.get('label') or ''}  *(revoked)*")
