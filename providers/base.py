"""Abstract base class for GPU cloud providers.

Every provider must implement:
  launch(tier)          → PodInfo  (spin up a new pod for the given tier)
  terminate(external_id) → None    (hard-stop a running pod)
  get_status(external_id) → PodInfo (current pod state)
  list_gpus()            → list[GpuOffer]  (available GPU types + spot prices)

launch() must block until the pod has an endpoint_url assigned by the
provider (not necessarily until Ollama is ready — InstanceManager handles
the Ollama readiness probe separately).

All methods may raise ProviderError on transient failures. InstanceManager
will retry via the fallback provider list.

Container strategy
------------------
Every pod launches the official `ollama/ollama` image with an entrypoint
that pre-pulls the tier model before exposing port 11434. Port 11434 is
mapped to port 11434 on the pod's public IP (providers expose this as the
endpoint_url).  Volume mounts are ephemeral — models are pulled at startup
and stored inside the container. This keeps the image generic and avoids
custom image management per tier.

Startup command:
  /bin/sh -c "ollama serve & sleep 3 && ollama pull $OLLAMA_MODEL && wait"
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ProviderError(Exception):
    """Raised on provider API failure (transient or capacity)."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class PodInfo:
    """Canonical pod descriptor returned by all provider methods."""

    external_id: str
    provider: str
    gpu: str                     # human-readable GPU name, e.g. "RTX 4090"
    model: str                   # Ollama model tag pre-pulled in container
    cost_per_hour_usd: float
    status: str                  # provisioning | starting | ready | terminated | failed
    endpoint_url: str = ""       # http://<ip>:11434 — empty until assigned
    metadata: dict = field(default_factory=dict)


@dataclass
class GpuOffer:
    """One available GPU type / instance from a provider."""

    provider: str
    gpu: str
    vram_gb: int
    cost_per_hour_usd: float
    available: bool
    gpu_type_id: str             # provider-internal identifier used in launch()
    metadata: dict = field(default_factory=dict)


# Tier → minimum VRAM requirements (GB).  Provider implementations use
# these to filter offers; the first offer meeting VRAM ≥ required is used.
TIER_VRAM_REQUIRED: dict[str, int] = {
    "simple": 8,          # 7B q4 ≈ 4 GB; 8 GB floor gives headroom
    "architecture": 20,   # 32B q4 ≈ 18 GB
    "maximum": 38,        # deepseek-v3 q4 ≈ 36 GB
    "ultra": 50,          # 72B q4 ≈ 48 GB
}

# Tier → preferred GPU type names (matched against provider GPU names,
# case-insensitive substring).  First match wins.
TIER_GPU_PREFERENCE: dict[str, list[str]] = {
    "simple":       ["4090", "3090", "a40", "a6000"],
    "architecture": ["4090", "3090", "a40", "a6000"],
    "maximum":      ["l40s", "l40", "a40", "a100 40", "a100-40"],
    "ultra":        ["a100 80", "a100-80", "a100 sxm4 80", "h100"],
}

# Ollama model tag per tier
TIER_MODEL: dict[str, str] = {
    "simple":       "qwen2.5-coder:7b-instruct-q4_K_M",
    "architecture": "qwen2.5-coder:32b-instruct-q4_K_M",
    "maximum":      "deepseek-v3:latest-q4_K_M",
    "ultra":        "qwen2.5:72b-instruct-q4_K_M",
}

OLLAMA_PORT = 11434

CONTAINER_STARTUP_CMD = (
    '/bin/sh -c "'
    "ollama serve & sleep 5 && "
    "ollama pull $OLLAMA_MODEL && "
    'wait"'
)


class BaseProvider(ABC):
    """Abstract GPU provider adapter."""

    name: str = ""

    @abstractmethod
    async def launch(self, tier: str) -> PodInfo:
        """Provision a new pod for *tier*.  Block until endpoint_url is assigned."""
        ...

    @abstractmethod
    async def terminate(self, external_id: str) -> None:
        """Hard-stop and delete the pod identified by *external_id*."""
        ...

    @abstractmethod
    async def get_status(self, external_id: str) -> PodInfo:
        """Return current state of the pod."""
        ...

    @abstractmethod
    async def list_gpus(self) -> list[GpuOffer]:
        """Return available GPU types with current pricing."""
        ...

    # ------------------------------------------------------------------
    # Shared helpers available to all subclasses
    # ------------------------------------------------------------------

    def _select_gpu_offer(self, tier: str, offers: list[GpuOffer]) -> GpuOffer:
        """Pick cheapest available offer matching tier VRAM + GPU preferences."""
        min_vram = TIER_VRAM_REQUIRED.get(tier, 8)
        prefs = TIER_GPU_PREFERENCE.get(tier, [])

        candidates = [o for o in offers if o.available and o.vram_gb >= min_vram]
        if not candidates:
            raise ProviderError(
                f"{self.name}: no available GPU with ≥{min_vram} GB VRAM for tier '{tier}'",
                retryable=True,
            )

        # Try preference order first
        for pref in prefs:
            pref_lower = pref.lower()
            match = next((o for o in candidates if pref_lower in o.gpu.lower()), None)
            if match:
                return match

        # Fall back to cheapest candidate
        return min(candidates, key=lambda o: o.cost_per_hour_usd)
