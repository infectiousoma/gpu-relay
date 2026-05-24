"""OpenAI-compatible commercial API providers.

These providers skip pod lifecycle entirely (no launching, no readiness probe,
no model pull). Requests are forwarded directly to the commercial API with an
Authorization header injected.

All providers here use the OpenAI chat completions wire format, so no payload
translation is needed. Anthropic requires format translation and is not yet
supported.

Tier → model mapping per provider. Override via env vars if needed:
  OPENAI_MODEL_SIMPLE, OPENAI_MODEL_ARCHITECTURE, OPENAI_MODEL_MAXIMUM, OPENAI_MODEL_ULTRA
  GROQ_MODEL_SIMPLE, etc.

Supported providers (set PROVIDER_PRIORITY to include them):
  openai    — OpenAI API   (OPENAI_API_KEY)
  groq      — Groq API     (GROQ_API_KEY)
  together  — Together AI  (TOGETHER_API_KEY)
  mistral   — Mistral AI   (MISTRAL_API_KEY)
  deepseek  — DeepSeek     (DEEPSEEK_API_KEY)
"""

from __future__ import annotations

import structlog

from bridge.settings import settings
from providers.base import BaseProvider, GpuOffer, PodInfo, TIER_MODEL

log = structlog.get_logger(__name__)


class OpenAICompatProvider(BaseProvider):
    """Base for any OpenAI-compatible commercial API."""

    provider_type = "api"
    needs_ollama_check = False

    _base_url: str = ""
    _default_models: dict[str, str] = {}

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def extra_request_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _model_for(self, tier: str) -> str:
        return self._default_models.get(tier, self._default_models.get("simple", ""))

    async def launch(self, tier: str, user_label: str | None = None) -> PodInfo:
        model = self._model_for(tier)
        log.info("api_provider_launch", provider=self.name, tier=tier, model=model)
        return PodInfo(
            external_id=f"{self.name}-{tier}",
            provider=self.name,
            gpu="api",
            model=model,
            cost_per_hour_usd=0.0,
            status="ready",
            endpoint_url=self._base_url,
        )

    async def terminate(self, external_id: str) -> None:
        pass

    async def get_status(self, external_id: str) -> PodInfo:
        tier = external_id.split("-", 1)[-1] if "-" in external_id else "simple"
        return PodInfo(
            external_id=external_id,
            provider=self.name,
            gpu="api",
            model=self._model_for(tier),
            cost_per_hour_usd=0.0,
            status="ready",
            endpoint_url=self._base_url,
        )

    async def list_gpus(self) -> list[GpuOffer]:
        return []


class OpenAIProvider(OpenAICompatProvider):
    name = "openai"
    _base_url = "https://api.openai.com"
    _default_models = {
        "simple":       "gpt-4o-mini",
        "architecture": "gpt-4o",
        "maximum":      "gpt-4o",
        "ultra":        "gpt-4o",
    }

    def __init__(self) -> None:
        super().__init__(settings.openai_api_key)
        if settings.openai_model_simple:
            self._default_models["simple"] = settings.openai_model_simple
        if settings.openai_model_architecture:
            self._default_models["architecture"] = settings.openai_model_architecture
        if settings.openai_model_maximum:
            self._default_models["maximum"] = settings.openai_model_maximum
        if settings.openai_model_ultra:
            self._default_models["ultra"] = settings.openai_model_ultra


class GroqProvider(OpenAICompatProvider):
    name = "groq"
    _base_url = "https://api.groq.com/openai"
    _default_models = {
        "vision":       "meta-llama/llama-4-scout-17b-16e-instruct",
        "simple":       "llama-3.1-8b-instant",
        "architecture": "llama-3.3-70b-versatile",
        "maximum":      "llama-3.3-70b-versatile",
        "ultra":        "llama-3.3-70b-versatile",
    }

    def __init__(self) -> None:
        super().__init__(settings.groq_api_key)


class TogetherProvider(OpenAICompatProvider):
    name = "together"
    _base_url = "https://api.together.xyz"
    _default_models = {
        "vision":       "meta-llama/Llama-Vision-Free",
        "simple":       "meta-llama/Llama-3.1-8B-Instruct-Turbo",
        "architecture": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        "maximum":      "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
        "ultra":        "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
    }

    def __init__(self) -> None:
        super().__init__(settings.together_api_key)


class MistralProvider(OpenAICompatProvider):
    name = "mistral"
    _base_url = "https://api.mistral.ai"
    _default_models = {
        "simple":       "mistral-small-latest",
        "architecture": "mistral-medium-latest",
        "maximum":      "mistral-large-latest",
        "ultra":        "mistral-large-latest",
    }

    def __init__(self) -> None:
        super().__init__(settings.mistral_api_key)


class DeepSeekProvider(OpenAICompatProvider):
    name = "deepseek"
    _base_url = "https://api.deepseek.com"
    _default_models = {
        "simple":       "deepseek-chat",
        "architecture": "deepseek-chat",
        "maximum":      "deepseek-reasoner",
        "ultra":        "deepseek-reasoner",
    }

    def __init__(self) -> None:
        super().__init__(settings.deepseek_api_key)
