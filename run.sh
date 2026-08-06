#!/usr/bin/env bash
#
# run.sh — start the MCP server and the ngrok tunnel with a single command.
#
# Both processes share this terminal. Output is prefixed so you can tell them
# apart:
#   [server] ...   <- local_dev_mcp.server.run() on MCP_HOST:MCP_PORT
#   [tunnel] ...   <- tunnel.py / public ngrok endpoint
#
# Ctrl+C (SIGINT) or SIGTERM tears both children down cleanly.
#
# Reads MCP_HOST, MCP_PORT, and NGROK_DOMAIN from .env via python-dotenv.
# When NGROK_DOMAIN is set, the tunnel binds to that reserved domain; otherwise
# ngrok issues an ephemeral URL that changes every run.
#
# Implementation note: we launch each child with process substitution
#   cmd > >(prefix ...) 2>&1 &
# so that $! is the *real* command PID (python), not the prefixer. Piping
# through a function (cmd | prefix &) would make $! the prefixer's subshell,
# which breaks wait/cleanup and can orphan the tunnel when the server dies.

set -uo pipefail

# Unbuffered child output so the [server]/[tunnel] prefixes appear live. Without
# this, Python block-buffers stdout when it's a pipe, and tunnel.py's
# "MCP connector URL" banner would sit in the buffer until the process exits.
export PYTHONUNBUFFERED=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
PY="$VENV/bin/python"

# --- Prerequisites ---------------------------------------------------------
if [[ ! -x "$PY" ]]; then
    echo "error: virtualenv not found at $VENV" >&2
    echo "       run: python3 -m venv .venv && source .venv/bin/activate && pip install -e ." >&2
    exit 1
fi
if [[ ! -f "$ROOT/.env" ]]; then
    echo "error: .env not found. Copy .env.example to .env and fill it in." >&2
    exit 1
fi
if [[ ! -f "$ROOT/projects.json" ]]; then
    echo "error: projects.json not found. Copy projects.example.json to projects.json." >&2
    exit 1
fi
if [[ ! -f "$ROOT/openai-connectors-mtls-ca.pem" ]]; then
    echo "error: openai-connectors-mtls-ca.pem not found (required by tunnel.py)." >&2
    exit 1
fi

# --- Load .env for pre-flight banner --------------------------------------
# tunnel.py and server.py load .env themselves; this is only for display and
# for the readiness probe. load_dotenv does not override existing env vars.
read_env() {
    "$PY" - "$1" <<'PY'
import os, sys
from dotenv import load_dotenv
load_dotenv(".env")
print(os.getenv(sys.argv[1], ""))
PY
}
MCP_HOST="$(read_env MCP_HOST)"
[[ -n "$MCP_HOST" ]] || MCP_HOST="127.0.0.1"
MCP_PORT="$(read_env MCP_PORT)"
[[ -n "$MCP_PORT" ]] || MCP_PORT="8000"
NGROK_DOMAIN="$(read_env NGROK_DOMAIN)"

echo "Starting local-dev-mcp"
echo "  server:  http://${MCP_HOST}:${MCP_PORT}"
if [[ -n "$NGROK_DOMAIN" ]]; then
    echo "  tunnel:  https://${NGROK_DOMAIN}/mcp  (reserved domain)"
else
    echo "  tunnel:  ephemeral ngrok URL (set NGROK_DOMAIN in .env for a stable one)"
fi
echo "  Ctrl+C to stop both. Logs are prefixed [server]/[tunnel]."
echo

# --- Output prefixing ------------------------------------------------------
prefix() {
    local label="$1"
    if sed --help >/dev/null 2>&1; then
        sed -u "s/^/[$label] /"
    else
        stdbuf -oL sed "s/^/[$label] /"
    fi
}

# --- Child tracking + cleanup ---------------------------------------------
SERVER_PID=""
TUNNEL_PID=""
declare -a CHILDREN_PIDS=()

cleanup() {
    local status=$?
    # Prevent re-entry from the signal handler and the explicit cleanup call.
    trap - INT TERM
    echo
    echo "Stopping server and tunnel..."
    for pid in "${CHILDREN_PIDS[@]}"; do
        kill -0 "$pid" 2>/dev/null && kill "$pid" 2>/dev/null || true
    done
    # Grace period, then force anything still alive.
    for _ in 1 2 3 4 5; do
        local alive=0
        for pid in "${CHILDREN_PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then alive=1; break; fi
        done
        [[ "$alive" -eq 0 ]] && break
        sleep 0.2
    done
    for pid in "${CHILDREN_PIDS[@]}"; do
        kill -9 "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    exit "${EXIT_CODE:-$status}"
}
trap cleanup INT TERM

# --- Launch the server -----------------------------------------------------
"$PY" "$ROOT/server.py" > >(prefix server) 2>&1 &
SERVER_PID=$!
CHILDREN_PIDS+=("$SERVER_PID")

# Wait for the server to bind its port before starting the tunnel. If it dies
# first, report the failure instead of leaving an orphaned tunnel online.
# We require that *our* server PID owns the listening socket — otherwise a
# different process already on $MCP_PORT (e.g. a leftover server) would make the
# probe pass while our server fails to bind and exits.
port_owned_by_server() {
    # ss prints lines like:  LISTEN 0 2048 127.0.0.1:8000 ... users:(("python",pid=12345,fd=11))
    ss -ltnp 2>/dev/null \
        | grep -E "[ :]+${MCP_PORT}[ ]" \
        | grep -q "pid=${SERVER_PID}"
}

SERVER_READY=0
for _ in $(seq 1 40); do  # up to ~20s
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo
        echo "error: server process exited before its port came up." >&2
        echo "       See the [server] log above (common cause: $MCP_PORT already in use)." >&2
        EXIT_CODE=1
        cleanup
    fi
    if port_owned_by_server; then
        SERVER_READY=1
        break
    fi
    sleep 0.5
done

if [[ "$SERVER_READY" -ne 1 ]]; then
    echo
    echo "error: server did not bind http://${MCP_HOST}:${MCP_PORT} within 20s." >&2
    echo "       See the [server] log above. Aborting before starting the tunnel." >&2
    EXIT_CODE=1
    cleanup
fi

echo "[run] server is up on http://${MCP_HOST}:${MCP_PORT}; starting tunnel..."

# --- Launch the tunnel -----------------------------------------------------
"$PY" "$ROOT/tunnel.py" > >(prefix tunnel) 2>&1 &
TUNNEL_PID=$!
CHILDREN_PIDS+=("$TUNNEL_PID")

# If either child exits, tear the other one down and propagate.
wait -n "$SERVER_PID" "$TUNNEL_PID"
cleanup