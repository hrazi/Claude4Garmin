#!/bin/bash
# app-it launcher: start or reattach the dev server, then exec the native
# WebKit wrapper so the generated .app keeps its own Dock identity.
#
# Template vars: app name/slug, baked project root, preferred port,
# start command, port mode, optional polyfill. START_COMMAND must honor PORT;
# hardcoded port literals bypass runtime fallback. Re-run desktop:build if the
# repo moves.

set -e

APP_NAME="__APP_NAME__"
APP_SLUG="__APP_SLUG__"
PROJECT_ROOT="__PROJECT_ROOT__"
PREFERRED_PORT=__PORT__
PORT_MODE="__PORT_MODE__"
POLYFILL_PATH="__POLYFILL_PATH__"

# Keep `$PORT` literal until the launcher picks its runtime port.
START_COMMAND="$(cat <<'APP_IT_START_COMMAND'
__START_COMMAND__
APP_IT_START_COMMAND
)"

STATE_DIR="$HOME/Library/Application Support/app-it/$APP_SLUG"
LOG_DIR="$HOME/Library/Logs/app-it/$APP_SLUG"
mkdir -p "$STATE_DIR" "$LOG_DIR"
SERVER_LOG="$LOG_DIR/server.log"
PID_FILE="$STATE_DIR/server.pid"
PORT_FILE="$STATE_DIR/server.port"
PID_ID_FILE="$STATE_DIR/server.identity"
INSTALL_LOG="$LOG_DIR/install.log"

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
# Finder/Dock start with bare PATH=/usr/bin:/bin; cover common toolchains.
NVM_BIN=""
if [ -d "$HOME/.nvm/versions/node" ]; then
    LATEST_NVM_NODE="$(ls -1 "$HOME/.nvm/versions/node" 2>/dev/null | sort -V | tail -1)"
    [ -n "$LATEST_NVM_NODE" ] && NVM_BIN="$HOME/.nvm/versions/node/$LATEST_NVM_NODE/bin"
fi
export PATH="$HOME/.bun/bin:$HOME/.deno/bin:$HOME/.volta/bin:$HOME/.local/share/mise/shims:$HOME/.asdf/shims:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:${NVM_BIN}:$HOME/Library/pnpm:$PATH"

# --- Project-root sanity ------------------------------------------------
if [ ! -d "$PROJECT_ROOT" ]; then
    /usr/bin/osascript -e "display alert \"$APP_NAME failed to launch\" message \"Project repo not found at:\n$PROJECT_ROOT\n\nThe .app was built against a path that no longer exists (worktree pruned, repo moved). Re-run desktop:build from the canonical repo location.\""
    exit 1
fi

# --- Pre-flight: required binary present? ------------------------------
# Fail fast on missing runners, including `cd web && npm run dev` shapes.
COMMAND_ROOT="$PROJECT_ROOT"
COMMAND_TO_RUN="$START_COMMAND"
case "$COMMAND_TO_RUN" in
    cd\ *\ \&\&\ *)
        COMMAND_SUBDIR="${COMMAND_TO_RUN#cd }"
        COMMAND_SUBDIR="${COMMAND_SUBDIR%% &&*}"
        COMMAND_SUBDIR="${COMMAND_SUBDIR%\"}"
        COMMAND_SUBDIR="${COMMAND_SUBDIR#\"}"
        COMMAND_SUBDIR="${COMMAND_SUBDIR%\'}"
        COMMAND_SUBDIR="${COMMAND_SUBDIR#\'}"
        COMMAND_ROOT="$PROJECT_ROOT/$COMMAND_SUBDIR"
        COMMAND_TO_RUN="${COMMAND_TO_RUN#* && }"
        ;;
esac
FIRST_BIN="$(echo "$COMMAND_TO_RUN" | awk '{print $1}')"
if [ -n "$FIRST_BIN" ] && ! command -v "$FIRST_BIN" >/dev/null 2>&1; then
    /usr/bin/osascript -e "display alert \"$APP_NAME can't start\" message \"Required binary not found on PATH:\n$FIRST_BIN\n\nThe launcher's PATH covers Homebrew, nvm, pnpm-store, Bun, Deno, Volta, mise, asdf, cargo. Install the missing tool or adjust START_COMMAND.\""
    exit 1
fi

# --- Pre-flight: node_modules present? ---------------------------------
# Only check if START_COMMAND is a node-package-manager invocation.
case "$COMMAND_TO_RUN" in
    npm\ *|pnpm\ *|yarn\ *|bun\ run*|bunx*|npx*)
        if [ ! -d "$COMMAND_ROOT/node_modules" ]; then
            /usr/bin/osascript -e "display alert \"$APP_NAME can't start\" message \"node_modules is missing in:\n$COMMAND_ROOT\n\nRun 'npm install' (or pnpm/yarn/bun) in that folder, then click again.\""
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

# If the recorded server PID is dead, scrap both PID and PORT files.
if [ -f "$PID_FILE" ]; then
    EXPECTED_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -z "$EXPECTED_PID" ] || ! kill -0 "$EXPECTED_PID" 2>/dev/null \
        || ! pid_identity_state_valid "$EXPECTED_PID" "$PID_ID_FILE"; then
        rm -f "$PID_FILE" "$PORT_FILE" "$PID_ID_FILE"
    fi
fi

# --- Reattach to our own existing server ------------------------------
# Permissive ownership-tree gate (F38 fix): the listener may be a child or
# grandchild of pnpm/npm. Reattach only if the recorded PID is alive, the port is
# bound by a descendant, and HTTP responds.
CHOSEN_PORT=""
if [ -f "$PID_FILE" ] && [ -f "$PORT_FILE" ]; then
    EXPECTED_PID="$(cat "$PID_FILE")"
    EXPECTED_PORT="$(cat "$PORT_FILE")"
    REATTACH_OK=0

    if kill -0 "$EXPECTED_PID" 2>/dev/null && pid_identity_state_valid "$EXPECTED_PID" "$PID_ID_FILE"; then
        LISTENERS="$(lsof -ti tcp:"$EXPECTED_PORT" 2>/dev/null || true)"
        if [ -n "$LISTENERS" ]; then
            # Walk descendants up to 4 levels (pnpm → node → node → next-server).
            DESCENDANTS="$EXPECTED_PID"
            CURRENT="$EXPECTED_PID"
            for _ in 1 2 3 4; do
                # One PID per pgrep call; macOS `pgrep -P` rejects generations.
                NEXT_GEN=""
                for _pid in $CURRENT; do
                    NEXT_GEN="$NEXT_GEN $(pgrep -P "$_pid" 2>/dev/null | tr '\n' ' ')"
                done
                [ -z "${NEXT_GEN// /}" ] && break
                DESCENDANTS="$DESCENDANTS $NEXT_GEN"
                CURRENT="$NEXT_GEN"
            done
            for pid in $LISTENERS; do
                if echo " $DESCENDANTS " | grep -q " $pid "; then
                    REATTACH_OK=1
                    break
                fi
            done
            # HTTP responding? (any status counts — wrapper shows the page)
            if [ "$REATTACH_OK" = "1" ]; then
                STATUS="$(curl -sS -o /dev/null --max-time 1 -w "%{http_code}" "http://localhost:$EXPECTED_PORT" 2>/dev/null || true)"
                [ -z "$STATUS" ] || [ "$STATUS" = "000" ] && REATTACH_OK=0
            fi
        fi
    fi

    if [ "$REATTACH_OK" = "1" ]; then
        CHOSEN_PORT="$EXPECTED_PORT"
    else
        rm -f "$PID_FILE" "$PORT_FILE" "$PID_ID_FILE"
    fi
fi

# --- Allocate a free port + start server ------------------------------
if [ -z "$CHOSEN_PORT" ]; then
    if [ "$PORT_MODE" = "fixed" ]; then
        if lsof -i tcp:"$PREFERRED_PORT" >/dev/null 2>&1; then
            MESSAGE="Port $PREFERRED_PORT is busy and this launcher is configured with port_mode fixed. App It did not choose a fallback port because browser storage may be tied to http://localhost:$PREFERRED_PORT. Quit the process using that port, or change port/port_mode in scripts/app-it.config.json and rebuild."
            printf '%s\n' "$MESSAGE" >&2
            /usr/bin/osascript -e "display alert \"$APP_NAME can't start\" message \"$MESSAGE\""
            exit 1
        fi
        CHOSEN_PORT="$PREFERRED_PORT"
    else
        # Scan from PREFERRED_PORT upward for the first free port.
        for p in $(seq "$PREFERRED_PORT" "$((PREFERRED_PORT + 50))"); do
            if ! lsof -i tcp:"$p" >/dev/null 2>&1; then
                CHOSEN_PORT="$p"
                break
            fi
        done

        if [ -z "$CHOSEN_PORT" ]; then
            /usr/bin/osascript -e "display alert \"$APP_NAME couldn't find a free port\" message \"Searched $PREFERRED_PORT–$((PREFERRED_PORT + 50)). Quit something using one of those ports and try again.\""
            exit 1
        fi
    fi

    cd "$PROJECT_ROOT"

    # Detach so wrapper exit/SIGHUP cannot kill the warm server tree.
    if command -v setsid >/dev/null 2>&1; then
        PORT="$CHOSEN_PORT" setsid bash -c "$START_COMMAND" > "$SERVER_LOG" 2>&1 < /dev/null &
    else
        PORT="$CHOSEN_PORT" nohup bash -c "trap '' HUP; $START_COMMAND" > "$SERVER_LOG" 2>&1 < /dev/null &
    fi
    SERVER_PID=$!
    echo "$SERVER_PID" > "$PID_FILE"
    echo "$CHOSEN_PORT" > "$PORT_FILE"
    write_pid_identity "$SERVER_PID" "$PID_ID_FILE"
    disown "$SERVER_PID" 2>/dev/null || true

    URL="http://localhost:$CHOSEN_PORT"

    # Two-stage readiness probe.
    # Stage 1: port is bound (any process listening counts).
    READY=0
    START_FAILURE=""
    for _ in $(seq 1 120); do
        if lsof -i tcp:"$CHOSEN_PORT" >/dev/null 2>&1; then
            READY=1
            break
        fi
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            START_FAILURE="The dev server process exited before binding to $URL."
            break
        fi
        sleep 0.5
    done

    # Stage 2: any HTTP status counts; 5xx should open as the real app error.
    if [ "$READY" = "1" ]; then
        READY=0
        for _ in $(seq 1 120); do
            STATUS="$(curl -sS -o /dev/null --max-time 1 -w "%{http_code}" "$URL" 2>/dev/null || true)"
            if [ -n "$STATUS" ] && [ "$STATUS" != "000" ]; then
                READY=1
                # Log 5xx without blocking; the wrapper should show the error.
                if [ "${STATUS:0:1}" = "5" ]; then
                    echo "$(date) — server up at $URL but returning HTTP $STATUS — see app's log for details" >> "$SERVER_LOG"
                fi
                break
            fi
            sleep 0.5
        done
    fi

    if [ "$READY" != "1" ]; then
        TAIL="$(tail -40 "$SERVER_LOG" 2>/dev/null | sed 's/"/\\"/g' | tr '\n' ' ' | head -c 800)"
        rm -f "$PID_FILE" "$PORT_FILE" "$PID_ID_FILE"
        MODE_NOTE=""
        if [ "$PORT_MODE" = "fixed" ]; then
            MODE_NOTE="\n\nFixed-port mode did not fall back because browser storage may be tied to http://localhost:$PREFERRED_PORT."
        fi
        [ -n "$START_FAILURE" ] || START_FAILURE="The dev server did not bind to $URL within 60 seconds."
        MESSAGE="$START_FAILURE$MODE_NOTE\n\nCheck $SERVER_LOG for the cause. Common ones:\n• Missing dependencies (run package-manager install in the repo)\n• Port literal hardcoded in START_COMMAND or framework config\n• Server crashed during startup\n\nLast log lines:\n$TAIL"
        printf '%s\n' "$MESSAGE" >&2
        /usr/bin/osascript -e "display alert \"$APP_NAME failed to start\" message \"$MESSAGE\""
        exit 1
    fi
fi

URL="http://localhost:$CHOSEN_PORT"

# --- Headless smoke seam (CI / SSH / --check) --------------------------
# APP_IT_SMOKE runs the Dock path up to, but not including, opening WebKit.
if [ -n "${APP_IT_SMOKE:-}" ]; then
    echo "app-it smoke: $APP_NAME ready at $URL (server pid $(cat "$PID_FILE" 2>/dev/null))"
    exit 0
fi

# --- Hand off to the native WebKit wrapper -----------------------------
# exec preserves this .app's Dock identity while WebKit owns the foreground.
WRAPPER="$HERE/wrapper"
if [ ! -x "$WRAPPER" ]; then
    /usr/bin/osascript -e "display alert \"$APP_NAME failed to launch\" message \"Native wrapper missing at:\n$WRAPPER\n\nRun desktop:build to rebuild the bundle.\""
    exit 1
fi

exec "$WRAPPER" "$URL" "$APP_NAME" "$CHOSEN_PORT" "$PID_FILE" "$POLYFILL_PATH"
