"""Centralized settings loaded from environment variables (.env)."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Bridge ---
    bridge_host: str = "0.0.0.0"
    bridge_port: int = 8000
    bridge_secret_key: str = "change-me"
    bridge_log_level: str = "INFO"
    bridge_cors_origins: str = "http://localhost:3000,http://localhost:8501"

    # --- DB ---
    database_url: str = "postgresql+asyncpg://llm:change-me@postgres:5432/llm_infra"

    # --- Redis ---
    redis_url: str = "redis://redis:6379/0"

    # --- Local Ollama (preprocessor) ---
    ollama_local_url: str = "http://ollama:11434"
    ollama_preprocessor_model: str = "qwen2.5-coder:7b-instruct-q4_K_M"
    ollama_embedding_model: str = "nomic-embed-text"

    # --- Providers ---
    provider_priority: str = "runpod,vast,lambda"
    runpod_api_key: str = ""
    runpod_network_volume_id: str = ""  # optional; if set, pods mount it at /runpod-volume
    vast_api_key: str = ""
    lambda_api_key: str = ""
    mock_providers: bool = False  # set MOCK_PROVIDERS=1 to use local Ollama as provider
    pipeline_default: str = "infer"  # global default pipeline; per-user and per-request override this

    # --- Commercial API providers (OpenAI-compatible) ---
    openai_api_key: str = ""
    openai_model_simple: str = ""        # override default gpt-4o-mini
    openai_model_architecture: str = ""  # override default gpt-4o
    openai_model_maximum: str = ""
    openai_model_ultra: str = ""
    groq_api_key: str = ""
    groq_max_output_tokens: int = 6000  # groq free tier TPM = input + max_tokens; cap output to stay under 12K
    together_api_key: str = ""
    mistral_api_key: str = ""
    deepseek_api_key: str = ""
    cerebras_api_key: str = ""
    sambanova_api_key: str = ""

    # --- Pool / reaper ---
    idle_reaper_interval_sec: int = 30
    cold_start_timeout_sec: int = 600
    health_check_interval_sec: int = 30
    health_check_fail_threshold: int = 3

    # --- Billing / quotas ---
    budget_default_usd: Decimal = Decimal("25.00")
    budget_alert_percents: str = "50,80,100"
    rate_limit_rpm_default: int = 60
    tokens_per_day_default: int = 1_000_000
    billing_mode_default: str = "postpaid"

    # --- Local GPU / inference control ---
    # 0=CPU/none 1=4-8GB 2=8-12GB 3=16-24GB(3090/4090) 4=24-48GB(A40/A6000) 5=48+GB(A100/H100)
    local_gpu_level: int = Field(default=3, ge=0, le=5)
    # Whether local Ollama may be used for preprocessing (works even at level 0 with small model)
    local_preprocess_enabled: bool = True

    # --- User provider key encryption ---
    provider_key_secret: str = "change-me-provider-key-secret"  # set PROVIDER_KEY_SECRET in .env

    # --- Tier config ---
    tiers_config_path: Path = REPO_ROOT / "config" / "tiers.yaml"

    # --- Open WebUI bridge link ---
    openwebui_bridge_url: str = "http://bridge:8000"

    @field_validator("mock_providers", mode="before")
    @classmethod
    def _bool_from_empty(cls, v: object) -> object:
        if v == "":
            return False
        return v

    @field_validator("bridge_log_level")
    @classmethod
    def _upper_log(cls, v: str) -> str:
        return v.upper()

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.bridge_cors_origins.split(",") if o.strip()]

    @property
    def provider_priority_list(self) -> list[str]:
        return [p.strip() for p in self.provider_priority.split(",") if p.strip()]

    @property
    def alert_percents_list(self) -> list[int]:
        return [int(x.strip()) for x in self.budget_alert_percents.split(",") if x.strip()]


@lru_cache(maxsize=1)
def _load() -> Settings:
    return Settings()


settings = _load()
