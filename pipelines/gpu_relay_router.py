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
from typing import Generator

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

    def _resolve_key(self, user: dict | None) -> str:
        email = (user or {}).get("email", "")
        try:
            key_map = json.loads(self.valves.user_key_map)
        except Exception:
            key_map = {}
        return key_map.get(email) or self.valves.fallback_key or self.valves.admin_key

    def _strip_model(self, body: dict) -> str:
        model = body.get("model", "")
        return model.split(".", 1)[1] if "." in model else model

    def pipe(self, body: dict, __user__: dict | None = None, **_) -> str | dict | Generator:
        api_key = self._resolve_key(__user__)
        if not api_key:
            return "Error: no bridge API key configured for this user."

        model = self._strip_model(body)
        # OpenAI spec: user is a string; Open WebUI injects a dict — strip it
        payload = {k: v for k, v in body.items() if k != "user"}
        payload["model"] = model
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        if body.get("stream", False):
            return self._stream(payload, headers)

        with httpx.Client(timeout=300) as client:
            resp = client.post(
                f"{self.valves.bridge_url}/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            if not resp.is_success:
                return f"Bridge error {resp.status_code}: {resp.text[:500]}"
            return resp.json()

    def _stream(self, payload: dict, headers: dict) -> Generator[str, None, None]:
        with httpx.Client(timeout=300) as client:
            with client.stream(
                "POST",
                f"{self.valves.bridge_url}/v1/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                if not resp.is_success:
                    yield f"Bridge error {resp.status_code}: {resp.read().decode()[:500]}"
                    return
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        yield line + "\n\n"
