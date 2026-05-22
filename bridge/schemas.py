"""OpenAI-compatible request/response models + internal DTOs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# OpenAI-compatible chat completions
# ---------------------------------------------------------------------------

Role = Literal["system", "user", "assistant", "tool"]

# OpenAI multimodal content part (text or image_url)
ContentPart = dict[str, Any]
# content can be a plain string or a list of content parts (multimodal)
MessageContent = str | list[ContentPart]


class ChatMessage(BaseModel):
    role: Role
    content: MessageContent
    name: str | None = None
    tool_call_id: str | None = None

    def text_content(self) -> str:
        """Flatten content to plain text for routing/preprocessing/token estimation."""
        if isinstance(self.content, str):
            return self.content
        return " ".join(
            p.get("text", "") for p in self.content if p.get("type") == "text"
        )


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, gt=0)
    stream: bool = False
    stop: list[str] | str | None = None
    user: str | None = None
    # Bridge extensions (ignored by upstream OpenAI clients):
    files_referenced: int | None = Field(default=None, ge=0, description="Hint for router")
    pipeline: str | None = Field(default=None, description="e.g. 'preprocess,infer'")


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str | None = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage
    # Bridge extensions:
    bridge: "BridgeMeta"


class BridgeMeta(BaseModel):
    tier: str
    routing_reason: str
    provider: str
    pod_id: str | None
    cost_usd: float
    latency_ms: int


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "self-hosted-llm"


class ModelsResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]


# ---------------------------------------------------------------------------
# Internal DTOs
# ---------------------------------------------------------------------------

class RoutingDecision(BaseModel):
    tier: str
    reason: str
    projected_cost_usd: float
    requires_pod: bool = True
    downgraded_from: str | None = None


class PodHandle(BaseModel):
    pod_id: str
    provider: str
    tier: str
    endpoint_url: str
    cost_per_hour_usd: float
    model: str = ""
    cold_start: bool = False
    extra_headers: dict[str, str] = {}  # injected on inference requests (API providers)


class CostReceipt(BaseModel):
    request_id: str
    user_id: str
    tier: str
    provider: str
    pod_id: str | None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    cost_usd: float


# ---------------------------------------------------------------------------
# Auth payloads
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    role: str = "user"


class ApiKeyResponse(BaseModel):
    id: str
    label: str | None
    key: str  # plaintext, shown ONCE
    prefix: str
    created_at: str


# ---------------------------------------------------------------------------
# Embeddings (OpenAI-compatible, proxied to local Ollama)
# ---------------------------------------------------------------------------

class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]
    encoding_format: str = "float"
    dimensions: int | None = None
    user: str | None = None


class EmbeddingData(BaseModel):
    object: Literal["embedding"] = "embedding"
    index: int
    embedding: list[float]


class EmbeddingUsage(BaseModel):
    prompt_tokens: int = 0
    total_tokens: int = 0


class EmbeddingResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[EmbeddingData]
    model: str
    usage: EmbeddingUsage


# ---------------------------------------------------------------------------
# Error envelope (OpenAI-shaped)
# ---------------------------------------------------------------------------

class ErrorDetail(BaseModel):
    message: str
    type: str
    code: str | None = None
    param: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


ChatCompletionResponse.model_rebuild()
