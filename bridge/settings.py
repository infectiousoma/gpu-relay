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

    # --- Providers ---
    provider_priority: str = "runpod,vast,lambda"
    runpod_api_key: str = ""
    vast_api_key: str = ""
    lambda_api_key: str = ""

    # --- Pool / reaper ---
    idle_reaper_interval_sec: int = 30
    cold_start_timeout_sec: int = 180
    health_check_interval_sec: int = 30
    health_check_fail_threshold: int = 3

    # --- Billing / quotas ---
    budget_default_usd: Decimal = Decimal("25.00")
    budget_alert_percents: str = "50,80,100"
    rate_limit_rpm_default: int = 60
    tokens_per_day_default: int = 1_000_000
    billing_mode_default: str = "postpaid"

    # --- Tier config ---
    tiers_config_path: Path = REPO_ROOT / "config" / "tiers.yaml"

    # --- Open WebUI bridge link ---
    openwebui_bridge_url: str = "http://bridge:8000"

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
