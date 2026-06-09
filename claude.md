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

| Tier | Model | GPU | $/hr |
|------|-------|-----|------|
| simple | qwen2.5-coder:7b-instruct-q4_K_M | RTX 4090 | ~0.69 |
| architecture | qwen2.5-coder:32b-instruct-q4_K_M | RTX 4090 | ~0.69 |
| maximum | deepseek-v3:latest-q4_K_M | L40S | ~1.14 |
| ultra | qwen2.5:72b-instruct-q4_K_M | A100 80GB | ~1.89 |
| vision | Llama-3.2-11B-Vision (Together dedicated) / MiniCPM-V (RunPod) | L40/RTX 4090 | ~0.69–1.49 |

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

0. Image content detected + model doesn't support vision → `vision` tier
0a. `llm-local` model → `simple` tier with `provider_override="local"` (bypasses cloud, zero cost)
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
