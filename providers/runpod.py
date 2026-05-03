"""RunPod provider — on-demand GPU pods via GraphQL API.

API reference: https://docs.runpod.io/reference/runpod-apis
Endpoint: https://api.runpod.io/graphql?api_key=<key>

Pod lifecycle on RunPod:
  CREATED → RUNNING (status field in pod query)

We launch a pod with:
  - containerDiskInGb: 100 (enough for model weights + Ollama cache)
  - volumeInGb: 0 (ephemeral; models re-pulled each cold start)
  - dockerArgs: startup command that pulls the model then serves
  - ports: "11434/http" (Ollama API exposed via RunPod's proxy URL)

Endpoint URL format after pod is RUNNING:
  https://<pod_id>-11434.proxy.runpod.net
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
)

log = structlog.get_logger(__name__)

_GQL_URL = "https://api.runpod.io/graphql"

# GPU type IDs from RunPod's catalog (as of 2026-05).
# Query with list_gpus() to refresh.
_GPU_TYPE_IDS: dict[str, str] = {
    "RTX 4090": "NVIDIA GeForce RTX 4090",
    "RTX 3090": "NVIDIA GeForce RTX 3090",
    "A40":      "NVIDIA A40",
    "A100 40GB PCIe": "NVIDIA A100 40GB PCIe",
    "A100 80GB SXM":  "NVIDIA A100-SXM4-80GB",
    "H100 80GB SXM":  "NVIDIA H100 80GB HBM3",
}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ProviderError):
        return exc.retryable
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


class RunPodProvider(BaseProvider):
    name = "runpod"

    def __init__(self) -> None:
        self._api_key = settings.runpod_api_key

    def _headers(self) -> dict:
        return {"Content-Type": "application/json"}

    def _url(self) -> str:
        return f"{_GQL_URL}?api_key={self._api_key}"

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(self._url(), json=payload, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        if "errors" in data:
            msgs = [e.get("message", str(e)) for e in data["errors"]]
            raise ProviderError(f"RunPod GQL error: {'; '.join(msgs)}", retryable=False)
        return data.get("data", {})

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
            raise ProviderError("RUNPOD_API_KEY not set", retryable=False)

        offers = await self.list_gpus()
        offer = self._select_gpu_offer(tier, offers)
        model = TIER_MODEL[tier]

        mutation = """
        mutation ($input: PodFindAndDeployOnDemandInput!) {
            podFindAndDeployOnDemand(input: $input) {
                id
                name
                desiredStatus
                imageName
                env
                machineId
                machine { podHostId gpuDisplayName costPerHr }
            }
        }
        """
        variables = {
            "input": {
                "cloudType": "SECURE",
                "gpuCount": 1,
                "gpuTypeId": offer.gpu_type_id,
                "containerDiskInGb": 100,
                "volumeInGb": 0,
                "minVcpuCount": 2,
                "minMemoryInGb": 16,
                "imageName": "ollama/ollama:latest",
                "dockerArgs": f'/bin/sh -c "ollama serve & sleep 5 && ollama pull {model} && wait"',
                "env": [{"key": "OLLAMA_MODEL", "value": model}],
                "ports": "11434/http",
                "name": f"llm-{tier}",
                "supportPublicIp": True,
            }
        }

        data = await self._gql(mutation, variables)
        pod_data = data.get("podFindAndDeployOnDemand")
        if not pod_data:
            raise ProviderError("RunPod returned empty pod response", retryable=True)

        external_id = pod_data["id"]
        cost = float(pod_data.get("machine", {}).get("costPerHr", offer.cost_per_hour_usd))

        log.info("runpod_pod_launched", external_id=external_id, tier=tier, gpu=offer.gpu)

        # Poll until RUNNING and proxy URL available
        endpoint_url = await self._wait_for_endpoint(external_id)

        return PodInfo(
            external_id=external_id,
            provider=self.name,
            gpu=offer.gpu,
            model=model,
            cost_per_hour_usd=cost,
            status="starting",
            endpoint_url=endpoint_url,
        )

    async def terminate(self, external_id: str) -> None:
        mutation = """
        mutation ($input: PodTerminateInput!) {
            podTerminate(input: $input)
        }
        """
        try:
            await self._gql(mutation, {"input": {"podId": external_id}})
            log.info("runpod_pod_terminated", external_id=external_id)
        except Exception as exc:
            log.warning("runpod_terminate_error", external_id=external_id, error=str(exc))

    async def get_status(self, external_id: str) -> PodInfo:
        query = """
        query ($podId: String!) {
            pod(input: { podId: $podId }) {
                id
                desiredStatus
                runtime { uptimeInSeconds ports { ip isIpPublic privatePort publicPort type } }
                machine { gpuDisplayName costPerHr }
                env
            }
        }
        """
        data = await self._gql(query, {"podId": external_id})
        pod = data.get("pod")
        if not pod:
            raise ProviderError(f"RunPod pod {external_id} not found", retryable=False)

        status_map = {"RUNNING": "ready", "EXITED": "terminated", "FAILED": "failed"}
        raw_status = pod.get("desiredStatus", "")
        status = status_map.get(raw_status, "starting")
        endpoint_url = self._extract_endpoint(pod)
        model = next((e["value"] for e in (pod.get("env") or []) if e.get("key") == "OLLAMA_MODEL"), "")

        return PodInfo(
            external_id=external_id,
            provider=self.name,
            gpu=pod.get("machine", {}).get("gpuDisplayName", ""),
            model=model,
            cost_per_hour_usd=float(pod.get("machine", {}).get("costPerHr", 0)),
            status=status,
            endpoint_url=endpoint_url,
        )

    async def list_gpus(self) -> list[GpuOffer]:
        query = """
        {
            gpuTypes {
                id
                displayName
                memoryInGb
                securePrice
                communityPrice
                lowestPrice { minimumBidPrice uninterruptablePrice }
            }
        }
        """
        data = await self._gql(query)
        offers: list[GpuOffer] = []
        for g in data.get("gpuTypes", []):
            price = g.get("securePrice") or g.get("communityPrice") or 0
            if price == 0:
                # Try lowestPrice
                lp = g.get("lowestPrice") or {}
                price = lp.get("uninterruptablePrice") or lp.get("minimumBidPrice") or 0
            offers.append(
                GpuOffer(
                    provider=self.name,
                    gpu=g.get("displayName", g["id"]),
                    vram_gb=int(g.get("memoryInGb", 0)),
                    cost_per_hour_usd=float(price),
                    available=float(price) > 0,
                    gpu_type_id=g["id"],
                )
            )
        return offers

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _wait_for_endpoint(self, external_id: str, timeout_sec: int = 120) -> str:
        deadline = asyncio.get_event_loop().time() + timeout_sec
        while asyncio.get_event_loop().time() < deadline:
            try:
                info = await self.get_status(external_id)
                if info.endpoint_url:
                    return info.endpoint_url
            except Exception:
                pass
            await asyncio.sleep(5)
        # Return proxy URL pattern even if not confirmed — InstanceManager
        # will do the Ollama readiness probe.
        return f"https://{external_id}-{OLLAMA_PORT}.proxy.runpod.net"

    @staticmethod
    def _extract_endpoint(pod: dict) -> str:
        runtime = pod.get("runtime") or {}
        for port in runtime.get("ports") or []:
            if port.get("privatePort") == OLLAMA_PORT and port.get("isIpPublic"):
                ip = port.get("ip", "")
                pub_port = port.get("publicPort", OLLAMA_PORT)
                if ip:
                    return f"http://{ip}:{pub_port}"
        # Fallback to RunPod proxy URL (requires proxy support on the pod)
        pod_id = pod.get("id", "")
        if pod_id:
            return f"https://{pod_id}-{OLLAMA_PORT}.proxy.runpod.net"
        return ""
