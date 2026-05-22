# Tiers & Model Selection

## Available Tiers

### Pod providers (RunPod / Vast.ai / Lambda Labs)

| Tier | Model | GPU | $/hr (est.) |
|------|-------|-----|-------------|
| `simple` | Qwen2.5-Coder 7B | RTX 4090 | ~$0.69 |
| `architecture` | Qwen2.5-Coder 32B | RTX 4090 | ~$0.69 |
| `maximum` | DeepSeek V3 | L40S 48GB | ~$1.14 |
| `ultra` | Qwen2.5 72B | A100 80GB | ~$1.89 |

Prices sync from live RunPod GPU catalog at startup. Budget gate uses projected hourly cost.

### API providers (OpenAI / Groq / Together / Mistral / DeepSeek)

| Tier | OpenAI | Groq | Together | Mistral | DeepSeek |
|------|--------|------|----------|---------|----------|
| `simple` | gpt-4o-mini | llama-3.1-8b-instant | Llama-3.2-11B | mistral-small | deepseek-chat |
| `architecture` | gpt-4o | llama-3.3-70b | Llama-3.1-70B | mistral-medium | deepseek-chat |
| `maximum` | gpt-4o | llama-3.3-70b | Llama-3.1-405B | mistral-large | deepseek-reasoner |
| `ultra` | gpt-4o | llama-3.3-70b | Llama-3.1-405B | mistral-large | deepseek-reasoner |

API providers have no hourly cost — budget gate is skipped when no pod providers are active. Override any model via env var (e.g. `OPENAI_MODEL_ARCHITECTURE=o1-mini`).

### Local GPU

Same Ollama models as pod providers. Zero cost — budget gate skipped.

## Model Names (OpenAI `model` field)

**Tier models** (direct GPU rental):
- `llm-simple`
- `llm-architecture`
- `llm-maximum`
- `llm-ultra`
- `llm-auto` — router picks based on request complexity

**Workflow models** (multi-step orchestration):
- `llm-smart` — local optimizer → best available tier
- `llm-code-review` — preprocessor → architecture tier
- `llm-refactor` — preprocessor → architecture tier
- `llm-arch-design` — preprocessor → maximum tier

## Auto-Routing Logic

When using `llm-auto`, the router picks a tier based on (priority order):

1. `X-Tier` header / `?tier=` query param
2. Per-user `allowed_tiers` whitelist
3. Budget gate (downgrade or 402)
4. Token count thresholds
5. File count thresholds
6. Complexity keywords
7. Default: `simple`

## Tier Locking

**Per-request override** — bypasses all auto-routing:
```bash
curl -H "X-Tier: simple" ...
curl "...?tier=architecture" ...
```

**Per-user whitelist** — restrict a user to specific tiers:
```bash
llmctl users tiers user@example.com                        # show current
llmctl users tiers user@example.com --set simple           # lock to one
llmctl users tiers user@example.com --set simple,architecture
llmctl users tiers user@example.com --set all              # remove restriction
```

**Budget gate** — router auto-downgrades when projected cost exceeds limit:
```bash
llmctl users budget user@example.com --usd 5   # ~7 hrs simple, blocks ultra
```
