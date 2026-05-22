# Development

## Local Setup

```bash
pip install -r requirements.txt
pre-commit install
pytest                                       # unit + integration (no Docker)
MOCK_PROVIDERS=1 ./scripts/smoke_test.sh     # full E2E with live stack
```

## Mock Mode (no GPU account)

`MOCK_PROVIDERS=1` routes all GPU requests to the local Ollama service instead of renting a pod. No billing, no cold start.

```bash
MOCK_PROVIDERS=1 ./scripts/smoke_test.sh
```

All 13 E2E tests pass in mock mode. The 7B coder model runs on CPU (slow but functional).

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
- `bridge/multi_model.py` — pipeline (preprocess→infer→postprocess)
- `bridge/settings.py` — all env vars via pydantic-settings
- `providers/base.py` — BaseProvider ABC
- `providers/runpod.py`, `vast.py`, `lambda_labs.py` — cloud GPU pod providers
- `providers/local.py` — routes to local Ollama, no pod lifecycle
- `providers/api_compat.py` — OpenAI/Groq/Together/Mistral/DeepSeek pass-through
- `database/models.py` — User, Pod, Request, ApiKey, Invoice

## Pod Lifecycle

1. `provider.launch(tier)` → `PodInfo` with `endpoint_url`
2. `_wait_for_ready()` — polls `GET /api/tags` until 200; timeout = `COLD_START_TIMEOUT_SEC`
3. `_pull_model()` — `POST /api/pull` with `stream:false`, 600 s read timeout
4. Pod marked `ready` in DB
5. Reaper terminates after `idle_timeout_sec` of inactivity (`SELECT FOR UPDATE SKIP LOCKED` prevents double-terminate across workers)

`provider_type` on BaseProvider controls lifecycle:
- `"pod"` — full lifecycle: launch → wait → pull → health check → terminate on idle
- `"local"` — wait + pull; terminate is no-op
- `"api"` — skip all lifecycle; inject `extra_request_headers()` per request

## DB Schema

Tables: `users`, `api_keys`, `pods`, `requests`, `invoices`, `audit_log`

API keys: SHA-256 hashed, plaintext shown once at issuance. Passwords: bcrypt.

## Environment Variables

See `.env.example` for the full list with comments. Key vars:

```
PROVIDER_PRIORITY=runpod,vast,lambda
RUNPOD_API_KEY=
RUNPOD_NETWORK_VOLUME_ID=
COLD_START_TIMEOUT_SEC=600
MOCK_PROVIDERS=false
PIPELINE_DEFAULT=infer
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
BUDGET_DEFAULT_USD=25.00
```
