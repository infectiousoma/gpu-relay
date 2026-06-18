"""Gateway — lightweight reverse proxy to a remote bridge.

Forwards all OpenAI-compatible API traffic to GATEWAY_BRIDGE_URL,
passing the caller's Authorization header through unchanged.
This lets users outside the LAN point their OpenAI client at the gateway
without needing a direct connection to the bridge.

Environment variables:
  GATEWAY_BRIDGE_URL   Remote bridge base URL (e.g. https://bridge.example.com)
  GATEWAY_PORT         Listen port (default 8080)
  GATEWAY_LOG_LEVEL    DEBUG | INFO | WARNING (default INFO)
"""

from __future__ import annotations

import os
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

BRIDGE_URL = os.environ.get("GATEWAY_BRIDGE_URL", "http://bridge:8000").rstrip("/")
LOG_LEVEL = os.environ.get("GATEWAY_LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gateway")

_client: httpx.AsyncClient | None = None

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-encoding", "content-length",
})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient(timeout=300, follow_redirects=False)
    log.info("gateway started  bridge=%s", BRIDGE_URL)
    yield
    await _client.aclose()
    log.info("gateway stopped")


app = FastAPI(title="LLM Gateway", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok", "bridge": BRIDGE_URL}


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy(path: str, request: Request) -> Response:
    assert _client is not None
    target_url = f"{BRIDGE_URL}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    headers["host"] = httpx.URL(BRIDGE_URL).host

    body = await request.body()

    # Detect streaming: either Accept header or stream:true in JSON body
    wants_stream = (
        "text/event-stream" in request.headers.get("accept", "")
        or (body and b'"stream":true' in body)
    )

    upstream_req = _client.build_request(
        request.method,
        target_url,
        headers=headers,
        content=body,
    )

    if wants_stream:
        upstream_resp = await _client.send(upstream_req, stream=True)
        response_headers = {
            k: v for k, v in upstream_resp.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }

        async def _body_gen():
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            _body_gen(),
            status_code=upstream_resp.status_code,
            headers=response_headers,
            media_type=upstream_resp.headers.get("content-type", "text/event-stream"),
        )

    resp = await _client.send(upstream_req)
    response_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    log.debug("proxy %s /%s -> %d", request.method, path, resp.status_code)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=response_headers,
        media_type=resp.headers.get("content-type"),
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("GATEWAY_PORT", "8080"))
    uvicorn.run("gateway.main:app", host="0.0.0.0", port=port, log_level=LOG_LEVEL.lower())
