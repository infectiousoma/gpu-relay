"""GPU pod pool manager.

Pod lifecycle — one independent lifecycle per pod_type (vision, simple, architecture, etc.):

  [request: tier=T]
       │
       ▼
  _get_lock("T")           ← per-type lock; vision never blocks simple, etc.
       │
       ▼
  DB: ready pod? ──────────yes──────────────────────────────► return handle
       │ no                                                    (_pods[T] = pod_id)
       ▼
  in-progress pod? ──yes──► poll DB every 5s ──► pod.ready ──► return handle
       │ no
       ▼
  _spinning_tiers.add("T")
       │
       ▼
  provider.launch(T)
       │
  provisioning ──► starting ──► ready ──► mark_active() / inference / release()
                                  │                               │
                              _pods[T] = pod_id          last_used_at = now
                                                                  │
                                                        idle_reaper (per-tier timeout)
                                                                  │
                                                             terminated
                                                        _pods.pop(T, None)

  Pod types are fully decoupled: vision pod spins/terminates independently of simple.
  Each type has: its own _get_lock(), its own _spinning_tiers entry, its own idle timeout.

State machine per pod:
  provisioning → starting → ready → terminated | failed

Pool logic:
  - One pod per tier kept warm while last_used + idle_timeout > now.
  - acquire(tier): return ready pod or spin a new one.
  - release(pod): update last_used; pool reaper decides termination.
  - Health loop: GET /api/tags every health_check_interval_sec;
    3 consecutive fails → mark failed → terminate.
  - Idle reaper: every idle_reaper_interval_sec, terminate pods past their
    idle_timeout if no active requests are using them.

Provider selection follows settings.provider_priority_list (RunPod → Vast → Lambda).
On provider capacity/5xx error, tries next in list before raising.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridge.schemas import PodHandle
from bridge.settings import settings
from database.models import Pod, PodStatus, TierName
from database.session import SessionLocal

if TYPE_CHECKING:
    from providers.base import BaseProvider

log = structlog.get_logger(__name__)


class InstanceManager:
    """Singleton accessed via app.state.instance_manager."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}  # keyed by pod_type/tier
        self._pods: dict[str, str] = {}  # pod_type → pod_id of current active pod
        self._providers: dict[str, "BaseProvider"] = {}
        self._active_requests: dict[str, int] = {}  # pod_id → count
        self._spinning_tiers: set[str] = set()  # tiers currently being launched
        self._reaper_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None

    def _get_lock(self, pod_type: str) -> asyncio.Lock:
        if pod_type not in self._locks:
            self._locks[pod_type] = asyncio.Lock()
        return self._locks[pod_type]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if settings.mock_providers:
            from providers.mock import MockProvider
            self._providers = {"mock": MockProvider()}
            log.info("instance_manager_mock_mode")
        else:
            from providers.runpod import RunPodProvider
            from providers.vast import VastProvider
            from providers.lambda_labs import LambdaProvider
            from providers.local import LocalProvider
            from providers.together_dedicated import TogetherDedicatedProvider
            from providers.api_compat import (
                OpenAIProvider, GroqProvider, TogetherProvider,
                MistralProvider, DeepSeekProvider,
            )
            candidates = {
                "runpod":             RunPodProvider(),
                "vast":               VastProvider(),
                "lambda":             LambdaProvider(),
                "local":              LocalProvider(),
                "together_dedicated": TogetherDedicatedProvider(),
                "openai":             OpenAIProvider(),
                "groq":               GroqProvider(),
                "together":           TogetherProvider(),
                "mistral":            MistralProvider(),
                "deepseek":           DeepSeekProvider(),
            }
            self._providers = {n: p for n, p in candidates.items() if p.is_configured()}
            if not self._providers:
                log.warning("no_providers_configured")
            else:
                log.info("providers_configured", providers=list(self._providers))
        await self._cleanup_stale_pods()
        await self._reset_api_provider_pods()
        await self._cleanup_dedicated_pods()
        self._reaper_task = asyncio.create_task(self._reaper_loop(), name="idle-reaper")
        self._health_task = asyncio.create_task(self._health_loop(), name="health-checker")
        asyncio.create_task(self._sync_prices(), name="price-sync")
        log.info("instance_manager_started")

    async def stop(self) -> None:
        for task in (self._reaper_task, self._health_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        log.info("instance_manager_stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(
        self,
        tier: str,
        provider_override: str | None = None,
        disabled_providers: list[str] | None = None,
        provider_order: list[str] | None = None,
        user_label: str | None = None,
    ) -> PodHandle:
        """Return a ready PodHandle; spins a new pod if none available.

        provider_override: if set (from X-Provider header), try this provider first.
        disabled_providers: per-user list of providers to skip.
        provider_order: user's preferred provider order; listed providers bubble to front.
        user_label: short label (email prefix) appended to provider pod names for visibility.
        """
        _disabled = set(disabled_providers or [])

        # Fast path: existing ready pod
        async with SessionLocal() as session:
            pod = await self._get_ready_pod(tier, session, provider=provider_override, excluded_providers=_disabled or None)
            if pod:
                self._pods[tier] = pod.id
                return self._handle(pod, cold_start=False)

        # Under tier-specific lock: re-check DB for ready or in-progress pod; claim spin rights if clear.
        # DB check makes this cross-process safe (multiple uvicorn workers share the DB).
        should_spin = False
        async with self._get_lock(tier):
            async with SessionLocal() as session:
                pod = await self._get_ready_pod(tier, session, provider=provider_override, excluded_providers=_disabled or None)
                if pod:
                    self._pods[tier] = pod.id
                    return self._handle(pod, cold_start=False)
                in_progress = await self._get_in_progress_pod(tier, session, excluded_providers=_disabled or None)
            if not in_progress and tier not in self._spinning_tiers:
                self._spinning_tiers.add(tier)
                should_spin = True

        if not should_spin:
            # Another coroutine/process is already launching this tier — wait for ready
            deadline = asyncio.get_event_loop().time() + settings.cold_start_timeout_sec + 120
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(5)
                async with SessionLocal() as session:
                    pod = await self._get_ready_pod(tier, session, provider=provider_override, excluded_providers=_disabled or None)
                    if pod:
                        self._pods[tier] = pod.id
                        return self._handle(pod, cold_start=False)
            raise RuntimeError(f"Timed out waiting for in-progress pod for tier={tier}")

        # Claimed spin rights — try providers in priority order
        last_error: Exception | None = None
        try:
            if settings.mock_providers:
                priority = ["mock"]
            else:
                from bridge.router import get_tiers
                _overrides = get_tiers().get(tier, {}).get("provider_overrides") or []
                priority = _overrides if _overrides else settings.provider_priority_list

            # Apply user's custom provider order (listed providers bubble to front)
            if provider_order:
                front = [p for p in provider_order if p in priority]
                rest = [p for p in priority if p not in provider_order]
                priority = front + rest

            # Per-request override: bubble requested provider to front
            if provider_override and provider_override in self._providers:
                priority = [provider_override] + [p for p in priority if p != provider_override]

            # Filter out providers the user has disabled
            if _disabled:
                priority = [p for p in priority if p not in _disabled]

            for provider_name in priority:
                provider = self._providers.get(provider_name)
                if provider is None:
                    continue
                try:
                    pod_handle = await self._spin_pod(tier, provider_name, provider, user_label=user_label)
                    self._pods[tier] = pod_handle.pod_id
                    return pod_handle
                except Exception as exc:
                    log.warning("provider_failed", provider=provider_name, tier=tier, error=str(exc))
                    last_error = exc

            raise RuntimeError(f"All providers failed for tier={tier}: {last_error}") from last_error
        finally:
            self._spinning_tiers.discard(tier)

    def mark_active(self, pod_id: str) -> None:
        self._active_requests[pod_id] = self._active_requests.get(pod_id, 0) + 1

    async def release(self, pod_id: str, session: AsyncSession) -> None:
        self._active_requests[pod_id] = max(0, self._active_requests.get(pod_id, 0) - 1)
        pod = await session.get(Pod, pod_id)
        if pod:
            pod.last_used_at = datetime.now(timezone.utc)
            await session.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_ready_pod(self, tier: str, session: AsyncSession, provider: str | None = None, excluded_providers: set[str] | None = None) -> Pod | None:
        q = select(Pod).where(Pod.tier == tier, Pod.status == PodStatus.ready)
        if provider:
            q = q.where(Pod.provider == provider)
        if excluded_providers:
            q = q.where(Pod.provider.notin_(excluded_providers))
        result = await session.execute(q.limit(1))
        return result.scalar_one_or_none()

    async def _get_in_progress_pod(self, tier: str, session: AsyncSession, excluded_providers: set[str] | None = None) -> Pod | None:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.cold_start_timeout_sec)
        q = select(Pod).where(
            Pod.tier == tier,
            Pod.status.in_([PodStatus.provisioning, PodStatus.starting]),
            Pod.started_at >= cutoff,
        )
        if excluded_providers:
            q = q.where(Pod.provider.notin_(excluded_providers))
        result = await session.execute(q.limit(1))
        return result.scalar_one_or_none()

    async def _spin_pod(self, tier: str, provider_name: str, provider: "BaseProvider", user_label: str | None = None) -> PodHandle:
        # API providers are stateless — upsert the single canonical row per (provider, tier).
        if getattr(provider, "provider_type", "") == "api":
            return await self._spin_api_pod(tier, provider_name, provider)

        pod_id = str(uuid.uuid4())
        log.info("spinning_pod", tier=tier, provider=provider_name, pod_id=pod_id)

        async with SessionLocal() as session:
            pod = Pod(
                id=pod_id,
                provider=provider_name,
                tier=tier,
                gpu="",
                model="",
                external_id=f"pending-{pod_id}",
                status=PodStatus.provisioning,
                cost_per_hour_usd=0,
            )
            session.add(pod)
            await session.commit()

        try:
            info = await provider.launch(tier, user_label=user_label)
        except Exception:
            async with SessionLocal() as session:
                pod = await session.get(Pod, pod_id)
                if pod:
                    pod.status = PodStatus.failed
                    await session.commit()
            raise

        async with SessionLocal() as session:
            pod = await session.get(Pod, pod_id)
            pod.external_id = info.external_id
            pod.gpu = info.gpu
            pod.model = info.model
            pod.cost_per_hour_usd = info.cost_per_hour_usd
            pod.endpoint_url = info.endpoint_url
            pod.status = PodStatus.starting
            await session.commit()

        if getattr(provider, "needs_ollama_check", True):
            endpoint = await self._wait_for_ready(info.endpoint_url, pod_id)
            if info.model:
                await self._pull_model(endpoint, info.model, pod_id)
        else:
            # Provider handled readiness in launch(); endpoint is already serving.
            endpoint = info.endpoint_url

        async with SessionLocal() as session:
            pod = await session.get(Pod, pod_id)
            pod.status = PodStatus.ready
            pod.ready_at = datetime.now(timezone.utc)
            pod.last_used_at = datetime.now(timezone.utc)
            await session.commit()
            return PodHandle(
                pod_id=pod_id,
                provider=provider_name,
                tier=tier,
                endpoint_url=endpoint,
                cost_per_hour_usd=float(pod.cost_per_hour_usd),
                model=pod.model or "",
                cold_start=True,
                extra_headers=provider.extra_request_headers(),
            )

    async def _spin_api_pod(self, tier: str, provider_name: str, provider: "BaseProvider") -> PodHandle:
        """Upsert the single canonical DB row for a stateless API provider."""
        info = await provider.launch(tier)
        log.info("spinning_pod", tier=tier, provider=provider_name, pod_id=info.external_id)
        now = datetime.now(timezone.utc)
        async with SessionLocal() as session:
            result = await session.execute(
                select(Pod).where(Pod.provider == provider_name, Pod.external_id == info.external_id)
            )
            pod = result.scalar_one_or_none()
            if pod is None:
                pod = Pod(
                    id=str(uuid.uuid4()),
                    provider=provider_name,
                    tier=tier,
                    external_id=info.external_id,
                )
                session.add(pod)
            pod.gpu = info.gpu
            pod.model = info.model
            pod.cost_per_hour_usd = info.cost_per_hour_usd
            pod.endpoint_url = info.endpoint_url
            pod.status = PodStatus.ready
            pod.ready_at = now
            pod.last_used_at = now
            await session.commit()
            return PodHandle(
                pod_id=pod.id,
                provider=provider_name,
                tier=tier,
                endpoint_url=info.endpoint_url,
                cost_per_hour_usd=0.0,
                model=info.model,
                cold_start=False,
                extra_headers=provider.extra_request_headers(),
            )

    async def _wait_for_ready(self, endpoint_url: str, pod_id: str) -> str:
        deadline = asyncio.get_event_loop().time() + settings.cold_start_timeout_sec
        async with httpx.AsyncClient(timeout=10) as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    r = await client.get(f"{endpoint_url}/api/tags")
                    if r.status_code == 200:
                        log.info("pod_ready", pod_id=pod_id, endpoint=endpoint_url)
                        return endpoint_url
                except Exception:
                    pass
                await asyncio.sleep(5)
        raise TimeoutError(f"Pod {pod_id} did not become ready within {settings.cold_start_timeout_sec}s")

    async def _pull_model(self, endpoint_url: str, model: str, pod_id: str) -> None:
        log.info("pulling_model", pod_id=pod_id, model=model)
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=600, write=30, pool=10)) as client:
            try:
                r = await client.post(
                    f"{endpoint_url}/api/pull",
                    json={"model": model, "stream": False},
                )
                r.raise_for_status()
                log.info("model_pulled", pod_id=pod_id, model=model)
            except Exception as exc:
                log.warning("model_pull_failed", pod_id=pod_id, model=model, error=str(exc))
                raise

    async def _cleanup_stale_pods(self) -> None:
        """On startup, mark provisioning/starting pods as failed.

        These pods were mid-launch when the bridge last crashed or restarted.
        Their launch coroutines are gone; they'll never become ready.
        """
        async with SessionLocal() as session:
            result = await session.execute(
                select(Pod).where(Pod.status.in_([PodStatus.provisioning, PodStatus.starting]))
            )
            pods = result.scalars().all()
            for pod in pods:
                pod.status = PodStatus.failed
            if pods:
                await session.commit()
                log.info("cleanup_stale_pods", count=len(pods))

    async def _reset_api_provider_pods(self) -> None:
        """Delete stale API provider pod rows on startup so they're recreated with fresh config.

        API providers don't have real pod lifecycle — their rows can survive restarts
        with stale endpoint_url or model values. Deleting them forces _spin_api_pod to
        upsert fresh rows on the next request.
        """
        api_providers = [
            name for name, p in self._providers.items()
            if getattr(p, "provider_type", "") in ("api", "local")
        ]
        if not api_providers:
            return
        async with SessionLocal() as session:
            result = await session.execute(
                select(Pod).where(Pod.provider.in_(api_providers))
            )
            pods = result.scalars().all()
            for pod in pods:
                await session.delete(pod)
            await session.commit()
            if pods:
                log.info("reset_api_provider_pods", count=len(pods), providers=api_providers)

    async def _cleanup_dedicated_pods(self) -> None:
        """On startup, terminate any stale dedicated-endpoint pods from prior sessions.

        Unlike API providers, dedicated pods have real running infrastructure that
        costs money. Delete them from the DB and fire terminate() so Together
        deallocates the GPU.
        """
        dedicated_providers = [
            name for name, p in self._providers.items()
            if p.provider_type == "pod" and not getattr(p, "needs_ollama_check", True)
        ]
        if not dedicated_providers:
            return
        async with SessionLocal() as session:
            result = await session.execute(
                select(Pod).where(Pod.provider.in_(dedicated_providers))
            )
            pods = result.scalars().all()
            for pod in pods:
                provider = self._providers.get(pod.provider)
                if provider and pod.external_id and not pod.external_id.startswith("pending-"):
                    asyncio.create_task(provider.terminate(pod.external_id))
                await session.delete(pod)
            await session.commit()
            if pods:
                log.info("cleanup_dedicated_pods", count=len(pods), providers=dedicated_providers)

    def _handle(self, pod: Pod, *, cold_start: bool) -> PodHandle:
        provider = self._providers.get(pod.provider)
        return PodHandle(
            pod_id=pod.id,
            provider=pod.provider,
            tier=pod.tier,
            endpoint_url=pod.endpoint_url or "",
            cost_per_hour_usd=float(pod.cost_per_hour_usd),
            model=pod.model or "",
            cold_start=cold_start,
            extra_headers=provider.extra_request_headers() if provider else {},
        )

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def _sync_prices(self) -> None:
        from bridge.router import refresh_tier_prices
        offers: dict = {}
        for name, provider in self._providers.items():
            try:
                offers[name] = await provider.list_gpus()
            except Exception as exc:
                log.warning("price_sync_failed", provider=name, error=str(exc))
        if offers:
            refresh_tier_prices(offers)

    async def _health_loop(self) -> None:
        interval = settings.health_check_interval_sec
        threshold = settings.health_check_fail_threshold
        while True:
            await asyncio.sleep(interval)
            try:
                async with SessionLocal() as session:
                    result = await session.execute(
                        select(Pod).where(Pod.status == PodStatus.ready)
                    )
                    pods = result.scalars().all()

                async with httpx.AsyncClient(timeout=5) as client:
                    for pod in pods:
                        if not pod.endpoint_url:
                            continue
                        provider = self._providers.get(pod.provider)
                        # Skip /api/tags probe for non-Ollama providers (they manage
                        # their own health; false failures would trigger unwanted terminates).
                        if not getattr(provider, "needs_ollama_check", True):
                            continue
                        try:
                            r = await client.get(f"{pod.endpoint_url}/api/tags")
                            if r.status_code == 200:
                                async with SessionLocal() as session:
                                    p = await session.get(Pod, pod.id)
                                    if p:
                                        p.health_failures = 0
                                        await session.commit()
                                continue
                        except Exception:
                            pass

                        async with SessionLocal() as session:
                            p = await session.get(Pod, pod.id)
                            if not p:
                                continue
                            p.health_failures += 1
                            if p.health_failures >= threshold:
                                log.error("pod_health_failed", pod_id=pod.id, failures=p.health_failures)
                                p.status = PodStatus.failed
                                if self._pods.get(p.tier) == p.id:
                                    self._pods.pop(p.tier, None)
                                provider = self._providers.get(pod.provider)
                                if provider and provider.provider_type == "pod":
                                    asyncio.create_task(provider.terminate(pod.external_id))
                            await session.commit()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.exception("health_loop_error", error=str(exc))

    async def _reaper_loop(self) -> None:
        import yaml
        interval = settings.idle_reaper_interval_sec
        while True:
            await asyncio.sleep(interval)
            try:
                with open(settings.tiers_config_path) as f:
                    tier_cfg = yaml.safe_load(f)["tiers"]

                now = datetime.now(timezone.utc)
                async with SessionLocal() as session:
                    result = await session.execute(
                        select(Pod).where(Pod.status == PodStatus.ready)
                    )
                    pods = result.scalars().all()

                for pod in pods:
                    if self._active_requests.get(pod.id, 0) > 0:
                        continue
                    idle_timeout = tier_cfg.get(pod.tier, {}).get("idle_timeout_sec", 300)
                    if pod.last_used_at is None:
                        continue
                    last = pod.last_used_at
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    idle_sec = (now - last).total_seconds()
                    if idle_sec > idle_timeout:
                        log.info("reaping_idle_pod", pod_id=pod.id, tier=pod.tier, idle_sec=idle_sec)
                        terminated = False
                        async with SessionLocal() as session:
                            p = await session.get(Pod, pod.id, with_for_update={"skip_locked": True})
                            if p and p.status == PodStatus.ready:
                                p.status = PodStatus.terminated
                                p.terminated_at = now
                                await session.commit()
                                terminated = True
                        if terminated:
                            if self._pods.get(pod.tier) == pod.id:
                                self._pods.pop(pod.tier, None)
                            provider = self._providers.get(pod.provider)
                            if provider and provider.provider_type == "pod":
                                asyncio.create_task(provider.terminate(pod.external_id))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.exception("reaper_loop_error", error=str(exc))


_manager: InstanceManager | None = None


def get_manager() -> InstanceManager:
    global _manager
    if _manager is None:
        _manager = InstanceManager()
    return _manager
