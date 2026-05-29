# Providers

The bridge supports four provider categories, mixed freely via `PROVIDER_PRIORITY` in `.env`.

| Provider | Type | Description |
|----------|------|-------------|
| `runpod`, `vast`, `lambda` | Cloud GPU pod | Rents a GPU, launches Ollama, terminates when idle |
| `local` | Local GPU | Routes to Ollama on this machine — zero cloud cost |
| `openai`, `groq`, `together`, `mistral`, `deepseek` | Commercial API | Calls vendor API directly — no pod management |
| `together_dedicated` | Dedicated GPU pod | Spins up a Together reserved GPU endpoint for vision models; no Ollama — inference via Together API |

```
PROVIDER_PRIORITY=runpod,vast,lambda   # try RunPod first, fall back to Vast, then Lambda
```

Only providers with a configured API key are active. Unconfigured providers are skipped silently.

---

## RunPod

```
RUNPOD_API_KEY=<key>
PROVIDER_PRIORITY=runpod
```

Pods launch on demand and terminate after `idle_timeout_sec` of inactivity. No cost while idle.

**GPU fallback**: if the preferred GPU has no secure-cloud capacity, the bridge tries every viable GPU type in preference order, then retries all candidates on community cloud before failing. Returns 503 only when all candidates are exhausted on both cloud types.

| Tier | GPU preference order | VRAM range |
|------|----------------------|------------|
| `simple` | RTX 4090 → RTX 3090 → A40 → A6000 → cheapest in range | 8–24 GB |
| `vision` | RTX 4090 → RTX 3090 → A40 → A6000 → cheapest in range | 10–24 GB |
| `architecture` | RTX 4090 → RTX 3090 → A40 → A6000 → cheapest ≥20 GB | ≥20 GB |
| `maximum` | L40S → L40 → A40 → A100 40GB → cheapest ≥38 GB | ≥38 GB |
| `ultra` | A100 80GB → H100 → cheapest ≥50 GB | ≥50 GB |

The VRAM cap on `simple` and `vision` prevents those tiers from landing on datacenter-class GPUs (A100, H100, H200) when preferred types are sold out — which would be 5–10× the cost with no benefit for a 7B/13B model.

**Community cloud fallback**: after all SECURE candidates are exhausted, the bridge retries with `cloudType: COMMUNITY` (same GPU types, shared/interruptible nodes). Community pods are lower cost but can be preempted. A `runpod_community_fallback` log event is emitted when this path is taken.

Cold starts pull the Docker image + model on first use:
- 7B model: ~3–5 min first run, ~30 s with network volume
- 32B model: ~10–15 min first run, ~30 s with network volume

### Persistent Model Cache

Without a network volume, models re-download on every cold start.

There are two ways to configure persistent storage: a **global env volume** (shared by all users) or **per-user volume keys** (each user registers their own volume).

#### Global env volume (shared)

1. Verify the stack works without a volume first
2. **Choose a datacenter** — see note below before creating the volume
3. RunPod dashboard → Storage → Network Volumes → create 100 GB in that datacenter
4. Copy the volume ID
5. Add to `.env`: `RUNPOD_NETWORK_VOLUME_ID=<id>`
6. Restart bridge: `docker compose up -d bridge`

Cost: ~$7–8/month. Cuts cold starts from minutes to ~30 s.

> **Warning:** If you delete the volume, clear `RUNPOD_NETWORK_VOLUME_ID` from `.env`. A stale ID causes every launch to fail.

#### Per-user volume keys

Users can register their own RunPod network volume via the API. When a user has a volume key for a provider, it takes precedence over the global env volume for their requests.

```http
POST /v1/user/volume-keys
Authorization: Bearer <user-token>
Content-Type: application/json

{
  "provider": "runpod",
  "volume_id": "abc12345",
  "api_key": "<runpod-api-key>",
  "datacenter": "EU-RO-1"
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `provider` | yes | `runpod`, `vast`, or `lambda` |
| `volume_id` | yes | The volume ID from RunPod Storage |
| `api_key` | yes | RunPod API key that owns the volume |
| `datacenter` | no | RunPod datacenter ID (e.g. `EU-RO-1`); if set, the launch is constrained to that DC and validated before launch |

The `api_key` is encrypted at rest using the same Fernet key as provider keys (`PROVIDER_KEY_SECRET`).

**Key validation:** When saving a volume key in the dashboard Settings page, the API key and volume ID are validated against RunPod's API before storing. Invalid keys or volume IDs are rejected immediately — nothing is saved until the validation passes.

**DC validation:** If `datacenter` is set, the bridge calls `{ myself { networkVolumes { id dataCenterId } } }` using the user's key before every launch. If the volume's actual `dataCenterId` doesn't match, the launch returns HTTP 400 immediately — no fallback to other providers.

Other endpoints:
```http
GET    /v1/user/volume-keys          # list (no decrypted api_key in response)
DELETE /v1/user/volume-keys/{id}     # remove
```

#### Admin: volume storage policy

Control what happens when a user's request arrives but no user volume key is configured for the provider:

```bash
llmctl users storage <email>                          # show current policy + keys
llmctl users storage <email> --policy use_env         # fall back to RUNPOD_NETWORK_VOLUME_ID (default)
llmctl users storage <email> --policy stateless       # launch without any volume
llmctl users storage <email> --policy block           # reject request with HTTP 400
llmctl users storage <email> --no-allow-env           # prevent use of env volume even with use_env policy
llmctl users storage <email> --allow-env              # restore env volume access
```

| Policy | Effect when no user volume key found |
|--------|--------------------------------------|
| `use_env` | Use `RUNPOD_NETWORK_VOLUME_ID` if `allow_env_storage=True` and var is set; otherwise launch stateless |
| `stateless` | Launch without any volume; models re-download on every cold start |
| `block` | Fail immediately with HTTP 400; no fallback to other providers |

#### Datacenter selection

RunPod pods launch in whichever datacenter has capacity for the requested GPU type — no datacenter is pinned in the launch request. When a volume is used (env or user-supplied), RunPod **constrains every pod to the same datacenter as the volume**. There is no cross-datacenter fallback: if that datacenter runs out of your target GPU type, all launches fail until capacity returns.

Choose a datacenter that has:
- Reliable availability of your tiers' preferred GPU (RTX 4090 for `simple`/`architecture`, L40S for `maximum`, A100 80GB for `ultra`)
- Network volume storage support (not all RunPod datacenters offer it — the volume creation UI only shows eligible DCs)

Before creating the volume, check RunPod's GPU availability table filtered to each datacenter, and prefer one that consistently shows stock across your target GPU types rather than the cheapest current price.

### Pod type concurrency

Each pod type (`vision`, `simple`, `architecture`, etc.) has an independent lifecycle: its own lock, its own idle timer, and its own spin-up/spin-down state. A vision pod spinning up or warming down never blocks a `simple` pod from acquiring, and vice versa. Multiple pod types can run simultaneously with no global serialisation.

---

## Local GPU

Route to the Ollama instance running on this machine — no cloud account needed.

```
PROVIDER_PRIORITY=local
```

Uses `OLLAMA_LOCAL_URL` (default: `http://ollama:11434`, the stack's built-in Ollama service). Models are pulled on first request.

**CPU-only:** Remove the `deploy:` GPU block from the `ollama` service in `docker-compose.yml`. A 7B q4 model on CPU takes ~30–60 s per request.

**`llm-local` model**: Selecting `llm-local` in Open WebUI (or sending `model=llm-local` to the API) forces the request directly to local Ollama, bypassing all cloud providers regardless of tier `provider_overrides`. Zero cost, instant routing — useful for Open WebUI background tasks (title generation, auto-tagging) to avoid spinning up cloud pods.

**Access control**: Local provider access is off by default (`allow_local=False`). Enable per user:
```bash
llmctl users local-access <email> --allow
llmctl users local-access <email> --deny
```

`llm-local` only appears in the model list when the local provider is configured and `is_configured()` returns true.

---

## Commercial APIs

All are OpenAI-compatible. No pod lifecycle — responses are instant (no cold start).

```
PROVIDER_PRIORITY=openai,runpod    # OpenAI first, RunPod as fallback
OPENAI_API_KEY=sk-...
```

| Provider | Key var | Default models (simple / architecture / maximum / ultra) |
|----------|---------|----------------------------------------------------------|
| `openai` | `OPENAI_API_KEY` | gpt-4o-mini / gpt-4o / gpt-4o / gpt-4o |
| `groq` | `GROQ_API_KEY` | llama-3.1-8b-instant / llama-3.3-70b / llama-3.3-70b / llama-3.3-70b |
| `together` | `TOGETHER_API_KEY` | Llama-3.2-11B / Llama-3.1-70B / Llama-3.1-405B / Llama-3.1-405B |
| `mistral` | `MISTRAL_API_KEY` | mistral-small / mistral-medium / mistral-large / mistral-large |
| `deepseek` | `DEEPSEEK_API_KEY` | deepseek-chat / deepseek-chat / deepseek-reasoner / deepseek-reasoner |

> **Together serverless vs dedicated**: The `together` provider uses Together's serverless API (pay-per-token, text models only). Vision models on Together require dedicated endpoints — use `together_dedicated` instead, which is listed separately under Vision below.

Override any model mapping:
```
OPENAI_MODEL_SIMPLE=gpt-4o-mini
OPENAI_MODEL_ARCHITECTURE=o1-mini
```

> **Note:** Anthropic (Claude) uses a different wire format and is not yet supported.

**Multimodal (images)**: Requests containing `image_url` content parts are automatically routed to the `vision` tier before any other routing logic — this is a hard stop, not a hint. Pure image requests never fall through to a non-vision pod. If the vision tier is unavailable for the user or configuration, the request fails immediately with HTTP 503. If a vision pod is acquired but then fails, the request returns HTTP 400 — images are not stripped and re-routed to a text model unless the request explicitly sets `downstream_model` (pipeline use). See [tiers.md](tiers.md#auto-routing-logic) for details.

---

## Together AI Dedicated Endpoints

Together's vision models are unavailable on the serverless API — they require a reserved GPU (dedicated endpoint). The `together_dedicated` provider manages this lifecycle identically to RunPod.

```
TOGETHER_API_KEY=<key>
# Add to vision tier provider_overrides in config/tiers.yaml (already set by default)
```

**How it works:**
1. Bridge POSTs to `POST /v1/endpoints` with `{"model": ..., "hardware": ...}`
2. Polls until state = `RUNNING` (up to 5 min)
3. Routes inference to `https://api.together.xyz` using the standard Together API
4. Endpoint is deleted after `idle_timeout_sec` of inactivity

**3-tier vision quality** — model selected based on routing tier:

| Routing tier | Vision model | Hardware |
|---|---|---|
| `simple` | Qwen3-VL-8B-Instruct | L40 48GB → L40S → A100-40GB |
| `vision` | Llama-3.2-11B-Vision-Instruct-Turbo | L40 48GB → L40S → A100 |
| `architecture` / `maximum` / `ultra` | Llama-3.2-90B-Vision-Instruct-Turbo | A100-80GB → H100-80GB |

Hardware is tried in price order; capacity misses fall through to the next option. If Together has no capacity at all, bridge falls back to RunPod.

**Capacity cooldown:** After all hardware options fail, Together is skipped for 10 minutes on subsequent requests — no wasted API calls, no delay before RunPod fallback. After the cooldown expires, the next request retries Together once. No proactive spinning — cooldown only activates on new incoming requests.

**Cost:** Billed per hour by Together while the dedicated endpoint is `RUNNING` (~$1.49–$6.49/hr depending on GPU). No serverless per-token charge.

**Balance:** Together has no public balance API. Dashboard shows a link to https://api.together.ai/settings/billing — check balance there manually.

---

## Vast.ai / Lambda Labs

```
VAST_API_KEY=<key>
LAMBDA_API_KEY=<key>
PROVIDER_PRIORITY=vast,lambda
```

Same pod lifecycle as RunPod. Used as fallback when RunPod has no capacity.
