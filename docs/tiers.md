# Tiers & Model Selection

## Available Tiers

### Pod providers (RunPod / Vast.ai / Lambda Labs)

| Tier | Model | GPU | $/hr (est.) |
|------|-------|-----|-------------|
| `simple` | Qwen2.5-Coder 7B | RTX 4090 | ~$0.69 |
| `mid` | Gemma4-Coder 12B | RTX 4090 | ~$0.69 |
| `architecture` | Qwen2.5-Coder 32B | RTX 4090 | ~$0.69 |
| `maximum` | DeepSeek V3 | L40S 48GB | ~$1.14 |
| `ultra` | Qwen2.5 72B | A100 80GB | ~$1.89 |
| `vision` | Llama-3.2-11B-Vision (Together dedicated) / MiniCPM-V (RunPod) | L40 / RTX 4090 | ~$0.69–1.49 |

**Vision quality tiers** (Together dedicated endpoints — `providers/together_dedicated.py`):

| Routing tier | Model | Hardware |
|---|---|---|
| `simple` | Qwen3-VL-8B-Instruct | L40 48GB / L40S / A100-40GB |
| `vision` | Llama-3.2-11B-Vision-Instruct-Turbo | L40 48GB / L40S / A100 |
| `architecture` / `maximum` / `ultra` | Llama-3.2-90B-Vision-Instruct-Turbo | A100-80GB / H100-80GB |

All vision models are capable of text too. Endpoint spins up on first image request, terminates after idle timeout.

Prices sync from live RunPod GPU catalog at startup. Budget gate uses projected hourly cost.

### API providers (OpenAI / Groq / Together / Mistral / DeepSeek)

| Tier | OpenAI | Groq | Together | Mistral | DeepSeek |
|------|--------|------|----------|---------|----------|
| `simple` | gpt-4o-mini | llama-3.1-8b-instant | Llama-3.2-11B | mistral-small | deepseek-chat |
| `mid` | gpt-4o-mini | llama-3.3-70b-versatile | Llama-3.1-70B | mistral-medium | deepseek-chat |
| `architecture` | gpt-4o | llama-3.3-70b | Llama-3.1-70B | mistral-medium | deepseek-chat |
| `maximum` | gpt-4o | llama-3.3-70b | Llama-3.1-405B | mistral-large | deepseek-reasoner |
| `ultra` | gpt-4o | llama-3.3-70b | Llama-3.1-405B | mistral-large | deepseek-reasoner |

API providers are billed per token (not per hour). Cost = `(prompt_tokens/1K × input_rate) + (completion_tokens/1K × output_rate)` using rates from `config/tiers.yaml` → `api_token_costs`. Update that section when a provider changes pricing — no code change needed.

Override any model via env var (e.g. `OPENAI_MODEL_ARCHITECTURE=o1-mini`).

### Local GPU

Same Ollama models as pod providers. Zero cost — budget gate skipped.

## Model Names (OpenAI `model` field)

**Tier models** (direct GPU rental):
- `llm-simple`
- `llm-mid`
- `llm-architecture`
- `llm-maximum`
- `llm-ultra`
- `llm-vision` — vision model (Together: Llama-3.2-11B / RunPod: MiniCPM-V); selected automatically for image requests
- `llm-auto` — router picks based on request complexity

**Workflow models** (multi-step orchestration):
- `llm-smart` — local optimizer → best available tier
- `llm-code-review` — preprocessor → architecture tier
- `llm-refactor` — preprocessor → architecture tier
- `llm-arch-design` — preprocessor → maximum tier
- `llm-visual-html` — LLaVA describes image → local coder generates HTML with image embedded as base64

## Auto-Routing Logic

When using `llm-auto`, the router picks a tier based on (priority order):

0. **Image content** (`image_url` parts) → `vision` tier — hard stop, no fallthrough (see below)
1. `X-Tier` header / `?tier=` query param
2. Per-user `allowed_tiers` whitelist
3. Budget gate (downgrade or 402)
4. Token count thresholds
5. File count thresholds
6. Complexity keywords
7. Default: `simple`

### Vision routing

Any request containing `image_url` content parts routes exclusively to the `vision` tier. Steps 1–7 are skipped entirely for image requests.

**If vision tier is unavailable** (not configured or user lacks access): HTTP 503 immediately — no fallthrough to a text tier.

**If a vision pod fails to acquire**: HTTP 400 — images are never silently stripped and re-routed to a text model unless the request sets `downstream_model` (multi-model pipeline use case).

**Pipeline exception**: Set `downstream_model` on the request to signal that vision output feeds into a second model. In that case, if the vision pod fails and `config/tiers.yaml → vision.fallback = strip_images`, images are stripped and the request is re-routed to the downstream text tier.

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
