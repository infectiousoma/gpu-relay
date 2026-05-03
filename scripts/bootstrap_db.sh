#!/usr/bin/env bash
# Bootstrap the LLM infrastructure database.
# Usage: ./scripts/bootstrap_db.sh [--with-admin]
#
# Steps:
#   1. Wait for postgres to be reachable.
#   2. Run alembic migrations to head.
#   3. (--with-admin) Create the bootstrap admin user from BOOTSTRAP_ADMIN_EMAIL/PASSWORD env.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

WITH_ADMIN=0
for arg in "$@"; do
    case "$arg" in
        --with-admin) WITH_ADMIN=1 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

: "${DATABASE_URL:?DATABASE_URL must be set}"

# 1. Wait for postgres
echo "==> Waiting for postgres at ${DATABASE_URL%%@*}@..."
python - <<'PY'
import asyncio
import os
import sys
import asyncpg

async def main():
    dsn = os.environ["DATABASE_URL"].replace("+asyncpg", "").replace("+psycopg", "")
    for attempt in range(60):
        try:
            conn = await asyncpg.connect(dsn=dsn)
            await conn.close()
            print("    postgres reachable")
            return
        except Exception as e:
            print(f"    attempt {attempt+1}/60 failed: {e}")
            await asyncio.sleep(1)
    sys.exit("postgres unreachable after 60s")

asyncio.run(main())
PY

# 2. Migrations
echo "==> Running alembic migrations"
alembic upgrade head

# 3. Optional admin bootstrap
if [[ "${WITH_ADMIN}" == "1" ]]; then
    : "${BOOTSTRAP_ADMIN_EMAIL:?BOOTSTRAP_ADMIN_EMAIL must be set}"
    : "${BOOTSTRAP_ADMIN_PASSWORD:?BOOTSTRAP_ADMIN_PASSWORD must be set}"
    echo "==> Creating bootstrap admin ${BOOTSTRAP_ADMIN_EMAIL}"
    python -m cli.llm_ctl users add \
        "${BOOTSTRAP_ADMIN_EMAIL}" \
        --role admin \
        --password "${BOOTSTRAP_ADMIN_PASSWORD}" \
        --idempotent
fi

echo "==> Done"
