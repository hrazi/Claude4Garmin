# Desktop launcher

Click **Garmin Health Coach** in `~/Applications/App It/` (or its Dock Stack) to
open the app in its own native window — not a browser tab.

## First launch

1. Right-click the app icon and choose **Open**, then click **Open** in the
   dialog. macOS remembers this and skips it on later launches (the bundle is
   unsigned, so Gatekeeper asks once).
2. There is **no cold-start compile** — this launcher wraps the already-running
   local server, so the window appears in WebKit startup time (~1 s).
3. If the window shows a connection error, the server isn't up. See
   "If the window can't connect" below.

## How this is wired (important)

This is a **URL-only launcher** (app-it Strategy E). The `.app` is a small Swift
WebKit shell that points at `http://127.0.0.1:8000`. It does **not** start the
server itself.

The server is owned by your login LaunchAgent
`com.garminhealthcoach.launcher`, which:

- starts the FastAPI server silently at login (no browser/window popup —
  `GHC_NO_BROWSER=1`), and
- is set to **`KeepAlive=true`**, so the server is always running and
  auto-restarts if it ever dies. That's why the Dock icon never hits a dead
  server.

### Window behavior

- **Click the icon:** opens a native window onto the running server. Re-clicking
  while open brings the existing window forward (no second window).
- **Red-X / Cmd+Q:** closes the **window only**. The server keeps running under
  the LaunchAgent. This is intentional — your data stays synced in the
  background and the next click is instant.
- A system tray icon ("Garmin Health Coach") also remains available from the
  LaunchAgent, exactly as before.

## If the window can't connect

The server is normally always up. If it isn't:

```bash
launchctl kickstart -k gui/$(id -u)/com.garminhealthcoach.launcher
```

Server logs:

- `~/Library/Logs/GarminHealthCoach.out.log`
- `~/Library/Logs/GarminHealthCoach.err.log`

## Start / stop the server (independent of the Dock icon)

```bash
# stop auto-start entirely (also stops the server)
launchctl bootout gui/$(id -u)/com.garminhealthcoach.launcher

# re-enable auto-start
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.garminhealthcoach.launcher.plist
```

## Rebuild / update the launcher

```bash
./scripts/desktop-build.sh    # rebuild desktop/Garmin Health Coach.app
./scripts/desktop-install.sh  # copy into ~/Applications/App It/, refresh Dock
```

The `~/Applications/App It/` folder is meant to live as a Dock Stack — drag it to
the right side of the Dock once and the app shows up there.

## Replace the app icon

Replace `assets/garmin-health-coach-icon.png` (square, ≥1024×1024 ideal), then:

```bash
APP_NAME='Garmin Health Coach' APP_SLUG='garmin-health-coach' ./scripts/desktop-icons-preview.sh --open   # optional preview
./scripts/desktop-build.sh
./scripts/desktop-install.sh
```

## Diagnose

```bash
./scripts/desktop-doctor.sh garmin-health-coach          # read-only health check
./scripts/desktop-doctor.sh garmin-health-coach --json   # machine-readable
```

> Note: `./scripts/desktop-verify.sh` reports a single false failure for this app
> ("runtime port was not recorded"). That check assumes a launcher-owned local
> server; URL-only launchers intentionally have no recorded runtime port. Use
> `desktop-doctor.sh` (which is URL-only-aware and passes clean) instead.

## Architecture

```
desktop/Garmin Health Coach.app/
  Contents/
    Info.plist        # CFBundleIdentifier = com.garminhealthcoach.app
    MacOS/
      run             # tiny native Mach-O stub (execs run.sh)
      run.sh          # URL-only launcher (execs wrapper with the URL)
      wrapper         # compiled Swift WebKit shell (universal arm64+x86_64)
    Resources/
      AppIcon.icns    # generated from assets/garmin-health-coach-icon.png
```

The target URL is baked at build time. If the server ever moves off
`127.0.0.1:8000`, update `scripts/app-it.config.json` and re-run build + install.

## Known limitations

- **Unsigned bundle.** First launch triggers Gatekeeper; right-click → Open once.
- **WebKit, not Chromium.** For Chrome devtools, point a browser tab at
  `http://127.0.0.1:8000`.
- **Depends on the LaunchAgent.** The Dock window shows the server's UI; if the
  LaunchAgent is removed, the window has nothing to connect to.
