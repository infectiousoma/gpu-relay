#!/usr/bin/env bash
# setup.sh — one-shot bootstrap for the self-hosted LLM infrastructure.
#
# Usage:
#   ./scripts/setup.sh [--skip-build] [--no-ollama-pull] [--dev]
#
# Options:
#   --skip-build      Skip docker image builds (use cached images)
#   --no-ollama-pull  Don't pre-pull the local Ollama model (saves time on re-runs)
#   --dev             Mount source dirs as volumes (no rebuild on code change)

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Load existing .env early so BOOTSTRAP_ADMIN_PASSWORD etc. survive re-runs
if [ -f "$REPO/.env" ]; then
    set -o allexport
    # shellcheck disable=SC1091
    source "$REPO/.env"
    set +o allexport
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC}  $1"; }
die()  { echo -e "${RED}✗${NC} $1" >&2; exit 1; }
step() { echo -e "\n${BOLD}→ $1${NC}"; }

SKIP_BUILD=0
NO_OLLAMA_PULL=0
DEV_MODE=0

for arg in "$@"; do
    case "$arg" in
        --skip-build)      SKIP_BUILD=1 ;;
        --no-ollama-pull)  NO_OLLAMA_PULL=1 ;;
        --dev)             DEV_MODE=1 ;;
        *) die "Unknown option: $arg" ;;
    esac
done

# ---------------------------------------------------------------------------
# 1. Preflight checks
# ---------------------------------------------------------------------------
step "Checking requirements"

command -v docker >/dev/null 2>&1 || die "Docker is required. Install from https://docs.docker.com/get-docker/"
ok "docker found: $(docker --version | head -1)"

# Support both 'docker compose' (plugin) and 'docker-compose' (standalone)
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "Docker Compose is required. Install from https://docs.docker.com/compose/install/"
fi
ok "compose found: $($COMPOSE version | head -1)"

# ---------------------------------------------------------------------------
# 2. Environment setup
# ---------------------------------------------------------------------------
step "Environment setup"

if [ ! -f "$REPO/.env" ]; then
    if [ ! -f "$REPO/.env.example" ]; then
        die ".env.example not found — is this the right directory?"
    fi
    cp "$REPO/.env.example" "$REPO/.env"
    warn ".env created from .env.example"
    warn "Edit .env and add your API keys (RUNPOD_API_KEY, etc.), then re-run this script."
    echo ""
    echo "  Required for GPU providers (at least one):"
    echo "    RUNPOD_API_KEY=..."
    echo "    VAST_API_KEY=..."
    echo "    LAMBDA_API_KEY=..."
    echo ""
    echo "  Required for security:"
    echo "    BRIDGE_SECRET_KEY=$(openssl rand -hex 32 2>/dev/null || echo 'run: openssl rand -hex 32')"
    echo "    OPENWEBUI_SECRET_KEY=$(openssl rand -hex 32 2>/dev/null || echo 'run: openssl rand -hex 32')"
    echo "    POSTGRES_PASSWORD=..."
    exit 1
fi
ok ".env present"

# Warn if obvious placeholder values remain
if grep -qE 'change-me|your.*key.*here' "$REPO/.env" 2>/dev/null; then
    warn "Some placeholder values remain in .env — review before production use"
fi

# Bootstrap admin credentials
ADMIN_EMAIL="${BOOTSTRAP_ADMIN_EMAIL:-admin@example.com}"
ADMIN_PASSWORD="${BOOTSTRAP_ADMIN_PASSWORD:-}"
if [ -z "$ADMIN_PASSWORD" ]; then
    ADMIN_PASSWORD="$(openssl rand -base64 18 2>/dev/null || echo "changeme-$(date +%s)")"
    warn "No BOOTSTRAP_ADMIN_PASSWORD set — generated: $ADMIN_PASSWORD"
fi

# ---------------------------------------------------------------------------
# 3. Build images
# ---------------------------------------------------------------------------
if [ "$SKIP_BUILD" -eq 0 ]; then
    step "Building Docker images"
    $COMPOSE build --parallel bridge dashboard
    ok "Images built"
else
    warn "Skipping image build (--skip-build)"
fi

# ---------------------------------------------------------------------------
# 4. Start services
# ---------------------------------------------------------------------------
step "Starting services"
$COMPOSE up -d postgres redis ollama
ok "Core services started"

# ---------------------------------------------------------------------------
# 5. Wait for postgres
# ---------------------------------------------------------------------------
step "Waiting for PostgreSQL"
POSTGRES_READY=0
for i in $(seq 1 60); do
    if $COMPOSE exec -T postgres pg_isready -U "${POSTGRES_USER:-llm}" -q 2>/dev/null; then
        POSTGRES_READY=1
        break
    fi
    if [ "$((i % 5))" -eq 0 ]; then
        echo "  …waiting (${i}s)"
    fi
    sleep 1
done
[ "$POSTGRES_READY" -eq 1 ] || die "PostgreSQL did not become ready in 60s"
ok "PostgreSQL ready"

# ---------------------------------------------------------------------------
# 6. Start bridge (needs postgres)
# ---------------------------------------------------------------------------
step "Starting bridge and dashboard"
$COMPOSE up -d bridge dashboard openwebui
ok "All services started"

# ---------------------------------------------------------------------------
# 7. Run migrations
# ---------------------------------------------------------------------------
step "Running database migrations"
if $COMPOSE run --rm \
    -e DATABASE_URL \
    bridge alembic upgrade head 2>&1; then
    ok "Migrations applied"
else
    die "Migrations failed — check: $COMPOSE logs bridge"
fi

# ---------------------------------------------------------------------------
# 8. Bootstrap admin user
# ---------------------------------------------------------------------------
step "Bootstrapping admin user: $ADMIN_EMAIL"
if $COMPOSE run --rm \
    -e DATABASE_URL \
    bridge python -m cli.llm_ctl users add "$ADMIN_EMAIL" \
        --role admin \
        --password "$ADMIN_PASSWORD" \
        --idempotent 2>&1; then
    ok "Admin user ready"
else
    warn "Admin bootstrap had errors (user may already exist)"
fi

# ---------------------------------------------------------------------------
# 9. Pre-pull local Ollama model
# ---------------------------------------------------------------------------
if [ "$NO_OLLAMA_PULL" -eq 0 ]; then
    step "Pre-pulling local Ollama model (this may take several minutes on first run)"
    OLLAMA_MODEL="${OLLAMA_PREPROCESSOR_MODEL:-qwen2.5-coder:7b-instruct-q4_K_M}"
    if $COMPOSE exec -T ollama ollama pull "$OLLAMA_MODEL" 2>&1; then
        ok "Model $OLLAMA_MODEL ready"
    else
        warn "Ollama model pull failed — bridge will pull on first request"
    fi
else
    warn "Skipping Ollama model pull (--no-ollama-pull)"
fi

# ---------------------------------------------------------------------------
# 10. Wait for bridge health
# ---------------------------------------------------------------------------
step "Waiting for bridge API"
BRIDGE_PORT="${BRIDGE_PORT:-8000}"
BRIDGE_READY=0
for i in $(seq 1 30); do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${BRIDGE_PORT}/healthz" 2>/dev/null || echo "0")
    if [ "$STATUS" = "200" ]; then
        BRIDGE_READY=1
        break
    fi
    sleep 2
done
[ "$BRIDGE_READY" -eq 1 ] && ok "Bridge API healthy" || warn "Bridge not yet healthy (may still be starting)"

# ---------------------------------------------------------------------------
# 11. Summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}✓ Setup complete!${NC}"
echo ""
echo "  Admin login:"
echo "    Email:    $ADMIN_EMAIL"
echo "    Password: $ADMIN_PASSWORD"
echo ""
echo "  Access points:"
echo "    Open WebUI:   http://localhost:3000"
echo "    Dashboard:    http://localhost:${DASHBOARD_PORT:-8501}"
echo "    Bridge API:   http://localhost:${BRIDGE_PORT:-8000}"
echo "    API docs:     http://localhost:${BRIDGE_PORT:-8000}/docs"
echo ""
echo "  CLI usage:"
echo "    python -m cli.llm_ctl --help"
echo "    python -m cli.llm_ctl status"
echo "    python -m cli.llm_ctl models"
echo ""
echo "  Useful commands:"
echo "    $COMPOSE logs -f bridge      # Bridge logs"
echo "    $COMPOSE logs -f openwebui   # Open WebUI logs"
echo "    $COMPOSE ps                  # Service status"
echo ""
echo -e "${YELLOW}Note:${NC} GPU provider API keys must be set in .env before making chat requests."
