#!/bin/bash
# app-it launcher (multi-server cohabiting variant). Used for A3.2 — when
# the project has a separate frontend + backend that must run together,
# AND the project does NOT already have a multi-process orchestrator.
#
# When the project DOES have an orchestrator (`concurrently`, `npm-run-all -p`,
# `turbo run dev`, `pnpm -r dev`), prefer A3.1: use the orchestrator as a
# single START_COMMAND in run-template.sh. Strictly simpler.
#
# This template allocates two ports (frontend + backend), exports them
# distinctly (PORT and API_PORT), boots both via setsid, waits for the
# frontend port, hands off to the wrapper. Records both ports in
# ~/Library/Application Support/app-it/<slug>/{server,backend}.port for cleanup.
#
# Required source edits (carve-out from "don't touch app source"):
#   • Frontend config: server.port reads process.env.PORT
#   • Frontend config: strictPort: true (Vite specifically)
#   • Frontend config: proxy target reads process.env.API_PORT
#   • Backend entrypoint: reads process.env.API_PORT BEFORE process.env.PORT
#     (cohabiting projects often have .env with PORT=, which the backend
#     would pick up and ignore the launcher's API_PORT)
#
# Substituted by desktop-build.sh:
#   __APP_NAME__, __APP_SLUG__, __PROJECT_ROOT__,
#   __PORT__                    preferred frontend port
#   __PORT_MODE__               fallback|fixed for the frontend origin
#   __START_COMMAND__            frontend dev command (honors PORT)
#   __BACKEND_PORT__             preferred backend port
#   __BACKEND_START_COMMAND__    backend command (honors API_PORT)
#   __POLYFILL_PATH__

set -e

APP_NAME="__APP_NAME__"
APP_SLUG="__APP_SLUG__"
PROJECT_ROOT="__PROJECT_ROOT__"
PREFERRED_FE_PORT=__PORT__
PORT_MODE="__PORT_MODE__"
PREFERRED_BE_PORT=__BACKEND_PORT__
POLYFILL_PATH="__POLYFILL_PATH__"

# Keep `$PORT` / `$API_PORT` and other shell syntax literal until the daemon
# spawns below. A plain double-quoted assignment here would expand those values
# before the launcher has selected its runtime ports.
START_COMMAND="$(cat <<'APP_IT_START_COMMAND'
__START_COMMAND__
APP_IT_START_COMMAND
)"
BACKEND_START_COMMAND="$(cat <<'APP_IT_BACKEND_START_COMMAND'
__BACKEND_START_COMMAND__
APP_IT_BACKEND_START_COMMAND
)"

STATE_DIR="$HOME/Library/Application Support/app-it/$APP_SLUG"
LOG_DIR="$HOME/Library/Logs/app-it/$APP_SLUG"
mkdir -p "$STATE_DIR" "$LOG_DIR"
SERVER_LOG="$LOG_DIR/server.log"
BACKEND_LOG="$LOG_DIR/backend.log"
PID_FILE="$STATE_DIR/server.pid"
PORT_FILE="$STATE_DIR/server.port"
PID_ID_FILE="$STATE_DIR/server.identity"
BACKEND_PID_FILE="$STATE_DIR/backend.pid"
BACKEND_PORT_FILE="$STATE_DIR/backend.port"
BACKEND_PID_ID_FILE="$STATE_DIR/backend.identity"
RUNTIME_SUMMARY_FILE="$STATE_DIR/runtime.json"

HERE="$(cd "$(dirname "$0")" && pwd)"

case "$PORT_MODE" in
    fallback|fixed) ;;
    *)
        MESSAGE="Invalid app-it port_mode: $PORT_MODE. Expected fallback or fixed. Rebuild after correcting scripts/app-it.config.json."
        printf '%s\n' "$MESSAGE" >&2
        /usr/bin/osascript -e "display alert \"$APP_NAME can't start\" message \"$MESSAGE\""
        exit 1
        ;;
esac

# --- PATH augmentation -------------------------------------------------
NVM_BIN=""
if [ -d "$HOME/.nvm/versions/node" ]; then
    LATEST_NVM_NODE="$(ls -1 "$HOME/.nvm/versions/node" 2>/dev/null | sort -V | tail -1)"
    [ -n "$LATEST_NVM_NODE" ] && NVM_BIN="$HOME/.nvm/versions/node/$LATEST_NVM_NODE/bin"
fi
export PATH="$HOME/.bun/bin:$HOME/.deno/bin:$HOME/.volta/bin:$HOME/.local/share/mise/shims:$HOME/.asdf/shims:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:${NVM_BIN}:$HOME/Library/pnpm:$PATH"

# --- Project-root sanity ------------------------------------------------
if [ ! -d "$PROJECT_ROOT" ]; then
    /usr/bin/osascript -e "display alert \"$APP_NAME failed to launch\" message \"Project repo not found at:\n$PROJECT_ROOT\""
    exit 1
fi

# --- Pre-flight binary checks ------------------------------------------
for cmd_str in "$START_COMMAND" "$BACKEND_START_COMMAND"; do
    BIN="$(echo "$cmd_str" | awk '{print $1}')"
    if [ -n "$BIN" ] && ! command -v "$BIN" >/dev/null 2>&1; then
        /usr/bin/osascript -e "display alert \"$APP_NAME can't start\" message \"Required binary not found on PATH:\n$BIN\""
        exit 1
    fi
done

case "$START_COMMAND$BACKEND_START_COMMAND" in
    *npm\ *|*pnpm\ *|*yarn\ *|*bun\ run*|*bunx*|*npx*)
        if [ ! -d "$PROJECT_ROOT/node_modules" ]; then
            /usr/bin/osascript -e "display alert \"$APP_NAME can't start\" message \"node_modules is missing in:\n$PROJECT_ROOT\""
            exit 1
        fi
        ;;
esac

# --- Stale-state cleanup ----------------------------------------------
pid_identity() {
    local pid="${1:-}"
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    ps -o lstart= -p "$pid" 2>/dev/null | awk '{$1=$1; print}'
}

write_pid_identity() {
    local pid="$1"
    local file="$2"
    local identity
    identity="$(pid_identity "$pid" || true)"
    if [ -n "$identity" ]; then
        printf '%s\n' "$identity" > "$file"
    else
        rm -f "$file"
    fi
}

pid_identity_state_valid() {
    local pid="$1"
    local file="$2"
    local recorded current
    [ -f "$file" ] || return 0  # legacy generated state; reattach still needs listener proof below.
    recorded="$(sed -n '1p' "$file" 2>/dev/null || true)"
    current="$(pid_identity "$pid" || true)"
    [ -n "$recorded" ] && [ "$recorded" = "$current" ]
}

for pf in "$PID_FILE" "$BACKEND_PID_FILE"; do
    if [ -f "$pf" ]; then
        EXPECTED_PID="$(cat "$pf" 2>/dev/null || true)"
        case "$pf" in
            "$PID_FILE") id_file="$PID_ID_FILE" ;;
            *) id_file="$BACKEND_PID_ID_FILE" ;;
        esac
        if [ -z "$EXPECTED_PID" ] || ! kill -0 "$EXPECTED_PID" 2>/dev/null \
            || ! pid_identity_state_valid "$EXPECTED_PID" "$id_file"; then
            rm -f "$pf" "$id_file"
            case "$pf" in
                *server.pid) rm -f "$PORT_FILE" "$PID_ID_FILE" "$RUNTIME_SUMMARY_FILE" ;;
                *backend.pid) rm -f "$BACKEND_PORT_FILE" "$BACKEND_PID_ID_FILE" "$RUNTIME_SUMMARY_FILE" ;;
            esac
        fi
    fi
done

# --- Reattach if both servers are still ours and responding -----------
# Same descendant-walk gate as run-template.sh, applied to both processes.
descendant_holds_port() {
    local supervisor=$1
    local port=$2
    local listeners
    listeners="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
    [ -z "$listeners" ] && return 1
    local descendants="$supervisor"
    local current="$supervisor"
    for _ in 1 2 3 4; do
        # One PID per pgrep call — macOS `pgrep -P` returns nothing for a
        # space-joined argument, so a multi-PID generation would otherwise halt
        # the walk and miss deeper listeners.
        local next_gen="" _pid
        for _pid in $current; do
            next_gen="$next_gen $(pgrep -P "$_pid" 2>/dev/null | tr '\n' ' ')"
        done
        [ -z "${next_gen// /}" ] && break
        descendants="$descendants $next_gen"
        current="$next_gen"
    done
    for pid in $listeners; do
        if echo " $descendants " | grep -q " $pid "; then
            return 0
        fi
    done
    return 1
}

write_runtime_summary() {
    local status="$1"
    local tmp="$RUNTIME_SUMMARY_FILE.tmp"
    APP_IT_RUNTIME_APP_NAME="$APP_NAME" \
    APP_IT_RUNTIME_APP_SLUG="$APP_SLUG" \
    APP_IT_RUNTIME_STATUS="$status" \
    APP_IT_RUNTIME_PORT_MODE="$PORT_MODE" \
    APP_IT_RUNTIME_FE_PREFERRED="$PREFERRED_FE_PORT" \
    APP_IT_RUNTIME_FE_PORT="${CHOSEN_FE_PORT:-}" \
    APP_IT_RUNTIME_FE_PID="${FE_PID:-}" \
    APP_IT_RUNTIME_BE_PREFERRED="$PREFERRED_BE_PORT" \
    APP_IT_RUNTIME_BE_PORT="${CHOSEN_BE_PORT:-}" \
    APP_IT_RUNTIME_BE_PID="${BE_PID:-}" \
    APP_IT_RUNTIME_SERVER_LOG="$SERVER_LOG" \
    APP_IT_RUNTIME_BACKEND_LOG="$BACKEND_LOG" \
    /usr/bin/python3 - <<'PY' > "$tmp" && mv "$tmp" "$RUNTIME_SUMMARY_FILE" || rm -f "$tmp"
import datetime
import json
import os
import sys

def as_int(value):
    try:
        return int(value) if value else None
    except ValueError:
        return None

payload = {
    "schema_version": 1,
    "tool": "app-it.runtime",
    "mode": "multi-server",
    "status": os.environ["APP_IT_RUNTIME_STATUS"],
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
    "app": {
        "name": os.environ["APP_IT_RUNTIME_APP_NAME"],
        "slug": os.environ["APP_IT_RUNTIME_APP_SLUG"],
    },
    "ports": {
        "frontend": {
            "mode": os.environ["APP_IT_RUNTIME_PORT_MODE"],
            "preferred": as_int(os.environ["APP_IT_RUNTIME_FE_PREFERRED"]),
            "runtime": as_int(os.environ["APP_IT_RUNTIME_FE_PORT"]),
        },
        "backend": {
            "preferred": as_int(os.environ["APP_IT_RUNTIME_BE_PREFERRED"]),
            "runtime": as_int(os.environ["APP_IT_RUNTIME_BE_PORT"]),
        },
    },
    "pids": {
        "frontend": as_int(os.environ["APP_IT_RUNTIME_FE_PID"]),
        "backend": as_int(os.environ["APP_IT_RUNTIME_BE_PID"]),
    },
    "logs": {
        "frontend": os.environ["APP_IT_RUNTIME_SERVER_LOG"],
        "backend": os.environ["APP_IT_RUNTIME_BACKEND_LOG"],
    },
}
json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
sys.stdout.write("\n")
PY
}

clear_runtime_state() {
    rm -f "$PID_FILE" "$PORT_FILE" "$PID_ID_FILE" \
        "$BACKEND_PID_FILE" "$BACKEND_PORT_FILE" "$BACKEND_PID_ID_FILE" \
        "$RUNTIME_SUMMARY_FILE"
}

CHOSEN_FE_PORT=""
CHOSEN_BE_PORT=""
if [ -f "$PID_FILE" ] && [ -f "$PORT_FILE" ] && [ -f "$BACKEND_PID_FILE" ] && [ -f "$BACKEND_PORT_FILE" ]; then
    FE_PID="$(cat "$PID_FILE")"
    FE_PORT="$(cat "$PORT_FILE")"
    BE_PID="$(cat "$BACKEND_PID_FILE")"
    BE_PORT="$(cat "$BACKEND_PORT_FILE")"

    if kill -0 "$FE_PID" 2>/dev/null && kill -0 "$BE_PID" 2>/dev/null \
        && pid_identity_state_valid "$FE_PID" "$PID_ID_FILE" \
        && pid_identity_state_valid "$BE_PID" "$BACKEND_PID_ID_FILE" \
        && descendant_holds_port "$FE_PID" "$FE_PORT" \
        && descendant_holds_port "$BE_PID" "$BE_PORT"; then
        FE_STATUS="$(curl -sS -o /dev/null --max-time 1 -w "%{http_code}" "http://localhost:$FE_PORT" 2>/dev/null || true)"
        if [ -n "$FE_STATUS" ] && [ "$FE_STATUS" != "000" ]; then
            CHOSEN_FE_PORT="$FE_PORT"
            CHOSEN_BE_PORT="$BE_PORT"
        fi
    fi

    if [ -z "$CHOSEN_FE_PORT" ]; then
        clear_runtime_state
    fi
fi

# --- Allocate fresh ports + start both servers -------------------------
if [ -z "$CHOSEN_FE_PORT" ]; then
    # Frontend port
    if [ "$PORT_MODE" = "fixed" ]; then
        if lsof -i tcp:"$PREFERRED_FE_PORT" >/dev/null 2>&1; then
            MESSAGE="Frontend port $PREFERRED_FE_PORT is busy and this launcher is configured with port_mode fixed. App It did not choose a fallback frontend port because browser storage may be tied to http://localhost:$PREFERRED_FE_PORT. Quit the process using that port, or change port/port_mode in scripts/app-it.config.json and rebuild."
            printf '%s\n' "$MESSAGE" >&2
            /usr/bin/osascript -e "display alert \"$APP_NAME can't start\" message \"$MESSAGE\""
            exit 1
        fi
        CHOSEN_FE_PORT="$PREFERRED_FE_PORT"
    else
        for p in $(seq "$PREFERRED_FE_PORT" "$((PREFERRED_FE_PORT + 50))"); do
            if ! lsof -i tcp:"$p" >/dev/null 2>&1; then
                CHOSEN_FE_PORT="$p"
                break
            fi
        done
    fi
    # Backend port (skip the FE port if ranges overlap)
    for p in $(seq "$PREFERRED_BE_PORT" "$((PREFERRED_BE_PORT + 50))"); do
        if [ "$p" = "$CHOSEN_FE_PORT" ]; then
            continue
        fi
        if ! lsof -i tcp:"$p" >/dev/null 2>&1; then
            CHOSEN_BE_PORT="$p"
            break
        fi
    done

    if [ -z "$CHOSEN_FE_PORT" ] || [ -z "$CHOSEN_BE_PORT" ]; then
        FE_RANGE="[$PREFERRED_FE_PORT–$((PREFERRED_FE_PORT + 50))]"
        [ "$PORT_MODE" = "fixed" ] && FE_RANGE="exactly $PREFERRED_FE_PORT"
        /usr/bin/osascript -e "display alert \"$APP_NAME couldn't find free ports\" message \"Searched FE $FE_RANGE and BE [$PREFERRED_BE_PORT–$((PREFERRED_BE_PORT + 50))].\""
        exit 1
    fi

    cd "$PROJECT_ROOT"

    # Start backend first so the frontend's proxy target is ready when
    # the frontend boots.
    if command -v setsid >/dev/null 2>&1; then
        API_PORT="$CHOSEN_BE_PORT" PORT="$CHOSEN_BE_PORT" setsid bash -c "$BACKEND_START_COMMAND" > "$BACKEND_LOG" 2>&1 < /dev/null &
    else
        API_PORT="$CHOSEN_BE_PORT" PORT="$CHOSEN_BE_PORT" nohup bash -c "trap '' HUP; $BACKEND_START_COMMAND" > "$BACKEND_LOG" 2>&1 < /dev/null &
    fi
    BE_PID=$!
    echo "$BE_PID" > "$BACKEND_PID_FILE"
    echo "$CHOSEN_BE_PORT" > "$BACKEND_PORT_FILE"
    write_pid_identity "$BE_PID" "$BACKEND_PID_ID_FILE"
    disown "$BE_PID" 2>/dev/null || true

    # Wait for the backend port to bind (don't curl — backends may not
    # serve a 200 on /).
    BACKEND_READY=0
    for _ in $(seq 1 60); do
        if lsof -i tcp:"$CHOSEN_BE_PORT" >/dev/null 2>&1; then
            BACKEND_READY=1
            break
        fi
        sleep 0.5
    done
    if [ "$BACKEND_READY" != "1" ]; then
        BE_TAIL="$(tail -20 "$BACKEND_LOG" 2>/dev/null | sed 's/"/\\"/g' | tr '\n' ' ' | head -c 400)"
        kill -TERM "$BE_PID" 2>/dev/null || true
        clear_runtime_state
        MESSAGE="Backend did not bind to runtime port :$CHOSEN_BE_PORT within 30s (preferred backend :$PREFERRED_BE_PORT; frontend preferred :$PREFERRED_FE_PORT, runtime :$CHOSEN_FE_PORT). The frontend was not started.\n\nBackend log:\n$BE_TAIL"
        printf '%s\n' "$MESSAGE" >&2
        /usr/bin/osascript -e "display alert \"$APP_NAME failed to start\" message \"$MESSAGE\""
        exit 1
    fi

    # Start frontend with both PORT and API_PORT exported.
    if command -v setsid >/dev/null 2>&1; then
        PORT="$CHOSEN_FE_PORT" API_PORT="$CHOSEN_BE_PORT" setsid bash -c "$START_COMMAND" > "$SERVER_LOG" 2>&1 < /dev/null &
    else
        PORT="$CHOSEN_FE_PORT" API_PORT="$CHOSEN_BE_PORT" nohup bash -c "trap '' HUP; $START_COMMAND" > "$SERVER_LOG" 2>&1 < /dev/null &
    fi
    FE_PID=$!
    echo "$FE_PID" > "$PID_FILE"
    echo "$CHOSEN_FE_PORT" > "$PORT_FILE"
    write_pid_identity "$FE_PID" "$PID_ID_FILE"
    disown "$FE_PID" 2>/dev/null || true

    URL="http://localhost:$CHOSEN_FE_PORT"

    # Two-stage probe on the frontend.
    READY=0
    START_FAILURE=""
    for _ in $(seq 1 120); do
        if lsof -i tcp:"$CHOSEN_FE_PORT" >/dev/null 2>&1; then
            READY=1
            break
        fi
        if ! kill -0 "$FE_PID" 2>/dev/null; then
            START_FAILURE="Frontend exited before binding to $URL."
            break
        fi
        sleep 0.5
    done
    if [ "$READY" = "1" ]; then
        READY=0
        for _ in $(seq 1 120); do
            STATUS="$(curl -sS -o /dev/null --max-time 1 -w "%{http_code}" "$URL" 2>/dev/null || true)"
            if [ -n "$STATUS" ] && [ "$STATUS" != "000" ]; then
                READY=1
                break
            fi
            sleep 0.5
        done
    fi

    if [ "$READY" != "1" ]; then
        FE_TAIL="$(tail -20 "$SERVER_LOG" 2>/dev/null | sed 's/"/\\"/g' | tr '\n' ' ' | head -c 400)"
        BE_TAIL="$(tail -20 "$BACKEND_LOG" 2>/dev/null | sed 's/"/\\"/g' | tr '\n' ' ' | head -c 400)"
        clear_runtime_state
        MODE_NOTE=""
        if [ "$PORT_MODE" = "fixed" ]; then
            MODE_NOTE="\n\nFixed-port mode did not fall back because browser storage may be tied to http://localhost:$PREFERRED_FE_PORT."
        fi
        [ -n "$START_FAILURE" ] || START_FAILURE="Frontend did not bind to $URL within 60s."
        MESSAGE="$START_FAILURE Preferred frontend :$PREFERRED_FE_PORT, runtime frontend :$CHOSEN_FE_PORT; preferred backend :$PREFERRED_BE_PORT, runtime backend :$CHOSEN_BE_PORT.$MODE_NOTE\n\nFrontend log:\n$FE_TAIL\n\nBackend log:\n$BE_TAIL"
        printf '%s\n' "$MESSAGE" >&2
        /usr/bin/osascript -e "display alert \"$APP_NAME failed to start\" message \"$MESSAGE\""
        exit 1
    fi
fi

URL="http://localhost:$CHOSEN_FE_PORT"
write_runtime_summary "running"

# --- Headless smoke seam (CI / SSH / --check) --------------------------
# Both servers are up, daemonized, and recorded (server.{pid,port} +
# backend.{pid,port}); the frontend is reachable. With APP_IT_SMOKE set,
# print the runtime URLs and exit 0 instead of opening the GUI window, so a
# headless caller can probe both ports (curl, desktop:doctor) and stop them
# (desktop:quit). Zero effect on a normal Dock launch (APP_IT_SMOKE unset).
if [ -n "${APP_IT_SMOKE:-}" ]; then
    echo "app-it smoke: $APP_NAME ready at $URL (fe pid $(cat "$PID_FILE" 2>/dev/null) :$CHOSEN_FE_PORT, be pid $(cat "$BACKEND_PID_FILE" 2>/dev/null) :$CHOSEN_BE_PORT)"
    exit 0
fi

# --- Hand off to the native WebKit wrapper -----------------------------
WRAPPER="$HERE/wrapper"
if [ ! -x "$WRAPPER" ]; then
    /usr/bin/osascript -e "display alert \"$APP_NAME failed to launch\" message \"Native wrapper missing at:\n$WRAPPER\""
    exit 1
fi

# Pass the frontend PID to the wrapper for Cmd+Q kill semantics.
# wrapper.swift's killServer() also discovers backend.pid / backend.port
# as siblings of $PID_FILE in the same log dir, so Cmd+Q tears down both
# servers without further argv plumbing. desktop-quit.sh remains the
# defensive sweep for whatever Cmd+Q didn't catch (re-parented children).
exec "$WRAPPER" "$URL" "$APP_NAME" "$CHOSEN_FE_PORT" "$PID_FILE" "$POLYFILL_PATH"
