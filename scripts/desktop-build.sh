#!/bin/bash
# Builds desktop/<AppName>.app for scripts/app-it.config.json entries.
# APP_IT_PROJECT_ROOT can bake a canonical repo path while running from a worktree.
# APP_IT_LAUNCHER_MODE chooses swift|chrome.
#
# app-it.config.json shape:
# {
#   "apps": [
#     {
#       "name": "Momó Studio",
#       "slug": "momo-studio",
#       "port": 5173,
#       "port_mode": "fallback",             // optional: fallback|fixed
#       "start_command": "npm run dev -- --port $PORT",
#       "bundle_id": "com.user.momo-studio",
#       "version": "0.1.0",
#       "polyfill_path": "",
#       "backend_port": null,                  // optional, A3.2 multi-server
#       "backend_start_command": null,         // optional, A3.2 multi-server
#       "external_url": ""                     // optional URL-only app, e.g. published Claude Artifact
#     }
#   ]
# }
#
# Single source of truth — desktop-quit.sh reads the same file.

set -euo pipefail

# Helpers stay next to this script; only the runtime project root is overridden.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="${APP_IT_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export APP_IT_PROJECT_ROOT="$ROOT"

CONFIG_FILE="$SCRIPT_DIR/app-it.config.json"

# --- Load apps from JSON (preferred) or bash APPS array (backward compat) ----
APPS=()
if [ -f "$CONFIG_FILE" ]; then
    # Internal record: name|slug|port|port_mode|start_command|bundle_id|version|polyfill_path|backend_port|backend_start_command|external_url
    while IFS= read -r line; do
        [ -n "$line" ] && APPS+=("$line")
    done < <(/usr/bin/python3 - "$CONFIG_FILE" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    cfg = json.load(f)
def text(value):
    return "" if value is None else str(value)
for a in cfg.get("apps", []):
    port_mode = a.get("port_mode", "fallback")
    if port_mode not in ("fallback", "fixed"):
        print(f"ERROR: app {a.get('slug') or a.get('name') or '<unnamed>'} has invalid port_mode {port_mode!r}; expected 'fallback' or 'fixed'", file=sys.stderr)
        sys.exit(1)
    external_url = a.get("external_url") or a.get("artifact_url") or a.get("url") or ""
    fields = [
        text(a.get("name", "")),
        text(a.get("slug", "")),
        text(a.get("port", "")),
        text(port_mode),
        text(a.get("start_command", "")),
        text(a.get("bundle_id", "")),
        text(a.get("version", "0.1.0")),
        text(a.get("polyfill_path", "")),
        text(a.get("backend_port") or ""),
        text(a.get("backend_start_command") or ""),
        text(external_url),
    ]
    # Reject any field containing pipe — would corrupt parsing.
    if any("|" in f for f in fields):
        print("ERROR: pipe character in field — refusing to build", file=sys.stderr)
        sys.exit(1)
    print("|".join(fields))
PY
)
else
    echo "Note: scripts/app-it.config.json not found — falling back to bash APPS array." >&2
    echo "      Recommended: copy templates/app-it.config.example.json to scripts/." >&2
    APPS=(
      # Replace these with your apps. One line per app.
      # Format: name|slug|port|port_mode|start_command|bundle_id|version|polyfill_path|backend_port|backend_start_command|external_url
      "__APP_NAME__|__APP_SLUG__|__PORT__|fallback|__START_COMMAND__|__BUNDLE_ID__|__VERSION__|__POLYFILL_PATH_ENTRY__|||"
    )
fi

if [ "${#APPS[@]}" -eq 0 ]; then
    echo "ERROR: no apps configured. Edit scripts/app-it.config.json." >&2
    exit 1
fi

# --- Bundle-ID validation -----------------------------------------------
# Reject com.$(id -un).*; LaunchServices can treat it as a personal-team prefix
# and refuse local bundles with `_LSOpenURLs... error -600 / procNotFound`.
USER_PREFIX="com.$(id -un | tr 'A-Z' 'a-z')."
for entry in "${APPS[@]}"; do
    IFS='|' read -r _ _ _ _ _ BID _ _ _ _ _ <<<"$entry"
    BID_LOWER="$(echo "$BID" | tr 'A-Z' 'a-z')"
    case "$BID_LOWER" in
        "$USER_PREFIX"*)
            echo "WARN: bundle_id '$BID' starts with com.\$(id -un). LaunchServices may reject it (error -600). Prefer com.user.<slug> or country-coded reverse-DNS." >&2
            ;;
    esac
done

# --- Launcher mode -----------------------------------------------------
LAUNCHER_MODE="${APP_IT_LAUNCHER_MODE:-swift}"
if [ "$LAUNCHER_MODE" = "swift" ] && ! command -v swiftc >/dev/null 2>&1; then
    echo "swiftc not found — falling back to Chrome --app launcher." >&2
    echo "(Install Xcode Command Line Tools: xcode-select --install)" >&2
    LAUNCHER_MODE="chrome"
fi

PLIST_TEMPLATE="$SCRIPT_DIR/info-plist-template.xml"
WRAPPER_SRC="$SCRIPT_DIR/wrapper.swift"
WRAPPER_BUILD="$ROOT/assets/icons/build/wrapper"
RUN_STUB_SRC="$SCRIPT_DIR/native-run-stub.c"
RUN_STUB_BUILD="$ROOT/assets/icons/build/run-stub"

if [ "$LAUNCHER_MODE" = "swift" ]; then
    RUN_TEMPLATE_SINGLE="$SCRIPT_DIR/run-template.sh"
    RUN_TEMPLATE_URL="$SCRIPT_DIR/run-template-url.sh"
else
    RUN_TEMPLATE_SINGLE="$SCRIPT_DIR/run-template-chrome.sh"
    RUN_TEMPLATE_URL="$SCRIPT_DIR/run-template-url-chrome.sh"
fi
RUN_TEMPLATE_MULTI="$SCRIPT_DIR/run-template-multiserver.sh"

if [ ! -f "$RUN_TEMPLATE_SINGLE" ] || [ ! -f "$RUN_TEMPLATE_URL" ] || [ ! -f "$PLIST_TEMPLATE" ]; then
    echo "Missing templates next to this script. Expected:" >&2
    echo "  $RUN_TEMPLATE_SINGLE" >&2
    echo "  $RUN_TEMPLATE_URL" >&2
    echo "  $PLIST_TEMPLATE" >&2
    exit 1
fi

# --- Compile the native WebKit wrapper (cached, universal) -------------
# Build universal by default; APP_IT_SWIFT_ARCHS can narrow the arch list.
if [ "$LAUNCHER_MODE" = "swift" ]; then
    if [ ! -f "$WRAPPER_SRC" ]; then
        echo "Missing wrapper source: $WRAPPER_SRC" >&2
        exit 1
    fi
    mkdir -p "$(dirname "$WRAPPER_BUILD")"

    SWIFT_ARCHS="${APP_IT_SWIFT_ARCHS:-arm64,x86_64}"
    NEEDS_REBUILD=0
    if [ ! -x "$WRAPPER_BUILD" ] || [ "$WRAPPER_SRC" -nt "$WRAPPER_BUILD" ]; then
        NEEDS_REBUILD=1
    fi

    if [ "$NEEDS_REBUILD" = "1" ]; then
        echo "Compiling native wrapper: $WRAPPER_BUILD ($SWIFT_ARCHS)"
        IFS=',' read -r -a ARCH_LIST <<<"$SWIFT_ARCHS"
        ARCH_BINS=()
        for arch in "${ARCH_LIST[@]}"; do
            arch_clean="$(echo "$arch" | tr -d ' ')"
            BIN="$WRAPPER_BUILD.$arch_clean"
            if swiftc "$WRAPPER_SRC" \
                -o "$BIN" \
                -framework Cocoa -framework WebKit \
                -target "$arch_clean-apple-macosx11" 2>/dev/null; then
                ARCH_BINS+=("$BIN")
            else
                echo "WARN: swiftc target $arch_clean failed — skipping (toolchain may not have SDK)." >&2
            fi
        done

        if [ "${#ARCH_BINS[@]}" -eq 0 ]; then
            # Last resort: build for host arch with no -target.
            echo "All targeted archs failed — building for host arch only." >&2
            swiftc "$WRAPPER_SRC" -o "$WRAPPER_BUILD" -framework Cocoa -framework WebKit
        elif [ "${#ARCH_BINS[@]}" -eq 1 ]; then
            mv "${ARCH_BINS[0]}" "$WRAPPER_BUILD"
        else
            lipo -create "${ARCH_BINS[@]}" -output "$WRAPPER_BUILD"
            rm -f "${ARCH_BINS[@]}"
        fi
    fi
fi

# --- Compile the native run stub --------------------------------------
# CFBundleExecutable should be Mach-O. Use a tiny run -> run.sh stub when a C
# toolchain exists; otherwise keep the older shell-as-run shape.
RUN_STUB_AVAILABLE=0
if [ -f "$RUN_STUB_SRC" ]; then
    RUN_STUB_CC="${APP_IT_CC:-$(command -v cc 2>/dev/null || command -v clang 2>/dev/null || true)}"
    if [ -n "$RUN_STUB_CC" ]; then
        if [ ! -x "$RUN_STUB_BUILD" ] || [ "$RUN_STUB_SRC" -nt "$RUN_STUB_BUILD" ]; then
            echo "Compiling native run stub: $RUN_STUB_BUILD"
            mkdir -p "$(dirname "$RUN_STUB_BUILD")"
            STUB_ARCHS="${APP_IT_STUB_ARCHS:-${APP_IT_SWIFT_ARCHS:-arm64,x86_64}}"
            IFS=',' read -r -a STUB_ARCH_LIST <<<"$STUB_ARCHS"
            STUB_ARCH_BINS=()
            for arch in "${STUB_ARCH_LIST[@]}"; do
                arch_clean="$(echo "$arch" | tr -d ' ')"
                BIN="$RUN_STUB_BUILD.$arch_clean"
                if "$RUN_STUB_CC" -arch "$arch_clean" -mmacosx-version-min=11.0 "$RUN_STUB_SRC" -o "$BIN" 2>/dev/null; then
                    STUB_ARCH_BINS+=("$BIN")
                else
                    rm -f "$BIN"
                fi
            done
            if [ "${#STUB_ARCH_BINS[@]}" -eq 0 ]; then
                if "$RUN_STUB_CC" "$RUN_STUB_SRC" -o "$RUN_STUB_BUILD" 2>/dev/null; then
                    RUN_STUB_AVAILABLE=1
                else
                    echo "WARN: native run stub failed to compile — falling back to shell CFBundleExecutable." >&2
                    rm -f "$RUN_STUB_BUILD"
                fi
            elif [ "${#STUB_ARCH_BINS[@]}" -eq 1 ]; then
                mv "${STUB_ARCH_BINS[0]}" "$RUN_STUB_BUILD"
                RUN_STUB_AVAILABLE=1
            else
                lipo -create "${STUB_ARCH_BINS[@]}" -output "$RUN_STUB_BUILD"
                rm -f "${STUB_ARCH_BINS[@]}"
                RUN_STUB_AVAILABLE=1
            fi
        else
            RUN_STUB_AVAILABLE=1
        fi
    else
        echo "WARN: no C compiler found — falling back to shell CFBundleExecutable." >&2
    fi
fi

# --- Substitution helper -----------------------------------------------
# Strip TEMPLATE-DOCS blocks before substituting placeholders into artifacts.
substitute() {
    /usr/bin/python3 - "$@" <<'PY'
import sys, pathlib, re
src = pathlib.Path(sys.argv[1]).read_text()
src = re.sub(r"### TEMPLATE-DOCS-START.*?### TEMPLATE-DOCS-END\n?", "", src, flags=re.DOTALL)
for arg in sys.argv[2:]:
    key, _, value = arg.partition("=")
    src = src.replace(key, value)
sys.stdout.write(src)
PY
}

# --- Build each app -----------------------------------------------------
for entry in "${APPS[@]}"; do
    IFS='|' read -r APP_NAME APP_SLUG PORT PORT_MODE START_COMMAND BUNDLE_ID VERSION POLYFILL_PATH BACKEND_PORT BACKEND_START_COMMAND EXTERNAL_URL <<<"$entry"
    POLYFILL_PATH="${POLYFILL_PATH//@ROOT@/$ROOT}"

    APP_DIR="$ROOT/desktop/${APP_NAME}.app"
    CONTENTS="$APP_DIR/Contents"
    MACOS="$CONTENTS/MacOS"
    RESOURCES="$CONTENTS/Resources"

    echo "Building: $APP_DIR"
    mkdir -p "$MACOS" "$RESOURCES"

    # URL-only apps (Claude Artifact links, hosted dashboards) do not start a
    # local daemon. Multi-server is only for local apps.
    if [ -n "$EXTERNAL_URL" ]; then
        if [ -n "$PORT" ] || [ -n "$START_COMMAND" ] || [ -n "$BACKEND_PORT" ] || [ -n "$BACKEND_START_COMMAND" ]; then
            echo "Warning: $APP_NAME sets an external URL and local server fields; URL-only mode wins and local server fields are ignored." >&2
        fi
        SELECTED_RUN_TEMPLATE="$RUN_TEMPLATE_URL"
        IS_MULTI=0
        IS_URL=1
    elif [ -n "$BACKEND_PORT" ] && [ -n "$BACKEND_START_COMMAND" ] && [ -f "$RUN_TEMPLATE_MULTI" ]; then
        SELECTED_RUN_TEMPLATE="$RUN_TEMPLATE_MULTI"
        IS_MULTI=1
        IS_URL=0
    else
        SELECTED_RUN_TEMPLATE="$RUN_TEMPLATE_SINGLE"
        IS_MULTI=0
        IS_URL=0
    fi

    substitute "$PLIST_TEMPLATE" \
        "__APP_NAME__=$APP_NAME" \
        "__BUNDLE_ID__=$BUNDLE_ID" \
        "__VERSION__=$VERSION" \
        > "$CONTENTS/Info.plist"

    RUN_SCRIPT="$MACOS/run"
    if [ "$RUN_STUB_AVAILABLE" = "1" ]; then
        RUN_SCRIPT="$MACOS/run.sh"
    fi

    if [ "$IS_URL" = "1" ]; then
        substitute "$SELECTED_RUN_TEMPLATE" \
            "__APP_NAME__=$APP_NAME" \
            "__APP_SLUG__=$APP_SLUG" \
            "__APP_URL__=$EXTERNAL_URL" \
            "__POLYFILL_PATH__=$POLYFILL_PATH" \
            > "$RUN_SCRIPT"
    elif [ "$IS_MULTI" = "1" ]; then
        substitute "$SELECTED_RUN_TEMPLATE" \
            "__APP_NAME__=$APP_NAME" \
            "__APP_SLUG__=$APP_SLUG" \
            "__PROJECT_ROOT__=$ROOT" \
            "__PORT__=$PORT" \
            "__PORT_MODE__=$PORT_MODE" \
            "__START_COMMAND__=$START_COMMAND" \
            "__BACKEND_PORT__=$BACKEND_PORT" \
            "__BACKEND_START_COMMAND__=$BACKEND_START_COMMAND" \
            "__POLYFILL_PATH__=$POLYFILL_PATH" \
            > "$RUN_SCRIPT"
    else
        substitute "$SELECTED_RUN_TEMPLATE" \
            "__APP_NAME__=$APP_NAME" \
            "__APP_SLUG__=$APP_SLUG" \
            "__PROJECT_ROOT__=$ROOT" \
            "__PORT__=$PORT" \
            "__PORT_MODE__=$PORT_MODE" \
            "__START_COMMAND__=$START_COMMAND" \
            "__POLYFILL_PATH__=$POLYFILL_PATH" \
            > "$RUN_SCRIPT"
    fi
    chmod +x "$RUN_SCRIPT"

    if [ "$RUN_STUB_AVAILABLE" = "1" ]; then
        cp "$RUN_STUB_BUILD" "$MACOS/run"
        chmod +x "$MACOS/run"
    else
        rm -f "$MACOS/run.sh"
    fi

    if [ "$LAUNCHER_MODE" = "swift" ]; then
        cp "$WRAPPER_BUILD" "$MACOS/wrapper"
        chmod +x "$MACOS/wrapper"
    fi

    # desktop-icons.sh is mtime-aware; always call it to avoid stale icons.
    APP_NAME="$APP_NAME" APP_SLUG="$APP_SLUG" "$SCRIPT_DIR/desktop-icons.sh"

    # Touch the bundle so Finder picks up changes (icon cache).
    touch "$APP_DIR"

    # --- Ad-hoc code signature ----------------------------------------
    # macOS 15+ can reject unsigned local apps from Finder/Dock. Ad-hoc signing
    # is enough; strip xattrs first because synced folders can taint bundles.
    /usr/bin/xattr -cr "$APP_DIR" 2>/dev/null || true
    if ! /usr/bin/codesign --force --deep --sign - "$APP_DIR" >/dev/null 2>&1; then
        echo "  WARN: codesign --sign - failed for $APP_DIR (app may be blocked by Gatekeeper)" >&2
    fi
done

echo
echo "Built ${#APPS[@]} app(s) under $ROOT/desktop/  (mode: $LAUNCHER_MODE)"
echo "  Install:  ./scripts/desktop-install.sh    # copies to ~/Applications/App It/, refreshes Dock"
