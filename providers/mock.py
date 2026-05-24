"""Mock provider for local testing (MOCK_PROVIDERS=1).

Returns the local Ollama container as a "pod" — no GPU rental, no billing.
Useful for smoke tests and CI where real provider keys aren't available.
"""

from __future__ import annotations

import os
import uuid

from providers.base import BaseProvider, GpuOffer, PodInfo, TIER_MODEL


class MockProvider(BaseProvider):
    name = "mock"

    def __init__(self) -> None:
        self._ollama_url = os.getenv("OLLAMA_LOCAL_URL", "http://ollama:11434")

    async def launch(self, tier: str, user_label: str | None = None) -> PodInfo:
        return PodInfo(
            external_id=f"mock-{uuid.uuid4().hex[:8]}",
            provider="mock",
            gpu="local-cpu",
            model="qwen2.5-coder:7b-instruct-q4_K_M",  # only model pulled locally
            cost_per_hour_usd=0.34,  # non-zero so cost assertions pass
            status="ready",
            endpoint_url=self._ollama_url,
        )

    async def terminate(self, external_id: str) -> None:
        pass  # nothing to terminate

    async def get_status(self, external_id: str) -> PodInfo:
        return PodInfo(
            external_id=external_id,
            provider="mock",
            gpu="local-cpu",
            model="",
            cost_per_hour_usd=0.0,
            status="ready",
            endpoint_url=self._ollama_url,
        )

    async def list_gpus(self) -> list[GpuOffer]:
        return []
