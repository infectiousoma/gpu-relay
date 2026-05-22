# Preprocessing Pipeline

The bridge includes a local Ollama model that rewrites prompts before sending them to a GPU tier. Off by default.

## Pipeline Stages

| Value | What happens |
|-------|-------------|
| `infer` | Direct GPU inference — no local model (default) |
| `preprocess,infer` | Local 7B rewrites + structures prompt, then GPU infers |
| `preprocess,infer,postprocess` | Adds a local critic/formatter pass after GPU response |

## Enabling

**Globally** (all users, all requests):
```
PIPELINE_DEFAULT=preprocess,infer
```

**Per-request** (via header):
```bash
curl -H "X-Pipeline: preprocess,infer" ...
```

**Per-user** (via DB):
```sql
UPDATE users SET pipeline_default = 'preprocess,infer' WHERE email = 'user@example.com';
```

## Preprocessor Model

```
OLLAMA_PREPROCESSOR_MODEL=qwen2.5-coder:7b-instruct-q4_K_M   # default
```

Any model available in the local Ollama instance works. Smaller is faster; the preprocessor only needs to output structured JSON.

## CPU-only Preprocessor

The `ollama` service in `docker-compose.yml` has a GPU reservation block:
```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

Comment it out and the preprocessor runs on CPU. ~30–60 s per pass on a 7B q4 model — acceptable for light use.
