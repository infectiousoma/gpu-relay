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
    VolumeValidationError,
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

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict:
        return {"Content-Type": "application/json"}

    def _url(self) -> str:
        return f"{_GQL_URL}?api_key={self._api_key}"

    async def _gql(self, query: str, variables: dict | None = None, api_key: str | None = None) -> dict:
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        url = f"{_GQL_URL}?api_key={api_key if api_key is not None else self._api_key}"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json=payload, headers=self._headers())
            if r.is_error:
                try:
                    body = r.json()
                    detail = "; ".join(e.get("message", str(e)) for e in body.get("errors", [])) or r.text[:200]
                except Exception:
                    detail = r.text[:200]
                raise ProviderError(
                    f"RunPod API HTTP {r.status_code}: {detail}",
                    retryable=r.status_code >= 500,
                )
            data = r.json()
        if "errors" in data:
            msgs = [e.get("message", str(e)) for e in data["errors"]]
            unique_msgs = list(dict.fromkeys(msgs))  # deduplicate while preserving order
            combined = "; ".join(unique_msgs)
            # "Something went wrong. Please try again later" = transient API issue
            retryable = any(
                "try again" in m.lower() or "something went wrong" in m.lower()
                for m in unique_msgs
            )
            raise ProviderError(f"RunPod GQL error: {combined}", retryable=retryable)
        return data.get("data", {})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def launch(
        self,
        tier: str,
        user_label: str | None = None,
        volume_id: str | None = None,
        volume_api_key: str | None = None,
        volume_datacenter: str | None = None,
    ) -> PodInfo:
        if not (volume_api_key or self._api_key):
            raise ProviderError("RUNPOD_API_KEY not set", retryable=False)

        offers = await self.list_gpus()
        candidates = self._rank_gpu_offers(tier, offers)
        model = TIER_MODEL[tier]

        mutation = """
        mutation ($input: PodFindAndDeployOnDemandInput!) {
            podFindAndDeployOnDemand(input: $input) {
                id
                name
                desiredStatus
                imageName
                env
                machine { podHostId gpuDisplayName costPerHr }
            }
        }
        """
        # User-supplied volume takes precedence over global env volume.
        effective_volume_id = volume_id or settings.runpod_network_volume_id
        if effective_volume_id:
            await self._validate_network_volume(
                effective_volume_id,
                api_key=volume_api_key,
                expected_datacenter=volume_datacenter,
            )
        env = [{"key": "OLLAMA_MODEL", "value": model}]
        if effective_volume_id:
            env.append({"key": "OLLAMA_MODELS", "value": "/runpod-volume/ollama"})

        last_error: Exception | None = None
        base_inp: dict = {
            "gpuCount": 1,
            "containerDiskInGb": 20,
            "volumeInGb": 50 if effective_volume_id else 0,
            "minVcpuCount": 2,
            "minMemoryInGb": 16,
            "imageName": "ollama/ollama:latest",
            "env": env,
            "ports": "11434/http",
            "name": f"llm-{tier}-{user_label}" if user_label else f"llm-{tier}",
            "supportPublicIp": True,
        }
        if effective_volume_id:
            base_inp["networkVolumeId"] = effective_volume_id
            base_inp["volumeMountPath"] = "/runpod-volume"
        if volume_datacenter:
            base_inp["dataCenterId"] = volume_datacenter

        # Try SECURE first, then COMMUNITY as fallback when DC capacity is low.
        pod_data: dict | None = None
        for cloud_type in ("SECURE", "COMMUNITY"):
            for offer in candidates:
                inp = {**base_inp, "cloudType": cloud_type, "gpuTypeId": offer.gpu_type_id}
                try:
                    data = await self._gql(mutation, {"input": inp}, api_key=volume_api_key)
                    pod_data = data.get("podFindAndDeployOnDemand")
                    if not pod_data:
                        raise ProviderError("RunPod returned empty pod response", retryable=True)
                except ProviderError as exc:
                    if "no longer any instances available" in str(exc).lower() or "no instances available" in str(exc).lower():
                        log.warning("runpod_gpu_no_capacity", gpu=offer.gpu, tier=tier, cloud=cloud_type)
                        last_error = exc
                        continue
                    raise
                if cloud_type == "COMMUNITY":
                    log.info("runpod_community_fallback", gpu=offer.gpu, tier=tier)
                break  # offer succeeded — break inner loop
            else:
                continue  # all offers for this cloud_type exhausted, try next
            break  # pod launched — break outer loop

        if not pod_data:
            raise ProviderError(
                f"RunPod: no capacity across {len(candidates)} GPU type(s) for tier '{tier}' "
                f"on SECURE or COMMUNITY cloud: {last_error}",
                retryable=True,
            )

        external_id = pod_data["id"]
        cost = float(pod_data.get("machine", {}).get("costPerHr", offer.cost_per_hour_usd))
        log.info("runpod_pod_launched", external_id=external_id, tier=tier, gpu=offer.gpu)

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
        for attempt in range(3):
            try:
                await self._gql(mutation, {"input": {"podId": external_id}})
                log.info("runpod_pod_terminated", external_id=external_id, attempt=attempt + 1)
                return
            except Exception as exc:
                if attempt < 2:
                    wait = 4 * (attempt + 1)
                    log.warning(
                        "runpod_terminate_retry",
                        external_id=external_id,
                        attempt=attempt + 1,
                        retry_in=wait,
                        error=str(exc),
                    )
                    await asyncio.sleep(wait)
                else:
                    log.error(
                        "runpod_terminate_failed",
                        external_id=external_id,
                        error=str(exc),
                        msg="Pod may still be running on RunPod — terminate manually",
                    )

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
            }
        }
        """
        data = await self._gql(query)
        offers: list[GpuOffer] = []
        for g in data.get("gpuTypes", []):
            price = g.get("securePrice") or g.get("communityPrice") or 0
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

    async def _validate_network_volume(
        self,
        volume_id: str,
        api_key: str | None = None,
        expected_datacenter: str | None = None,
    ) -> None:
        data = await self._gql(
            "{ myself { networkVolumes { id dataCenterId } } }",
            api_key=api_key,
        )
        volumes = data.get("myself", {}).get("networkVolumes", [])
        vol = next((v for v in volumes if v["id"] == volume_id), None)
        if vol is None:
            ids = [v["id"] for v in volumes]
            raise VolumeValidationError(
                f"RunPod network volume '{volume_id}' not found on account "
                f"(found: {ids or 'none'}). "
                "Create one at runpod.io/console/user/storage or update your volume key."
            )
        if expected_datacenter:
            actual_dc = vol.get("dataCenterId", "")
            if actual_dc != expected_datacenter:
                raise VolumeValidationError(
                    f"RunPod volume '{volume_id}' is in datacenter '{actual_dc}', "
                    f"but volume key specifies '{expected_datacenter}'. "
                    "Update your volume key's datacenter field to match."
                )

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
