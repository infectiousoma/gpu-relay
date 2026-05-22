"""FastAPI application: OpenAI-compatible API + bridge management routes.

Routes
------
GET  /healthz                      — liveness probe
GET  /v1/models                    — list tiers as model objects
POST /v1/chat/completions          — main inference (streaming + non-streaming)
POST /v1/embeddings                — embeddings via local Ollama (no GPU cost)
POST /auth/login                   — issue JWT
POST /auth/keys                    — create API key (returns plaintext once)
DELETE /auth/keys/{key_id}         — revoke API key
GET  /admin/pods                   — list pods (admin)
DELETE /admin/pods/{pod_id}        — force terminate pod (admin)
GET  /admin/users                  — list users (admin)
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

import httpx
import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bridge import __version__
from bridge.auth import (
    AdminUser,
    CurrentUser,
    create_access_token,
    generate_api_key,
    hash_password,
    verify_password,
)
from bridge.cost_tracker import estimate_cost, record_request
from bridge.instance_manager import InstanceManager, get_manager
from bridge.multi_model import WORKFLOW_MODELS, WorkflowOrchestrator, run_pipeline, run_postprocess
from bridge.quota import check_daily_tokens, check_monthly_budget, check_rpm
from bridge.router import select_tier
from bridge.schemas import (
    ApiKeyResponse,
    BridgeMeta,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ChatCompletionChoice,
    EmbeddingData,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingUsage,
    ErrorDetail,
    ErrorResponse,
    LoginRequest,
    ModelInfo,
    ModelsResponse,
    TokenResponse,
    Usage,
)
from bridge.settings import settings
from database.models import ApiKey, Request as DBRequest, RequestStatus, User
from database.session import SessionLocal, get_session

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Redis connection (module-level; shared across requests)
# ---------------------------------------------------------------------------

_redis: Redis | None = None


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized")
    return _redis


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis
    _redis = Redis.from_url(settings.redis_url, decode_responses=True)

    manager: InstanceManager = get_manager()
    await manager.start()
    app.state.manager = manager

    log.info("bridge_started", version=__version__)
    yield

    await manager.stop()
    await _redis.aclose()
    log.info("bridge_stopped")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Self-Hosted LLM Bridge",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------------------
# Models list
# ---------------------------------------------------------------------------

@app.get("/v1/models", response_model=ModelsResponse)
async def list_models(user: CurrentUser):
    await check_rpm(user.id, get_redis())
    import yaml
    with open(settings.tiers_config_path) as f:
        tiers = yaml.safe_load(f)["tiers"]

    now_ts = int(time.time())
    models = [
        ModelInfo(id=f"llm-{name}", created=now_ts)
        for name in tiers
    ]
    models.insert(0, ModelInfo(id="llm-auto", created=now_ts))
    for wm in WORKFLOW_MODELS.values():
        models.append(ModelInfo(id=wm["id"], created=now_ts))
    return ModelsResponse(data=models)


# ---------------------------------------------------------------------------
# Embeddings (proxied to local Ollama — no GPU cost)
# ---------------------------------------------------------------------------

@app.post("/v1/embeddings", response_model=EmbeddingResponse)
async def create_embeddings(body: EmbeddingRequest, user: CurrentUser):
    await check_rpm(user.id, get_redis())
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            r = await client.post(
                f"{settings.ollama_local_url}/api/embed",
                json={"model": settings.ollama_embedding_model, "input": body.input},
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Embedding service error: {exc}")
    data = r.json()
    embeddings: list[list[float]] = data.get("embeddings", [])
    prompt_tokens: int = data.get("prompt_eval_count", 0)
    return EmbeddingResponse(
        data=[EmbeddingData(index=i, embedding=emb) for i, emb in enumerate(embeddings)],
        model=settings.ollama_embedding_model,
        usage=EmbeddingUsage(prompt_tokens=prompt_tokens, total_tokens=prompt_tokens),
    )


# ---------------------------------------------------------------------------
# Chat completions (primary route)
# ---------------------------------------------------------------------------

@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    redis = get_redis()
    manager: InstanceManager = request.app.state.manager

    # --- Quota checks ---
    prompt_tokens_est = max(1, sum(len(m.text_content()) for m in body.messages) // 4)
    await check_rpm(user.id, redis)
    await check_daily_tokens(user.id, prompt_tokens_est, redis)

    # --- Monthly spend for routing + budget gate ---
    from sqlalchemy import func
    result = await session.execute(
        select(func.coalesce(func.sum(DBRequest.cost_usd), 0)).where(
            DBRequest.user_id == user.id,
            DBRequest.status == RequestStatus.ok,
            DBRequest.created_at >= datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        )
    )
    monthly_spent = result.scalar_one()

    # --- Check for workflow model ---
    workflow_def = WORKFLOW_MODELS.get(body.model)

    # --- Route ---
    if workflow_def:
        # Workflow models bypass the standard tier router; tier is fixed per workflow.
        # llm-smart determines its tier from the preprocessor; default to "simple" if unset.
        gpu_tier = workflow_def.get("gpu_tier") or "simple"
        from bridge.router import _projected_cost as _pc
        projected = _pc(gpu_tier, prompt_tokens_est)
        from bridge.schemas import RoutingDecision as _RD
        decision = _RD(
            tier=gpu_tier,
            reason=f"workflow:{workflow_def['workflow']}",
            projected_cost_usd=projected,
        )
    else:
        decision = await select_tier(body, user, request, monthly_spent)

    # --- Budget check (after routing in case of downgrade) ---
    await check_monthly_budget(user, decision.projected_cost_usd, session, redis)

    # --- Multi-model pipeline (preprocess stage, non-workflow only) ---
    pipeline = body.pipeline or user.pipeline_default or settings.pipeline_default
    pipeline_meta: dict = {"stages_run": [], "preprocessor_output": None, "errors": []}
    if not workflow_def:
        body, pipeline_meta = await run_pipeline(body, pipeline)

    # --- Acquire pod ---
    try:
        pod = await manager.acquire(decision.tier)
    except Exception as exc:
        log.error("acquire_failed", tier=decision.tier, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No capacity available for tier '{decision.tier}': {exc}",
        )

    # Track user on pod (for concurrent-user cost calc)
    await redis.sadd(f"pod_users:{pod.pod_id}", user.id)
    await redis.expire(f"pod_users:{pod.pod_id}", 3600)

    idempotency_key = request.headers.get("x-idempotency-key")
    api_key_id = getattr(request.state, "api_key_id", None)

    # Streaming: mark_active/release/record_request happen inside event_generator
    # so the active count is accurate during the stream and billing uses real token counts.
    if body.stream and not workflow_def:
        return await _stream_response(
            body, pod, decision, pipeline, pipeline_meta,
            user, session, redis, manager, request,
            idempotency_key, api_key_id,
        )

    manager.mark_active(pod.pod_id)
    start_ms = int(time.time() * 1000)
    error_message: str | None = None
    completion_text = ""
    prompt_tokens = 0
    completion_tokens = 0

    try:
        if workflow_def:
            # --- Workflow orchestration ---
            orchestrator = WorkflowOrchestrator()
            completion_text, wf_meta = await orchestrator.execute(
                workflow_def["workflow"], body, pod.endpoint_url
            )
            pipeline_meta.update(wf_meta)
            prompt_tokens = prompt_tokens_est
            completion_tokens = max(1, len(completion_text) // 4)
        else:
            # --- Standard non-streaming ---
            payload = _build_inference_payload(body, stream=False, model=pod.model or None)
            async with httpx.AsyncClient(timeout=300) as client:
                r = await client.post(
                    f"{pod.endpoint_url}/v1/chat/completions",
                    json=payload,
                    headers=pod.extra_headers,
                )
                r.raise_for_status()
                data = r.json()

            choice = data["choices"][0]
            completion_text = choice["message"]["content"]
            prompt_tokens = data.get("usage", {}).get("prompt_tokens", prompt_tokens_est)
            completion_tokens = data.get("usage", {}).get("completion_tokens", 0)

            # Postprocess
            completion_text = await run_postprocess(completion_text, pipeline, pipeline_meta)

    except Exception as exc:
        error_message = str(exc)
        log.exception("inference_error", pod_id=pod.pod_id, tier=decision.tier)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Inference error: {exc}")
    finally:
        latency_ms = int(time.time() * 1000) - start_ms
        await manager.release(pod.pod_id, session)
        receipt = await record_request(
            user=user,
            pod=pod,
            decision=decision,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            files_referenced=body.files_referenced or 0,
            latency_ms=latency_ms,
            pipeline=pipeline,
            api_key_id=api_key_id,
            idempotency_key=idempotency_key,
            error_message=error_message,
            redis=redis,
            session=session,
        )

    response_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    response = ChatCompletionResponse(
        id=response_id,
        created=int(time.time()),
        model=f"llm-{decision.tier}",
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=completion_text),
                finish_reason="stop",
            )
        ],
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        bridge=BridgeMeta(
            tier=decision.tier,
            routing_reason=decision.reason,
            provider=pod.provider,
            pod_id=pod.pod_id,
            cost_usd=receipt.cost_usd,
            latency_ms=receipt.latency_ms,
        ),
    )

    headers = {
        "X-LLM-Tier": decision.tier,
        "X-LLM-Provider": pod.provider,
        "X-LLM-Cost-USD": str(receipt.cost_usd),
        "X-LLM-Latency-MS": str(receipt.latency_ms),
        "X-LLM-Routing-Reason": decision.reason,
    }
    return JSONResponse(content=response.model_dump(), headers=headers)


async def _stream_response(
    body, pod, decision, pipeline, pipeline_meta,
    user, session, redis, manager, request,
    idempotency_key, api_key_id,
):
    """SSE streaming passthrough from Ollama with cost accounting on close."""
    payload = _build_inference_payload(body, stream=True, model=pod.model or None)

    async def event_generator():
        start_ms = int(time.time() * 1000)
        manager.mark_active(pod.pod_id)
        prompt_tokens = max(1, sum(len(m.text_content()) for m in body.messages) // 4)
        completion_tokens = 0
        error_message = None
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", f"{pod.endpoint_url}/v1/chat/completions", json=payload, headers=pod.extra_headers) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            chunk = line[6:]
                            if chunk.strip() == "[DONE]":
                                yield "data: [DONE]\n\n"
                                break
                            yield f"{line}\n\n"
                            try:
                                import json
                                data = json.loads(chunk)
                                delta = data["choices"][0].get("delta", {}).get("content", "")
                                completion_tokens += len(delta) // 4
                            except Exception:
                                pass
        except Exception as exc:
            error_message = str(exc)
            log.exception("stream_error", pod_id=pod.pod_id)
        finally:
            latency_ms = int(time.time() * 1000) - start_ms
            await manager.release(pod.pod_id, session)
            await record_request(
                user=user,
                pod=pod,
                decision=decision,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                files_referenced=body.files_referenced or 0,
                latency_ms=latency_ms,
                pipeline=pipeline,
                api_key_id=api_key_id,
                idempotency_key=idempotency_key,
                error_message=error_message,
                redis=redis,
                session=session,
            )

    headers = {
        "X-LLM-Tier": decision.tier,
        "X-LLM-Provider": pod.provider,
        "X-LLM-Routing-Reason": decision.reason,
    }
    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=headers)


def _build_inference_payload(body: ChatCompletionRequest, *, stream: bool, model: str | None = None) -> dict:
    payload: dict = {
        "model": model or body.model,
        "messages": [{"role": m.role, "content": m.content} for m in body.messages],
        "stream": stream,
    }
    if body.temperature is not None:
        payload["temperature"] = body.temperature
    if body.max_tokens is not None:
        payload["max_tokens"] = body.max_tokens
    if body.stop is not None:
        payload["stop"] = body.stop
    return payload


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.post("/auth/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    result = await session.execute(select(User).where(User.email == body.email, User.is_active.is_(True)))
    user = result.scalar_one_or_none()
    if user is None or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token, expires_in = create_access_token(user.id)
    return TokenResponse(access_token=token, expires_in=expires_in, role=user.role.value)


@app.post("/auth/keys", response_model=ApiKeyResponse)
async def create_api_key(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    label: str | None = None,
):
    plaintext, key_hash, prefix = generate_api_key()
    row = ApiKey(
        user_id=user.id,
        key_hash=key_hash,
        key_prefix=prefix,
        label=label,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return ApiKeyResponse(
        id=row.id,
        label=row.label,
        key=plaintext,
        prefix=prefix,
        created_at=row.created_at.isoformat(),
    )


@app.delete("/auth/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: str,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    row = await session.get(ApiKey, key_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Key not found")
    row.revoked_at = datetime.now(timezone.utc)
    await session.commit()


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.post("/admin/pods/prewarm", status_code=202)
async def admin_prewarm_pod(
    _user: AdminUser,
    request: Request,
    tier: str = "simple",
):
    """Queue a pod pre-warm for the given tier. Returns immediately; acquisition is async."""
    from bridge.router import TIER_ORDER
    if tier not in TIER_ORDER:
        raise HTTPException(status_code=400, detail=f"Unknown tier: {tier!r}. Must be one of {TIER_ORDER}")
    import asyncio
    manager: InstanceManager = request.app.state.manager
    asyncio.create_task(manager.acquire(tier))
    return {"status": "queued", "tier": tier}


@app.get("/admin/pods")
async def admin_list_pods(
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from database.models import Pod
    result = await session.execute(select(Pod).order_by(Pod.started_at.desc()).limit(200))
    pods = result.scalars().all()
    return [
        {
            "id": p.id, "provider": p.provider, "tier": p.tier,
            "status": p.status, "started_at": p.started_at.isoformat() if p.started_at else None,
            "last_used_at": p.last_used_at.isoformat() if p.last_used_at else None,
            "endpoint_url": p.endpoint_url,
        }
        for p in pods
    ]


@app.delete("/admin/pods/{pod_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_kill_pod(
    pod_id: str,
    user: AdminUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from database.models import Pod
    pod = await session.get(Pod, pod_id)
    if pod is None:
        raise HTTPException(status_code=404, detail="Pod not found")
    manager: InstanceManager = request.app.state.manager
    pod.status = PodStatus.terminated
    pod.terminated_at = datetime.now(timezone.utc)
    await session.commit()
    provider = manager._providers.get(pod.provider)
    if provider:
        import asyncio
        asyncio.create_task(provider.terminate(pod.external_id))


@app.get("/admin/users")
async def admin_list_users(
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    result = await session.execute(select(User).order_by(User.created_at.desc()).limit(500))
    users = result.scalars().all()
    return [
        {
            "id": u.id, "email": u.email, "role": u.role,
            "billing_mode": u.billing_mode, "is_active": u.is_active,
            "monthly_budget_usd": float(u.monthly_budget_usd),
            "prepaid_balance_usd": float(u.prepaid_balance_usd),
            "created_at": u.created_at.isoformat(),
        }
        for u in users
    ]


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=ErrorDetail(
                message=str(exc.detail),
                type="api_error",
                code=str(exc.status_code),
            )
        ).model_dump(),
        headers=getattr(exc, "headers", None),
    )


from database.models import PodStatus
