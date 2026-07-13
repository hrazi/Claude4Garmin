#!/bin/bash
# Phase 1 inspection helper. Emits a one-page report covering everything
# the agent needs to decide strategy before touching any files. Read-only.
#
# Designed to be invoked by the agent at the start of a /app-it session,
# before deciding worktree strategy, dev script, framework port semantics,
# multi-app structure, FSA usage, asset candidates, sibling-app collisions.
#
# Usage:
#   ./scripts/inspect.sh                  # report on current repo
#   /path/to/templates/inspect.sh         # report on current working directory
#   APP_IT_PROJECT_ROOT=/path/to/main \
#       ./scripts/inspect.sh              # inspect a different path

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${APP_IT_PROJECT_ROOT:-}" ]; then
    ROOT="$APP_IT_PROJECT_ROOT"
elif [ "$(basename "$SCRIPT_DIR")" = "templates" ] && [ -f "$SCRIPT_DIR/../SKILL.md" ]; then
    ROOT="$(pwd)"
else
    ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

cd "$ROOT"

print_section() {
    echo
    echo "=== $1 ==="
}

print_section "Repo location & worktree status"
echo "ROOT: $ROOT"
echo "id -un: $(id -un)"
if [ -d "$ROOT/.git" ] || [ -f "$ROOT/.git" ]; then
    GIT_DIR="$(git -C "$ROOT" rev-parse --git-dir 2>/dev/null || true)"
    GIT_COMMON="$(git -C "$ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
    if [ -n "$GIT_DIR" ] && [ -n "$GIT_COMMON" ] && [ "$GIT_DIR" != "$GIT_COMMON" ]; then
        echo "WORKTREE: yes — common-dir is $GIT_COMMON"
        echo "  Pick a strategy: (a) bypass — write to main checkout; (b) APP_IT_PROJECT_ROOT env override; (c) bake worktree + document rebuild."
    else
        echo "Worktree: no (canonical checkout)"
    fi
    case "$ROOT" in
        */.claude/worktrees/*) echo "  (path matches .claude/worktrees pattern — likely an agent-spawned worktree)" ;;
    esac
fi

print_section "Project type signals (verify from disk, ignore CLAUDE.md)"
for f in package.json next.config.ts next.config.js next.config.mjs vite.config.ts vite.config.js vite.config.mjs astro.config.ts astro.config.js astro.config.mjs svelte.config.ts svelte.config.js svelte.config.mjs tauri.conf.json electron.json electron-builder.yml electron-builder.json pyproject.toml requirements.txt Cargo.toml Gemfile manifest.json index.html; do
    [ -f "$ROOT/$f" ] && echo "  $f" || true
done
[ -d "$ROOT/src-tauri" ] && echo "  src-tauri/ (Tauri project)" || true
[ -d "$ROOT/apps" ] && echo "  apps/ (monorepo?)" || true
[ -d "$ROOT/packages" ] && echo "  packages/" || true
if [ -f "$ROOT/turbo.json" ] || [ -f "$ROOT/nx.json" ] || [ -f "$ROOT/pnpm-workspace.yaml" ]; then
    echo "  workspace config: $(ls "$ROOT"/turbo.json "$ROOT"/nx.json "$ROOT"/pnpm-workspace.yaml 2>/dev/null | tr '\n' ' ')"
fi

print_section "Dev / start script inventory"
if [ -f "$ROOT/package.json" ]; then
    /usr/bin/python3 - <<'PY'
import json, os, re, sys
try:
    with open("package.json") as f:
        pkg = json.load(f)
except Exception as e:
    print(f"  (could not parse package.json: {e})")
    sys.exit(0)
scripts = pkg.get("scripts", {})
matched = [(k, v) for k, v in scripts.items() if re.match(r"^(dev|start)(:|$)", k)]

def package_manager():
    declared = str(pkg.get("packageManager", ""))
    if declared.startswith("pnpm@"):
        return "pnpm"
    if declared.startswith("yarn@"):
        return "yarn"
    if declared.startswith("bun@"):
        return "bun"
    if declared.startswith("npm@"):
        return "npm"
    if os.path.exists("pnpm-lock.yaml"):
        return "pnpm"
    if os.path.exists("yarn.lock"):
        return "yarn"
    if os.path.exists("bun.lockb") or os.path.exists("bun.lock"):
        return "bun"
    return "npm"

PM = package_manager()

def script_runner():
    if PM == "pnpm":
        return "pnpm run dev --"
    if PM == "yarn":
        return "yarn run dev --"
    if PM == "bun":
        return "bun run dev --"
    return "npm run dev --"

def exec_binary(binary, args):
    if PM == "pnpm":
        return f"pnpm exec {binary} {args}"
    if PM == "yarn":
        return f"yarn exec {binary} {args}"
    if PM == "bun":
        return f"bunx {binary} {args}"
    return f"npm exec -- {binary} {args}"

def has_port_literal(command):
    return re.search(r"(^|\s)(--port(=|\s+)\d+|-p\s*\d+)", command) is not None

def is_orchestrator(command):
    return re.search(r"\b(concurrently|npm-run-all|turbo(\s+run)?|pnpm\s+-r|nx\s+run)\b", command) is not None

NEXT_DIRECT = exec_binary("next", 'dev --hostname 127.0.0.1 --port "$PORT"')
VITE_FLAGS = '--host 127.0.0.1 --port "$PORT" --strictPort'
ASTRO_FLAGS = '--host 127.0.0.1 --port "$PORT"'

if not matched:
    print("  (no dev/start scripts found)")
for k, v in matched:
    flag_warn = ""
    # Hardcoded -p / --port flag -> launcher's PORT env will be ignored.
    if has_port_literal(v):
        flag_warn = "   ⚠ hardcoded port literal — launcher PORT may be ignored; bypass via direct binary or add dev:app-it"
    # concurrently / npm-run-all / turbo / pnpm -r -> orchestrator detected.
    elif is_orchestrator(v):
        flag_warn = "   ✓ multi-process orchestrator — A3.1 candidate (reuse existing)"
    elif re.search(r"(^|\s)next\s+dev\b", v):
        flag_warn = "   ↳ Next direct-binary option avoids package-script flag-routing traps"
    print(f"  {k:<20} → {v}{flag_warn}")
print()
print(f"  package.json name:        {pkg.get('name', '(none)')}")
print(f"  package.json displayName: {pkg.get('displayName', '(none)')}")
print(f"  package manager:          {PM}")

deps = {}
for section in ("dependencies", "devDependencies", "optionalDependencies"):
    deps.update(pkg.get(section, {}))

def has(name):
    return name in deps

recipes = []
has_next_signal = has("next") or any(re.search(r"(^|\s)next\s+dev\b", value) for _, value in matched) or any(
    os.path.exists(name) for name in ("next.config.ts", "next.config.js", "next.config.mjs")
)
if has_next_signal:
    recipes.append(
        f"Next.js — port 3000; direct binary '{NEXT_DIRECT}' when a script hardcodes flags or already wraps next dev"
    )
if has("vite") and has("react") and has("react-dom") and (
    has("@vitejs/plugin-react") or has("@vitejs/plugin-react-swc")
):
    recipes.append(
        f"Vite + React — port 5173; start_command '{script_runner()} {VITE_FLAGS}'"
    )
if has("@sveltejs/kit") and has("@sveltejs/vite-plugin-svelte") and has("svelte") and has("vite"):
    recipes.append(
        f"SvelteKit — port 5173; start_command '{script_runner()} {VITE_FLAGS}'"
    )
if has("astro"):
    recipes.append(
        f"Astro — port 4321; start_command '{script_runner()} {ASTRO_FLAGS}'"
    )

if recipes:
    print()
    print("  framework recipe candidates:")
    for recipe in recipes:
        print(f"    - {recipe}")
if has_next_signal:
    risky = [name for name, value in matched if re.search(r"(^|\s)next\s+dev\b", value) and (has_port_literal(value) or is_orchestrator(value) or re.search(r"[;&|]|--hostname|--port|-p\b", value))]
    print()
    print("  Next.js start-command hint:")
    print(f"    - Next direct-binary recommendation: {NEXT_DIRECT}")
    if risky:
        print(f"    - Risky Next script shape in: {', '.join(risky)}. Prefer the direct binary or a clean dev:app-it script.")
    else:
        print("    - Plain next dev scripts are OK, but direct binary is safest when adding --hostname/--port.")
PY
fi

print_section "Claude Artifact signals"
ARTIFACT_URL_PATTERN="https://claude\.ai/[^ )\"'>]+"
ARTIFACT_API_PATTERN="window\.claude|window\.storage|claude\.complete|claude\.request"
if command -v rg >/dev/null 2>&1; then
    ARTIFACT_URLS="$(rg --no-heading -n -E "$ARTIFACT_URL_PATTERN" \
        -g '*.md' -g '*.json' -g '*.html' -g '*.js' -g '*.jsx' -g '*.ts' -g '*.tsx' \
        -g '!node_modules/**' -g '!desktop/**' -g '!assets/icons/**' -g '!.git/**' . 2>/dev/null | head -8 || true)"
    ARTIFACT_API="$(rg --no-heading -n -E "$ARTIFACT_API_PATTERN" \
        -g '*.html' -g '*.js' -g '*.jsx' -g '*.ts' -g '*.tsx' \
        -g '!node_modules/**' -g '!desktop/**' -g '!assets/icons/**' -g '!.git/**' . 2>/dev/null | head -8 || true)"
else
    ARTIFACT_URLS="$(grep -RnE "$ARTIFACT_URL_PATTERN" . \
        --include='*.md' --include='*.json' --include='*.html' --include='*.js' --include='*.jsx' --include='*.ts' --include='*.tsx' \
        --exclude-dir=node_modules --exclude-dir=desktop --exclude-dir=.git 2>/dev/null | head -8 || true)"
    ARTIFACT_API="$(grep -RnE "$ARTIFACT_API_PATTERN" . \
        --include='*.html' --include='*.js' --include='*.jsx' --include='*.ts' --include='*.tsx' \
        --exclude-dir=node_modules --exclude-dir=desktop --exclude-dir=.git 2>/dev/null | head -8 || true)"
fi
if [ -n "$ARTIFACT_URLS" ]; then
    echo "$ARTIFACT_URLS" | sed 's/^/  possible hosted artifact URL: /'
else
    echo "  (no claude.ai URLs found in local project files)"
fi
if [ -n "$ARTIFACT_API" ]; then
    echo "$ARTIFACT_API" | sed 's/^/  Claude Artifact runtime API usage: /'
    echo "  -> If this source needs the logged-in Claude plan, package a published/shared Claude Artifact URL with external_url/artifact_url."
    echo "     Do not shim Claude auth, cookies, or API keys into a local JSX bundle."
else
    echo "  (no window.claude/window.storage usage found in local source)"
fi

print_section "Framework port literals (would override launcher's PORT env)"
if [ -f "$ROOT/vite.config.ts" ] || [ -f "$ROOT/vite.config.js" ] || [ -f "$ROOT/vite.config.mjs" ]; then
    for cfg in "$ROOT"/vite.config.{ts,js,mjs}; do
        [ -f "$cfg" ] || continue
        if grep -nE 'server:\s*\{[^}]*port:\s*[0-9]+' "$cfg" 2>/dev/null | head -3; then
            echo "  → vite.config.ts has hardcoded server.port literal."
            echo "    Vanilla single-server: pass CLI flags via START_COMMAND ('npm run dev -- --host 127.0.0.1 --port \$PORT --strictPort')."
            echo "    Multi-server / proxy: edit vite.config.ts — see SKILL.md A3.2 carve-out."
        fi
        if grep -nE 'proxy:\s*\{[^}]*target:\s*["'"'"']http://localhost:[0-9]+' "$cfg" 2>/dev/null | head -3; then
            echo "  → vite.config.ts has hardcoded proxy target. Multi-server cohabiting (A3) likely."
        fi
    done
fi

print_section "FSA (File System Access) usage"
if command -v rg >/dev/null 2>&1; then
    GREP_CMD="rg --no-heading -n -E"
else
    GREP_CMD="grep -RnIE"
fi
echo "Stage 1: any FSA usage at all (polyfill candidate)"
$GREP_CMD "showDirectoryPicker|FileSystemDirectoryHandle|FileSystemFileHandle" \
    --include='*.{ts,tsx,js,jsx}' src/ services/ app/ 2>/dev/null | head -8 || echo "  (none found)"
echo
echo "Stage 2: real-I/O usage (polyfill cannot satisfy this — chrome-fallback or D)"
$GREP_CMD "\.createWritable\(|\.getFile\(\)|writable\.write\(" \
    --include='*.{ts,tsx,js,jsx}' src/ services/ app/ 2>/dev/null | head -8 || echo "  (none found)"

print_section "Sibling appified apps & their preferred ports (collision check)"
PORTS_FOUND=""
for install_dir in "$HOME/Applications/App It" "$HOME/Desktop/MyApps"; do
    [ -d "$install_dir" ] || continue
    for app in "$install_dir"/*.app; do
        [ -d "$app" ] || continue
        name="$(basename "$app" .app)"
        run_script="$app/Contents/MacOS/run"
        if [ -f "$run_script" ]; then
            port="$(grep -E "^PREFERRED(_FE)?_PORT=" "$run_script" 2>/dev/null | head -1 | cut -d= -f2 || true)"
            if [ -n "$port" ]; then
                echo "  $name → :$port ($install_dir)"
                PORTS_FOUND="$PORTS_FOUND $port"
            fi
        fi
    done
done
if [ -z "$PORTS_FOUND" ]; then
    echo "  (no app-it launchers found under the default install folders)"
fi
if [ -d "$HOME/Applications/App It" ] && [ -d "$HOME/Desktop/MyApps" ]; then
    legacy_dupes=""
    for legacy_app in "$HOME/Desktop/MyApps"/*.app; do
        [ -d "$legacy_app" ] || continue
        name="$(basename "$legacy_app")"
        if [ -d "$HOME/Applications/App It/$name" ]; then
            legacy_dupes="$legacy_dupes
  $name"
        fi
    done
    if [ -n "$legacy_dupes" ]; then
        echo
        echo "  ⚠ legacy duplicate app bundles found in ~/Desktop/MyApps and ~/Applications/App It:"
        printf '%s\n' "$legacy_dupes" | head -10
        echo "    Keep one install location. The default is ~/Applications/App It; Desktop is often iCloud-backed and can cause extra Gatekeeper/iCloud scanning."
    fi
fi

app_process_snapshot() {
    command -v lsappinfo >/dev/null 2>&1 || return 0
    /usr/bin/lsappinfo 2>/dev/null | awk '
        function flush() {
            if (name != "" && bid != "" && pid != "") {
                print name "\t" bid "\t" pid "\tunknown"
            }
        }
        /^[[:space:]]*[0-9]+\) "/ {
            flush()
            name=$0
            sub(/^[^\"]*\"/, "", name)
            sub(/\".*/, "", name)
            bid=""
            pid=""
        }
        /bundleID="/ {
            bid=$0
            sub(/^.*bundleID="/, "", bid)
            sub(/".*/, "", bid)
        }
        /pid = / {
            pid=$0
            sub(/^.*pid = /, "", pid)
            sub(/ .*/, "", pid)
        }
        END { flush() }
    ' || true
}

print_section "Live appified app/window processes (diagnostic only)"
APP_ROWS="$(app_process_snapshot)"
WINDOW_FOUND=0
APP_BUNDLES_FOUND=0
for install_dir in "$HOME/Applications/App It" "$HOME/Desktop/MyApps"; do
    [ -d "$install_dir" ] || continue
    for app in "$install_dir"/*.app; do
        [ -d "$app" ] || continue
        plist="$app/Contents/Info.plist"
        [ -f "$plist" ] || continue
        bid="$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$plist" 2>/dev/null || true)"
        [ -n "$bid" ] || continue
        APP_BUNDLES_FOUND=1
        matches="$(printf '%s\n' "$APP_ROWS" | awk -F '\t' -v bid="$bid" '$2 == bid {print}')"
        [ -n "$matches" ] || continue
        while IFS="$(printf '\t')" read -r proc_name proc_bid proc_pid proc_windows; do
            [ -n "$proc_pid" ] || continue
            echo "  $(basename "$app" .app) — live PID $proc_pid, bundle $proc_bid"
            echo "    unknown/probably foreign to this repo unless it is the app you are inspecting; inspect will not close or kill it"
            WINDOW_FOUND=1
        done <<< "$matches"
    done
done
if [ "$APP_BUNDLES_FOUND" = "0" ]; then
    echo "  (no installed app-it launchers found under the default install folders)"
elif [ -z "$APP_ROWS" ]; then
    echo "  (could not query live app/window processes — LaunchServices did not report visible apps)"
elif [ "$WINDOW_FOUND" = "0" ]; then
    echo "  (no live installed app-it app/window processes found)"
fi

CONFIG_PORTS="$(
    /usr/bin/python3 - <<'PY' 2>/dev/null || true
import json
from pathlib import Path
ports = []
for rel in ("scripts/app-it.config.json", "app-it.config.json"):
    path = Path(rel)
    if not path.exists():
        continue
    try:
        payload = json.loads(path.read_text())
    except Exception:
        continue
    for app in payload.get("apps", []):
        for key in ("port", "backend_port"):
            value = app.get(key)
            if value not in (None, ""):
                ports.append(str(value))
print(" ".join(ports))
PY
)"

print_section "Currently bound ports (config + common dev ports)"
SEEN_PORTS=""
for p in $CONFIG_PORTS $PORTS_FOUND 3000 3001 3002 3003 3004 3005 5173 5174 5175 8000 8080; do
    case "$p" in ''|*[!0-9]*) continue ;; esac
    case " $SEEN_PORTS " in *" $p "*) continue ;; esac
    SEEN_PORTS="$SEEN_PORTS $p"
    holder="$(lsof -i tcp:"$p" -sTCP:LISTEN -nP 2>/dev/null | awk 'NR>1 {printf "%s/%s ", $1, $2}' || true)"
    [ -n "$holder" ] && echo "  :$p — $holder (unknown/probably foreign unless it is one of this launcher’s recorded PIDs; inspect will not stop it)" || true
done

print_section "Toolchain availability"
for cmd in swiftc node npm pnpm yarn bun deno python3 cargo; do
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "  $cmd → $(command -v "$cmd")"
    fi
done

print_section "Runtime data paths the launcher will need (process.cwd, env-keyed paths)"
$GREP_CMD "process\.cwd\(\)|process\.env\.[A-Z_]+|sqlite:\/\/\/|file:\.\/[^ \"']+" \
    --include='*.{ts,tsx,js,jsx,py}' src/ lib/ services/ app/ 2>/dev/null | head -10 || echo "  (none found)"

print_section "Gitignored runtime artifacts (data files, caches the launcher needs)"
if [ -f "$ROOT/.gitignore" ]; then
    grep -E "^(data/|\.env|.*\.db|.*\.sqlite|cache/|\.next/|build/|dist/)" "$ROOT/.gitignore" 2>/dev/null | head -10 || echo "  (no obvious data/env entries in .gitignore)"
fi

print_section "Asset candidates (potential icon sources)"
if command -v find >/dev/null 2>&1; then
    find "$ROOT" -maxdepth 4 \
        \( -path '*/node_modules' -prune -o -path '*/.git' -prune -o -path '*/desktop' -prune \) -o \
        \( -iname 'app-icon*' -o -iname 'app_icon*' -o -iname 'appicon*' \
           -o -iname 'icon.png' -o -iname 'icon.svg' -o -iname 'icon@*.png' \
           -o -iname 'logo*.png' -o -iname 'logo*.svg' \
           -o -iname 'apple-touch-icon*' -o -iname 'android-chrome-*' \
           -o -iname 'favicon-512*' -o -iname 'manifest.json' \
        \) -size +1k -type f -print 2>/dev/null | head -15
fi

print_section "Recent git commit subjects (project-name vocabulary)"
if [ -d "$ROOT/.git" ] || [ -f "$ROOT/.git" ]; then
    git -C "$ROOT" log --pretty=%s -10 2>/dev/null | head -10 || echo "  (no git history)"
fi

echo
echo "=== End of inspection ==="
echo "Next: pick worktree strategy (if applicable), strategy (A1/A2/A3/A4/B/D),"
echo "      bundle ID prefix (NOT com.\$(id -un).*), and dev script per app."
