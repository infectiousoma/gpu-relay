"""Local Ollama provider — routes to the Ollama instance running in the same stack.

Use when the host machine has a GPU and Ollama is serving locally.
No pod lifecycle: launch() returns immediately, terminate() is a no-op.
Model pull still runs on first use (idempotent via Ollama API).

Enable with PROVIDER_PRIORITY=local (or prepend it: local,runpod,vast).
"""

from __future__ import annotations

import structlog

from bridge.settings import settings
from providers.base import BaseProvider, GpuOffer, PodInfo, TIER_MODEL

log = structlog.get_logger(__name__)


class LocalProvider(BaseProvider):
    name = "local"
    provider_type = "local"

    def is_configured(self) -> bool:
        return True

    async def launch(self, tier: str) -> PodInfo:
        model = TIER_MODEL[tier]
        log.info("local_provider_launch", tier=tier, model=model, endpoint=settings.ollama_local_url)
        return PodInfo(
            external_id=f"local-{tier}",
            provider=self.name,
            gpu="local",
            model=model,
            cost_per_hour_usd=0.0,
            status="ready",
            endpoint_url=settings.ollama_local_url,
        )

    async def terminate(self, external_id: str) -> None:
        pass  # never terminate local Ollama

    async def get_status(self, external_id: str) -> PodInfo:
        tier = external_id.removeprefix("local-")
        return PodInfo(
            external_id=external_id,
            provider=self.name,
            gpu="local",
            model=TIER_MODEL.get(tier, ""),
            cost_per_hour_usd=0.0,
            status="ready",
            endpoint_url=settings.ollama_local_url,
        )

    async def list_gpus(self) -> list[GpuOffer]:
        return []
