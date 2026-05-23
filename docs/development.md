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
- `bridge/main.py` — routes, `/v1/chat/completions`, `/v1/embeddings`, auth, admin; `_resolve_image_urls()` fetches external image URLs → base64 data URIs before forwarding to Ollama
- `bridge/router.py` — tier selection (vision routing at step 0, tokens/files/keywords/budget); vision helpers (`has_image_content`, `model_supports_vision`, `strip_images_from_messages`, `get_tiers`)
- `bridge/schemas.py` — request/response models; `ChatMessage.content` accepts `str | list[ContentPart]` for multimodal
- `bridge/instance_manager.py` — pod pool, lifecycle, health, reaper
- `bridge/multi_model.py` — WorkflowOrchestrator; pipeline (preprocess→infer→postprocess) + named workflows (`llm-visual-html` etc.)
- `bridge/settings.py` — all env vars via pydantic-settings
- `providers/base.py` — BaseProvider ABC; `_rank_gpu_offers()` returns all viable GPUs in preference order for fallback
- `providers/runpod.py`, `vast.py`, `lambda_labs.py` — cloud GPU pod providers
- `providers/local.py` — routes to local Ollama, no pod lifecycle
- `providers/api_compat.py` — OpenAI/Groq/Together/Mistral/DeepSeek pass-through
- `database/models.py` — User, Pod, Request, ApiKey, Invoice
- `cli/llm_ctl.py` — admin CLI (users, budget, tiers, keys)

## Admin CLI

`scripts/llmctl` wraps `cli/llm_ctl.py` via docker compose:

```bash
./scripts/llmctl users list
./scripts/llmctl users tiers user@example.com --set simple,vision
./scripts/llmctl users budget user@example.com --usd 10
./scripts/llmctl keys create user@example.com
```

Dashboard's allowed-tiers multiselect loads available tiers dynamically from `get_tiers()` — adding a new tier to `config/tiers.yaml` is enough; no code change needed.

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

## Dashboard

Streamlit app at http://localhost:8501. Pages:

| Page | Path | Description |
|------|------|-------------|
| Overview | `dashboard/pages/overview.py` | Month spend, active pods, cost charts, provider balances |
| Monitoring | `dashboard/pages/monitoring.py` | Live pods, in-flight requests, hourly cost ticker, pod kill |
| Analytics | `dashboard/pages/analytics.py` | Daily cost by tier, token heatmap, latency percentiles |
| Users | `dashboard/pages/users.py` | List, budget, allowed-tiers, suspend/activate, add user |
| Billing | `dashboard/pages/billing.py` | Invoices, budget alerts |

**Provider balances** (`dashboard/provider_balances.py`): fetches live credit from RunPod (GraphQL `clientBalance`) and Vast.ai (REST `/api/v0/users/current/`). Reads API keys from env — returns `None` for unconfigured providers, never raises. Lambda Labs balance API unavailable.

**API provider usage**: today's token count + cost per API provider queried from the `requests` table (joined to `pods` for provider name). Shown alongside credit balances on Overview.

**Pod kill**: Dashboard writes directly to DB (`Pod.status = terminated`) rather than calling bridge admin API — avoids auth issues.

Key dashboard files:
- `dashboard/app.py` — entry point, page registry
- `dashboard/db.py` — sync SQLAlchemy queries (psycopg3, not asyncpg)
- `dashboard/api_client.py` — bridge health/stats via HTTP
- `dashboard/provider_balances.py` — provider credit balance queries
- `dashboard/components/` — shared metrics formatters and Plotly charts

## DB Schema

Tables: `users`, `api_keys`, `pods`, `requests`, `invoices`, `audit_log`

API keys: SHA-256 hashed, plaintext shown once at issuance. Passwords: bcrypt.

### Migrations

```bash
docker compose run --rm bridge alembic upgrade head
```

Run after pulling commits that add migrations (check `database/migrations/versions/`). Current migrations:

| Revision | Change |
|----------|--------|
| `0001_initial_schema` | Full schema — users, pods, requests, invoices, audit_log |
| `0002_user_allowed_tiers` | Add `allowed_tiers` JSON column to users |
| `0003_vision_tier` | Add `'vision'` value to `tier_name` PostgreSQL enum |

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
