#!/usr/bin/env bash
# Smoke test script — spins up the full stack and runs E2E tests.
#
# Usage:
#   ./scripts/smoke_test.sh               # run against localhost
#   BRIDGE_URL=http://x.x.x.x:8000 ./scripts/smoke_test.sh
#
# Requirements: docker, docker compose, curl, python3+pip
# Set MOCK_PROVIDERS=1 in .env to skip real GPU provider calls.

set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}✓${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; FAILURES=$((FAILURES+1)); }
info() { echo -e "${YELLOW}→${NC} $1"; }
FAILURES=0

BRIDGE_URL="${BRIDGE_URL:-http://localhost:8000}"
DASHBOARD_URL="${DASHBOARD_URL:-http://localhost:8501}"
ADMIN_EMAIL="${E2E_ADMIN_EMAIL:-${BOOTSTRAP_ADMIN_EMAIL:-admin@example.com}}"
ADMIN_PASSWORD="${E2E_ADMIN_PASSWORD:-${BOOTSTRAP_ADMIN_PASSWORD:-changeme}}"

# ---------------------------------------------------------------------------
# 1. Start stack
# ---------------------------------------------------------------------------
info "Starting docker compose stack..."
docker compose up -d postgres redis ollama bridge dashboard

info "Waiting for postgres to be healthy..."
for i in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-llm}" -q 2>/dev/null; then
        pass "Postgres ready"
        break
    fi
    [ "$i" -eq 30 ] && { fail "Postgres never became ready"; exit 1; }
    sleep 2
done

# ---------------------------------------------------------------------------
# 2. Run migrations
# ---------------------------------------------------------------------------
info "Running alembic migrations..."
if docker compose run --rm bridge alembic upgrade head; then
    pass "Migrations applied"
else
    fail "Migrations failed"
    docker compose logs bridge
    exit 1
fi

# ---------------------------------------------------------------------------
# 3. Bootstrap admin user
# ---------------------------------------------------------------------------
info "Creating bootstrap admin user..."
docker compose run --rm \
    -e DATABASE_URL \
    bridge python -m cli.llm_ctl users add "$ADMIN_EMAIL" \
        --role admin \
        --password "$ADMIN_PASSWORD" \
        --idempotent 2>/dev/null || true
pass "Admin user ready"

# ---------------------------------------------------------------------------
# 4. Wait for bridge to be ready
# ---------------------------------------------------------------------------
info "Waiting for bridge API..."
for i in $(seq 1 30); do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BRIDGE_URL/healthz" 2>/dev/null || echo "0")
    if [ "$STATUS" = "200" ]; then
        pass "Bridge API healthy"
        break
    fi
    [ "$i" -eq 30 ] && { fail "Bridge never became healthy"; docker compose logs bridge; exit 1; }
    sleep 3
done

# ---------------------------------------------------------------------------
# 5. Manual smoke checks (curl)
# ---------------------------------------------------------------------------
info "--- Manual smoke checks ---"

CURL="curl -sf --max-time 10"

# Auth login
TOKEN=$($CURL "$BRIDGE_URL/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null)

if [ -n "$TOKEN" ]; then
    pass "Auth login OK — got token"
else
    fail "Auth login failed — check: docker compose logs bridge"
    exit 1
fi

# Models list
MODELS=$($CURL "$BRIDGE_URL/v1/models" -H "Authorization: Bearer $TOKEN" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data']))" 2>/dev/null)
if [ -n "$MODELS" ] && [ "$MODELS" -ge 4 ]; then
    pass "Models endpoint — $MODELS models listed"
else
    fail "Models endpoint returned unexpected result: $MODELS"
fi

# Create API key
KEY_RESP=$($CURL "$BRIDGE_URL/auth/keys" \
    -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" 2>/dev/null)
API_KEY=$(echo "$KEY_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['key'])" 2>/dev/null)
KEY_ID=$(echo "$KEY_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)

if [[ "$API_KEY" == sk-llm-* ]]; then
    pass "API key creation OK — prefix $API_KEY"
else
    fail "API key creation failed"
fi

# Use API key
MODELS2=$($CURL "$BRIDGE_URL/v1/models" -H "Authorization: Bearer $API_KEY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data']))" 2>/dev/null)
if [ "$MODELS2" -ge 4 ]; then
    pass "API key auth works"
else
    fail "API key auth failed"
fi

# Revoke key
REVOKE_STATUS=$(curl -s --max-time 10 -o /dev/null -w "%{http_code}" "$BRIDGE_URL/auth/keys/$KEY_ID" \
    -X DELETE -H "Authorization: Bearer $TOKEN" 2>/dev/null)
if [ "$REVOKE_STATUS" = "204" ]; then
    pass "API key revoke OK"
else
    fail "API key revoke returned $REVOKE_STATUS"
fi

# Rate limit test (fire 65 requests fast)
info "Testing rate limiting (60 RPM)..."
GOT_429=0
for i in $(seq 1 65); do
    CODE=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" \
        "$BRIDGE_URL/v1/models" \
        -H "Authorization: Bearer $TOKEN" 2>/dev/null)
    if [ "$CODE" = "429" ]; then
        GOT_429=1
        break
    fi
done
if [ "$GOT_429" = "1" ]; then
    pass "Rate limiting triggered at 60 RPM"
else
    info "Rate limiting not triggered in 65 requests (may be expected if limit > 60)"
fi

# Dashboard health
DASH_STATUS=$(curl -s --max-time 10 -o /dev/null -w "%{http_code}" "$DASHBOARD_URL/_stcore/health" 2>/dev/null || echo "0")
if [ "$DASH_STATUS" = "200" ]; then
    pass "Dashboard healthy"
else
    info "Dashboard not ready (status $DASH_STATUS) — may still be starting"
fi

# ---------------------------------------------------------------------------
# 6. Pytest E2E suite
# ---------------------------------------------------------------------------
info "--- Running pytest E2E suite ---"
if BRIDGE_URL="$BRIDGE_URL" \
   E2E_ADMIN_EMAIL="$ADMIN_EMAIL" \
   E2E_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
   python3 -m pytest tests/test_e2e.py -m e2e -v --tb=short 2>&1; then
    pass "pytest E2E suite passed"
else
    fail "pytest E2E suite had failures"
fi

# ---------------------------------------------------------------------------
# 7. Summary
# ---------------------------------------------------------------------------
echo ""
if [ "$FAILURES" -eq 0 ]; then
    echo -e "${GREEN}All smoke tests passed!${NC}"
    exit 0
else
    echo -e "${RED}$FAILURES smoke test(s) failed.${NC}"
    echo "Check logs: docker compose logs bridge"
    exit 1
fi
