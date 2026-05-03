"""Vast.ai provider — cost-optimised GPU rentals via REST API.

API reference: https://vast.ai/docs/api
Base URL: https://console.vast.ai/api/v0/

Vast.ai workflow:
  1. GET  /bundles/           → search offers with filters (VRAM, reliability, etc.)
  2. POST /instances/         → rent cheapest matching offer
  3. GET  /instances/{id}/    → poll until status == "running"
  4. DELETE /instances/{id}/  → terminate

Instance exposes ports via a public IP + mapped port. Vast API returns
`ssh_host` and `ports` dict; port 11434 is in `ports["11434/tcp"]`.

We request Docker image `ollama/ollama:latest` with `onstart` script that
pre-pulls the model. Vast uses `image` + `onstart` fields in the create body.

Note: Vast.ai instances are on-demand but shared infrastructure; they're
cheaper than RunPod for the same GPU, but with higher variance in reliability.
The `reliability2` field (0-1) is filtered to ≥ 0.95.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from bridge.settings import settings
from providers.base import (
    BaseProvider,
    GpuOffer,
    OLLAMA_PORT,
    PodInfo,
    ProviderError,
    TIER_MODEL,
    TIER_VRAM_REQUIRED,
)

log = structlog.get_logger(__name__)

_BASE = "https://console.vast.ai/api/v0"
_MIN_RELIABILITY = 0.95
_DISK_GB = 100


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ProviderError):
        return exc.retryable
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


class VastProvider(BaseProvider):
    name = "vast"

    def __init__(self) -> None:
        self._api_key = settings.vast_api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{_BASE}{path}", headers=self._headers(), params=params)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{_BASE}{path}", headers=self._headers(), json=body)
            r.raise_for_status()
            return r.json()

    async def _delete(self, path: str) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.delete(f"{_BASE}{path}", headers=self._headers())
            r.raise_for_status()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    async def launch(self, tier: str) -> PodInfo:
        if not self._api_key:
            raise ProviderError("VAST_API_KEY not set", retryable=False)

        offers = await self.list_gpus()
        offer = self._select_gpu_offer(tier, offers)
        model = TIER_MODEL[tier]
        onstart = (
            f"#!/bin/bash\n"
            f"ollama serve &\n"
            f"sleep 5\n"
            f"ollama pull {model}\n"
            f"wait\n"
        )

        body = {
            "client_id": "me",
            "image": "ollama/ollama:latest",
            "disk": _DISK_GB,
            "onstart": onstart,
            "runtype": "ssh_direc tcp",
            "env": f"-e OLLAMA_MODEL={model} -p {OLLAMA_PORT}:{OLLAMA_PORT}",
            "extra_env": {"OLLAMA_MODEL": model},
            "label": f"llm-{tier}",
        }

        data = await self._post(f"/asks/{offer.gpu_type_id}/", body)
        instance_id = str(data.get("new_contract", data.get("id", "")))
        if not instance_id or instance_id == "None":
            raise ProviderError(f"Vast.ai launch failed: {data}", retryable=True)

        log.info("vast_instance_launched", instance_id=instance_id, tier=tier, gpu=offer.gpu)

        endpoint_url = await self._wait_for_running(instance_id)

        return PodInfo(
            external_id=instance_id,
            provider=self.name,
            gpu=offer.gpu,
            model=model,
            cost_per_hour_usd=offer.cost_per_hour_usd,
            status="starting",
            endpoint_url=endpoint_url,
        )

    async def terminate(self, external_id: str) -> None:
        try:
            await self._delete(f"/instances/{external_id}/")
            log.info("vast_instance_terminated", instance_id=external_id)
        except Exception as exc:
            log.warning("vast_terminate_error", instance_id=external_id, error=str(exc))

    async def get_status(self, external_id: str) -> PodInfo:
        data = await self._get(f"/instances/{external_id}/")
        instance = data.get("instances", data)  # API wraps in 'instances' key sometimes
        if isinstance(instance, list):
            instance = instance[0] if instance else {}

        raw_status = instance.get("actual_status", "")
        status_map = {"running": "ready", "exited": "terminated", "offline": "failed", "created": "starting"}
        status = status_map.get(raw_status, "starting")

        endpoint = self._extract_endpoint(instance)
        return PodInfo(
            external_id=external_id,
            provider=self.name,
            gpu=instance.get("gpu_name", ""),
            model=instance.get("extra_env", {}).get("OLLAMA_MODEL", ""),
            cost_per_hour_usd=float(instance.get("dph_total", 0)),
            status=status,
            endpoint_url=endpoint,
        )

    async def list_gpus(self) -> list[GpuOffer]:
        data = await self._get(
            "/bundles/",
            params={
                "q": (
                    "reliability>0.95 "
                    "num_gpus=1 "
                    "rentable=True "
                    "rented=False "
                    "type=on-demand "
                    "order=dph_total"
                ),
                "limit": 200,
            },
        )
        offers: list[GpuOffer] = []
        seen: set[str] = set()
        for o in data.get("offers", []):
            gpu_name = o.get("gpu_name", "")
            vram = int(o.get("gpu_ram", 0) // 1024)  # MB → GB
            cost = float(o.get("dph_total", 0))
            offer_id = str(o.get("id", ""))

            if not gpu_name or cost <= 0:
                continue

            # Dedupe by GPU name (keep cheapest for router; individual offers tracked by id)
            key = gpu_name.lower()
            if key in seen:
                continue
            seen.add(key)

            offers.append(
                GpuOffer(
                    provider=self.name,
                    gpu=gpu_name,
                    vram_gb=vram,
                    cost_per_hour_usd=cost,
                    available=True,
                    gpu_type_id=offer_id,  # ask-id used in launch URL
                    metadata={"reliability": o.get("reliability2", 0), "offer_id": offer_id},
                )
            )
        return offers

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _wait_for_running(self, instance_id: str, timeout_sec: int = 120) -> str:
        deadline = asyncio.get_event_loop().time() + timeout_sec
        while asyncio.get_event_loop().time() < deadline:
            try:
                info = await self.get_status(instance_id)
                if info.endpoint_url and info.status in ("ready", "starting"):
                    return info.endpoint_url
            except Exception:
                pass
            await asyncio.sleep(5)
        return ""

    @staticmethod
    def _extract_endpoint(instance: dict) -> str:
        ssh_host = instance.get("ssh_host", "")
        ports: dict = instance.get("ports", {}) or {}
        port_key = f"{OLLAMA_PORT}/tcp"
        mapped = ports.get(port_key)
        if mapped and isinstance(mapped, list) and mapped:
            pub_port = mapped[0].get("HostPort", OLLAMA_PORT)
            if ssh_host:
                return f"http://{ssh_host}:{pub_port}"
        if ssh_host:
            return f"http://{ssh_host}:{OLLAMA_PORT}"
        return ""
