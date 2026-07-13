## App-it report

**1. Project type detected:**
Python 3.13 FastAPI single-server web app (`coach/server.py`, uvicorn), served on
fixed port 8000 via `find_free_port(8000, 8010)`. Canonical checkout at
`/Users/hrazi/Claude4Garmin` (no worktree). Toolchains present: `swiftc`,
`python3`, `cc`. The server's lifecycle is owned by an existing macOS LaunchAgent
(`com.garminhealthcoach.launcher`) that silently auto-starts it at login.

**1.5. Name resolution**
Picked: "Garmin Health Coach". Sources surveyed: tray title in `launcher.py`
("Garmin Health Coach"), repo name (`Claude4Garmin`), recent commit vocabulary,
LaunchAgent label (`com.garminhealthcoach.*`). Reason: the tray/app already
presents itself as "Garmin Health Coach". To override: edit
`scripts/app-it.config.json`, then `desktop:build && desktop:install`.

**2. Apps detected:** 1
- **Garmin Health Coach** — FastAPI server on `http://127.0.0.1:8000`, started and
  kept alive by the login LaunchAgent (not by app-it).

**3. Strategy chosen per app:**
- Garmin Health Coach: **Strategy E (URL-only WebKit wrapper)** — the server is
  already always-on under the user's LaunchAgent, so the Dock app wraps
  `http://127.0.0.1:8000` instead of owning a second daemon.

**4. Why these are the lowest-effort robust approaches:**
The user explicitly wants to keep silent auto-start at login (LaunchAgent) and
just add a clickable Dock window. A standard A1 native launcher would start its
own daemon and collide with the LaunchAgent on fixed port 8000. URL-only mode
wraps the running server with zero business-logic changes, is fully reversible,
and matches the user's "always-on + on-demand window" intent. To remove the
"server could be down" edge, the LaunchAgent was switched to `KeepAlive=true` so
the server auto-restarts and the Dock window never hits a dead server.

**5. Files added/changed:**
- `assets/garmin-health-coach-icon.png` (copied from `assets/icon.png`, the slug source icon)
- `desktop/Garmin Health Coach.app/...` (generated bundle; gitignored)
- copied `scripts/` templates + `scripts/app-it.config.json` (URL-only config)
- `~/Library/LaunchAgents/com.garminhealthcoach.launcher.plist` — `KeepAlive` false → true (outside repo)
- `docs/desktop-launcher.md`
- `docs/desktop-launcher.app-it-report.md`
- `.gitignore` — added `desktop/` and `assets/icons/`

**6. Icon source per app:**
- Garmin Health Coach: `assets/garmin-health-coach-icon.png` — 512×512 PNG (square),
  copied from the project's existing tray icon `assets/icon.png`. Beat alternatives
  because it's the app's real brand mark and the only icon asset in the repo.
  Considered: `assets/icon.png` (same file), placeholder generator (not needed).

**7. To change an app icon later:**
Replace `assets/garmin-health-coach-icon.png`, optionally run
`./scripts/desktop-icons-preview.sh`, then run `desktop-build.sh` and
`desktop-install.sh`. Install refreshes the Dock/Finder icon caches.

**8. Build / install / quit commands:**
- Build: `./scripts/desktop-build.sh`
- Install: `./scripts/desktop-install.sh`
- Quit: closing the window / Cmd+Q (URL-only: closes window only; server stays up under the LaunchAgent)
- Diagnose: `./scripts/desktop-doctor.sh garmin-health-coach`

**9. Generated launcher locations:**
- Repo: `desktop/Garmin Health Coach.app`
- Installed: `~/Applications/App It/Garmin Health Coach.app`
- Runtime port after first click: n/a (URL-only; no launcher-owned server, so no
  `server.port` is recorded). The wrapped server is fixed at `127.0.0.1:8000`.

**10. Verification (per app):**
- [x] Build succeeded; `.app` exists; `run`/`run.sh` + universal Swift `wrapper` present; `AppIcon.icns` valid
- [x] Bundle metadata correct (`CFBundleName=Garmin Health Coach`, `CFBundleIdentifier=com.garminhealthcoach.app`, `CFBundleExecutable=run`); no placeholder leakage in text files; ad-hoc signed
- [x] Installed-path open exits 0; wrapper runs as `wrapper "http://127.0.0.1:8000" "Garmin Health Coach" "" "" "" "allow-external-hosts"`; server responds HTTP 200
- [x] Process identity confirmed — `System Events` reports a running process with bundle id `com.garminhealthcoach.app`; window title "Garmin Health Coach"
- [x] Cmd+Q via Apple Event closes the window; **server intentionally survives** (HTTP 200, LaunchAgent pid still running) — correct for URL-only / Option B+
- [x] Red-X / quit leaves the server running (owned by LaunchAgent)
- [x] Warm relaunch is fast (window reopens onto the running server in ~1 s)
- [x] `desktop-doctor` passed clean (15 ok, 0 warn, 0 fail; URL-only-aware)
- [ ] needs human: actual rendered window content (dashboard visuals), Dock icon
      identity at real sizes, keyboard shortcuts inside the page
- [~] `desktop-verify` reports one false `fail` ("runtime port was not recorded")
      because that script version assumes a launcher-owned local server and is not
      URL-only-aware. Not applicable here; `desktop-doctor` is the correct check.

**11. Dock Stack:**
- [x] `~/Applications/App It/` exists
- [ ] User has dragged `~/Applications/App It/` to the right side of the Dock

**12. Known limitations:**
- Unsigned bundle (Gatekeeper right-click → Open once).
- URL-only: the Dock app depends on the LaunchAgent-managed server; if that
  LaunchAgent is removed, the window has nothing to connect to.
- WebKit, not Chromium.
- This skill's `desktop-verify.sh` is not URL-only-aware (one false failure;
  documented above). `desktop-doctor.sh` is URL-only-aware and authoritative.

## Decision history
- 2026-06-16: Initial build (Strategy E URL-only, bundle-id com.garminhealthcoach.app,
  URL http://127.0.0.1:8000, icon: assets/garmin-health-coach-icon.png). LaunchAgent
  switched to KeepAlive=true so the wrapped server is always-on (user chose
  "Option B+": keep silent auto-start + always-restart + on-demand Dock window).
