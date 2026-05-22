# Embeddings

`POST /v1/embeddings` is fully OpenAI-compatible. Proxied to the local Ollama instance — no GPU cost, no cold start.

## Usage

```bash
curl -H "Authorization: Bearer $API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model":"text-embedding-ada-002","input":"hello world"}' \
     http://localhost:8000/v1/embeddings
```

The `model` field is accepted for API compatibility but ignored — the bridge always uses `OLLAMA_EMBEDDING_MODEL`.

## Configuration

```
OLLAMA_EMBEDDING_MODEL=nomic-embed-text   # default (274 MB)
```

Pull the model: `docker compose exec ollama ollama pull nomic-embed-text`

## Open WebUI RAG

In Admin → Settings → Documents:
- Embedding model URL: `http://bridge:8000/v1`
- Model: `nomic-embed-text`
