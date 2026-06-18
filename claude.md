# Self-Hosted LLM Infrastructure

Multi-tenant OpenAI-compatible bridge that routes requests to GPU providers running Ollama, local GPU, or commercial APIs.

## Architecture

```
Claude Code → ccr (port 3456) → Bridge (FastAPI :8000) → Router → InstanceManager → Provider → Ollama/API
Other clients ──────────────────────────────────────────────↑
                                                          ↓
                                               Local Ollama (preprocessor + embeddings)
                                                          ↓
                                               Postgres (state/billing) + Redis (quota/cache)
```

**CCR (claude-code-router):** optional Docker service (`docker/Dockerfile.ccr`) that sits between Claude Code and the bridge. Converts Anthropic API format ↔ OpenAI format. Activate with `source scripts/ccr-activate.sh`, then run `claude`. Config template: `config/ccr-config.json.example`. See `docs/ccr.md`.

**Key files:**
- `bridge/main.py` — routes, `/v1/chat/completions`, `/v1/embeddings`, auth, admin; `_resolve_image_urls()` fetches external image URLs → base64 data URIs before forwarding to Ollama
- `bridge/router.py` — tier selection (vision routing at step 0, tokens/files/keywords/budget); vision helpers `has_image_content()`, `model_supports_vision()`, `strip_images_from_messages()`, `get_tiers()`
- `bridge/schemas.py` — request/response models; `ChatMessage.content` accepts `str | list[ContentPart]` for multimodal
- `bridge/instance_manager.py` — pod pool, lifecycle, health, reaper
- `bridge/multi_model.py` — WorkflowOrchestrator; pipeline (preprocess→infer→postprocess) + named workflows (`llm-visual-html`: vision-describe → local HTML generation with base64 image injection)
- `bridge/settings.py` — all env vars via pydantic-settings
- `providers/base.py` — BaseProvider ABC; `provider_type`: `"pod"/"local"/"api"`; `needs_ollama_check`: skip Ollama probe for non-Ollama pods
- `providers/runpod.py`, `vast.py`, `lambda_labs.py` — cloud GPU pod providers
- `providers/local.py` — routes to local Ollama, no pod lifecycle
- `providers/api_compat.py` — OpenAI/Groq/Together/Mistral/DeepSeek pass-through
- `providers/together_dedicated.py` — Together AI dedicated endpoint provider; spins up reserved GPU for vision models; `_TIER_CONFIG` maps tier → (hardware list, model)
- `database/models.py` — User (preferred_tiers, disabled_providers, provider_order, allowed_tiers, allow_env_storage, no_volume_policy), Pod, Request, ApiKey, UserProviderKey (encrypted personal API keys), UserVolumeKey (per-provider persistent storage volume keys), Invoice
- `cli/llm_ctl.py` — admin CLI; **must run from project dir**: `docker compose exec bridge python -m cli.llm_ctl`; see `docs/cli.md`

## Tiers

| Tier | Model (tools) | Model (no tools) | GPU | $/hr |
|------|---------------|------------------|-----|------|
| simple | qwen2.5-coder:7b-instruct-q4_K_M | — | RTX 4090 | ~0.69 |
| mid | gemma4-coder:12b-q4_K_M | — | RTX 4090 | ~0.69 |
| architecture | qwen2.5:32b-instruct-q4_K_M | qwen2.5-coder:32b-instruct-q4_K_M | RTX 4090 | ~0.69 |
| maximum | deepseek-v3:latest-q4_K_M | — | L40S | ~1.14 |
| ultra | qwen2.5:72b-instruct-q4_K_M | — | A100 80GB | ~1.89 |
| vision | Llama-3.2-11B-Vision (Together dedicated) / MiniCPM-V (RunPod) | — | L40/RTX 4090 | ~0.69–1.49 |

`model` is used when request has tools (e.g. Claude Code tool-call sessions). `model_no_tools` (set in `config/tiers.yaml`) is used for plain chat with no tool definitions — routes to coder variant which is faster for pure text generation. Falls back to primary model if `model_no_tools` is not pulled on the pod.

`TIER_ORDER` in `bridge/router.py` must list tiers in ascending capability order. `mid` sits between `simple` and `architecture`.

Pod provider prices synced from live RunPod GPU catalog at startup.

**API provider costs** calculated per-token using rates in `config/tiers.yaml` → `api_token_costs` (USD per 1K tokens, separate input/output rates per model). Edit that block to update pricing — no code change needed. `Request.model` stores the actual model name (e.g. `gpt-4o-mini`) for API providers.

## Volume Storage (per-user)

Users can register their own persistent storage volume per provider instead of sharing the global `RUNPOD_NETWORK_VOLUME_ID`.

**DB table:** `user_volume_keys` — `user_id`, `provider`, `volume_id`, `api_key_encrypted` (Fernet, same `PROVIDER_KEY_SECRET`), `datacenter`, `created_at`. One row per user per provider.

**User fields:**
- `allow_env_storage` (bool, default True) — admin toggle; if False, user cannot use the env-level volume even if no user key is set
- `no_volume_policy` (enum, default `use_env`) — controls what happens when no user volume key is found for the provider being launched:
  - `use_env` — fall back to `RUNPOD_NETWORK_VOLUME_ID` if `allow_env_storage` and var is set
  - `stateless` — launch without any volume (models re-pulled on every cold start)
  - `block` — reject the request with HTTP 400

**Volume key API (authenticated user):**
- `GET /v1/user/volume-keys` — list (no decrypted key in response)
- `POST /v1/user/volume-keys` — upsert `{provider, volume_id, api_key, datacenter?}` (one per provider)
- `DELETE /v1/user/volume-keys/{id}` — remove

**DC validation:** Before launch, `_validate_network_volume()` in `providers/runpod.py` queries `{ myself { networkVolumes { id datacenterId } } }` using the user's key. If `datacenter` is set on the volume key and does not match the volume's actual `datacenterId`, the launch fails immediately with HTTP 400 (`VolumeValidationError`). This error is non-retryable — `InstanceManager` does not fall through to the next provider.

**Admin CLI:** `llmctl users storage <email> [--allow-env/--no-allow-env] [--policy use_env|stateless|block]`

**Known limitation:** Post-launch RunPod management (health, terminate) uses the system `RUNPOD_API_KEY`. If the user's key is for a different account, `terminate()` will silently fail. Ollama HTTP health checks are unaffected (no API key needed).

## Provider Types

`provider_type` on BaseProvider controls lifecycle in InstanceManager:
- `"pod"` — full lifecycle: launch → wait-for-ready (volume resolved first) → pull model → health check → terminate on idle
- `"local"` — wait-for-ready + pull model; terminate is no-op
- `"api"` — skip all lifecycle; inject `extra_request_headers()` (Bearer token) per request

`needs_ollama_check = False` — provider handles its own readiness (e.g. `together_dedicated`); instance_manager skips `/api/tags` probe and model pull.

`is_configured()` filters out providers with missing API keys at startup.

`PROVIDER_PRIORITY` (comma list) controls order and which providers are active.

**GPU fallback**: `_rank_gpu_offers(tier, offers)` in `providers/base.py` returns all viable GPU candidates sorted by `TIER_GPU_PREFERENCE` then price. RunPod's `launch()` iterates through them — if the preferred GPU has no capacity, it tries the next automatically. Returns 503 only when all candidates fail.

## Multimodal / Vision

`ChatMessage.content` accepts `str | list[ContentPart]` (OpenAI multimodal format). Use `msg.text_content()` anywhere plain text is needed (routing, token estimation, preprocessing).

**Vision routing** (`bridge/router.py` step 0): requests containing `image_url` content parts are automatically routed to the `vision` tier before all other routing signals, unless the requested model already supports vision (`gpt-4o`, `llava`, `gemini`, `llama-3.2`, `llama-4`, Qwen VL, etc.). If vision pod unavailable, `config/tiers.yaml` → `vision.fallback`:
- `strip_images` (default) — drops image parts, continues on text tier
- `error` — returns HTTP 400 `vision_not_supported`

**Together dedicated vision tiers** (3 quality levels via `providers/together_dedicated.py`):
| Routing tier | Model | Hardware |
|---|---|---|
| `simple` | Qwen3-VL-8B-Instruct | L40 / L40S / A100-40GB |
| `vision` | Llama-3.2-11B-Vision-Instruct-Turbo | L40 / L40S / A100 |
| `architecture` / `maximum` / `ultra` | Llama-3.2-90B-Vision-Instruct-Turbo | A100-80GB / H100 |

Together dedicated endpoints = reserved GPU (hourly billing). Bridge creates endpoint, waits for RUNNING, routes inference through standard Together API. Endpoint terminated on idle.

**Capacity cooldown**: if all hardware options fail (Together has no capacity), bridge falls to RunPod instantly and skips Together for the next 10 min (`_CAPACITY_COOLDOWN_SEC = 600`). Cooldown resets on first successful launch. No proactive retry — only triggers on the next incoming request after cooldown expires.

**API vision payloads**: `_sanitize_messages_for_api_vision()` in `main.py` strips history, sends only `[system?, last_user_with_image]` to non-Ollama vision providers (Together/OpenAI). Prevents consecutive-user-message errors.

Three helpers in `bridge/router.py`: `has_image_content()`, `model_supports_vision()`, `strip_images_from_messages()`. `_VISION_MODELS` set includes `llama-3.2`, `llama-4`, `vision-instruct`, `qwen3-vl`, `qwen2.5-vl`, `qwen2-vl`.

**Groq vision model**: `meta-llama/llama-4-scout-17b-16e-instruct` (llama-3.2 vision models decommissioned 2026-05).

## Pod Lifecycle (pod type only)

1. `provider.launch(tier)` → `PodInfo` with endpoint_url
2. `_wait_for_ready()` — polls `GET /api/tags` until 200, timeout=`COLD_START_TIMEOUT_SEC` (default 600s)
3. `_pull_model()` — `POST /api/pull` with `stream:true`, consumes progress lines to keep RunPod proxy alive (prevents 502 timeout on 19GB+ models)
4. `_warmup_model()` — sends minimal inference (`max_tokens=1`) to load model into VRAM; prevents 524 Cloudflare timeout on first real request
5. Alt model pull (`model_no_tools` from tiers.yaml) — non-fatal; `alt_model_pull_skipped` logged on failure
6. Pod marked ready in DB
7. Reaper terminates after `idle_timeout_sec` of inactivity (SELECT FOR UPDATE SKIP LOCKED prevents double-terminate across workers)

## Pipeline

`pipeline = body.pipeline or user.pipeline_default or settings.pipeline_default`

Values: `"infer"` (default), `"preprocess,infer"`, `"preprocess,infer,postprocess"`

Preprocessing rewrites user prompt via local Ollama 7B → structured JSON before GPU call.

## Embeddings

`POST /v1/embeddings` — proxied to local Ollama (`/api/embed`), no GPU cost. Model: `OLLAMA_EMBEDDING_MODEL` (default `nomic-embed-text`). Response translated to OpenAI format.

## Routing (priority order)

0. Image content detected + model doesn't support vision → `vision` tier
0a. `llm-local` model → `simple` tier with `provider_override="local"` (bypasses cloud, zero cost)
1. `X-Tier` header / `?tier=` param (ccr passes tier as model name, e.g. `model: "architecture"`)
2. Per-user `allowed_tiers` whitelist
3. Budget gate (downgrade or 402)
4. Token count thresholds
5. File count thresholds
6. Complexity keywords
7. Default: simple

**Tool definition stripping** (`_slim_tools` in `main.py`): `slim_tools=True` is applied to ALL providers (Ollama included). Strips `description` fields from tool definitions and parameter schemas. Cuts Claude Code's tool payload from ~64K tokens to ~5K. Critical for API provider TPM limits; also prevents Cloudflare 524 on RunPod/Ollama — full tool schemas caused 126s+ generation time for qwen2.5:32b, exceeding Cloudflare's ~100s proxy timeout.

**Tool call argument escaping** (`_fix_tool_arg_escaping` in `main.py`): Ollama models emit literal `\\n` (double-escaped) in JSON tool call arguments. Applied to all Ollama SSE streaming chunks and non-streaming responses — replaces `\\n` → `\n` in `function.arguments` strings.

## Key Env Vars

```
PROVIDER_PRIORITY=runpod,vast,lambda    # or: local / openai,runpod / etc.
RUNPOD_API_KEY=
RUNPOD_NETWORK_VOLUME_ID=              # optional global volume; superseded per-user if user volume key set
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
PROVIDER_KEY_SECRET=                   # required for personal API key encryption (generate: python3 -c "import secrets; print(secrets.token_hex(32))")
```

## DB Tables

`users`, `api_keys`, `pods`, `requests`, `invoices`, `audit_log`, `user_provider_keys`, `user_volume_keys`

API keys: SHA-256 hashed, plaintext shown once. Passwords: bcrypt.

## User Preferences (dashboard Settings page)

Users can configure per-request behavior without admin intervention:
- **Tier preferences** — subset of allowed_tiers the router may use (e.g. skip `ultra` to save cost)
- **Provider order + disable** — multiselect ordered list; selected = enabled in that priority order; unselected = disabled. Overrides tier `provider_overrides` for that user's requests.
- **Personal API keys** — per-provider keys (groq, openai, together, mistral, deepseek) stored Fernet-encrypted in `user_provider_keys`. Bridge substitutes the user's key on outbound requests; requires `PROVIDER_KEY_SECRET` in `.env`.
- **Volume keys** — per-provider persistent storage volume registrations in `user_volume_keys`. See Volume Storage section above.

Admin ceiling (`allowed_tiers` via `llmctl users tiers`) always trumps user preferences.

**Local provider access** is off by default (`allow_local=False`). Enable per-user:
```bash
llmctl users local-access <email> --allow
```

## Multi-tenancy

JWT or `sk-llm-...` API key auth. Per-user quotas (RPM/TPD/USD), `allowed_tiers` whitelist, prepaid/postpaid billing.

## Docs & Website

- `docs/index.html` — single-page GitHub Pages site (Lain cyberpunk aesthetic, inline CSS/JS, no build step)
- `docs/screenshots/` — PNG files auto-loaded by the gallery section
- `docs/deployment.md` — how to enable GitHub Pages, GitHub Wiki sync, Netlify/Vercel/nginx alternatives
- Enable Pages: repo Settings → Pages → branch `master` → folder `/docs`

## Open WebUI Sync & Gateway

### User sync
- `bridge/openwebui_sync.py` — `create_openwebui_user(email, password)` and `update_pipeline_user_key_map(email, api_key)`. Both read URL/creds from `settings` directly (no positional args for config).
- `POST /admin/users` — creates bridge user + optional OW sync + API key in one HTTP call. Body: `CreateUserRequest` (email, password, name?, role, billing_mode, budget_usd, sync_openwebui, create_api_key, sync_pipeline). Returns `CreateUserResponse` with plaintext `api_key` (shown once).
- `POST /admin/sync-openwebui?email=X&api_key=Y` — upserts one email→key entry in pipeline valve.
- `GET /v1/usage` — user endpoint (no admin needed). Returns: `this_month` (request_count, success_count, prompt_tokens, completion_tokens, cost_usd), `last_30_days` (daily rows), `monthly_budget_usd`, `prepaid_balance_usd`, `billing_mode`, `email`, `user_id`.
- CLI: `llmctl users add --sync-openwebui`, `llmctl users keys-add --sync-pipeline`.

### Pipeline valve API paths (Pipelines service)
- `GET  /v1/pipelines/{pipeline_id}/valves` — fetch current valve values (Bearer `PIPELINES_API_KEY`)
- `POST /v1/pipelines/{pipeline_id}/valves/update` — update valve values; `user_key_map` is a JSON string embedded in the valve JSON object

### New settings (bridge/settings.py)
```
openwebui_url: str = "http://openwebui:8080"        # bridge→OW internal URL
openwebui_admin_email: str = ""
openwebui_admin_password: str = ""
pipelines_url: str = "http://pipelines:9099"
pipelines_api_key: str = ""
pipeline_id: str = "gpu-relay"
```

### Gateway
- `gateway/main.py` — transparent reverse proxy. All HTTP methods/paths forwarded to `GATEWAY_BRIDGE_URL`. `Authorization` header passed through unchanged (user supplies their own `sk-llm-` key). Hop-by-hop headers stripped. Streaming detected via `Accept: text/event-stream` or `"stream":true` in body; uses `_client.send(..., stream=True)` + `aiter_bytes()`.
- `docker-compose.gateway.yml` — compose overlay; use alongside main stack: `docker compose -f docker-compose.yml -f docker-compose.gateway.yml up -d gateway`
- `docker/docker-compose.gateway.yml` — standalone file; gateway-only deploy pointing at remote bridge
- `docker/Dockerfile.gateway` — Python 3.12-slim + `gateway/requirements.txt`

### User portal
- `dashboard/pages/user_portal.py` — Streamlit page in existing dashboard container (port 8501). Login: paste `sk-llm-` key (no email/password). Calls `GET /v1/usage`. No direct DB access — works against any bridge URL via `OPENWEBUI_BRIDGE_URL` env var.
