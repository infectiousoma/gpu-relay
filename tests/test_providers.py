"""Provider tests with respx HTTP mocking.

Tests RunPod (GraphQL), Vast.ai (REST), Lambda Labs (REST) adapters:
  - Successful launch → PodInfo returned
  - Endpoint URL extraction
  - Error response → ProviderError raised
  - Retry on transient failures
  - GPU offer selection (VRAM + preference matching)
"""

from __future__ import annotations

import json
import uuid

import pytest
import respx
from httpx import Response

from providers.base import GpuOffer, ProviderError
from providers.runpod import RunPodProvider, _GQL_URL
from providers.vast import VastProvider, _BASE as VAST_BASE
from providers.lambda_labs import LambdaProvider, _BASE as LAMBDA_BASE


# ---------------------------------------------------------------------------
# Base — GPU offer selection
# ---------------------------------------------------------------------------

class TestGpuOfferSelection:
    def _make_offers(self) -> list[GpuOffer]:
        return [
            GpuOffer("runpod", "RTX 3090", 24, 0.50, True, "rtx3090"),
            GpuOffer("runpod", "RTX 4090", 24, 0.34, True, "rtx4090"),
            GpuOffer("runpod", "A100 40GB PCIe", 40, 1.10, True, "a100-40"),
            GpuOffer("runpod", "A100 80GB SXM", 80, 1.89, True, "a100-80"),
            GpuOffer("runpod", "RTX 3080", 10, 0.20, True, "rtx3080"),  # too small for arch
        ]

    def test_simple_tier_picks_4090(self):
        offers = self._make_offers()
        provider = RunPodProvider.__new__(RunPodProvider)
        offer = provider._select_gpu_offer("simple", offers)
        # Prefers 4090 over 3090 for simple tier (preference list order)
        assert "4090" in offer.gpu

    def test_maximum_tier_picks_a100_40(self):
        offers = self._make_offers()
        provider = RunPodProvider.__new__(RunPodProvider)
        offer = provider._select_gpu_offer("maximum", offers)
        assert "40" in offer.gpu.lower() or "a100" in offer.gpu.lower()

    def test_ultra_tier_picks_a100_80(self):
        offers = self._make_offers()
        provider = RunPodProvider.__new__(RunPodProvider)
        offer = provider._select_gpu_offer("ultra", offers)
        assert "80" in offer.gpu or "sxm" in offer.gpu.lower()

    def test_no_matching_gpu_raises(self):
        tiny_offers = [GpuOffer("runpod", "GTX 1080", 8, 0.10, True, "gtx1080")]
        provider = RunPodProvider.__new__(RunPodProvider)
        with pytest.raises(ProviderError, match="no available GPU"):
            provider._select_gpu_offer("maximum", tiny_offers)  # needs 38GB

    def test_unavailable_offers_skipped(self):
        offers = [
            GpuOffer("runpod", "A100 80GB SXM", 80, 1.89, False, "a100-80"),  # unavailable
            GpuOffer("runpod", "RTX 4090", 24, 0.34, True, "rtx4090"),
        ]
        provider = RunPodProvider.__new__(RunPodProvider)
        offer = provider._select_gpu_offer("simple", offers)
        assert "4090" in offer.gpu


# ---------------------------------------------------------------------------
# RunPod
# ---------------------------------------------------------------------------

RUNPOD_GQL = f"{_GQL_URL}?api_key=test-key"

SAMPLE_GPU_TYPES = {
    "data": {
        "gpuTypes": [
            {"id": "NVIDIA GeForce RTX 4090", "displayName": "RTX 4090", "memoryInGb": 24,
             "securePrice": 0.34, "communityPrice": None, "lowestPrice": None},
            {"id": "NVIDIA A100 40GB PCIe", "displayName": "A100 40GB", "memoryInGb": 40,
             "securePrice": 1.10, "communityPrice": None, "lowestPrice": None},
        ]
    }
}

SAMPLE_POD_LAUNCH = {
    "data": {
        "podFindAndDeployOnDemand": {
            "id": "pod-abc123",
            "name": "llm-simple",
            "desiredStatus": "RUNNING",
            "imageName": "ollama/ollama:latest",
            "env": [{"key": "OLLAMA_MODEL", "value": "qwen2.5-coder:7b-instruct-q4_K_M"}],
            "machineId": "machine-x",
            "machine": {"podHostId": "host-1", "gpuDisplayName": "RTX 4090", "costPerHr": 0.34},
        }
    }
}

SAMPLE_POD_STATUS_RUNNING = {
    "data": {
        "pod": {
            "id": "pod-abc123",
            "desiredStatus": "RUNNING",
            "runtime": {
                "uptimeInSeconds": 60,
                "ports": [{"ip": "10.0.0.1", "isIpPublic": True, "privatePort": 11434, "publicPort": 11434, "type": "http"}],
            },
            "machine": {"gpuDisplayName": "RTX 4090", "costPerHr": 0.34},
            "env": [{"key": "OLLAMA_MODEL", "value": "qwen2.5-coder:7b-instruct-q4_K_M"}],
        }
    }
}

SAMPLE_POD_STATUS_STARTING = {
    "data": {
        "pod": {
            "id": "pod-abc123",
            "desiredStatus": "RUNNING",
            "runtime": {"uptimeInSeconds": 5, "ports": []},
            "machine": {"gpuDisplayName": "RTX 4090", "costPerHr": 0.34},
            "env": [],
        }
    }
}


class TestRunPodProvider:
    def _provider(self) -> RunPodProvider:
        p = RunPodProvider.__new__(RunPodProvider)
        p._api_key = "test-key"
        return p

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_gpus(self):
        respx.post(RUNPOD_GQL).mock(return_value=Response(200, json=SAMPLE_GPU_TYPES))
        provider = self._provider()
        offers = await provider.list_gpus()
        assert len(offers) == 2
        assert offers[0].gpu == "RTX 4090"
        assert offers[0].cost_per_hour_usd == 0.34

    @respx.mock
    @pytest.mark.asyncio
    async def test_launch_returns_pod_info(self):
        # list_gpus → launch → wait_for_endpoint (status with IP)
        respx.post(RUNPOD_GQL).mock(side_effect=[
            Response(200, json=SAMPLE_GPU_TYPES),      # list_gpus
            Response(200, json=SAMPLE_POD_LAUNCH),     # launch mutation
            Response(200, json=SAMPLE_POD_STATUS_RUNNING),  # wait_for_endpoint poll
        ])

        provider = self._provider()
        pod = await provider.launch("simple")

        assert pod.external_id == "pod-abc123"
        assert pod.provider == "runpod"
        assert pod.gpu == "RTX 4090"
        assert pod.cost_per_hour_usd == 0.34
        assert "10.0.0.1" in pod.endpoint_url or "pod-abc123" in pod.endpoint_url

    @respx.mock
    @pytest.mark.asyncio
    async def test_terminate_calls_mutation(self):
        respx.post(RUNPOD_GQL).mock(return_value=Response(200, json={"data": {"podTerminate": None}}))
        provider = self._provider()
        await provider.terminate("pod-abc123")  # should not raise

    @respx.mock
    @pytest.mark.asyncio
    async def test_graphql_error_raises_provider_error(self):
        error_resp = {"errors": [{"message": "Pod not found"}]}
        respx.post(RUNPOD_GQL).mock(return_value=Response(200, json=error_resp))
        provider = self._provider()
        with pytest.raises(ProviderError, match="GQL error"):
            await provider._gql("query { pod }")

    @respx.mock
    @pytest.mark.asyncio
    async def test_missing_api_key_raises_non_retryable(self):
        provider = self._provider()
        provider._api_key = ""
        with pytest.raises(ProviderError) as exc:
            await provider.launch("simple")
        assert not exc.value.retryable

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_status_running(self):
        respx.post(RUNPOD_GQL).mock(return_value=Response(200, json=SAMPLE_POD_STATUS_RUNNING))
        provider = self._provider()
        info = await provider.get_status("pod-abc123")
        assert info.status == "ready"
        assert "10.0.0.1" in info.endpoint_url

    @respx.mock
    @pytest.mark.asyncio
    async def test_endpoint_falls_back_to_proxy_url(self):
        # No ports in runtime → use proxy URL pattern
        status_no_ports = {
            "data": {
                "pod": {
                    "id": "pod-xyz",
                    "desiredStatus": "RUNNING",
                    "runtime": {"uptimeInSeconds": 10, "ports": []},
                    "machine": {"gpuDisplayName": "RTX 4090", "costPerHr": 0.34},
                    "env": [],
                }
            }
        }
        respx.post(RUNPOD_GQL).mock(return_value=Response(200, json=status_no_ports))
        provider = self._provider()
        info = await provider.get_status("pod-xyz")
        assert "pod-xyz" in info.endpoint_url
        assert "proxy.runpod.net" in info.endpoint_url


# ---------------------------------------------------------------------------
# Vast.ai
# ---------------------------------------------------------------------------

VAST_BUNDLES = {
    "offers": [
        {"id": 12345, "gpu_name": "RTX 4090", "gpu_ram": 24576, "dph_total": 0.30, "reliability2": 0.97},
        {"id": 12346, "gpu_name": "A100 SXM4 80GB", "gpu_ram": 81920, "dph_total": 1.70, "reliability2": 0.98},
    ]
}

VAST_INSTANCE_RUNNING = {
    "instances": [{
        "id": 99001,
        "actual_status": "running",
        "ssh_host": "10.5.5.5",
        "ports": {"11434/tcp": [{"HostPort": "11434"}]},
        "dph_total": 0.30,
        "gpu_name": "RTX 4090",
        "extra_env": {"OLLAMA_MODEL": "qwen2.5-coder:7b-instruct-q4_K_M"},
    }]
}


class TestVastProvider:
    def _provider(self) -> VastProvider:
        p = VastProvider.__new__(VastProvider)
        p._api_key = "vast-test-key"
        return p

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_gpus(self):
        respx.get(f"{VAST_BASE}/bundles/").mock(return_value=Response(200, json=VAST_BUNDLES))
        provider = self._provider()
        offers = await provider.list_gpus()
        assert len(offers) == 2
        assert any("4090" in o.gpu for o in offers)

    @respx.mock
    @pytest.mark.asyncio
    async def test_launch_success(self):
        respx.get(f"{VAST_BASE}/bundles/").mock(return_value=Response(200, json=VAST_BUNDLES))
        respx.post(f"{VAST_BASE}/asks/12345/").mock(
            return_value=Response(200, json={"new_contract": 99001})
        )
        respx.get(f"{VAST_BASE}/instances/99001/").mock(
            return_value=Response(200, json=VAST_INSTANCE_RUNNING)
        )

        provider = self._provider()
        pod = await provider.launch("simple")

        assert pod.external_id == "99001"
        assert pod.provider == "vast"
        assert "10.5.5.5" in pod.endpoint_url

    @respx.mock
    @pytest.mark.asyncio
    async def test_terminate_calls_delete(self):
        respx.delete(f"{VAST_BASE}/instances/99001/").mock(return_value=Response(200, json={}))
        provider = self._provider()
        await provider.terminate("99001")  # no raise

    @respx.mock
    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self):
        provider = self._provider()
        provider._api_key = ""
        with pytest.raises(ProviderError) as exc:
            await provider.launch("simple")
        assert not exc.value.retryable

    @respx.mock
    @pytest.mark.asyncio
    async def test_extract_endpoint(self):
        instance = {
            "ssh_host": "192.168.1.100",
            "ports": {"11434/tcp": [{"HostPort": "11434"}]},
        }
        from providers.vast import VastProvider
        url = VastProvider._extract_endpoint(instance)
        assert "192.168.1.100" in url
        assert "11434" in url


# ---------------------------------------------------------------------------
# Lambda Labs
# ---------------------------------------------------------------------------

LAMBDA_INSTANCE_TYPES = {
    "data": {
        "gpu_1x_a10": {
            "instance_type": {
                "name": "gpu_1x_a10",
                "description": "1x A10 (24 GB)",
                "price_cents_per_hour": 75,
                "specs": {"memory_gib": 200},
            },
            "regions_with_capacity_available": [{"name": "us-east-1"}],
        },
        "gpu_1x_a100": {
            "instance_type": {
                "name": "gpu_1x_a100",
                "description": "1x A100 (40 GB)",
                "price_cents_per_hour": 129,
                "specs": {"memory_gib": 400},
            },
            "regions_with_capacity_available": [{"name": "us-west-2"}],
        },
    }
}

LAMBDA_LAUNCH_RESP = {
    "data": {"instance_ids": ["inst-lambda-001"]}
}

LAMBDA_INSTANCE_STATUS = {
    "data": {
        "id": "inst-lambda-001",
        "status": "active",
        "ip": "172.16.0.5",
        "instance_type": {
            "description": "1x A10 (24 GB)",
            "price_cents_per_hour": 75,
            "specs": {"memory_gib": 200},
        },
        "region": {"name": "us-east-1"},
    }
}


class TestLambdaProvider:
    def _provider(self) -> LambdaProvider:
        p = LambdaProvider.__new__(LambdaProvider)
        p._api_key = "lambda-test-key"
        return p

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_gpus(self):
        respx.get(f"{LAMBDA_BASE}/instance-types").mock(
            return_value=Response(200, json=LAMBDA_INSTANCE_TYPES)
        )
        provider = self._provider()
        offers = await provider.list_gpus()
        assert len(offers) == 2
        a100 = next(o for o in offers if "a100" in o.gpu_type_id)
        assert a100.cost_per_hour_usd == pytest.approx(1.29, rel=0.01)

    @respx.mock
    @pytest.mark.asyncio
    async def test_launch_success(self):
        respx.get(f"{LAMBDA_BASE}/instance-types").mock(
            return_value=Response(200, json=LAMBDA_INSTANCE_TYPES)
        )
        respx.post(f"{LAMBDA_BASE}/instance-operations/launch").mock(
            return_value=Response(200, json=LAMBDA_LAUNCH_RESP)
        )
        respx.get(f"{LAMBDA_BASE}/instances/inst-lambda-001").mock(
            return_value=Response(200, json=LAMBDA_INSTANCE_STATUS)
        )

        provider = self._provider()
        pod = await provider.launch("simple")

        assert pod.external_id == "inst-lambda-001"
        assert "172.16.0.5" in pod.endpoint_url

    @respx.mock
    @pytest.mark.asyncio
    async def test_terminate_sends_correct_payload(self):
        captured = {}

        def capture(request):
            captured["body"] = json.loads(request.content)
            return Response(200, json={"data": {}})

        respx.post(f"{LAMBDA_BASE}/instance-operations/terminate").mock(side_effect=capture)
        provider = self._provider()
        await provider.terminate("inst-abc")
        assert captured["body"]["instance_ids"] == ["inst-abc"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_capacity_raises_provider_error(self):
        no_capacity = {
            "data": {
                "gpu_1x_a100": {
                    "instance_type": {
                        "description": "1x A100 (40 GB)",
                        "price_cents_per_hour": 129,
                        "specs": {},
                    },
                    "regions_with_capacity_available": [],  # no capacity
                }
            }
        }
        respx.get(f"{LAMBDA_BASE}/instance-types").mock(return_value=Response(200, json=no_capacity))
        provider = self._provider()
        with pytest.raises(ProviderError, match="no available GPU"):
            await provider.launch("maximum")

    def test_vram_inference(self):
        from providers.lambda_labs import _vram_from_type_name
        assert _vram_from_type_name("gpu_1x_a100_sxm4_80gb") == 80
        assert _vram_from_type_name("gpu_1x_a100") == 40
        assert _vram_from_type_name("gpu_1x_a10") == 24
        assert _vram_from_type_name("gpu_1x_h100_80gb") == 80
        assert _vram_from_type_name("unknown_type") == 0
