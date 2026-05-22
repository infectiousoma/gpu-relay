# Self-Hosted LLM Infrastructure

Multi-tenant OpenAI-compatible bridge that routes requests to GPU providers running Ollama, local GPU, or commercial APIs.

## Architecture

```
Client (OpenAI API) → Bridge (FastAPI) → Router → InstanceManager → Provider → Ollama/API
                                       ↓
                              Local Ollama (preprocessor + embeddings)
                                       ↓
                              Postgres (state/billing) + Redis (quota/cache)
```

**Key files:**
- `bridge/main.py` — routes, `/v1/chat/completions`, `/v1/embeddings`, auth, admin
- `bridge/router.py` — tier selection (tokens/files/keywords/budget)
- `bridge/instance_manager.py` — pod pool, lifecycle, health, reaper
- `bridge/multi_model.py` — pipeline (preprocess→infer→postprocess), WorkflowOrchestrator
- `bridge/settings.py` — all env vars via pydantic-settings
- `providers/base.py` — BaseProvider ABC; `provider_type`: `"pod"/"local"/"api"`
- `providers/runpod.py`, `vast.py`, `lambda_labs.py` — cloud GPU pod providers
- `providers/local.py` — routes to local Ollama, no pod lifecycle
- `providers/api_compat.py` — OpenAI/Groq/Together/Mistral/DeepSeek pass-through
- `database/models.py` — User, Pod, Request, ApiKey, Invoice

## Tiers

| Tier | Model | GPU | $/hr |
|------|-------|-----|------|
| simple | qwen2.5-coder:7b-instruct-q4_K_M | RTX 4090 | ~0.69 |
| architecture | qwen2.5-coder:32b-instruct-q4_K_M | RTX 4090 | ~0.69 |
| maximum | deepseek-v3:latest-q4_K_M | L40S | ~0.86 |
| ultra | qwen2.5:72b-instruct-q4_K_M | A100 80GB | ~1.89 |

Prices synced from live provider GPU catalog at startup.

## Provider Types

`provider_type` on BaseProvider controls lifecycle in InstanceManager:
- `"pod"` — full lifecycle: launch → wait-for-ready → pull model → health check → terminate on idle
- `"local"` — wait-for-ready + pull model; terminate is no-op
- `"api"` — skip all lifecycle; inject `extra_request_headers()` (Bearer token) per request

`is_configured()` filters out providers with missing API keys at startup.

`PROVIDER_PRIORITY` (comma list) controls order and which providers are active.

## Pod Lifecycle (pod type only)

1. `provider.launch(tier)` → `PodInfo` with endpoint_url
2. `_wait_for_ready()` — polls `GET /api/tags` until 200, timeout=`COLD_START_TIMEOUT_SEC` (default 600s)
3. `_pull_model()` — `POST /api/pull` with `stream:false`, read timeout 600s
4. Pod marked ready in DB
5. Reaper terminates after `idle_timeout_sec` of inactivity (SELECT FOR UPDATE SKIP LOCKED prevents double-terminate across workers)

## Pipeline

`pipeline = body.pipeline or user.pipeline_default or settings.pipeline_default`

Values: `"infer"` (default), `"preprocess,infer"`, `"preprocess,infer,postprocess"`

Preprocessing rewrites user prompt via local Ollama 7B → structured JSON before GPU call.

## Embeddings

`POST /v1/embeddings` — proxied to local Ollama (`/api/embed`), no GPU cost. Model: `OLLAMA_EMBEDDING_MODEL` (default `nomic-embed-text`). Response translated to OpenAI format.

## Routing (priority order)

1. `X-Tier` header / `?tier=` param
2. Per-user `allowed_tiers` whitelist
3. Budget gate (downgrade or 402)
4. Token count thresholds
5. File count thresholds
6. Complexity keywords
7. Default: simple

## Key Env Vars

```
PROVIDER_PRIORITY=runpod,vast,lambda    # or: local / openai,runpod / etc.
RUNPOD_API_KEY=
RUNPOD_NETWORK_VOLUME_ID=              # optional; if set, validated before launch
OPENAI_API_KEY=                        # enables openai provider
GROQ_API_KEY=                          # enables groq provider
TOGETHER_API_KEY=
MISTRAL_API_KEY=
DEEPSEEK_API_KEY=
OLLAMA_LOCAL_URL=http://ollama:11434
OLLAMA_PREPROCESSOR_MODEL=qwen2.5-coder:7b-instruct-q4_K_M
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
PIPELINE_DEFAULT=infer
COLD_START_TIMEOUT_SEC=600
MOCK_PROVIDERS=false                   # 1 = use local Ollama, skip all pod providers
```

## DB Tables

`users`, `api_keys`, `pods`, `requests`, `invoices`, `audit_log`

API keys: SHA-256 hashed, plaintext shown once. Passwords: bcrypt.

## Multi-tenancy

JWT or `sk-llm-...` API key auth. Per-user quotas (RPM/TPD/USD), `allowed_tiers` whitelist, prepaid/postpaid billing.
