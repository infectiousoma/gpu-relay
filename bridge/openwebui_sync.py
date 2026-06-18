"""Sync bridge users/keys to Open WebUI and the gpu-relay pipeline valve."""

from __future__ import annotations

import json

import httpx
import structlog

from bridge.settings import settings

log = structlog.get_logger(__name__)


async def _ow_admin_token() -> str:
    """Obtain a short-lived admin JWT from Open WebUI."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{settings.openwebui_url}/api/v1/auths/signin",
            json={"email": settings.openwebui_admin_email, "password": settings.openwebui_admin_password},
        )
        r.raise_for_status()
        return r.json()["token"]


async def create_openwebui_user(email: str, password: str, name: str | None = None) -> str:
    """Create a user in Open WebUI. Returns the OW user id, or '' if skipped/already exists."""
    if not (settings.openwebui_admin_email and settings.openwebui_admin_password):
        log.debug("ow_sync_skipped", reason="openwebui_admin creds not configured")
        return ""
    try:
        token = await _ow_admin_token()
        headers = {"Authorization": f"Bearer {token}"}
        display_name = name or email.split("@")[0]
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{settings.openwebui_url}/api/v1/auths/add",
                json={"email": email, "password": password, "name": display_name, "role": "user"},
                headers=headers,
            )
            if r.status_code in (400, 409) and "already" in r.text.lower():
                log.info("ow_user_already_exists", email=email)
                return ""
            r.raise_for_status()
            ow_id = r.json().get("id", "")
            log.info("ow_user_created", email=email, ow_id=ow_id)
            return ow_id
    except Exception as exc:
        log.warning("ow_user_create_failed", email=email, error=str(exc))
        return ""


async def update_pipeline_user_key_map(email: str, api_key: str) -> bool:
    """Upsert email→api_key in the gpu-relay pipeline's user_key_map valve.

    Returns True on success, False if skipped or failed.
    """
    if not settings.pipelines_api_key:
        log.debug("pipeline_sync_skipped", reason="pipelines_api_key not set")
        return False
    pipeline_id = settings.pipeline_id
    headers = {"Authorization": f"Bearer {settings.pipelines_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{settings.pipelines_url}/v1/pipelines/{pipeline_id}/valves",
                headers=headers,
            )
            if not r.is_success:
                log.warning("pipeline_valves_fetch_failed", pipeline_id=pipeline_id, status=r.status_code)
                return False

            valves = r.json()
            try:
                key_map = json.loads(valves.get("user_key_map") or "{}")
            except Exception:
                key_map = {}
            key_map[email] = api_key
            valves["user_key_map"] = json.dumps(key_map)

            r2 = await client.post(
                f"{settings.pipelines_url}/v1/pipelines/{pipeline_id}/valves/update",
                json=valves,
                headers=headers,
            )
            if not r2.is_success:
                log.warning("pipeline_valves_update_failed", pipeline_id=pipeline_id, status=r2.status_code, body=r2.text[:200])
                return False

            log.info("pipeline_user_key_map_updated", email=email, pipeline_id=pipeline_id)
            return True
    except Exception as exc:
        log.warning("pipeline_sync_failed", email=email, error=str(exc))
        return False
