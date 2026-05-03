# Self-Hosted LLM Infrastructure

Multi-tenant, multi-provider LLM serving with intelligent tier routing. OpenAI-compatible API in front of RunPod / Vast.ai / Lambda Labs GPUs running Ollama.

See [`claude.md`](./claude.md) for full architecture.

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

| Service     | Port | URL                       |
|-------------|------|---------------------------|
| Bridge API  | 8000 | http://localhost:8000     |
| Dashboard   | 8501 | http://localhost:8501     |
| Open WebUI  | 3000 | http://localhost:3000     |
| Postgres    | 5432 | (internal)                |
| Redis       | 6379 | (internal)                |
| Ollama      | 11434| (internal preprocessor)   |

## Tiers

Available models (use as `model` field in OpenAI requests):

**Tier models** (direct GPU rental):
- `llm-simple` — Qwen2.5-Coder 7B on RTX 4090 — $0.34/hr
- `llm-architecture` — Qwen2.5-Coder 32B on RTX 4090 — $0.34/hr
- `llm-maximum` — DeepSeek V3 on A100 40GB — $1.10/hr
- `llm-ultra` — Qwen2.5 72B on A100 80GB — $1.89/hr
- `llm-auto` — let the router pick (default)

**Workflow models** (multi-step orchestration via Open WebUI):
- `llm-smart` — local optimizer → best available GPU tier
- `llm-code-review` — Ollama preprocessor → architecture tier reviewer
- `llm-refactor` — Ollama preprocessor → architecture tier refactorer
- `llm-arch-design` — Ollama preprocessor → maximum tier architect

## CLI

```bash
llmctl users add <email>                        # create user, print API key
llmctl users budget <email> --usd 50            # set monthly cap
llmctl users credit-add <email> --usd 20        # add prepaid credit
llmctl pods ls [--tier simple]                  # list running pods
llmctl pods kill <pod_id>                       # terminate
llmctl pods start --tier architecture           # prewarm a pod
llmctl bills run --month 2026-05                # generate invoices
llmctl models [--user-type personal]            # tier table with effective $/hr
llmctl status [--tier architecture]             # active pods + running cost
llmctl budget [--email u@example.com]           # spend vs cap progress bars
llmctl costs [--month 2026-05] [--email ...]    # per-tier cost breakdown
```

## Development

```bash
pip install -r requirements.txt
pre-commit install
pytest
```
