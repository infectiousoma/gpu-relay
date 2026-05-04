# Self-Hosted LLM Infrastructure

## Project Overview

Multi-tenant, multi-provider LLM serving platform with intelligent tier routing. Optimizes cost vs. capability by selecting the cheapest GPU/model that can satisfy the request, then auto-shuts-down idle instances.

**Primary goal**: Run a coding-focused LLM stack at <30% of OpenAI/Anthropic API cost while maintaining sub-3s cold-start and OpenAI API compatibility for any client (Open WebUI, Cline, Continue, Aider, etc.).

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Clients: Open WebUI, Cline, Continue, curl, custom apps     │
└─────────────────────────┬────────────────────────────────────┘
                          │ OpenAI-compatible HTTPS
                          ▼
┌──────────────────────────────────────────────────────────────┐
│                     Bridge (FastAPI)                         │
│  ┌────────┐  ┌────────┐  ┌──────────┐  ┌─────────────────┐  │
│  │ Auth   │→ │ Router │→ │ Instance │→ │ Multi-Model     │  │
│  │ (JWT)  │  │ (tier) │  │ Manager  │  │ Orchestrator    │  │
│  └────────┘  └────────┘  └──────────┘  └─────────────────┘  │
│       │           │            │               │             │
│       ▼           ▼            ▼               ▼             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  Cost Tracker  │  Quota Enforcer  │  Audit Logger  │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────┬────────────────────────────────────┘
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
   ┌─────────┐      ┌──────────┐      ┌──────────┐
   │ RunPod  │      │ Vast.ai  │      │  Lambda  │
   │ (prim)  │      │ (cheap)  │      │ (fallbk) │
   └─────────┘      └──────────┘      └──────────┘
        │                 │                 │
        ▼                 ▼                 ▼
   ┌────────────────────────────────────────────┐
   │  GPU pods running Ollama + tier-specific   │
   │  pre-baked model images                    │
   └────────────────────────────────────────────┘

   ┌──────────────────┐    ┌─────────────────────────┐
   │ Local Ollama 7B  │    │ Postgres (state, bills) │
   │ (preprocessor)   │    │ Redis (sessions, cache) │
   └──────────────────┘    └─────────────────────────┘
```

## Tier Configuration

| Tier         | GPU         | Model                            | VRAM | $/hr  | Idle TO | Use cases                                       |
|--------------|-------------|----------------------------------|------|-------|---------|-------------------------------------------------|
| simple       | RTX 4090    | qwen2.5-coder:7b-instruct-q4     | 4GB  | 0.69  | 5 min   | quick fixes, code completion, simple debug      |
| architecture | RTX 4090    | qwen2.5-coder:32b-instruct-q4    | 18GB | 0.69  | 10 min  | multi-file refactor, API design, code review    |
| maximum      | L40S        | deepseek-v3:latest-q4            | 36GB | 1.14  | 10 min  | large codebase analysis, sysdesign, sec audit   |
| ultra        | A100 80GB   | qwen2.5:72b-instruct-q4          | 48GB | 1.89  | 10 min  | full-codebase audits, mission-critical          |

## Routing Logic (priority order)

1. **Explicit override**: `X-Tier: <name>` header or `?tier=<name>` query param.
2. **Budget gate**: If user remaining budget < projected cost, downgrade or reject.
3. **Context size**: prompt tokens > 32k → maximum or ultra; > 8k → architecture+.
4. **File count**: ≥ 10 files in request → architecture+; ≥ 50 → maximum+.
5. **Complexity keywords**: `architecture|design|audit|analyze|refactor|review|security` → architecture+.
6. **Fallback**: simple.

Router emits `tier`, `reason`, `projected_cost_usd` for every decision (logged + returned in `X-LLM-*` response headers).

## Multi-Model Orchestration

For supported clients, requests can be chained:

1. **Local Ollama 7B (preprocessor)** - converts free-form user prompt to structured JSON: `{intent, files_referenced, complexity_signals, target_tier_hint}`. Runs on the bridge host (no GPU rental cost).
2. **Remote tier model (primary)** - performs the inference using the routed tier.
3. **Optional post-processor** - validates JSON output, re-formats code blocks, or runs a cheap critic pass on the smaller model.

Enabled per-request via `X-Pipeline: preprocess,infer[,postprocess]` or per-user default in DB.

## Multi-Tenancy

- **Auth**: JWT (HS256) issued by bridge `/auth/login`, or per-user API key (`sk-llm-...`).
- **Isolation**: Every request tagged with `user_id`. Instance pool is shared across users (tier-keyed) but logs/quotas/bills are per-user.
- **Quotas**: `requests_per_min`, `tokens_per_day`, `usd_per_month` (any breach → 429 + cutoff).
- **Billing modes**: `prepaid` (decrement balance) or `postpaid` (accrue, invoice monthly).
- **Alerts**: 50/80/100% of budget → email + dashboard banner.

## Instance Pooling

- One pod per (provider, tier) kept warm while `last_used + idle_timeout > now`.
- Cold-start budget: `COLD_START_TIMEOUT_SEC` (default 180 s). First-pull cold starts are much longer: 7B ~2 min, 32B ~15 min, 72B ~30 min. Increase to 600+ for production.
- **Network volume** (optional, recommended): set `RUNPOD_NETWORK_VOLUME_ID` to mount a RunPod persistent volume at `/runpod-volume`. Ollama model cache lives there; subsequent cold starts skip the download entirely (~30 s ready time). Without it, models re-download each cold start.
- Health probe: `GET /api/tags` on Ollama every 30s. 3 fails → mark unhealthy → drain → terminate.
- Graceful degradation: provider X 5xx or capacity error → automatically retry next provider in priority list.

## Critical Features

1. **OpenAI-compatible**: `/v1/chat/completions`, `/v1/completions`, `/v1/models` (lists currently-available tiers as model names, e.g. `llm-simple`, `llm-architecture`).
2. **Real-time cost calc**: charged seconds = `request_duration + (idle_timeout / shared_users_in_window)`. Returned in `X-LLM-Cost-USD` response header. Uses actual `costPerHr` from provider API — never the tiers.yaml estimate.
3. **Live price sync**: at startup, `instance_manager` calls `list_gpus()` on each provider and updates in-memory tier cost estimates via `router.refresh_tier_prices()`. Budget-gate projections stay accurate without manual config edits.
4. **Dynamic GPU selection**: no hardcoded GPU type IDs. `list_gpus()` fetches live catalog; `_select_gpu_offer()` matches by VRAM floor + name preference order.
5. **Auto-recovery**: failed pod → spin replacement → replay request (idempotency key required for non-streaming).
6. **Comprehensive logging**: every request → `audit_log` table (user, tier, provider, latency, tokens, USD).

## Tech Stack

| Layer       | Tech                                |
|-------------|-------------------------------------|
| Bridge API  | FastAPI + uvicorn + httpx           |
| Auth        | python-jose, passlib[bcrypt]        |
| DB          | Postgres 16 + SQLAlchemy 2 + Alembic|
| Cache/Queue | Redis 7                             |
| Dashboard   | Streamlit                           |
| CLI         | Typer + Rich                        |
| Local LLM   | Ollama (preprocessor)               |
| Deploy      | Docker Compose (single host)        |
| Observ.     | structlog + Prometheus exporter     |

## Repository Layout

```
llm-infrastructure/
├── claude.md                  # this file
├── README.md                  # quickstart for humans
├── requirements.txt           # pinned deps
├── .env.example               # env template
├── docker-compose.yml         # full stack
├── alembic.ini                # migration config
│
├── bridge/                    # FastAPI app
│   ├── __init__.py
│   ├── main.py                # app entrypoint, OpenAI-compat routes
│   ├── router.py              # tier selection
│   ├── instance_manager.py    # pod pool
│   ├── cost_tracker.py        # per-request USD calc + persist
│   ├── multi_model.py         # preprocessor → primary → post chain; WorkflowOrchestrator; WORKFLOW_MODELS
│   ├── pricing.py             # calculate_price(), effective_hourly_rate(), markup_summary()
│   ├── auth.py                # JWT + API key
│   ├── quota.py               # rate limits + budget gate
│   ├── settings.py            # pydantic-settings (env)
│   └── schemas.py             # request/response models
│
├── providers/                 # GPU-rental adapters
│   ├── __init__.py
│   ├── base.py                # ABC: launch, terminate, status, list_gpus
│   ├── runpod.py
│   ├── vast.py
│   ├── lambda_labs.py
│   └── mock.py                # MOCK_PROVIDERS=1 — local Ollama as fake pod
│
├── dashboard/                 # Streamlit
│   ├── app.py
│   ├── components/
│   │   ├── usage_charts.py
│   │   ├── cost_tracking.py
│   │   └── user_management.py
│   └── billing.py
│
├── cli/                       # operator CLI
│   └── llm_ctl.py             # `llmctl users add`, `pods ls`, `bills run`
│
├── database/
│   ├── models.py              # SQLAlchemy models
│   ├── session.py             # engine + session factory
│   ├── migrations/            # alembic versions
│   └── seeds/
│       ├── tiers.json
│       └── default_admin.json
│
├── config/
│   └── tiers.yaml             # tier definitions (loaded at startup)
│
├── docker/
│   ├── Dockerfile.bridge
│   ├── Dockerfile.dashboard
│   └── ollama-models/
│       ├── Dockerfile.simple
│       ├── Dockerfile.architecture
│       ├── Dockerfile.maximum
│       └── Dockerfile.ultra
│
├── scripts/
│   ├── setup.sh                   # one-shot stack setup + bootstrap
│   └── smoke_test.sh              # full E2E: starts stack, manual curl checks, pytest
│
└── tests/
    ├── test_router.py
    ├── test_quota.py
    ├── test_providers.py
    ├── test_auth.py
    ├── test_cost_tracker.py
    ├── test_integration.py
    ├── test_multi_model.py
    ├── test_pricing.py
    └── test_e2e.py              # E2E against live stack; run via smoke_test.sh
```

## Environment Variables

See `.env.example`. Critical ones:

- `BRIDGE_SECRET_KEY` - JWT signing
- `DATABASE_URL` - postgres DSN
- `REDIS_URL`
- `RUNPOD_API_KEY`, `VAST_API_KEY`, `LAMBDA_API_KEY`
- `RUNPOD_NETWORK_VOLUME_ID` - optional; mounts persistent volume for Ollama model cache
- `PROVIDER_PRIORITY` - comma list, e.g. `runpod,vast,lambda`
- `OLLAMA_LOCAL_URL` - default `http://ollama:11434` (preprocessor)
- `BUDGET_DEFAULT_USD` - new-user monthly cap
- `IDLE_REAPER_INTERVAL_SEC` - default 30

## Database Schema (high-level)

- `users` (id, email, role, api_key_hash, billing_mode, monthly_budget_usd, created_at)
- `quotas` (user_id, rpm, tpd, usd_pm)
- `pods` (id, provider, tier, gpu, external_id, endpoint_url, status, started_at, last_used_at, terminated_at)
- `requests` (id, user_id, pod_id, tier, model, prompt_tokens, completion_tokens, latency_ms, cost_usd, created_at)
- `invoices` (id, user_id, period_start, period_end, amount_usd, status, paid_at)
- `audit_log` (id, user_id, action, resource, details_json, created_at)

Indexes on `(user_id, created_at)`, `(pod_id, created_at)`, `(tier, status)`.

## Security Notes

- All API keys in DB are SHA-256 hashed. Plaintext shown once at issuance. Passwords (human) use bcrypt with 72-byte truncation.
- Bridge → provider API keys read from env; never logged.
- Open WebUI runs behind bridge auth; no anonymous access in prod.
- `/v1/*` rate-limited per-user via Redis token bucket.
- Audit log immutable (append-only; no UPDATE/DELETE grant).

## Out of Scope (v1)

- Fine-tuning / training
- Multi-region pod scheduling
- BYO-model registry (only the 4 tiers above)
- SSO (planned v2)
