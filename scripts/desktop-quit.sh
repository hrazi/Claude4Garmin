#!/bin/bash
# Stop the persistent dev servers spawned by the desktop launchers, plus any
# open wrapper windows. Closing the app window with the red X does NOT kill
# these — the launcher daemonizes them so the next click is fast.
#
# v2 reads scripts/app-it.config.json (single source of truth) — falls back
# to a bash APPS array for backward compat. Stops recorded frontend/backend PID
# trees for multi-server apps, and only force-cleans listeners proven to be in
# those recorded trees.
#
# IMPORTANT: pgrep matches against the kernel's process command line, which on
# macOS stores paths in NFD (decomposed Unicode). Our shell strings are
# typically NFC. Matching paths with non-ASCII characters via `pgrep -f` will
# silently fail on the bundle name — we use the ASCII portion of the path
# (.app/Contents/MacOS/wrapper) as the match anchor and verify with the
# wrapper's URL/port argv (also ASCII).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="${APP_IT_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CONFIG_FILE="$SCRIPT_DIR/app-it.config.json"

# --- Load apps from JSON (preferred) or bash array (backward compat) -----
# Internal record per app: name|slug|preferred_port|backend_port
APPS=()
if [ -f "$CONFIG_FILE" ]; then
    while IFS= read -r line; do
        [ -n "$line" ] && APPS+=("$line")
    done < <(/usr/bin/python3 - "$CONFIG_FILE" <<'PY'
import json, re, sys
with open(sys.argv[1]) as f:
    cfg = json.load(f)
def text(value):
    return "" if value is None else str(value)
for a in cfg.get("apps", []):
    name = a.get("name") or ""
    slug = a.get("slug") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    print(f'{name}|{slug}|{text(a.get("port",""))}|{text(a.get("backend_port") or "")}')
PY
)
else
    APPS=(
      # Replace these with your apps. Format: name|slug|preferred_port|backend_port
      "__APP_NAME__|__APP_SLUG__|__PORT__|"
    )
fi

if [ "${#APPS[@]}" -eq 0 ]; then
    echo "ERROR: no apps configured. Edit scripts/app-it.config.json." >&2
    exit 1
fi

is_live_pid() {
    local pid="${1:-}"
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    kill -0 "$pid" 2>/dev/null
}

read_first_line() {
    local file="$1"
    [ -f "$file" ] || return 0
    sed -n '1p' "$file" 2>/dev/null || true
}

pid_identity() {
    local pid="${1:-}"
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    ps -o lstart= -p "$pid" 2>/dev/null | awk '{$1=$1; print}'
}

pid_identity_matches() {
    local pid="$1"
    local file="$2"
    local recorded current
    [ -f "$file" ] || return 1
    recorded="$(read_first_line "$file")"
    current="$(pid_identity "$pid" || true)"
    [ -n "$recorded" ] && [ "$recorded" = "$current" ]
}

descendant_tree() {
    local supervisor="${1:-}"
    is_live_pid "$supervisor" || return 0

    local descendants="$supervisor"
    local current="$supervisor"
    local next_gen _pid
    for _ in 1 2 3 4; do
        next_gen=""
        for _pid in $current; do
            next_gen="$next_gen $(pgrep -P "$_pid" 2>/dev/null | tr '\n' ' ')"
        done
        [ -z "${next_gen// /}" ] && break
        descendants="$descendants $next_gen"
        current="$next_gen"
    done
    printf '%s\n' "$descendants"
}

pid_in_list() {
    local needle="$1"
    local haystack="$2"
    case " $haystack " in
        *" $needle "*) return 0 ;;
        *) return 1 ;;
    esac
}

listeners_owned_by_tree() {
    local tree="$1"
    local port="${2:-}"
    [ -n "$tree" ] || return 0
    [ -n "$port" ] || return 0

    local listeners pid owned=""
    listeners="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
    for pid in $listeners; do
        if pid_in_list "$pid" "$tree"; then
            owned="$owned $pid"
        fi
    done
    printf '%s\n' "$owned"
}

term_pids() {
    local pid
    for pid in "$@"; do
        [ -n "$pid" ] || continue
        [ "$pid" = "$$" ] && continue
        kill -TERM "$pid" 2>/dev/null || true
    done
}

kill_pids() {
    local pid
    for pid in "$@"; do
        [ -n "$pid" ] || continue
        [ "$pid" = "$$" ] && continue
        kill -KILL "$pid" 2>/dev/null || true
    done
}

still_live_pids() {
    local pid still=""
    for pid in "$@"; do
        is_live_pid "$pid" && still="$still $pid"
    done
    printf '%s\n' "$still"
}

stop_owned_runtime() {
    local pid_file="$1"
    local port_file="$2"
    local preferred_port="${3:-}"
    local extra_state_file="${4:-}"
    local identity_file="${5:-}"

    local recorded_pid recorded_port tree owned_listeners ports port pids_to_stop still ownership_ok
    recorded_pid="$(read_first_line "$pid_file")"
    recorded_port="$(read_first_line "$port_file")"

    if is_live_pid "$recorded_pid"; then
        tree="$(descendant_tree "$recorded_pid")"
        pids_to_stop="$tree"
        ownership_ok=0

        ports="$recorded_port"
        if [ -n "$preferred_port" ] && [ "$preferred_port" != "$recorded_port" ]; then
            ports="$ports $preferred_port"
        fi
        for port in $ports; do
            owned_listeners="$(listeners_owned_by_tree "$tree" "$port")"
            pids_to_stop="$pids_to_stop $owned_listeners"
            [ -n "${owned_listeners// /}" ] && [ ! -f "$identity_file" ] && ownership_ok=1
        done

        if pid_identity_matches "$recorded_pid" "$identity_file"; then
            ownership_ok=1
        fi

        if [ "$ownership_ok" = "1" ]; then
            # TERM first, then KILL only the same recorded tree / proven listeners.
            term_pids $pids_to_stop
            for _ in 1 2 3; do
                still="$(still_live_pids $pids_to_stop)"
                [ -z "${still// /}" ] && break
                sleep 0.5
            done
            still="$(still_live_pids $pids_to_stop)"
            [ -n "${still// /}" ] && kill_pids $still
            CLOSED_ANY=1
        else
            STALE_ANY=1
        fi
    elif [ -f "$pid_file" ] || [ -f "$port_file" ]; then
        STALE_ANY=1
    fi

    rm -f "$pid_file" "$port_file"
    [ -n "$identity_file" ] && rm -f "$identity_file"
    [ -n "$extra_state_file" ] && rm -f "$extra_state_file"
}

CLOSED_ANY=0
STALE_ANY=0
for entry in "${APPS[@]}"; do
    IFS='|' read -r APP_NAME APP_SLUG PREFERRED_PORT BACKEND_PORT <<<"$entry"
    STATE_DIR="$HOME/Library/Application Support/app-it/$APP_SLUG"
    PID_FILE="$STATE_DIR/server.pid"
    PORT_FILE="$STATE_DIR/server.port"
    PID_ID_FILE="$STATE_DIR/server.identity"
    BACKEND_PID_FILE="$STATE_DIR/backend.pid"
    BACKEND_PORT_FILE="$STATE_DIR/backend.port"
    BACKEND_PID_ID_FILE="$STATE_DIR/backend.identity"
    RUNTIME_SUMMARY_FILE="$STATE_DIR/runtime.json"

    stop_owned_runtime "$PID_FILE" "$PORT_FILE" "$PREFERRED_PORT" "$RUNTIME_SUMMARY_FILE" "$PID_ID_FILE"

    # Backend (if multi-server).
    if [ -n "$BACKEND_PORT" ]; then
        stop_owned_runtime "$BACKEND_PID_FILE" "$BACKEND_PORT_FILE" "$BACKEND_PORT" "$RUNTIME_SUMMARY_FILE" "$BACKEND_PID_ID_FILE"
    fi

    rm -f "$RUNTIME_SUMMARY_FILE"
done

# Native WebKit wrapper windows. Prefer the generated pid-file argument over
# broad process names; it is unique to this app's launcher state. URL-only
# wrappers have no pid-file argument, so fall back to the app name in argv.
for entry in "${APPS[@]}"; do
    IFS='|' read -r APP_NAME APP_SLUG _ _ <<<"$entry"
    STATE_DIR="$HOME/Library/Application Support/app-it/$APP_SLUG"
    PID_FILE="$STATE_DIR/server.pid"
    for p in $(pgrep -f "MacOS/wrapper " 2>/dev/null); do
        cmdline="$(ps -o command= -p "$p" 2>/dev/null || true)"
        if echo "$cmdline" | grep -qF "$PID_FILE" || echo "$cmdline" | grep -qF "$APP_NAME"; then
            kill -TERM "$p" 2>/dev/null || true
            CLOSED_ANY=1
        fi
    done
    # Chrome --user-data-dir windows from chrome-fallback builds.
    PROFILE="$HOME/Library/Application Support/app-it/$APP_SLUG/BrowserProfile"
    for p in $(pgrep -f "user-data-dir=$PROFILE" 2>/dev/null); do
        kill -TERM "$p" 2>/dev/null || true
        CLOSED_ANY=1
    done
done

if [ "$CLOSED_ANY" = "1" ] && [ "$STALE_ANY" = "1" ]; then
    echo "Stopped owned App It servers/windows and cleaned stale state."
elif [ "$CLOSED_ANY" = "1" ]; then
    echo "Stopped owned App It servers/windows."
elif [ "$STALE_ANY" = "1" ]; then
    echo "Cleaned stale App It state; no owned server was running."
else
    echo "Already clean — no App It servers or windows were running."
fi
