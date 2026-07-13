#!/bin/bash
# app-it URL-only launcher (Swift WebKit shell variant).
#
# Used for hosted apps that should not start a local dev server. The main
# current case is a published/shared Claude Artifact URL: Claude's artifact
# host provides the AI bridge, storage, MCP prompts, and user authentication.
# app-it only wraps that URL in a Dock-launchable native window; it never
# ships API keys, cookies, or copied Claude auth state.
#
# Substituted by desktop-build.sh:
#   __APP_NAME__       human display name
#   __APP_SLUG__       file-safe slug
#   __APP_URL__        http(s) URL to load
#   __POLYFILL_PATH__  optional JS injected at document_start (usually empty)

set -e

APP_NAME="__APP_NAME__"
APP_SLUG="__APP_SLUG__"
POLYFILL_PATH="__POLYFILL_PATH__"

APP_URL="$(cat <<'APP_IT_URL'
__APP_URL__
APP_IT_URL
)"

HERE="$(cd "$(dirname "$0")" && pwd)"

show_alert() {
    /usr/bin/osascript - "$1" "$2" <<'APPLESCRIPT'
on run argv
    display alert (item 1 of argv) message (item 2 of argv)
end run
APPLESCRIPT
}

case "$APP_URL" in
    http://*|https://*) ;;
    *)
        show_alert "$APP_NAME failed to launch" "URL-only launchers require an http(s) URL.

Configured URL:
$APP_URL"
        exit 1
        ;;
esac

# Headless seam for fixture tests and SSH sessions. URL-only launchers have no
# daemon or runtime port; proving the run script validates and selects the URL
# mode is enough before GUI verification.
if [ -n "${APP_IT_SMOKE:-}" ]; then
    echo "app-it smoke: $APP_NAME ready at $APP_URL (url-only; no local server)"
    exit 0
fi

WRAPPER="$HERE/wrapper"
if [ ! -x "$WRAPPER" ]; then
    show_alert "$APP_NAME failed to launch" "Native wrapper missing at:
$WRAPPER

Run desktop:build to rebuild the bundle."
    exit 1
fi

# Empty port/pid args mean Cmd+Q only closes the wrapper. The final flag keeps
# hosted auth redirects, Claude's artifact iframe, and AI API bridge traffic
# inside this window instead of ejecting them to the default browser.
exec "$WRAPPER" "$APP_URL" "$APP_NAME" "" "" "$POLYFILL_PATH" "allow-external-hosts"
