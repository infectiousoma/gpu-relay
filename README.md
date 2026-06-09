# Self-Hosted LLM Infrastructure

Multi-tenant, OpenAI-compatible LLM gateway. Route requests to cloud GPU pods (RunPod, Vast.ai, Lambda Labs), a local GPU, or commercial APIs — all behind one API endpoint.

**Use cases:**
- Self-host large models (7B–70B) on rented GPUs; pay only when running
- Drop-in replacement for OpenAI API — works with Open WebUI, Cursor, any OpenAI client
- Mix providers: local GPU for dev, cloud GPU for heavy loads, OpenAI as fallback
- Per-user billing, quotas, and tier restrictions for team deployments

## Index

- [Quickstart](#quickstart)
- [Endpoints](#endpoints)
- [Tiers & Models](docs/tiers.md)
- [Providers](docs/providers.md) — RunPod, local GPU, OpenAI, Groq, DeepSeek, etc.
- [CLI Reference](docs/cli.md) — user management, API keys, billing, observability
- [Embeddings](docs/embeddings.md)
- [Preprocessing Pipeline](docs/pipeline.md)
- [Development & Architecture](docs/development.md)
- [Website & Wiki Deployment](docs/deployment.md) — GitHub Pages, Netlify, Vercel, GitHub Wiki

## Quickstart

```bash
# 1. Clone and configure
cp .env.example .env
$EDITOR .env   # set provider keys, DB password, JWT secret

# 2. One-shot setup (builds images, starts stack, runs migrations, bootstraps admin)
bash scripts/setup.sh

# 3. Smoke test
curl -H "Authorization: Bearer $API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model":"llm-simple","messages":[{"role":"user","content":"hello"}]}' \
     http://localhost:8000/v1/chat/completions
```

## Endpoints

| Service | Port | URL |
|---------|------|-----|
| Bridge API | 8000 | http://localhost:8000 |
| Dashboard | 8501 | http://localhost:8501 |
| Open WebUI | 3000 | http://localhost:3000 |
| Postgres | 5432 | (internal) |
| Redis | 6379 | (internal) |
| Ollama | 11434 | (internal) |

## Basic Usage

Send requests using the OpenAI API format:

```bash
# Use a specific tier
curl -H "Authorization: Bearer $API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model":"llm-architecture","messages":[{"role":"user","content":"review this code"}]}' \
     http://localhost:8000/v1/chat/completions

# Let the router pick (based on complexity)
curl ... -d '{"model":"llm-auto","messages":[...]}'

# Force a tier per-request
curl -H "X-Tier: simple" ...
```

Available models: `llm-simple`, `llm-architecture`, `llm-maximum`, `llm-ultra`, `llm-vision`, `llm-auto`

See [Tiers & Models](docs/tiers.md) for full details and tier locking options.

## Testing Without a GPU

```bash
MOCK_PROVIDERS=1 ./scripts/smoke_test.sh
```

Routes all requests to the local Ollama service — no cloud account, no billing. See [Development](docs/development.md).
