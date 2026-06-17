# Claude Code Router (CCR)

[claude-code-router](https://github.com/musistudio/claude-code-router) sits between Claude Code and the bridge. It converts the Anthropic API format Claude Code expects into the OpenAI format the bridge speaks, letting you use Claude Code backed by your own local/cloud LLMs.

```
Claude Code → ccr (:3456) → Bridge (:8000) → Groq / local GPU / RunPod / …
```

## Quick Start

### 1. Copy and configure ccr config

```bash
cp config/ccr-config.json.example config/ccr-config.json
```

Edit `config/ccr-config.json`:
- `APIKEY` — pick any secret string; this is what Claude Code uses to authenticate to ccr
- `api_key` for the `bridge` provider — set to your bridge API key (create one via `POST /auth/keys` after logging in)

### 2. Add env vars to `.env`

```bash
CCR_PORT=3456
CCR_BRIDGE_API_KEY=sk-llm-...   # bridge API key created via POST /auth/keys
```

### 3. Start the ccr service

```bash
docker compose up -d ccr
```

### 4. Activate on the host before running Claude Code

```bash
source scripts/ccr-activate.sh
claude
```

The script reads `APIKEY` from `config/ccr-config.json` and exports:
- `ANTHROPIC_BASE_URL=http://localhost:3456`
- `ANTHROPIC_AUTH_TOKEN=<APIKEY from ccr-config.json>`

Add `--save-rc` to persist to `~/.bashrc` / `~/.zshrc`.

---

## ccr-config.json

```json
{
  "APIKEY": "pick-any-secret",
  "HOST": "0.0.0.0",
  "LOG": true,
  "API_TIMEOUT_MS": 600000,
  "Providers": [
    {
      "name": "bridge",
      "api_base_url": "http://bridge:8000/v1/chat/completions",
      "api_key": "$CCR_BRIDGE_API_KEY",
      "models": ["simple", "mid", "architecture", "maximum", "ultra", "llm-local"]
    }
  ],
  "Router": {
    "default":    "bridge,architecture",
    "background": "bridge,simple",
    "think":      "bridge,maximum",
    "longContext": "bridge,ultra"
  }
}
```

`Router` maps ccr request types to `provider,model` pairs. The `model` value is passed as the `model` field to the bridge, which treats it as a tier name.

| ccr type | when used | bridge tier |
|----------|-----------|-------------|
| `default` | most requests | `architecture` |
| `background` | low-priority / short tasks | `simple` |
| `think` | extended thinking | `maximum` |
| `longContext` | large context window | `ultra` |

---

## How Auth Works

Two separate keys:

| Key | Where set | Purpose |
|-----|-----------|---------|
| `APIKEY` in ccr-config.json | Claude Code → ccr | Claude Code authenticates to ccr |
| `CCR_BRIDGE_API_KEY` in `.env` | ccr → bridge | ccr authenticates to bridge |

The bridge API key is created once via `POST /auth/keys` (requires login JWT). It's a `sk-llm-...` key stored SHA-256 hashed in the bridge DB.

---

## Tool Calling

ccr converts Claude Code's Anthropic-format tool definitions to OpenAI format before forwarding to the bridge. The bridge strips verbose descriptions from tool schemas (`_slim_tools` in `bridge/main.py`) before forwarding to **all** providers — this is applied unconditionally, including Ollama/RunPod. Claude Code sends 26+ tools with long descriptions (~64K tokens); stripping cuts this to ~5K, which:
- Keeps requests within Groq's free-tier TPM limit
- Prevents Cloudflare 524 timeout on RunPod — full schemas caused 126s+ generation time for 32B models, exceeding Cloudflare's ~100s proxy timeout

Tool calls return as `tool_calls` in OpenAI format; ccr converts them back to Anthropic `tool_use` blocks for Claude Code.

## Two-Model Routing (Ollama tiers)

For Ollama-backed tiers (RunPod, Vast, local), the bridge routes to different models based on whether the request includes tool definitions:

| Request type | Model used |
|---|---|
| Has tools (Claude Code tool-call session) | `model` in tiers.yaml (e.g. `qwen2.5:32b-instruct-q4_K_M`) |
| No tools (plain chat, title generation) | `model_no_tools` in tiers.yaml (e.g. `qwen2.5-coder:32b-instruct-q4_K_M`) |

The `architecture` tier uses `qwen2.5:32b` for tool sessions because the instruct variant handles tool call JSON correctly, and falls back to `qwen2.5-coder:32b` for plain chat (faster text generation). If the coder model isn't pulled on the pod yet, the bridge falls back to the primary model automatically.

---

## Groq Free Tier Limits

Groq's free (on_demand) tier has a 12,000 TPM limit. Even with tool stripping, a full Claude Code session context may hit this limit. Options:

1. **Upgrade Groq to Dev Tier** — removes the tight TPM cap
2. **Use `simple` tier** for lightweight tasks (routes to `llama-3.1-8b-instant`, higher TPM allowance)
3. **Use local GPU** — no TPM limits; set `provider_overrides: [local, groq, ...]` in `config/tiers.yaml`

---

## Files

| File | Purpose |
|------|---------|
| `docker/Dockerfile.ccr` | Node 20 Alpine image with ccr installed |
| `docker/ccr-entrypoint.sh` | Handles ccr daemonization, monitors port 3456 |
| `config/ccr-config.json.example` | Config template |
| `config/ccr-config.json` | Live config (gitignored — contains secrets) |
| `scripts/ccr-activate.sh` | Host-side script to set `ANTHROPIC_*` env vars |
