"""HTTP client for bridge API actions (pod lifecycle, admin ops).

Dashboard reads data directly from DB for speed. This client is used only
for write operations that must go through the bridge (pod kill, user suspend)
so InstanceManager state stays consistent.

Token cached in st.session_state["bridge_token"].
"""

from __future__ import annotations

import os

import httpx
import streamlit as st

_BRIDGE_URL = os.getenv("OPENWEBUI_BRIDGE_URL", "http://bridge:8000").rstrip("/")
_TIMEOUT = 15


def _token() -> str | None:
    return st.session_state.get("bridge_token")


def _headers() -> dict:
    tok = _token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def login(email: str, password: str) -> bool:
    """Attempt bridge login; store JWT in session_state. Returns success."""
    try:
        r = httpx.post(
            f"{_BRIDGE_URL}/auth/login",
            json={"email": email, "password": password},
            timeout=_TIMEOUT,
        )
        if r.status_code == 200:
            data = r.json()
            st.session_state["bridge_token"] = data["access_token"]
            st.session_state["user_role"] = data.get("role", "user")
            return True
    except Exception as exc:
        st.error(f"Login error: {exc}")
    return False


def kill_pod(pod_id: str) -> bool:
    try:
        r = httpx.delete(
            f"{_BRIDGE_URL}/admin/pods/{pod_id}",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        return r.status_code in (200, 204)
    except Exception as exc:
        st.error(f"Kill pod error: {exc}")
        return False


def list_pods_api() -> list[dict]:
    try:
        r = httpx.get(f"{_BRIDGE_URL}/admin/pods", headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def get_bridge_health() -> dict:
    try:
        r = httpx.get(f"{_BRIDGE_URL}/healthz", timeout=5)
        return r.json() if r.status_code == 200 else {"status": "error", "code": r.status_code}
    except Exception as exc:
        return {"status": "unreachable", "error": str(exc)}
