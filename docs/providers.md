# Providers

The bridge supports four provider categories, mixed freely via `PROVIDER_PRIORITY` in `.env`.

| Provider | Type | Description |
|----------|------|-------------|
| `runpod`, `vast`, `lambda` | Cloud GPU pod | Rents a GPU, launches Ollama, terminates when idle |
| `local` | Local GPU | Routes to Ollama on this machine — zero cloud cost |
| `openai`, `groq`, `together`, `mistral`, `deepseek` | Commercial API | Calls vendor API directly — no pod management |

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
| `simple` / `architecture` | RTX 4090 → RTX 3090 → A40 → A6000 → cheapest ≥8 GB |
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

Override any model mapping:
```
OPENAI_MODEL_SIMPLE=gpt-4o-mini
OPENAI_MODEL_ARCHITECTURE=o1-mini
```

> **Note:** Anthropic (Claude) uses a different wire format and is not yet supported.

**Multimodal (images)**: API providers that support vision (e.g. `openai` with `gpt-4o`, `together` with Llama vision models) accept `image_url` content parts in the OpenAI format. The bridge passes them through unchanged. Pod providers running standard Qwen/DeepSeek models do not support vision.

---

## Vast.ai / Lambda Labs

```
VAST_API_KEY=<key>
LAMBDA_API_KEY=<key>
PROVIDER_PRIORITY=vast,lambda
```

Same pod lifecycle as RunPod. Used as fallback when RunPod has no capacity.
