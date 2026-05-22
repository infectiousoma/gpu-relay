"""Lambda Labs provider — fallback GPU cloud via REST API.

API reference: https://docs.lambdalabs.com/public-cloud/lambda-cloud-api/
Base URL: https://cloud.lambdalabs.com/api/v1/

Lambda Labs workflow:
  1. GET  /instance-types         → available instance types + pricing
  2. POST /instance-operations/launch → start instance(s)
  3. GET  /instances/{id}          → poll until status == "active"
  4. POST /instance-operations/terminate → terminate one or more instances

Lambda instances are SSH-accessible. They don't have a built-in port-mapping
layer like RunPod's proxy. We expose Ollama on port 11434 via the instance's
public IP, which Lambda provides in the `ip` field of the instance object.

Container strategy: Lambda instances run full VMs (Ubuntu), not Docker
containers. We use the `user_data` (cloud-init) script to:
  1. Install Docker + NVIDIA container toolkit
  2. Pull ollama/ollama image
  3. Run it with model pre-pull

This adds ~3-5 minutes to cold start. Lambda is therefore a last-resort
fallback for capacity emergencies, not a primary provider.

GPU → instance type mapping (Lambda Labs naming as of 2026-05):
  simple/architecture: gpu_1x_a10          (24 GB, $0.75/hr) — no 4090 offered
  maximum:             gpu_1x_a100         (40 GB, $1.29/hr)
  ultra:               gpu_1x_a100_sxm4_80gb (80 GB, $1.89/hr) or gpu_8x_a100 (split)
"""

from __future__ import annotations

import asyncio
import textwrap

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
)

log = structlog.get_logger(__name__)

_BASE = "https://cloud.lambdalabs.com/api/v1"
_SSH_KEY_NAME = "llm-infra"  # SSH key must be pre-registered in Lambda Labs dashboard


def _cloud_init(model: str) -> str:
    return textwrap.dedent(f"""\
        #!/bin/bash
        set -e
        # Install NVIDIA Docker support if not present
        if ! command -v docker &>/dev/null; then
            curl -fsSL https://get.docker.com | sh
        fi
        distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
        curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \\
            tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
        apt-get update -qq && apt-get install -y -qq nvidia-container-toolkit
        nvidia-ctk runtime configure --runtime=docker
        systemctl restart docker
        # Pull and run Ollama with model pre-loaded
        docker run -d \\
            --gpus all \\
            --name ollama \\
            -v ollama:/root/.ollama \\
            -p {OLLAMA_PORT}:{OLLAMA_PORT} \\
            -e OLLAMA_MODEL={model} \\
            ollama/ollama:latest
        sleep 5
        docker exec ollama ollama pull {model}
    """)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ProviderError):
        return exc.retryable
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


class LambdaProvider(BaseProvider):
    name = "lambda"

    def __init__(self) -> None:
        self._api_key = settings.lambda_api_key

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _auth(self) -> tuple[str, str]:
        return (self._api_key, "")  # Lambda uses HTTP Basic with key as username

    async def _get(self, path: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{_BASE}{path}", auth=self._auth())
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{_BASE}{path}", auth=self._auth(), json=body)
            r.raise_for_status()
            return r.json()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=60),
        reraise=True,
    )
    async def launch(self, tier: str) -> PodInfo:
        if not self._api_key:
            raise ProviderError("LAMBDA_API_KEY not set", retryable=False)

        offers = await self.list_gpus()
        offer = self._select_gpu_offer(tier, offers)
        model = TIER_MODEL[tier]

        body = {
            "region_name": offer.metadata.get("region", "us-east-1"),
            "instance_type_name": offer.gpu_type_id,
            "ssh_key_names": [_SSH_KEY_NAME],
            "quantity": 1,
            "name": f"llm-{tier}",
            "user_data": _cloud_init(model),
        }

        data = await self._post("/instance-operations/launch", body)
        instance_ids = data.get("data", {}).get("instance_ids", [])
        if not instance_ids:
            raise ProviderError(f"Lambda launch returned no instance IDs: {data}", retryable=True)

        external_id = instance_ids[0]
        log.info("lambda_instance_launched", external_id=external_id, tier=tier, gpu=offer.gpu)

        # Lambda cold start is long (cloud-init); return endpoint immediately
        # after IP is assigned; InstanceManager does Ollama readiness probe.
        endpoint_url = await self._wait_for_ip(external_id)

        return PodInfo(
            external_id=external_id,
            provider=self.name,
            gpu=offer.gpu,
            model=model,
            cost_per_hour_usd=offer.cost_per_hour_usd,
            status="starting",
            endpoint_url=endpoint_url,
        )

    async def terminate(self, external_id: str) -> None:
        try:
            await self._post("/instance-operations/terminate", {"instance_ids": [external_id]})
            log.info("lambda_instance_terminated", external_id=external_id)
        except Exception as exc:
            log.warning("lambda_terminate_error", external_id=external_id, error=str(exc))

    async def get_status(self, external_id: str) -> PodInfo:
        data = await self._get(f"/instances/{external_id}")
        inst = data.get("data", {})

        status_map = {"active": "ready", "terminated": "terminated", "unhealthy": "failed"}
        raw = inst.get("status", "")
        status = status_map.get(raw, "starting")

        ip = inst.get("ip", "")
        endpoint_url = f"http://{ip}:{OLLAMA_PORT}" if ip else ""

        itype = inst.get("instance_type", {})
        specs = itype.get("specs", {})

        return PodInfo(
            external_id=external_id,
            provider=self.name,
            gpu=itype.get("description", ""),
            model="",  # not tracked in Lambda metadata
            cost_per_hour_usd=float(itype.get("price_cents_per_hour", 0)) / 100,
            status=status,
            endpoint_url=endpoint_url,
            metadata={"region": inst.get("region", {}).get("name", "")},
        )

    async def list_gpus(self) -> list[GpuOffer]:
        data = await self._get("/instance-types")
        offers: list[GpuOffer] = []
        for type_name, info in (data.get("data") or {}).items():
            itype = info.get("instance_type", {})
            specs = itype.get("specs", {})
            regions = info.get("regions_with_capacity_available", [])
            available = len(regions) > 0
            region = regions[0].get("name", "us-east-1") if regions else "us-east-1"

            vram_mb = specs.get("memory_gib", 0) * 1024  # Lambda reports total memory, not VRAM
            # For GPU VRAM: Lambda doesn't expose VRAM separately; infer from type name
            vram_gb = _vram_from_type_name(type_name)
            cost = float(itype.get("price_cents_per_hour", 0)) / 100
            gpu_desc = itype.get("description", type_name)

            offers.append(
                GpuOffer(
                    provider=self.name,
                    gpu=gpu_desc,
                    vram_gb=vram_gb,
                    cost_per_hour_usd=cost,
                    available=available,
                    gpu_type_id=type_name,
                    metadata={"region": region},
                )
            )
        return offers

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _wait_for_ip(self, external_id: str, timeout_sec: int = 300) -> str:
        deadline = asyncio.get_event_loop().time() + timeout_sec
        while asyncio.get_event_loop().time() < deadline:
            try:
                info = await self.get_status(external_id)
                if info.endpoint_url:
                    return info.endpoint_url
            except Exception:
                pass
            await asyncio.sleep(10)
        return ""


def _vram_from_type_name(type_name: str) -> int:
    """Infer VRAM GB from Lambda instance type name (heuristic)."""
    t = type_name.lower()
    if "a100_sxm4_80gb" in t or "80gb" in t:
        return 80
    if "a100" in t and "40gb" in t:
        return 40
    if "a100" in t:
        return 40
    if "h100" in t and "80gb" in t:
        return 80
    if "h100" in t:
        return 80
    if "a10" in t:
        return 24
    if "v100" in t and "16gb" in t:
        return 16
    if "v100" in t:
        return 16
    if "a6000" in t:
        return 48
    return 0
