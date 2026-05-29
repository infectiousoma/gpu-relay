"""Local Ollama provider — routes to the Ollama instance running in the same stack.

Use when the host machine has a GPU and Ollama is serving locally.
No pod lifecycle: launch() returns immediately, terminate() is a no-op.
Model pull still runs on first use (idempotent via Ollama API).

Enable with PROVIDER_PRIORITY=local (or prepend it: local,runpod,vast).

GPU level → allowed inference tiers (set LOCAL_GPU_LEVEL in .env):
  0 — CPU / no GPU  : no inference (preprocess only if LOCAL_PREPROCESS_ENABLED)
  1 — 4–8 GB        : simple
  2 — 8–12 GB       : simple, vision
  3 — 16–24 GB      : simple, vision, architecture        (default; RTX 3090/4090)
  4 — 24–48 GB      : simple, vision, architecture, maximum
  5 — 48+ GB        : all tiers
"""

from __future__ import annotations

import structlog

from bridge.settings import settings
from providers.base import BaseProvider, GpuOffer, PodInfo, ProviderError, TIER_MODEL

log = structlog.get_logger(__name__)

# Tiers each GPU level may serve locally (cumulative).
_LEVEL_TIERS: dict[int, list[str]] = {
    0: [],
    1: ["simple"],
    2: ["simple", "vision"],
    3: ["simple", "vision", "architecture"],
    4: ["simple", "vision", "architecture", "maximum"],
    5: ["simple", "vision", "architecture", "maximum", "ultra"],
}


class LocalProvider(BaseProvider):
    name = "local"
    provider_type = "local"

    def is_configured(self) -> bool:
        return True

    def tiers_allowed(self) -> list[str]:
        return _LEVEL_TIERS.get(settings.local_gpu_level, [])

    async def launch(
        self,
        tier: str,
        user_label: str | None = None,
        volume_id: str | None = None,
        volume_api_key: str | None = None,
        volume_datacenter: str | None = None,
    ) -> PodInfo:
        allowed = self.tiers_allowed()
        if tier not in allowed:
            raise ProviderError(
                f"Local GPU level {settings.local_gpu_level} does not support tier={tier} "
                f"(allowed: {allowed or 'none'})",
                retryable=False,
            )
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
        pass

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
