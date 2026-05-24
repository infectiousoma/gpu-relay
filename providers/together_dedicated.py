"""Together AI dedicated endpoint provider.

Together's Llama vision models are unavailable on the serverless API —
they require a dedicated endpoint (a reserved GPU instance). This provider
manages that lifecycle identically to RunPod:

  launch(tier)        → POST /v1/endpoints, wait for RUNNING
  terminate(id)       → DELETE /v1/endpoints/{id}
  get_status(id)      → GET /v1/endpoints/{id}

Inference uses the standard Together API URL (https://api.together.xyz).
Once a dedicated endpoint is RUNNING for a model, Together routes requests
for that model to the dedicated GPU transparently.

needs_ollama_check = False: launch() handles readiness; instance_manager
skips the /api/tags Ollama probe and model pull for this provider.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import structlog

from bridge.settings import settings
from providers.base import BaseProvider, GpuOffer, PodInfo, ProviderError

log = structlog.get_logger(__name__)

_API_BASE = "https://api.together.xyz/v1"
_INFERENCE_BASE = "https://api.together.xyz"

# After all hardware options fail, skip retries for this many seconds.
_CAPACITY_COOLDOWN_SEC = 600  # 10 minutes
_capacity_miss_at: float = 0.0  # module-level; shared across all launch() calls

# Together hardware IDs (format: {count}x_{gpu_type}_{vram}gb_{link}) → (display, VRAM GB, cost $/hr)
# Verified via GET /v1/hardware and create probes (2026-05).
# Only `model` + `hardware` are accepted in the create body — no other fields.
_HARDWARE: dict[str, tuple[str, int, float]] = {
    "1x_nvidia_l40_48gb_pcie":    ("NVIDIA L40 48GB",       48, 1.49),
    "1x_nvidia_l40s_48gb_pcie":   ("NVIDIA L40S 48GB",      48, 2.10),
    "1x_nvidia_a100_40gb_sxm":    ("NVIDIA A100 40GB SXM",  40, 2.40),
    "1x_nvidia_a100_80gb_pcie":   ("NVIDIA A100 80GB PCIe", 80, 2.40),
    "1x_nvidia_a100_40gb_pcie":   ("NVIDIA A100 40GB PCIe", 40, 3.00),
    "1x_nvidia_a100_80gb_sxm":    ("NVIDIA A100 80GB SXM",  80, 2.59),
    "1x_nvidia_h100_80gb_sxm":    ("NVIDIA H100 80GB SXM",  80, 6.49),
}

# Tier → (hardware preference list in price order, model)
# Vision tiers: simple=8B (cheap), vision=11B (mid), architecture/maximum/ultra=90B (quality).
# Bridge tries each hardware in order; falls through to next on capacity miss.
_TIER_CONFIG: dict[str, tuple[list[str], str]] = {
    "simple": (
        ["1x_nvidia_l40_48gb_pcie", "1x_nvidia_l40s_48gb_pcie",
         "1x_nvidia_a100_40gb_sxm", "1x_nvidia_a100_40gb_pcie"],
        "Qwen/Qwen3-VL-8B-Instruct",
    ),
    "vision": (
        ["1x_nvidia_l40_48gb_pcie", "1x_nvidia_l40s_48gb_pcie",
         "1x_nvidia_a100_40gb_sxm", "1x_nvidia_a100_80gb_pcie",
         "1x_nvidia_a100_40gb_pcie"],
        "meta-llama/Llama-3.2-11B-Vision-Instruct-Turbo",
    ),
    "architecture": (
        ["1x_nvidia_a100_80gb_pcie", "1x_nvidia_a100_80gb_sxm", "1x_nvidia_h100_80gb_sxm"],
        "meta-llama/Llama-3.2-90B-Vision-Instruct-Turbo",
    ),
    "maximum": (
        ["1x_nvidia_a100_80gb_pcie", "1x_nvidia_a100_80gb_sxm", "1x_nvidia_h100_80gb_sxm"],
        "meta-llama/Llama-3.2-90B-Vision-Instruct-Turbo",
    ),
    "ultra": (
        ["1x_nvidia_h100_80gb_sxm", "1x_nvidia_a100_80gb_sxm"],
        "meta-llama/Llama-3.2-90B-Vision-Instruct-Turbo",
    ),
}

_RUNNING_STATES = {"RUNNING", "IDLE"}
_TERMINAL_STATES = {"FAILED", "DELETING", "DELETED"}


class TogetherDedicatedProvider(BaseProvider):
    """Together AI dedicated endpoint — on-demand GPU with full lifecycle."""

    name = "together_dedicated"
    provider_type = "pod"
    needs_ollama_check = False  # launch() handles readiness; skip /api/tags probe

    def __init__(self) -> None:
        self._api_key = settings.together_api_key

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def extra_request_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def launch(self, tier: str, user_label: str | None = None) -> PodInfo:
        global _capacity_miss_at
        cooldown_remaining = _capacity_miss_at + _CAPACITY_COOLDOWN_SEC - time.monotonic()
        if cooldown_remaining > 0:
            log.info(
                "together_dedicated_skipped_cooldown",
                cooldown_remaining_sec=round(cooldown_remaining),
            )
            raise ProviderError(
                f"Together: all hardware failed recently — cooldown {round(cooldown_remaining)}s remaining",
                retryable=True,
            )

        hw_list, model = _TIER_CONFIG.get(tier, _TIER_CONFIG["vision"])

        last_err: str = ""
        async with httpx.AsyncClient(timeout=30) as client:
            for hw_id in hw_list:
                gpu_name, _vram, cost = _HARDWARE[hw_id]
                log.info("together_dedicated_creating", tier=tier, model=model, hardware=hw_id)
                r = await client.post(
                    f"{_API_BASE}/endpoints",
                    json={"model": model, "hardware": hw_id},
                    headers=self._headers(),
                )
                if r.is_success:
                    data = r.json()
                    endpoint_id = data.get("id", "")
                    if not endpoint_id:
                        raise ProviderError(f"Together returned no ID: {data}", retryable=False)
                    log.info("together_dedicated_created", endpoint_id=endpoint_id, hardware=hw_id)
                    await self._wait_for_running(endpoint_id)
                    return PodInfo(
                        external_id=endpoint_id,
                        provider=self.name,
                        gpu=gpu_name,
                        model=model,
                        cost_per_hour_usd=cost,
                        status="ready",
                        endpoint_url=_INFERENCE_BASE,
                    )

                err = r.json().get("error", {}).get("message", r.text[:200])
                last_err = f"{hw_id}: {r.status_code} {err}"
                if "not available for this model" in err:
                    log.debug("together_dedicated_hw_unsupported", hardware=hw_id, model=model)
                    continue
                # "hardware request not available" = capacity miss → try next
                log.warning("together_dedicated_hw_unavailable", hardware=hw_id, error=err)

        _capacity_miss_at = time.monotonic()
        raise ProviderError(
            f"Together: no hardware capacity for tier={tier}. Last: {last_err}",
            retryable=True,
        )

    async def terminate(self, external_id: str) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in range(3):
                try:
                    r = await client.delete(
                        f"{_API_BASE}/endpoints/{external_id}",
                        headers=self._headers(),
                    )
                    if r.is_success or r.status_code == 404:
                        log.info("together_dedicated_deleted", endpoint_id=external_id)
                        return
                    raise ProviderError(f"Delete returned {r.status_code}: {r.text[:200]}")
                except Exception as exc:
                    if attempt < 2:
                        await asyncio.sleep(4 * (attempt + 1))
                    else:
                        log.error(
                            "together_dedicated_terminate_failed",
                            endpoint_id=external_id,
                            error=str(exc),
                            msg="Endpoint may still be running — delete manually at api.together.ai",
                        )

    async def get_status(self, external_id: str) -> PodInfo:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{_API_BASE}/endpoints/{external_id}",
                headers=self._headers(),
            )
            r.raise_for_status()
            data = r.json()

        state = data.get("state", "")
        model = data.get("model", "")
        hw = data.get("hardware", {})
        gpu_name = hw.get("display_name", str(hw)) if isinstance(hw, dict) else str(hw)

        return PodInfo(
            external_id=external_id,
            provider=self.name,
            gpu=gpu_name,
            model=model,
            cost_per_hour_usd=0.0,
            status=_map_state(state),
            endpoint_url=_INFERENCE_BASE,
        )

    async def list_gpus(self) -> list[GpuOffer]:
        return [
            GpuOffer(
                provider=self.name,
                gpu=gpu_name,
                vram_gb=vram,
                cost_per_hour_usd=cost,
                available=True,
                gpu_type_id=hw_id,
            )
            for hw_id, (gpu_name, vram, cost) in _HARDWARE.items()
            if vram <= 80
        ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _wait_for_running(self, endpoint_id: str, timeout_sec: int = 300) -> None:
        """Poll /v1/endpoints/{id} until state is RUNNING."""
        deadline = asyncio.get_event_loop().time() + timeout_sec
        async with httpx.AsyncClient(timeout=15) as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    r = await client.get(
                        f"{_API_BASE}/endpoints/{endpoint_id}",
                        headers=self._headers(),
                    )
                    if r.is_success:
                        state = r.json().get("state", "")
                        log.debug("together_dedicated_poll", endpoint_id=endpoint_id, state=state)
                        if state in _RUNNING_STATES:
                            return
                        if state in _TERMINAL_STATES:
                            raise ProviderError(
                                f"Together endpoint {endpoint_id} entered state {state}",
                                retryable=False,
                            )
                except ProviderError:
                    raise
                except Exception as exc:
                    log.debug("together_dedicated_poll_error", error=str(exc))
                await asyncio.sleep(10)
        raise ProviderError(
            f"Together endpoint {endpoint_id} did not become RUNNING within {timeout_sec}s",
            retryable=True,
        )


def _map_state(state: str) -> str:
    return {
        "RUNNING": "ready",
        "IDLE":    "ready",
        "CREATING": "provisioning",
        "STARTING": "starting",
        "SCALING":  "starting",
        "DELETING": "terminated",
        "DELETED":  "terminated",
        "FAILED":   "failed",
    }.get(state, "starting")
