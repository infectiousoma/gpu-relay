"""llmctl — operator CLI for the self-hosted LLM infrastructure.

Usage examples:
  llmctl users add admin@example.com --role admin
  llmctl users set-password admin@example.com
  llmctl users budget admin@example.com --usd 100
  llmctl users credit-add admin@example.com --usd 50
  llmctl users keys-add admin@example.com --label "cline"
  llmctl users keys-list admin@example.com
  llmctl users keys-revoke <key_id>
  llmctl pods ls
  llmctl pods kill <pod_id>
  llmctl bills run --month 2026-05
  llmctl bills show admin@example.com --month 2026-05
  llmctl start --tier simple
  llmctl status
  llmctl budget --email admin@example.com
  llmctl costs --month 2026-05
  llmctl models
  llmctl gain

Run as: python -m cli.llm_ctl <command>
Or install entrypoint: llmctl = cli.llm_ctl:app
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from sqlalchemy import func, select

from bridge.auth import generate_api_key, hash_password
from bridge.pricing import PRICING_CONFIG, calculate_price, markup_summary
from database.models import (
    ApiKey,
    AuditLog,
    BillingMode,
    Invoice,
    InvoiceStatus,
    Pod,
    PodStatus,
    Quota,
    Request,
    RequestStatus,
    TierName,
    User,
    UserRole,
)
from database.session import SessionLocal

app = typer.Typer(name="llmctl", add_completion=False, pretty_exceptions_short=True)
users_app = typer.Typer(help="User management")
keys_app = typer.Typer(help="API key management")
pods_app = typer.Typer(help="Pod management")
bills_app = typer.Typer(help="Billing operations")

app.add_typer(users_app, name="users")
app.add_typer(pods_app, name="pods")
app.add_typer(bills_app, name="bills")

console = Console()
err_console = Console(stderr=True, style="red")


def async_cmd(fn):
    """Decorator: run async command in event loop."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))
    return wrapper


def _period_bounds(month_str: str) -> tuple[datetime, datetime]:
    """Parse 'YYYY-MM' → (period_start, period_end)."""
    import calendar
    year, month = map(int, month_str.split("-"))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
    return start, end


# ---------------------------------------------------------------------------
# users commands
# ---------------------------------------------------------------------------

@users_app.command("add")
@async_cmd
async def users_add(
    email: str,
    role: str = typer.Option("user", help="admin | user | service"),
    password: Optional[str] = typer.Option(None, help="password (prompted if omitted)"),
    billing_mode: str = typer.Option("postpaid", help="prepaid | postpaid"),
    budget_usd: float = typer.Option(25.0, "--budget", help="monthly budget USD"),
    idempotent: bool = typer.Option(False, help="skip if user already exists"),
):
    """Create a new user and print their user ID."""
    if password is None:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)

    async with SessionLocal() as session:
        existing = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing:
            if idempotent:
                console.print(f"[yellow]User {email} already exists (id={existing.id})[/yellow]")
                return
            err_console.print(f"User {email} already exists")
            raise typer.Exit(1)

        user = User(
            email=email,
            password_hash=hash_password(password),
            role=UserRole(role),
            billing_mode=BillingMode(billing_mode),
            monthly_budget_usd=Decimal(str(budget_usd)),
        )
        session.add(user)
        await session.flush()  # get user.id

        session.add(Quota(
            user_id=user.id,
            requests_per_minute=60,
            tokens_per_day=1_000_000,
            usd_per_month=Decimal(str(budget_usd)),
            max_tier=TierName.ultra,
        ))
        await session.commit()

    console.print(f"[green]Created user[/green] {email} (id={user.id})")


@users_app.command("set-password")
@async_cmd
async def users_set_password(
    email: str,
    password: Optional[str] = typer.Option(None, help="new password (prompted if omitted)"),
):
    """Reset a user's password."""
    if password is None:
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)

    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not user:
            err_console.print(f"User not found: {email}")
            raise typer.Exit(1)
        user.password_hash = hash_password(password)
        await session.commit()

    console.print(f"[green]Password updated[/green] for {email}")


@users_app.command("budget")
@async_cmd
async def users_budget(
    email: str,
    usd: float = typer.Option(..., "--usd", help="new monthly budget in USD"),
):
    """Set monthly budget cap for a user."""
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not user:
            err_console.print(f"User not found: {email}")
            raise typer.Exit(1)
        user.monthly_budget_usd = Decimal(str(usd))
        quota = await session.get(Quota, user.id)
        if quota:
            quota.usd_per_month = Decimal(str(usd))
        await session.commit()

    console.print(f"[green]Budget set[/green] ${usd:.2f}/month for {email}")


@users_app.command("list")
@async_cmd
async def users_list():
    """List all users."""
    async with SessionLocal() as session:
        result = await session.execute(select(User).order_by(User.created_at))
        users = result.scalars().all()

    t = Table(title="Users")
    for col in ("Email", "Role", "Billing", "Budget USD", "Active", "Created"):
        t.add_column(col)
    for u in users:
        t.add_row(
            u.email,
            u.role,
            u.billing_mode,
            f"${u.monthly_budget_usd:.2f}",
            "✓" if u.is_active else "✗",
            u.created_at.strftime("%Y-%m-%d"),
        )
    console.print(t)


@users_app.command("tiers")
@async_cmd
async def users_tiers(
    email: str,
    set_tiers: Optional[str] = typer.Option(
        None, "--set",
        help="Comma-separated allowed tiers (e.g. 'simple,architecture'), or 'all' to remove restriction",
    ),
):
    """Show or set the allowed-tier whitelist for a user.

    Examples:
      llmctl users tiers user@example.com               # show current setting
      llmctl users tiers user@example.com --set simple  # lock to simple only
      llmctl users tiers user@example.com --set simple,architecture
      llmctl users tiers user@example.com --set all     # remove restriction
    """
    from bridge.router import TIER_ORDER

    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not user:
            err_console.print(f"User not found: {email}")
            raise typer.Exit(1)

        if set_tiers is not None:
            if set_tiers.strip().lower() == "all":
                user.allowed_tiers = None
                new_val = None
            else:
                tiers = [t.strip() for t in set_tiers.split(",") if t.strip()]
                invalid = [t for t in tiers if t not in TIER_ORDER]
                if invalid:
                    err_console.print(f"Unknown tier(s): {invalid}. Valid: {TIER_ORDER}")
                    raise typer.Exit(1)
                user.allowed_tiers = tiers
                new_val = tiers
            await session.commit()
            if new_val is None:
                console.print(f"[green]Tier restriction removed[/green] for {email} (all tiers allowed)")
            else:
                console.print(f"[green]Allowed tiers set[/green] for {email}: {new_val}")
        else:
            current = user.allowed_tiers
            if current is None:
                console.print(f"{email}: [dim]unrestricted[/dim] (all tiers allowed)")
            else:
                console.print(f"{email}: allowed tiers = [bold]{current}[/bold]")


@users_app.command("deactivate")
@async_cmd
async def users_deactivate(email: str):
    """Deactivate (soft-delete) a user."""
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not user:
            err_console.print(f"User not found: {email}")
            raise typer.Exit(1)
        user.is_active = False
        await session.commit()
    console.print(f"[yellow]Deactivated[/yellow] {email}")


# ---------------------------------------------------------------------------
# users keys sub-commands (registered on users_app)
# ---------------------------------------------------------------------------

@users_app.command("keys-add")
@async_cmd
async def users_keys_add(
    email: str,
    label: Optional[str] = typer.Option(None, "--label"),
):
    """Create an API key for a user. Plaintext shown ONCE."""
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not user:
            err_console.print(f"User not found: {email}")
            raise typer.Exit(1)

        plaintext, key_hash, prefix = generate_api_key()
        row = ApiKey(user_id=user.id, key_hash=key_hash, key_prefix=prefix, label=label)
        session.add(row)
        await session.commit()
        await session.refresh(row)

    console.print(f"[green]API key created[/green] (id={row.id})")
    console.print(f"[bold yellow]Key (copy now — shown once):[/bold yellow] {plaintext}")


@users_app.command("keys-list")
@async_cmd
async def users_keys_list(email: str):
    """List active API keys for a user."""
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not user:
            err_console.print(f"User not found: {email}")
            raise typer.Exit(1)
        result = await session.execute(
            select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        )
        keys = result.scalars().all()

    t = Table(title=f"API Keys — {email}")
    for col in ("ID", "Prefix", "Label", "Last Used", "Created"):
        t.add_column(col)
    for k in keys:
        t.add_row(
            k.id,
            k.key_prefix,
            k.label or "",
            k.last_used_at.strftime("%Y-%m-%d") if k.last_used_at else "never",
            k.created_at.strftime("%Y-%m-%d"),
        )
    console.print(t)


@users_app.command("keys-revoke")
@async_cmd
async def users_keys_revoke(key_id: str):
    """Revoke an API key by ID."""
    async with SessionLocal() as session:
        row = await session.get(ApiKey, key_id)
        if not row:
            err_console.print(f"Key not found: {key_id}")
            raise typer.Exit(1)
        row.revoked_at = datetime.now(timezone.utc)
        await session.commit()
    console.print(f"[yellow]Revoked[/yellow] key {key_id}")


@users_app.command("reset-key")
@async_cmd
async def users_reset_key(
    email: str,
    label: Optional[str] = typer.Option(None, "--label"),
):
    """Revoke all active API keys for a user and issue a fresh one. Plaintext shown ONCE."""
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not user:
            err_console.print(f"User not found: {email}")
            raise typer.Exit(1)

        now = datetime.now(timezone.utc)
        existing = (await session.execute(
            select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        )).scalars().all()
        for k in existing:
            k.revoked_at = now

        plaintext, key_hash, prefix = generate_api_key()
        row = ApiKey(user_id=user.id, key_hash=key_hash, key_prefix=prefix, label=label)
        session.add(row)
        await session.commit()
        await session.refresh(row)

    console.print(f"[yellow]Revoked {len(existing)} old key(s)[/yellow] for {email}")
    console.print(f"[green]New API key created[/green] (id={row.id})")
    console.print(f"[bold yellow]Key (copy now — shown once):[/bold yellow] {plaintext}")


@users_app.command("credit-add")
@async_cmd
async def users_credit_add(
    email: str,
    usd: float = typer.Option(..., "--usd", help="Amount in USD to add to prepaid balance"),
    note: Optional[str] = typer.Option(None, "--note", help="Optional note for the audit log"),
):
    """Add prepaid credit to a user's balance (admin only)."""
    if usd <= 0:
        err_console.print("Amount must be positive")
        raise typer.Exit(1)

    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not user:
            err_console.print(f"User not found: {email}")
            raise typer.Exit(1)

        before = user.prepaid_balance_usd
        user.prepaid_balance_usd += Decimal(str(usd))
        after = user.prepaid_balance_usd

        session.add(AuditLog(
            user_id=user.id,
            action="credit_add",
            resource=f"user:{user.id}",
            details_json={
                "amount_usd": usd,
                "balance_before": float(before),
                "balance_after": float(after),
                "note": note or "",
            },
        ))
        await session.commit()

    console.print(f"[green]Added ${usd:.2f}[/green] credit to {email}")
    console.print(f"  Balance: ${float(before):.4f} → [bold]${float(after):.4f}[/bold]")


# ---------------------------------------------------------------------------
# pods commands
# ---------------------------------------------------------------------------

@pods_app.command("ls")
@async_cmd
async def pods_ls(
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status"),
):
    """List pods (most recent 200)."""
    async with SessionLocal() as session:
        q = select(Pod).order_by(Pod.started_at.desc()).limit(200)
        if status:
            q = q.where(Pod.status == status)
        result = await session.execute(q)
        pods = result.scalars().all()

    t = Table(title="Pods")
    for col in ("ID", "Provider", "Tier", "GPU", "Status", "Started", "Last Used", "$/hr"):
        t.add_column(col)
    for p in pods:
        status_style = "green" if p.status == "ready" else ("red" if p.status in ("failed", "terminated") else "yellow")
        t.add_row(
            p.id[:12] + "…",
            p.provider,
            p.tier,
            p.gpu or "—",
            f"[{status_style}]{p.status}[/{status_style}]",
            p.started_at.strftime("%m-%d %H:%M") if p.started_at else "—",
            p.last_used_at.strftime("%m-%d %H:%M") if p.last_used_at else "—",
            f"${p.cost_per_hour_usd:.2f}",
        )
    console.print(t)


@pods_app.command("kill")
@async_cmd
async def pods_kill(pod_id: str, yes: bool = typer.Option(False, "--yes", "-y")):
    """Force-terminate a pod by ID (or ID prefix)."""
    if not yes:
        typer.confirm(f"Terminate pod {pod_id}?", abort=True)

    async with SessionLocal() as session:
        # Allow prefix match
        result = await session.execute(select(Pod).where(Pod.id.startswith(pod_id)))
        pod = result.scalar_one_or_none()
        if not pod:
            err_console.print(f"Pod not found: {pod_id}")
            raise typer.Exit(1)

        from bridge.instance_manager import get_manager
        manager = get_manager()
        # Only providers need real launch; for CLI terminate just call directly
        from providers.runpod import RunPodProvider
        from providers.vast import VastProvider
        from providers.lambda_labs import LambdaProvider
        provider_map = {"runpod": RunPodProvider(), "vast": VastProvider(), "lambda": LambdaProvider()}
        provider = provider_map.get(pod.provider)
        if provider and pod.external_id and pod.external_id != "pending":
            await provider.terminate(pod.external_id)

        pod.status = PodStatus.terminated
        pod.terminated_at = datetime.now(timezone.utc)
        await session.commit()

    console.print(f"[yellow]Terminated[/yellow] pod {pod.id}")


# ---------------------------------------------------------------------------
# bills commands
# ---------------------------------------------------------------------------

@bills_app.command("run")
@async_cmd
async def bills_run(
    month: str = typer.Option(..., "--month", help="YYYY-MM"),
):
    """Generate invoices for all postpaid users for the given month."""
    period_start, period_end = _period_bounds(month)

    async with SessionLocal() as session:
        users_result = await session.execute(
            select(User).where(User.billing_mode == BillingMode.postpaid, User.is_active.is_(True))
        )
        users = users_result.scalars().all()

        created = 0
        for user in users:
            # Check if invoice already exists
            existing = (
                await session.execute(
                    select(Invoice).where(
                        Invoice.user_id == user.id,
                        Invoice.period_start == period_start,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                continue

            agg = await session.execute(
                select(
                    func.sum(Request.cost_usd).label("total"),
                    func.count(Request.id).label("count"),
                ).where(
                    Request.user_id == user.id,
                    Request.status == RequestStatus.ok,
                    Request.created_at >= period_start,
                    Request.created_at <= period_end,
                )
            )
            row = agg.one()
            total = Decimal(str(row.total or 0))
            count = int(row.count or 0)

            if total == 0 and count == 0:
                continue

            inv = Invoice(
                user_id=user.id,
                period_start=period_start,
                period_end=period_end,
                amount_usd=total,
                request_count=count,
                status=InvoiceStatus.open,
            )
            session.add(inv)
            created += 1

        await session.commit()

    console.print(f"[green]Generated {created} invoice(s)[/green] for {month}")


@bills_app.command("show")
@async_cmd
async def bills_show(
    email: str,
    month: str = typer.Option(..., "--month", help="YYYY-MM"),
):
    """Show invoice and per-tier breakdown for a user."""
    period_start, period_end = _period_bounds(month)

    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not user:
            err_console.print(f"User not found: {email}")
            raise typer.Exit(1)

        inv = (
            await session.execute(
                select(Invoice).where(
                    Invoice.user_id == user.id,
                    Invoice.period_start == period_start,
                )
            )
        ).scalar_one_or_none()

        tier_rows = await session.execute(
            select(
                Request.tier,
                func.count(Request.id).label("reqs"),
                func.sum(Request.prompt_tokens).label("prompt_tokens"),
                func.sum(Request.completion_tokens).label("completion_tokens"),
                func.sum(Request.cost_usd).label("cost"),
            ).where(
                Request.user_id == user.id,
                Request.status == RequestStatus.ok,
                Request.created_at >= period_start,
                Request.created_at <= period_end,
            ).group_by(Request.tier)
        )
        tiers = tier_rows.all()

    console.print(f"\n[bold]Invoice — {email} — {month}[/bold]")
    if inv:
        status_color = "green" if inv.status == "paid" else "yellow"
        console.print(f"Status: [{status_color}]{inv.status}[/{status_color}]  "
                      f"Total: [bold]${inv.amount_usd:.4f}[/bold]  "
                      f"Requests: {inv.request_count}")
    else:
        console.print("[dim]No invoice generated yet — run `bills run --month`[/dim]")

    if tiers:
        t = Table(title="Breakdown by tier")
        for col in ("Tier", "Requests", "Prompt Tokens", "Completion Tokens", "Cost USD"):
            t.add_column(col, justify="right" if col not in ("Tier",) else "left")
        for r in tiers:
            t.add_row(r.tier, str(r.reqs), f"{r.prompt_tokens:,}", f"{r.completion_tokens:,}", f"${r.cost:.4f}")
        console.print(t)


# ---------------------------------------------------------------------------
# gain — cost savings report
# ---------------------------------------------------------------------------

OPENAI_GPT4O_COST_PER_1K_PROMPT = 0.0025
OPENAI_GPT4O_COST_PER_1K_COMPLETION = 0.010

@app.command("gain")
@async_cmd
async def gain(
    month: str = typer.Option(
        datetime.now(timezone.utc).strftime("%Y-%m"),
        "--month",
        help="YYYY-MM (defaults to current month)",
    ),
):
    """Show cost savings vs. equivalent OpenAI GPT-4o usage."""
    period_start, period_end = _period_bounds(month)

    async with SessionLocal() as session:
        result = await session.execute(
            select(
                func.sum(Request.cost_usd).label("actual_cost"),
                func.sum(Request.prompt_tokens).label("total_prompt"),
                func.sum(Request.completion_tokens).label("total_completion"),
                func.count(Request.id).label("total_requests"),
            ).where(
                Request.status == RequestStatus.ok,
                Request.created_at >= period_start,
                Request.created_at <= period_end,
            )
        )
        row = result.one()

    actual = float(row.actual_cost or 0)
    prompt_k = float(row.total_prompt or 0) / 1000
    completion_k = float(row.total_completion or 0) / 1000
    total_reqs = int(row.total_requests or 0)

    openai_equiv = (
        prompt_k * OPENAI_GPT4O_COST_PER_1K_PROMPT
        + completion_k * OPENAI_GPT4O_COST_PER_1K_COMPLETION
    )
    saved = openai_equiv - actual
    pct = (saved / openai_equiv * 100) if openai_equiv > 0 else 0

    console.print(f"\n[bold]Cost summary — {month}[/bold]")
    console.print(f"  Requests:         {total_reqs:,}")
    console.print(f"  Actual cost:      [bold red]${actual:.4f}[/bold red]")
    console.print(f"  GPT-4o equiv:     [dim]${openai_equiv:.4f}[/dim]")
    console.print(f"  Saved:            [bold green]${saved:.4f}  ({pct:.1f}%)[/bold green]")


# ---------------------------------------------------------------------------
# models — list tiers from config
# ---------------------------------------------------------------------------

@app.command("models")
def cmd_models(
    user_type: str = typer.Option("personal", "--user-type", help="personal | family | business"),
):
    """List available tiers with model, GPU, context size, and effective hourly rate."""
    import yaml
    from bridge.settings import settings

    try:
        with open(settings.tiers_config_path) as f:
            tiers = yaml.safe_load(f)["tiers"]
    except FileNotFoundError:
        err_console.print(f"tiers.yaml not found at {settings.tiers_config_path}")
        raise typer.Exit(1)

    rates = markup_summary(user_type) if user_type in PRICING_CONFIG else {}

    t = Table(title=f"Available Tiers  (pricing: {user_type})")
    for col in ("Model ID", "Tier", "Model", "GPU", "VRAM", "Context", "$/hr", "Use Cases"):
        t.add_column(col)

    for name, cfg in tiers.items():
        rate = rates.get(name, cfg.get("cost_per_hour_usd", 0))
        use_cases = ", ".join(cfg.get("use_cases", [])[:2])
        t.add_row(
            f"llm-{name}",
            name,
            cfg.get("model", "—"),
            cfg.get("gpu", "—").replace("_", " "),
            f"{cfg.get('vram_gb', '?')} GB",
            f"{cfg.get('max_context_tokens', 0) // 1000}k",
            f"${rate:.2f}",
            use_cases,
        )

    # Workflow models
    from bridge.multi_model import WORKFLOW_MODELS
    for wm_id, wm in WORKFLOW_MODELS.items():
        gpu_tier = wm.get("gpu_tier") or "varies"
        tier_cfg = tiers.get(gpu_tier, {})
        rate = rates.get(gpu_tier, tier_cfg.get("cost_per_hour_usd", 0)) if gpu_tier in tiers else 0
        rate_str = f"${rate:.2f}" if rate else "auto"
        t.add_row(
            wm_id,
            f"workflow:{wm['workflow']}",
            "multi-step",
            gpu_tier.replace("_", " "),
            "—",
            "—",
            rate_str,
            wm.get("description", ""),
        )

    console.print(t)


# ---------------------------------------------------------------------------
# status — active pods with uptime and running cost
# ---------------------------------------------------------------------------

@app.command("status")
@async_cmd
async def cmd_status(
    tier: Optional[str] = typer.Option(None, "--tier", help="Filter by tier"),
):
    """Show active/starting GPU instances with uptime and running cost."""
    now = datetime.now(timezone.utc)

    async with SessionLocal() as session:
        q = select(Pod).where(Pod.status.in_(["provisioning", "starting", "ready", "draining"]))
        if tier:
            q = q.where(Pod.tier == tier)
        q = q.order_by(Pod.started_at.desc())
        pods = (await session.execute(q)).scalars().all()

    if not pods:
        console.print("[dim]No active instances.[/dim]")
        return

    t = Table(title="Active Instances")
    for col in ("ID", "Provider", "Tier", "GPU", "Status", "Uptime", "Running Cost", "Endpoint"):
        t.add_column(col)

    for p in pods:
        uptime_secs = (now - p.started_at).total_seconds() if p.started_at else 0
        hours = int(uptime_secs // 3600)
        mins = int((uptime_secs % 3600) // 60)
        uptime_str = f"{hours}h {mins:02d}m" if hours else f"{mins}m"
        running_cost = float(p.cost_per_hour_usd) * uptime_secs / 3600
        status_color = "green" if p.status == "ready" else "yellow"
        endpoint_short = (p.endpoint_url or "—")[:30]
        t.add_row(
            p.id[:12] + "…",
            p.provider,
            str(p.tier),
            (p.gpu or "—").replace("_", " "),
            f"[{status_color}]{p.status}[/{status_color}]",
            uptime_str,
            f"${running_cost:.4f}",
            endpoint_short,
        )

    console.print(t)
    total_running = sum(
        float(p.cost_per_hour_usd) * max(0, (now - p.started_at).total_seconds()) / 3600
        for p in pods if p.started_at
    )
    console.print(f"\nTotal running cost: [bold yellow]${total_running:.4f}[/bold yellow]  ({len(pods)} instance(s))")


# ---------------------------------------------------------------------------
# budget — spending progress bar for one or all users
# ---------------------------------------------------------------------------

@app.command("budget")
@async_cmd
async def cmd_budget(
    email: Optional[str] = typer.Option(None, "--email", "-e", help="Show single user (omit for all)"),
    month: str = typer.Option(
        datetime.now(timezone.utc).strftime("%Y-%m"),
        "--month",
        help="YYYY-MM",
    ),
):
    """Show monthly budget status with progress bars."""
    period_start, period_end = _period_bounds(month)

    async with SessionLocal() as session:
        if email:
            user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if not user:
                err_console.print(f"User not found: {email}")
                raise typer.Exit(1)
            users = [user]
        else:
            users = (await session.execute(select(User).where(User.is_active.is_(True)).order_by(User.email))).scalars().all()

        rows = []
        for user in users:
            result = await session.execute(
                select(func.coalesce(func.sum(Request.cost_usd), Decimal("0"))).where(
                    Request.user_id == user.id,
                    Request.status == RequestStatus.ok,
                    Request.created_at >= period_start,
                    Request.created_at <= period_end,
                )
            )
            spent = float(result.scalar_one())
            budget = float(user.monthly_budget_usd)
            prepaid = float(user.prepaid_balance_usd)
            rows.append((user.email, spent, budget, prepaid, user.billing_mode))

    console.print(f"\n[bold]Budget Status — {month}[/bold]\n")
    for email_addr, spent, budget, prepaid, billing in rows:
        remaining = max(0.0, budget - spent)
        pct = min(100, int(spent / budget * 100)) if budget > 0 else 0
        bar_filled = pct // 5
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        color = "red" if pct >= 90 else ("yellow" if pct >= 75 else "green")
        console.print(f"  [bold]{email_addr}[/bold]  [{billing}]")
        console.print(f"  [{color}]{bar}[/{color}] {pct}%  "
                      f"${spent:.4f} / ${budget:.2f}  "
                      f"(${remaining:.4f} remaining)")
        if billing == "prepaid":
            console.print(f"  Prepaid balance: [cyan]${prepaid:.4f}[/cyan]")
        console.print()


# ---------------------------------------------------------------------------
# costs — detailed cost breakdown by tier, with pricing
# ---------------------------------------------------------------------------

@app.command("costs")
@async_cmd
async def cmd_costs(
    month: str = typer.Option(
        datetime.now(timezone.utc).strftime("%Y-%m"),
        "--month",
        help="YYYY-MM",
    ),
    email: Optional[str] = typer.Option(None, "--email", "-e", help="Filter to single user"),
    user_type: str = typer.Option("personal", "--user-type", help="personal | family | business (for markup display)"),
):
    """Detailed cost breakdown by tier for a given month."""
    period_start, period_end = _period_bounds(month)

    async with SessionLocal() as session:
        base_q = select(
            Request.tier,
            func.count(Request.id).label("requests"),
            func.sum(Request.prompt_tokens).label("prompt_tokens"),
            func.sum(Request.completion_tokens).label("completion_tokens"),
            func.sum(Request.cost_usd).label("cost_usd"),
            func.avg(Request.latency_ms).label("avg_latency_ms"),
        ).where(
            Request.status == RequestStatus.ok,
            Request.created_at >= period_start,
            Request.created_at <= period_end,
        )

        if email:
            user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if not user:
                err_console.print(f"User not found: {email}")
                raise typer.Exit(1)
            base_q = base_q.where(Request.user_id == user.id)

        tiers = (await session.execute(base_q.group_by(Request.tier))).all()

        # Summary totals
        totals = (await session.execute(
            select(
                func.sum(Request.cost_usd).label("total_cost"),
                func.count(Request.id).label("total_requests"),
                func.sum(Request.prompt_tokens + Request.completion_tokens).label("total_tokens"),
            ).where(
                Request.status == RequestStatus.ok,
                Request.created_at >= period_start,
                Request.created_at <= period_end,
                *([Request.user_id == user.id] if email else []),
            )
        )).one()

    scope = f" — {email}" if email else ""
    console.print(f"\n[bold]Cost Breakdown — {month}{scope}[/bold]\n")

    t = Table()
    for col, justify in [
        ("Tier", "left"), ("Requests", "right"), ("Prompt Tok", "right"),
        ("Completion Tok", "right"), ("Actual Cost", "right"),
        ("Effective $/hr", "right"), ("Avg Latency", "right"),
    ]:
        t.add_column(col, justify=justify)

    for r in tiers:
        rate = calculate_price(str(r.tier), 3600, user_type=user_type) if str(r.tier) in PRICING_CONFIG.get(user_type, {}) else "—"
        rate_str = f"${rate:.2f}" if isinstance(rate, float) else rate
        t.add_row(
            str(r.tier),
            f"{r.requests:,}",
            f"{int(r.prompt_tokens or 0):,}",
            f"{int(r.completion_tokens or 0):,}",
            f"${float(r.cost_usd or 0):.4f}",
            rate_str,
            f"{int(r.avg_latency_ms or 0):,} ms",
        )

    console.print(t)
    total_cost = float(totals.total_cost or 0)
    total_reqs = int(totals.total_requests or 0)
    total_tokens = int(totals.total_tokens or 0)
    console.print(f"\n  Total requests: {total_reqs:,}")
    console.print(f"  Total tokens:   {total_tokens:,}")
    console.print(f"  Total cost:     [bold]${total_cost:.4f}[/bold]")


# ---------------------------------------------------------------------------
# start — pre-warm a pod via the bridge admin API
# ---------------------------------------------------------------------------

@app.command("start")
@async_cmd
async def cmd_start(
    tier: str = typer.Option("simple", "--tier", "-t", help="simple | architecture | maximum | ultra"),
    provider: str = typer.Option("runpod", "--provider", help="Preferred provider hint"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show cost estimate without starting"),
    bridge_url: str = typer.Option(
        os.getenv("BRIDGE_URL", "http://localhost:8000"),
        "--bridge-url",
        help="Bridge API URL",
    ),
    api_key: str = typer.Option(
        os.getenv("BRIDGE_API_KEY", ""),
        "--api-key",
        help="Bridge API key (or set BRIDGE_API_KEY env)",
    ),
):
    """Pre-warm a GPU pod for the given tier via the bridge admin API."""
    import yaml
    from bridge.settings import settings

    try:
        with open(settings.tiers_config_path) as f:
            tiers_cfg = yaml.safe_load(f)["tiers"]
    except FileNotFoundError:
        tiers_cfg = {}

    tier_cfg = tiers_cfg.get(tier, {})
    rate = tier_cfg.get("cost_per_hour_usd", 0)
    model = tier_cfg.get("model", "unknown")
    gpu = tier_cfg.get("gpu", "unknown").replace("_", " ")

    console.print(f"\n[bold]Pod pre-warm request[/bold]")
    console.print(f"  Tier:     {tier}")
    console.print(f"  Model:    {model}")
    console.print(f"  GPU:      {gpu}")
    console.print(f"  Rate:     ${rate:.2f}/hr")
    console.print(f"  Provider: {provider} (preferred)")

    if dry_run:
        console.print("\n[dim]Dry run — no pod started.[/dim]")
        return

    if not api_key:
        err_console.print("BRIDGE_API_KEY not set. Use --api-key or set env BRIDGE_API_KEY.")
        raise typer.Exit(1)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{bridge_url}/admin/pods/prewarm",
                params={"tier": tier},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as exc:
        err_console.print(f"Bridge returned {exc.response.status_code}: {exc.response.text}")
        raise typer.Exit(1)
    except httpx.ConnectError:
        err_console.print(f"Could not connect to bridge at {bridge_url}")
        raise typer.Exit(1)

    console.print(f"\n[green]✓ Pre-warm queued[/green] — status: {data.get('status')}, tier: {data.get('tier')}")
    console.print("[dim]The pod will appear in `llmctl status` once it reaches 'starting' state.[/dim]")


if __name__ == "__main__":
    app()
