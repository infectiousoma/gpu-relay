#!/usr/bin/env bash
# Source this script — do NOT execute it directly.
#   source scripts/ccr-activate.sh
#
# Sets ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN so Claude Code routes through ccr.
# Reads APIKEY from config/ccr-config.json and CCR_PORT from .env (default 3456).
#
# Pass --save to append vars to ~/.config/claude-code-router/env (sourced on next run)
# or --save-rc to append to your shell rc file.

_ccr_activate() {
    local SCRIPT_DIR
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
    local PROJECT_ROOT
    PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

    local CCR_CONFIG="$PROJECT_ROOT/config/ccr-config.json"
    local ENV_FILE="$PROJECT_ROOT/.env"

    # --- resolve port ---
    local CCR_PORT="${CCR_PORT:-}"
    if [[ -z "$CCR_PORT" && -f "$ENV_FILE" ]]; then
        CCR_PORT=$(grep -E '^CCR_PORT=' "$ENV_FILE" | cut -d= -f2 | tr -d '"' | tr -d "'")
    fi
    CCR_PORT="${CCR_PORT:-3456}"

    # --- resolve APIKEY from ccr config ---
    local APIKEY=""
    if command -v jq &>/dev/null && [[ -f "$CCR_CONFIG" ]]; then
        APIKEY=$(jq -r '.APIKEY // empty' "$CCR_CONFIG" 2>/dev/null)
    elif [[ -f "$CCR_CONFIG" ]]; then
        APIKEY=$(grep -oP '"APIKEY"\s*:\s*"\K[^"]+' "$CCR_CONFIG" 2>/dev/null || true)
    fi

    if [[ -z "$APIKEY" ]]; then
        echo "Warning: APIKEY not found in $CCR_CONFIG" >&2
        echo "  Add  \"APIKEY\": \"your-secret\"  to ccr-config.json, then re-source." >&2
        APIKEY="placeholder"
    fi

    # --- export ---
    export ANTHROPIC_BASE_URL="http://localhost:${CCR_PORT}"
    export ANTHROPIC_AUTH_TOKEN="$APIKEY"
    export NO_PROXY="localhost,127.0.0.1,::1"
    export DISABLE_TELEMETRY=1
    export DISABLE_COST_WARNINGS=1

    echo "ccr-activate: ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"
    echo "ccr-activate: ANTHROPIC_AUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN:0:4}****"

    # --- optional persistence ---
    local MODE="${1:-}"
    if [[ "$MODE" == "--save-rc" ]]; then
        local RC_FILE="${HOME}/.bashrc"
        [[ -n "$ZSH_VERSION" || "$SHELL" == */zsh ]] && RC_FILE="${HOME}/.zshrc"
        local MARKER="# ccr-activate (self-host-llm)"
        if ! grep -qF "$MARKER" "$RC_FILE" 2>/dev/null; then
            cat >> "$RC_FILE" <<EOF

$MARKER
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL}"
export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN}"
export NO_PROXY="localhost,127.0.0.1,::1"
export DISABLE_TELEMETRY=1
export DISABLE_COST_WARNINGS=1
EOF
            echo "ccr-activate: appended to $RC_FILE"
        else
            echo "ccr-activate: $RC_FILE already has ccr vars — skipping"
        fi
    fi
}

_ccr_activate "$@"
unset -f _ccr_activate
