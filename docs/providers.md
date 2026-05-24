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

**GPU fallback**: if the preferred GPU (e.g. RTX 4090) has no capacity at deploy time, the bridge automatically tries the next viable GPU type in preference order (`TIER_GPU_PREFERENCE` in `providers/base.py`) before failing. Returns 503 to the client only when all candidates are exhausted. Preference order for each tier:

| Tier | GPU preference order |
|------|----------------------|
| `simple` / `architecture` / `vision` | RTX 4090 → RTX 3090 → A40 → A6000 → cheapest ≥8 GB |
| `maximum` | L40S → L40 → A40 → A100 40GB → cheapest ≥38 GB |
| `ultra` | A100 80GB → H100 → cheapest ≥50 GB |

Cold starts pull the Docker image + model on first use:
- 7B model: ~3–5 min first run, ~30 s with network volume
- 32B model: ~10–15 min first run, ~30 s with network volume

### Optional: Persistent Model Cache

Without a network volume, models re-download on every cold start.

1. Verify the stack works without a volume first
2. RunPod dashboard → Storage → Network Volumes → create 100 GB (same datacenter as pods)
3. Copy the volume ID
4. Add to `.env`: `RUNPOD_NETWORK_VOLUME_ID=<id>`
5. Restart bridge: `docker compose up -d bridge`

Cost: ~$7–8/month. Cuts cold starts from minutes to ~30 s.

> **Warning:** If you delete the volume, clear `RUNPOD_NETWORK_VOLUME_ID` from `.env`. A stale ID causes every launch to fail.

---

## Local GPU

Route to the Ollama instance running on this machine — no cloud account needed.

```
PROVIDER_PRIORITY=local
```

Uses `OLLAMA_LOCAL_URL` (default: `http://ollama:11434`, the stack's built-in Ollama service). Models are pulled on first request.

**CPU-only:** Remove the `deploy:` GPU block from the `ollama` service in `docker-compose.yml`. A 7B q4 model on CPU takes ~30–60 s per request.

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

**Multimodal (images)**: Requests containing `image_url` content parts are automatically routed to the `vision` tier (LLaVA) before any other routing logic. If the requested model already supports vision (`gpt-4o`, `llava`, `gemini`, etc.) the request passes through unchanged. If no vision pod is available, behavior depends on `config/tiers.yaml` → `vision.fallback` (`strip_images` or `error`). See [tiers.md](tiers.md#auto-routing-logic) for details.

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

---

## Vast.ai / Lambda Labs

```
VAST_API_KEY=<key>
LAMBDA_API_KEY=<key>
PROVIDER_PRIORITY=vast,lambda
```

Same pod lifecycle as RunPod. Used as fallback when RunPod has no capacity.
