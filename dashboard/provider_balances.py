"""Synchronous provider balance queries for the dashboard.

Uses httpx (sync) since Streamlit's render functions are synchronous.
Returns None for unconfigured or failed providers — never raises.
"""

from __future__ import annotations

import os

import httpx

_TIMEOUT = 10


def _runpod_balance() -> dict | None:
    api_key = os.getenv("RUNPOD_API_KEY", "")
    if not api_key:
        return None
    try:
        r = httpx.post(
            f"https://api.runpod.io/graphql?api_key={api_key}",
            json={"query": "query { myself { creditBalance } }"},
            headers={"Content-Type": "application/json"},
            timeout=_TIMEOUT,
        )
        if not r.is_success:
            return {"provider": "RunPod", "balance": None, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        resp = r.json()
        errors = resp.get("errors")
        if errors:
            return {"provider": "RunPod", "balance": None, "error": errors[0].get("message", str(errors))}
        data = resp.get("data", {}).get("myself", {})
        return {
            "provider": "RunPod",
            "balance": data.get("creditBalance"),
            "currency": "USD",
        }
    except Exception as exc:
        return {"provider": "RunPod", "balance": None, "error": str(exc)}


def _vast_balance() -> dict | None:
    api_key = os.getenv("VAST_API_KEY", "")
    if not api_key:
        return None
    try:
        r = httpx.get(
            "https://console.vast.ai/api/v0/users/current/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return {
            "provider": "Vast.ai",
            "balance": data.get("credit"),
            "currency": "USD",
        }
    except Exception as exc:
        return {"provider": "Vast.ai", "balance": None, "error": str(exc)}


def _lambda_balance() -> dict | None:
    api_key = os.getenv("LAMBDA_API_KEY", "")
    if not api_key:
        return None
    # Lambda Labs doesn't expose a balance endpoint; show key is configured
    return {"provider": "Lambda Labs", "balance": None, "note": "Balance API not available"}


def get_provider_balances() -> list[dict]:
    """Return balance info for all configured providers. Never raises."""
    results = []
    for fn in (_runpod_balance, _vast_balance, _lambda_balance):
        result = fn()
        if result is not None:
            results.append(result)
    return results
