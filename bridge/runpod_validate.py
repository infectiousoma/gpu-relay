"""Standalone RunPod volume validation — safe to import from the dashboard.

Does not import from providers/; uses httpx directly so it works in any process.
"""

from __future__ import annotations

import asyncio
import concurrent.futures

import httpx

_GQL_URL = "https://api.runpod.io/graphql"
_QUERY = "{ myself { networkVolumes { id dataCenterId } } }"


async def _validate_async(volume_id: str, api_key: str, datacenter: str | None) -> str | None:
    url = f"{_GQL_URL}?api_key={api_key}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            url,
            json={"query": _QUERY},
            headers={"Content-Type": "application/json"},
        )
        if r.is_error:
            try:
                body = r.json()
                detail = "; ".join(e.get("message", "") for e in body.get("errors", [])) or r.text[:200]
            except Exception:
                detail = r.text[:200]
            return f"RunPod API error ({r.status_code}): {detail}"

        body = r.json()
        if "errors" in body:
            msgs = [e.get("message", str(e)) for e in body["errors"]]
            return f"RunPod error: {'; '.join(msgs)}"

        volumes = (body.get("data") or {}).get("myself", {}).get("networkVolumes", [])

    vol = next((v for v in volumes if v["id"] == volume_id), None)
    if vol is None:
        ids = [v["id"] for v in volumes]
        return (
            f"RunPod network volume '{volume_id}' not found on account "
            f"(found: {ids or 'none'}). "
            "Create one at runpod.io/console/user/storage or update your volume key."
        )

    if datacenter:
        actual_dc = vol.get("dataCenterId", "")
        if actual_dc != datacenter:
            return (
                f"RunPod volume '{volume_id}' is in datacenter '{actual_dc}', "
                f"but volume key specifies '{datacenter}'. "
                "Update your volume key's datacenter field to match."
            )

    return None


def validate_runpod_volume(volume_id: str, api_key: str, datacenter: str | None = None) -> str | None:
    """Validate a RunPod network volume. Returns error message or None if valid.

    Runs the async HTTP call in a fresh thread so it's safe to call from
    Streamlit's sync context even when an event loop is already running.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _validate_async(volume_id, api_key, datacenter)).result(timeout=35)
