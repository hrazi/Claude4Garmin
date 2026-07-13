#!/bin/bash
# app-it URL-only launcher (Chrome --app fallback variant).
#
# Used only when APP_IT_LAUNCHER_MODE=chrome or swiftc is unavailable. It wraps
# a hosted URL without starting a local server. For Claude Artifacts this still
# avoids shared keys/auth: each user signs into Claude inside this browser
# profile and usage counts against that user's Claude plan.

set -e

APP_NAME="__APP_NAME__"
APP_SLUG="__APP_SLUG__"

APP_URL="$(cat <<'APP_IT_URL'
__APP_URL__
APP_IT_URL
)"

STATE_DIR="$HOME/Library/Application Support/app-it/$APP_SLUG"
PROFILE="$STATE_DIR/BrowserProfile"
mkdir -p "$PROFILE"

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

if [ -n "${APP_IT_SMOKE:-}" ]; then
    echo "app-it smoke: $APP_NAME ready at $APP_URL (url-only chrome fallback; no local server)"
    exit 0
fi

CHROME_BIN=""
for browser in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
    "/Applications/Arc.app/Contents/MacOS/Arc"; do
    if [ -x "$browser" ]; then
        CHROME_BIN="$browser"
        break
    fi
done

if [ -z "$CHROME_BIN" ]; then
    exec open "$APP_URL"
fi

exec "$CHROME_BIN" --app="$APP_URL" --user-data-dir="$PROFILE"
