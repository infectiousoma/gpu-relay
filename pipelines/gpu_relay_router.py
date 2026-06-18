"""
GPU-Relay User Key Router
=========================
Manifold pipeline: fetches models from the bridge with the admin key,
routes each user's inference with their own bridge API key.

Setup after installing this pipeline in Open WebUI:
  Admin Panel → Pipelines → GPU-Relay Router → Valves:
    bridge_url:   http://bridge:8000
    admin_key:    sk-llm-xxx        (model discovery only)
    user_key_map: {"alice@example.com":"sk-llm-aaa","bob@example.com":"sk-llm-bbb"}
    fallback_key: sk-llm-xxx        (used when user has no mapping)
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx
from pydantic import BaseModel


class Pipeline:
    class Valves(BaseModel):
        bridge_url: str = "http://bridge:8000"
        admin_key: str = ""
        user_key_map: str = "{}"
        fallback_key: str = ""

    def __init__(self):
        self.type = "manifold"
        self.id = "gpu-relay"
        self.name = "GPU-Relay/"
        self.valves = self.Valves()

    def pipelines(self) -> list[dict]:
        try:
            r = httpx.get(
                f"{self.valves.bridge_url}/v1/models",
                headers={"Authorization": f"Bearer {self.valves.admin_key}"},
                timeout=10,
            )
            if r.is_success:
                return [{"id": m["id"], "name": m["id"]} for m in r.json().get("data", [])]
        except Exception:
            pass
        return [{"id": "llm-auto", "name": "llm-auto (bridge unavailable)"}]

    async def pipe(self, body: dict, __user__: dict | None = None, **_) -> AsyncIterator[str] | str:
        email = (__user__ or {}).get("email", "")
        try:
            key_map = json.loads(self.valves.user_key_map)
        except Exception:
            key_map = {}

        api_key = key_map.get(email) or self.valves.fallback_key or self.valves.admin_key
        if not api_key:
            yield "Error: no bridge API key configured for this user."
            return

        # Strip pipeline prefix: "gpu-relay.llm-simple" → "llm-simple"
        model = body.get("model", "")
        if "." in model:
            model = model.split(".", 1)[1]

        payload = {**body, "model": model}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=300) as client:
            if body.get("stream", False):
                async with client.stream(
                    "POST",
                    f"{self.valves.bridge_url}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                ) as resp:
                    if not resp.is_success:
                        err = await resp.aread()
                        yield f"Bridge error {resp.status_code}: {err.decode()[:500]}"
                        return
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            yield line + "\n\n"
            else:
                resp = await client.post(
                    f"{self.valves.bridge_url}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                )
                if not resp.is_success:
                    yield f"Bridge error {resp.status_code}: {resp.text[:500]}"
                    return
                yield resp.text
