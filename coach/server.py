"""server.py — FastAPI web server for Garmin Health Coach.

Entry point for the browser-based UI. Keeps main.py (CLI) untouched.

Startup sequence:
  1. Load credentials from OS keychain
  2. Authenticate with Garmin Connect (reuses cached session tokens)
  3. Fetch 7 days of health data
  4. Create ClaudeCoach with health context baked into system prompt
  5. Launch uvicorn on localhost:8000 and open the browser automatically

If credentials are missing or Garmin fails, the server still starts and
redirects the user to /settings to enter credentials from the browser.

Run with:
    python launcher.py
"""

import asyncio
import json
import os
import re
import secrets
import hmac
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import quote

from contextlib import asynccontextmanager
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from dotenv import load_dotenv

from . import activity_cache as ac
from . import activities as av
from . import credentials_manager as cm
from . import data_cache as dc
from . import nutrition_parser as np_
from . import settings_manager as sm
from . import skills_manager as skm
from . import memory_manager as mm
from . import token_tracker as tt
from . import training_log as tl
from . import insights as ins
from . import goals as gl
from . import weekly_review as wr
from . import analytics as an
from . import pillars as pl
from . import activity_dedupe as dedupe
from . import manual_activities as ma
from . import checkin as ci
from . import fueling as fl
from . import training_plan as tp
from .garmin_client import get_garmin_client, fetch_health_data, fetch_activity_history, format_health_summary, format_trend_summary, fill_readiness_estimates, backfill_sleep_times
from .claude_client import ClaudeCoach
from .paths import bundle_dir, user_data_dir


# ---------------------------------------------------------------------------
# Port selection — try 8000 first, fall back if it's already in use
# ---------------------------------------------------------------------------

def find_free_port(start: int = 8000, end: int = 8010) -> int:
    """Return the first available TCP port in [start, end)."""
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"No free port found between {start} and {end - 1}.")


# Resolved once at import time so launcher.py can read server.APP_PORT
APP_PORT: int = find_free_port()


# ---------------------------------------------------------------------------
# Server state — single-user app, module-level variables are fine
# ---------------------------------------------------------------------------

coach: ClaudeCoach | None = None
health_summary: str | None = None
health_data: dict | None = None
nutrition_data: dict = {}
nutrition_log: dict = {}
garmin_connected: bool = False
garmin_client: object | None = None   # reusable authed client for on-demand fetches (e.g. Training Log)
connection_error: str | None = None
coach_memory: dict = {}       # loaded/updated by _connect() and memory extraction
activity_details: dict = {}   # keyed by activity_id; loaded/updated by _connect()


# ---------------------------------------------------------------------------
# Coach factory — returns the right coach based on settings
# ---------------------------------------------------------------------------

def _make_coach(health_summary: str, history_file: Path):
    """Instantiate the coach configured in settings (Claude or Gemini)."""
    settings = sm.load_settings()
    provider = settings.get("ai_provider", "claude")
    model    = settings.get("ai_model",    "claude-sonnet-4-6")
    if provider == "gemini":
        from .gemini_coach import GeminiCoach
        api_key = cm.load_credential("gemini_api_key") or ""
        if not api_key:
            raise ValueError(
                "Gemini API key not configured. Add it in Settings → Connection."
            )
        return GeminiCoach(
            health_summary=health_summary,
            history_file=history_file,
            api_key=api_key,
            model=model,
        )
    return ClaudeCoach(health_summary=health_summary, history_file=history_file)


def _extra_context_notes(hd: dict, settings: dict) -> str:
    """Compose the proactive-coaching notes (active alerts + goal progress)."""
    parts = []
    alerts = ins.compute_alerts(hd)
    if alerts:
        parts.append(ins.format_for_prompt(alerts))
    progress = gl.compute_progress(gl.load_goals(), hd, (hd or {}).get("activities", []))
    if progress:
        parts.append(gl.format_for_prompt(progress, (settings or {}).get("units", "mi")))
    # Habits and subjective check-ins: what the athlete chose to do and how
    # they actually felt, which the watch cannot see.
    habits = ci.format_for_prompt()
    if habits:
        parts.append(habits)
    fuel = fl.format_for_prompt(fl.build(hd, nutrition_data, settings))
    if fuel:
        parts.append(fuel)
    # The training plan, plus how today's session was adjusted to the athlete's
    # actual recovery. Loaded from the saved plan only — never regenerated here,
    # because building a plan needs the full history and this runs on every
    # prompt.
    plan = tp.load_plan()
    if plan:
        today = date.today()
        adaptation = tp.adapt_session(
            tp.session_for(plan, today), tp.readiness_signals(hd, today)
        )
        block = tp.format_for_prompt(plan, adaptation,
                                     (settings or {}).get("units", "mi"), today)
        if block:
            parts.append(block)
    return "\n\n".join(parts)


def _health_data_for_coach() -> dict:
    """
    health_data with hand-entered activities folded into the recent window.

    The coach reads health_data, not the training log, so without this a
    session logged by hand would be invisible to it: the athlete would add
    yesterday's run and then be told they had not trained. Only manual
    activities inside the existing window are added, so the coach's view keeps
    the same time horizon rather than quietly growing a tail of old sessions.

    Returns a shallow copy; health_data itself is never mutated, because it is
    written back to the Garmin cache and manual rows must not leak into it.
    """
    if not health_data:
        return health_data
    recorded = health_data.get("activities") or []
    manual = ma.load_manual()
    if not manual:
        return health_data

    # Anchor to the window the fetch actually covered. With no recorded
    # activities at all there is no window to respect, so fall back to the
    # configured count.
    oldest = min((str(a.get("date") or "") for a in recorded if a.get("date")), default="")
    in_window = [m for m in manual if not oldest or str(m.get("date") or "") >= oldest]

    merged = sorted(
        list(recorded) + in_window,
        key=lambda r: (str(r.get("date") or ""), str(r.get("start_time") or "")),
        reverse=True,
    )
    out = dict(health_data)
    out["activities"] = merged
    return out


def _build_coach_summary(settings: dict) -> str:
    """
    Build the full system-context string for the coach from current global
    health/nutrition state. Single source of truth used by _connect() and
    every route that rebuilds the coach (profile/goals changes).
    """
    trend_summary = format_trend_summary(health_data) if health_data else ""
    memory_notes = mm.format_memory_for_prompt(mm.load_memory())
    extra_notes = _extra_context_notes(health_data, settings) if health_data else ""
    return format_health_summary(
        _health_data_for_coach(), settings, nutrition_data, nutrition_log,
        memory_notes=memory_notes,
        trend_summary=trend_summary,
        extra_notes=extra_notes,
    )


def _rebuild_coach() -> None:
    """Rebuild health_summary + coach from current globals (after profile/goal edits)."""
    global health_summary, coach
    if not health_data:
        return
    settings = sm.load_settings()
    health_summary = _build_coach_summary(settings)
    _provider = settings.get("ai_provider", "claude")
    coach = _make_coach(health_summary, user_data_dir() / f"chat_history_{_provider}.json")


# ---------------------------------------------------------------------------
# Connection helper — called on startup and after credential updates
# ---------------------------------------------------------------------------

async def _connect() -> None:
    """
    Load credentials, auth Garmin, fetch health data, create coach.
    Updates module-level state. Never raises — errors are stored in
    connection_error so the UI can display them gracefully.
    """
    global coach, health_summary, health_data, nutrition_data, nutrition_log, garmin_connected, garmin_client, connection_error, coach_memory, activity_details

    # Keychain → env var fallback, then load .env for any remaining gaps
    cm.inject_into_env()
    load_dotenv()

    if not cm.credentials_complete():
        connection_error = "Credentials not configured. Please fill in the form below."
        garmin_connected = False
        return

    email = os.environ["GARMIN_EMAIL"]
    password = os.environ["GARMIN_PASSWORD"]

    try:
        # Garmin auth and data fetching are blocking — run in thread pool
        settings = sm.load_settings()
        days_back = int(settings.get("days_back", 7))

        # Incremental fetch: only hit the Garmin API for stale or missing dates
        cache = dc.load_cache()
        dates, fetch_shared = dc.plan_fetch(cache, days_back)

        garmin = await asyncio.to_thread(get_garmin_client, email, password)
        garmin_client = garmin   # keep the authed client for on-demand fetches (Training Log)
        raw = await asyncio.to_thread(
            fetch_health_data, garmin, settings, dates, fetch_shared
        )

        # Merge fresh data into the cached baseline, then persist
        if cache is not None:
            raw = dc.merge(cache["health_data"], raw, days_back)
        # Re-estimate readiness across the merged window so today's estimate uses
        # the full resting-HR baseline (and older cached days get backfilled).
        fill_readiness_estimates(raw)
        dc.save_cache(raw)

        health_data = raw
        nutrition_data = np_.load_nutrition()
        nutrition_log = np_.load_nutrition_log()

        # Build the full coach context (memory + trends + alerts + goals)
        coach_memory  = mm.load_memory()
        health_summary = _build_coach_summary(settings)
        _provider = settings.get("ai_provider", "claude")
        coach = _make_coach(health_summary, user_data_dir() / f"chat_history_{_provider}.json")
        garmin_connected = True
        connection_error = None
        print("✓ Connected to Garmin. Web server ready.")

        # Load activity detail cache and enrich new activity IDs in background
        activity_details = ac.load_activity_details()
        missing_ids = ac.get_missing_ids(raw.get("activities", []), activity_details)
        if missing_ids:
            asyncio.create_task(_enrich_activities_background(garmin, missing_ids, settings))

        # Launch background memory extraction if enough new turns have accumulated
        if coach and mm.should_extract(coach.history, coach_memory):
            asyncio.create_task(_extract_memory_background(coach))
    except Exception as e:
        garmin_connected = False
        connection_error = str(e)
        coach = None
        print(f"✗ Garmin connection failed: {e}")


# ---------------------------------------------------------------------------
# Background memory extraction
# ---------------------------------------------------------------------------

async def _extract_memory_background(current_coach) -> None:
    """
    Background task: extract key facts from recent conversation turns using
    Claude Haiku, update coach_memory.json, and rebuild the active coach's
    system prompt so new facts take effect immediately.

    Uses a snapshot of the coach object passed at task creation time — safe
    against coach being replaced by a concurrent _connect() call.
    """
    global coach_memory, health_summary, coach

    try:
        memory           = mm.load_memory()
        history_snapshot = list(current_coach.history)   # snapshot to avoid mutation
        updated          = await asyncio.to_thread(mm.extract_memory, history_snapshot, memory)
        mm.save_memory(updated)
        coach_memory = updated

        # Rebuild system prompt in-place — only if coach hasn't been replaced
        if coach is current_coach and health_data is not None:
            settings      = sm.load_settings()
            trend_summary = format_trend_summary(health_data)
            memory_notes  = mm.format_memory_for_prompt(updated)
            new_summary   = format_health_summary(
                health_data, settings, nutrition_data, nutrition_log,
                memory_notes=memory_notes,
                trend_summary=trend_summary,
            )
            health_summary = new_summary
            current_coach._base_system_prompt = current_coach._build_system_prompt(new_summary)
            if not current_coach.active_persona:
                current_coach.system_prompt = current_coach._base_system_prompt

        note_count = len([
            l for l in (updated.get("notes") or "").splitlines()
            if l.strip().startswith("- ")
        ])
        print(f"✓ Coach memory updated — {note_count} notes stored.")
    except Exception as e:
        print(f"✗ Memory extraction failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Background activity enrichment
# ---------------------------------------------------------------------------

async def _enrich_activities_background(garmin, missing_ids: list, settings: dict) -> None:
    """
    Fetch per-activity enrichments (HR zones, splits, exercise sets, power zones)
    for new activity IDs and cache them to data/activity_details.json.

    Runs in background after startup. Saves after each activity — fault-tolerant
    if interrupted. Updates the module-level activity_details dict when done.
    """
    global activity_details

    details = ac.load_activity_details()

    for activity_id in missing_ids:
        entry = {"fetched_at": datetime.now().isoformat(timespec="seconds")}

        if settings.get("activity_detail_hr_zones", True):
            try:
                entry["hr_zones"] = await asyncio.to_thread(
                    garmin.get_activity_hr_in_timezones, activity_id
                )
            except Exception as e:
                entry["hr_zones_error"] = str(e)

        if settings.get("activity_detail_splits", True):
            try:
                entry["splits"] = await asyncio.to_thread(
                    garmin.get_activity_splits, activity_id
                )
            except Exception as e:
                entry["splits_error"] = str(e)

        if settings.get("activity_detail_exercise_sets", True):
            try:
                entry["exercise_sets"] = await asyncio.to_thread(
                    garmin.get_activity_exercise_sets, activity_id
                )
            except Exception as e:
                entry["exercise_sets_error"] = str(e)

        if settings.get("activity_detail_power_zones", True):
            try:
                entry["power_zones"] = await asyncio.to_thread(
                    garmin.get_activity_power_in_timezones, activity_id
                )
            except Exception as e:
                entry["power_zones_error"] = str(e)

        details[activity_id] = entry
        ac.save_activity_details(details)   # save after each — fault-tolerant

    activity_details = details
    print(f"✓ Activity enrichment done — {len(missing_ids)} new activities cached.")


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await _connect()
    # Open the browser half a second after startup (gives uvicorn time to bind).
    # When launched via launcher.py the browser open is handled there instead,
    # so this only fires when running server.py directly (dev mode).
    if not getattr(sys, "frozen", False) and "launcher" not in sys.modules:
        target = (
            f"http://localhost:{APP_PORT}"
            if garmin_connected
            else f"http://localhost:{APP_PORT}/settings"
        )
        threading.Timer(0.5, lambda: webbrowser.open(target)).start()
    yield


app = FastAPI(lifespan=lifespan, title="Garmin Health Coach")
app.mount("/static", StaticFiles(directory=str(bundle_dir() / "static")), name="static")
templates = Jinja2Templates(directory=str(bundle_dir() / "templates"))


def static_url(filename: str) -> str:
    """
    URL for a static asset, tagged with the file's modification time.

    Without this the browser keeps serving whatever it cached, so a shipped
    CSS or JS change simply does not arrive until the user happens to hard
    refresh — which they have no reason to do, because from the outside the
    feature just looks broken. Keying on mtime means the URL changes exactly
    when the file does: new bytes are fetched immediately, unchanged files
    stay cached.
    """
    try:
        mtime = int((bundle_dir() / "static" / filename).stat().st_mtime)
    except OSError:
        # Missing file is the template's problem, not something to crash on
        # here; fall back to the plain URL and let the 404 be visible.
        return f"/static/{filename}"
    return f"/static/{filename}?v={mtime}"


templates.env.globals["static_url"] = static_url


# ---------------------------------------------------------------------------
# LAN access authentication (#2)
# ---------------------------------------------------------------------------
# When lan_access is on the server binds 0.0.0.0, exposing all health data and
# the AI chat to the local network. Loopback (the owner on this machine) stays
# unauthenticated; any non-loopback client must present the shared token via a
# cookie, the ?token= query param (which then sets the cookie), or an
# X-GHC-Token header. /health and /static are always allowed.

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}
_AUTH_COOKIE = "ghc_lan_token"

_UNLOCK_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Garmin Health Coach — Unlock</title>
<style>body{font-family:-apple-system,Segoe UI,sans-serif;background:#f2f4f7;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0}
.c{background:#fff;padding:32px;border-radius:14px;box-shadow:0 6px 24px rgba(0,0,0,.08);width:min(360px,92vw)}
h1{font-size:18px;margin:0 0 6px}p{color:#6b7078;font-size:13px;margin:0 0 16px}
input{width:100%;padding:10px;border:1px solid #d7dae0;border-radius:8px;font-size:15px;box-sizing:border-box}
button{width:100%;margin-top:12px;padding:10px;border:0;border-radius:8px;background:#2b2f36;color:#fff;font-size:15px;cursor:pointer}
</style></head><body><div class="c"><h1>🔒 Garmin Health Coach</h1>
<p>This device needs the access token to view your health data.</p>
<form method="get"><input name="token" placeholder="Access token" autofocus>
<button type="submit">Unlock</button></form></div></body></html>"""


def get_or_create_lan_token() -> str:
    """Return the LAN access token, generating and persisting one if absent."""
    settings = sm.load_settings()
    token = settings.get("lan_token")
    if not token:
        token = secrets.token_urlsafe(18)
        settings["lan_token"] = token
        sm.save_settings(settings)
    return token


@app.middleware("http")
async def _lan_auth_middleware(request: Request, call_next):
    settings = sm.load_settings()
    if not settings.get("lan_access"):
        return await call_next(request)   # loopback-only mode: nothing exposed

    client_host = request.client.host if request.client else ""
    path = request.url.path
    if client_host in _LOOPBACK_HOSTS or path == "/health" or path.startswith("/static"):
        return await call_next(request)

    token = get_or_create_lan_token()
    supplied = (
        request.cookies.get(_AUTH_COOKIE)
        or request.headers.get("X-GHC-Token")
        or request.query_params.get("token")
    )
    if supplied and hmac.compare_digest(supplied, token):
        response = await call_next(request)
        # Persist via cookie so the token isn't needed on every navigation.
        if request.query_params.get("token"):
            response.set_cookie(_AUTH_COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 90)
        return response

    return HTMLResponse(_UNLOCK_PAGE, status_code=401)


# ── Jinja2 template filters ───────────────────────────────────────────────────

def _fmt_date(date_str: str) -> str:
    """YYYY-MM-DD → 'Today', 'Yesterday', or 'Mon, Feb 28'."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        delta = (date.today() - d).days
        if delta == 0:
            return "Today"
        if delta == 1:
            return "Yesterday"
        return d.strftime("%a, %b %d")
    except Exception:
        return date_str


def _fmt_date_short(date_str: str) -> str:
    """YYYY-MM-DD → 'Today', 'Yest', or weekday abbreviation ('Mon', 'Tue'…)."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        delta = (date.today() - d).days
        if delta == 0:
            return "Today"
        if delta == 1:
            return "Yest"
        return d.strftime("%a")
    except Exception:
        return date_str


def _hm(seconds) -> str:
    """Seconds → '7h 22m'."""
    if not seconds:
        return "—"
    return f"{int(seconds) // 3600}h {(int(seconds) % 3600) // 60}m"


def _dur(seconds) -> str:
    """Seconds → '45:23' (mm:ss)."""
    if not seconds:
        return "—"
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _compact(n) -> str:
    """Abbreviate large numbers for compact table cells: 8,234 → '8.2k'."""
    if n is None:
        return "—"
    n = int(n)
    if n >= 10000:
        return f"{n // 1000}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


templates.env.filters["fmt_date"]       = _fmt_date
templates.env.filters["fmt_date_short"] = _fmt_date_short
templates.env.filters["compact"]        = _compact
templates.env.filters["hm"]            = _hm
templates.env.filters["dur"]           = _dur


# ---------------------------------------------------------------------------
# Digest — Task Scheduler helpers
# ---------------------------------------------------------------------------

DIGEST_TASK_NAME = "GarminHealthCoachDigest"
_DIGEST_SCRIPT   = bundle_dir() / "digest.py"


def _register_digest_task(send_time: str) -> None:
    """Create or overwrite a daily Task Scheduler entry for the digest."""
    tr = f'"{sys.executable}" "{_DIGEST_SCRIPT}"'
    subprocess.run(
        ["schtasks", "/Create", "/F",
         "/TN", DIGEST_TASK_NAME,
         "/TR", tr,
         "/SC", "DAILY",
         "/ST", send_time],
        check=True, capture_output=True, text=True,
    )


def _unregister_digest_task() -> None:
    """Remove the scheduled task. Silently ignores if it doesn't exist."""
    subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", DIGEST_TASK_NAME],
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def _profile_complete(settings: dict) -> bool:
    """Profile is considered complete when at least sport and goal are filled in."""
    p = settings.get("athlete_profile") or {}
    return bool(p.get("sports") and p.get("goal"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not garmin_connected or not coach:
        return RedirectResponse("/settings")
    settings = sm.load_settings()
    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "health_summary": health_summary,
        "health_data": health_data,
        "nutrition_data": nutrition_data,
        "athlete_profile": settings.get("athlete_profile") or {},
        "profile_complete": _profile_complete(settings),
    })


# ---------------------------------------------------------------------------
# Training Log — full activity history, rendered as a Strava-style weekly grid
# ---------------------------------------------------------------------------

async def _load_activity_history(force_refresh: bool = False) -> dict:
    """
    Return {activities, fetched_at, stale, error} for the Training Log.

    Serves the cached history from data/training_log.json unless a refresh is
    requested or the cache is stale/missing, in which case it re-fetches the
    full history from Garmin using the already-authenticated client (no login).
    """
    cache = tl.load_training_log()
    if not force_refresh and not tl.is_stale(cache):
        return {
            "activities": ma.merge_into(cache["activities"]),
            "fetched_at": cache.get("fetched_at"),
            "stale": False,
            "error": None,
        }

    if garmin_client is None:
        # Not connected yet — serve whatever we have (possibly empty) and flag it.
        return {
            "activities": ma.merge_into((cache or {}).get("activities", [])),
            "fetched_at": (cache or {}).get("fetched_at"),
            "stale": True,
            "error": None if cache else "Not connected to Garmin yet.",
        }

    try:
        fetched = await asyncio.to_thread(fetch_activity_history, garmin_client)
        before = (cache or {}).get("activities") or []
        activities = tl.merge_activities(before, fetched)
        saved = tl.save_training_log(activities)
        return {
            # Merged AFTER saving so hand-entered rows never leak into the
            # Garmin cache file, which is rebuilt from Garmin and would drop
            # them on the next refresh.
            "activities": ma.merge_into(activities),
            "fetched_at": saved.get("fetched_at"),
            "stale": False,
            "error": None,
            "added": max(0, len(activities) - len(before)),
        }
    except Exception as e:
        # Fall back to cache on any fetch error so the page still renders.
        return {
            "activities": ma.merge_into((cache or {}).get("activities", [])),
            "fetched_at": (cache or {}).get("fetched_at"),
            "stale": True,
            "error": str(e),
        }


@app.get("/training-log")
async def training_log_page():
    """
    The Training Log's weekly grid now lives on /activities as its grid view,
    where the same filters and click-through detail apply to it. Kept as a
    redirect so existing bookmarks and links still land somewhere useful.
    """
    return RedirectResponse("/activities?view=grid", status_code=308)


@app.get("/api/training-log")
async def api_training_log(refresh: int = 0):
    result = await _load_activity_history(force_refresh=bool(refresh))
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Activities — browsable history with per-activity detail
# ---------------------------------------------------------------------------

@app.get("/activities", response_class=HTMLResponse)
async def activities_page(request: Request):
    if not garmin_connected:
        return RedirectResponse("/settings")
    settings = sm.load_settings()
    return templates.TemplateResponse(request, "activities.html", {
        "request": request,
        "athlete_profile": settings.get("athlete_profile") or {},
        "units": settings.get("units", "mi"),
    })


@app.get("/api/activities")
async def api_activities(group: str = "", year: str = "", search: str = "",
                         sort: str = "date", desc: int = 1,
                         limit: int = 50, offset: int = 0):
    """Filtered, sorted, paged slice of the full activity history."""
    history = await _load_activity_history()
    acts = history.get("activities") or []
    result = av.query(acts, group=group, year=year, search=search,
                      sort=sort, desc=bool(desc),
                      limit=min(max(1, limit), 200), offset=offset)
    result["facets"] = av.facets(acts)
    result["fetched_at"] = history.get("fetched_at")
    # Streaks describe the whole filtered set, not the page, so they are
    # computed before paging rather than from the rows being returned.
    result["streaks"] = av.weekly_streaks(
        av.query(acts, group=group, year=year, search=search,
                 limit=len(acts) or 1)["activities"])
    # Which of these rows already have enrichment, so the list can show it
    # without a round trip per row.
    result["detailed"] = [a["activity_id"] for a in result["activities"]
                          if a["activity_id"] in activity_details]
    return JSONResponse(result)


@app.get("/api/activities/weeks")
async def api_activity_weeks(group: str = "", year: str = "", search: str = ""):
    """
    Week-by-week buckets for the grid view, plus consistency streaks.

    Declared before /api/activities/{activity_id} on purpose: FastAPI matches
    routes in definition order, and "weeks" would otherwise be swallowed as an
    activity id.
    """
    history = await _load_activity_history()
    acts = history.get("activities") or []
    rows = av.query(acts, group=group, year=year, search=search,
                    limit=len(acts) or 1, offset=0)["activities"]
    return JSONResponse({
        "weeks":      av.weekly_buckets(rows),
        "streaks":    av.weekly_streaks(rows),
        "total":      len(rows),
        "facets":     av.facets(acts),
        "fetched_at": history.get("fetched_at"),
    })


async def _fetch_activity_detail(activity_id: str) -> dict:
    """
    Pull one activity's enrichment from Garmin and cache it.

    Only 30 of ~890 activities were enriched at startup, so opening an older
    one has to fetch on demand. Each section fails independently: a missing
    power meter should not cost you the lap splits.
    """
    global activity_details

    if garmin_client is None:
        raise HTTPException(503, detail="Not connected to Garmin.")

    settings = sm.load_settings()
    entry: dict = {"fetched_at": datetime.now().isoformat(timespec="seconds")}

    sections = (
        ("hr_zones",      "activity_detail_hr_zones",      garmin_client.get_activity_hr_in_timezones),
        ("splits",        "activity_detail_splits",        garmin_client.get_activity_splits),
        ("exercise_sets", "activity_detail_exercise_sets", garmin_client.get_activity_exercise_sets),
        ("power_zones",   "activity_detail_power_zones",   garmin_client.get_activity_power_in_timezones),
    )
    for key, toggle, fn in sections:
        if not settings.get(toggle, True):
            continue
        try:
            entry[key] = await asyncio.to_thread(fn, activity_id)
        except Exception as e:
            entry[f"{key}_error"] = str(e)

    details = ac.load_activity_details()
    details[activity_id] = entry
    ac.save_activity_details(details)
    activity_details = details
    return entry


@app.post("/api/activities/manual")
async def api_add_manual_activity(request: Request):
    """
    Record a session no connected device holds.

    Warns rather than blocks when the entry looks like something already in the
    history: the duplicate check is a heuristic, and refusing outright would
    make a genuine second session of the day impossible to log.
    """
    fields = await request.json()
    try:
        row = ma.build_row(fields)
    except ma.ManualEntryError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    history = await _load_activity_history()
    existing = history.get("activities") or []
    if not fields.get("confirm_duplicate"):
        match = dedupe.find_duplicate(row, existing)
        if match:
            return JSONResponse({
                "duplicate": {
                    "activity_id": match.get("activity_id"),
                    "name": match.get("name"),
                    "date": match.get("date"),
                    "start_time": match.get("start_time"),
                    "type": match.get("type"),
                    "distance_meters": match.get("distance_meters"),
                    "duration_seconds": match.get("duration_seconds"),
                },
            }, status_code=409)

    saved = ma.add(fields)
    return {"activity": saved, "total_manual": len(ma.load_manual())}


@app.delete("/api/activities/manual/{activity_id}")
async def api_delete_manual_activity(activity_id: str):
    if not ma.is_manual(activity_id):
        return JSONResponse({"error": "Only manually added activities can be deleted."},
                            status_code=400)
    if not ma.delete(activity_id):
        raise HTTPException(404, detail="No such manual activity.")
    return {"ok": True, "total_manual": len(ma.load_manual())}


# ---------------------------------------------------------------------------
# Training plan (#23)
# ---------------------------------------------------------------------------

async def _plan_context(force_refresh: bool = False) -> dict:
    """Fitness, race and feasibility measured from the full cached history."""
    settings = sm.load_settings()
    history = await _load_activity_history(force_refresh=force_refresh)
    activities = history.get("activities") or []
    today = date.today()
    fitness = tp.assess_fitness(activities, today)
    race = tp.infer_race(gl.load_goals(), settings.get("athlete_profile"))
    feasibility = tp.assess_feasibility(fitness, race, today)
    return {"settings": settings, "fitness": fitness, "race": race,
            "feasibility": feasibility, "today": today}


def _today_payload(plan: dict | None) -> dict:
    """Today's session and how the athlete's recovery changes it."""
    today = date.today()
    signals = tp.readiness_signals(_health_data_for_coach() or health_data, today)
    session = tp.session_for(plan, today) if plan else None
    adaptation = tp.adapt_session(session, signals)
    return {"date": today.isoformat(), "signals": signals, "adaptation": adaptation,
            "week": tp.current_week(plan, today) if plan else None}


@app.get("/api/plan")
async def api_plan():
    plan = tp.load_plan()
    ctx = await _plan_context()
    return {
        "plan": plan,
        # Always recompute feasibility against today's fitness: a plan saved
        # six weeks ago was assessed against a base the athlete no longer has.
        "feasibility": ctx["feasibility"],
        "fitness": ctx["fitness"],
        "race": ctx["race"],
        "today": _today_payload(plan),
        "units": ctx["settings"].get("units", "mi"),
        "options": [{"key": k, "label": v["label"]} for k, v in tp.RACES.items()],
    }


@app.get("/api/plan/today")
async def api_plan_today():
    return _today_payload(tp.load_plan())


class PlanRequest(BaseModel):
    mode: str = "auto"


@app.post("/api/plan/generate")
async def api_plan_generate(req: PlanRequest):
    ctx = await _plan_context()
    if not ctx["race"]:
        return JSONResponse(
            {"error": "No race goal with a date. Add one on the Goals page first."},
            status_code=400)
    if not ctx["fitness"].get("has_history"):
        return JSONResponse(
            {"error": "No run history to plan from. Sync activities or add a few by hand."},
            status_code=400)
    if not (ctx["race"].get("spec")):
        return JSONResponse(
            {"error": f"Couldn't tell what distance \u201c{ctx['race']['name']}\u201d is. "
                      "Include 5K, 10K, half or marathon in the goal name."},
            status_code=400)

    plan = tp.build_plan(ctx["fitness"], ctx["race"], ctx["feasibility"],
                         ctx["settings"].get("athlete_profile"), ctx["today"],
                         mode=(req.mode or "auto"))
    tp.save_plan(plan)
    _rebuild_coach()
    return {"plan": plan, "today": _today_payload(plan),
            "units": ctx["settings"].get("units", "mi")}


@app.delete("/api/plan")
async def api_plan_delete():
    existed = tp.clear_plan()
    _rebuild_coach()
    return {"ok": True, "deleted": existed}


@app.get("/api/activities/{activity_id}")
async def api_activity_detail(activity_id: str, fetch: int = 1):
    """One activity with its splits, HR zones and any strength sets."""
    history = await _load_activity_history()
    activity = av.find(history.get("activities") or [], activity_id)
    if activity is None:
        raise HTTPException(404, detail="No such activity.")

    detail = activity_details.get(activity_id)
    fetched_now = False
    # A hand-entered activity exists nowhere but this machine, so asking Garmin
    # to enrich it would be a guaranteed round trip to a 404.
    if detail is None and fetch and not ma.is_manual(activity_id):
        try:
            detail = await _fetch_activity_detail(activity_id)
            fetched_now = True
        except HTTPException:
            raise
        except Exception as e:
            detail = {"fetch_error": str(e)}

    payload = av.detail_payload(activity, detail)
    payload["fetched_now"] = fetched_now
    payload["cached"] = activity_id in activity_details
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Charts — daily metric trends (steps, stress, RHR, training readiness)
# ---------------------------------------------------------------------------

def _build_chart_series() -> dict:
    """
    Return aligned daily series for charting, drawn from the in-memory
    health_data (up to the 90-day archive). Includes steps, stress, resting HR,
    training readiness, sleep score, sleep hours, HRV (overnight + baseline),
    body battery, and weight. Also returns 7-day rolling averages and the set of
    dates that had an activity (for annotations). Missing values are null.
    """
    hd = health_data or {}
    by_date: dict[str, dict] = {}

    def put(rows, tag):
        for r in rows or []:
            day = r.get("date")
            if day:
                by_date.setdefault(day, {})[tag] = r

    put(hd.get("daily_stats"), "stats")
    put(hd.get("training_readiness"), "ready")
    put(hd.get("sleep"), "sleep")
    put(hd.get("hrv"), "hrv")
    put(hd.get("body_composition"), "body")

    dates = sorted(by_date)
    series: dict[str, list] = {
        "steps": [], "stress": [], "resting_hr": [], "readiness": [],
        "readiness_estimated": [], "sleep_score": [], "sleep_hours": [],
        "hrv": [], "hrv_baseline": [], "body_battery": [], "weight": [],
    }
    for day in dates:
        s = by_date[day].get("stats") or {}
        r = by_date[day].get("ready") or {}
        sl = by_date[day].get("sleep") or {}
        hv = by_date[day].get("hrv") or {}
        bd = by_date[day].get("body") or {}
        total_sleep = sl.get("total_seconds")
        series["steps"].append(s.get("steps"))
        series["stress"].append(s.get("stress_avg"))
        series["resting_hr"].append(s.get("resting_hr"))
        series["readiness"].append(r.get("score"))
        series["readiness_estimated"].append(bool(r.get("estimated")))
        series["sleep_score"].append(sl.get("score"))
        series["sleep_hours"].append(round(total_sleep / 3600, 2) if total_sleep else None)
        series["hrv"].append(hv.get("last_night_avg"))
        series["hrv_baseline"].append(hv.get("weekly_avg"))
        series["body_battery"].append(s.get("body_battery"))
        series["weight"].append(bd.get("weight"))

    # Subjective check-in ratings, aligned onto the same date axis (issue #20).
    journal = ci.entries_between(dates[0], dates[-1]) if dates else {}
    for field in ci.JOURNAL_FIELDS:
        series[field] = [(journal.get(d) or {}).get(field) for d in dates]

    def rolling(vals, window=7):
        out = []
        for i in range(len(vals)):
            lo = max(0, i - window + 1)
            w = [v for v in vals[lo:i + 1] if isinstance(v, (int, float))]
            out.append(round(sum(w) / len(w), 1) if w else None)
        return out

    rolling_keys = ("steps", "stress", "resting_hr", "readiness",
                    "sleep_score", "sleep_hours", "hrv", "body_battery", "weight",
                    *ci.JOURNAL_FIELDS)
    roll = {k: rolling(series[k]) for k in rolling_keys}

    activity_days = sorted({
        (a.get("date") or "")[:10]
        for a in hd.get("activities") or [] if a.get("date")
    })

    return {"dates": dates, **series, "rolling": roll, "activity_days": activity_days}


@app.get("/charts", response_class=HTMLResponse)
async def charts_page(request: Request):
    if not garmin_connected:
        return RedirectResponse("/settings")
    return templates.TemplateResponse(request, "charts.html", {"request": request})


@app.get("/api/charts-data")
async def api_charts_data():
    return JSONResponse(_build_chart_series())


# ---------------------------------------------------------------------------
# Analytics — deeper visual analysis (#33 #34 #36 #38 #42 #46)
# ---------------------------------------------------------------------------

@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    if not garmin_connected:
        return RedirectResponse("/settings")
    return templates.TemplateResponse(request, "analytics.html", {"request": request})


@app.get("/api/units")
async def api_get_units():
    """Report the saved unit preference so a fresh browser adopts it."""
    return JSONResponse({"units": sm.load_settings().get("units", "mi")})


@app.post("/api/units")
async def api_set_units(request: Request):
    """
    Persist the distance-unit preference so the coach's health summary uses the
    same units as the UI. Rebuilds the coach so the change takes effect at once.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    units = "km" if body.get("units") == "km" else "mi"

    settings = sm.load_settings()
    if settings.get("units") != units:
        settings["units"] = units
        sm.save_settings(settings)
        _rebuild_coach()
    return JSONResponse({"units": units})


@app.get("/api/analytics")
async def api_analytics(view: str = "all", days: int = 90):
    """
    Serve one or all analytics views. Activity-derived views use the full
    training-log history; day-metric views use the 90-day health archive.
    """
    history = await _load_activity_history()
    activities = history.get("activities") or []
    hd = health_data or {}

    days = max(7, min(days, 365))
    builders = {
        "heatmap": lambda: an.build_heatmap(
            activities, hd.get("daily_stats"), hd.get("training_readiness")
        ),
        "sleep_bands": lambda: an.build_sleep_bands(hd.get("sleep")),
        "hr_zones": lambda: an.build_hr_zones(
            activities, activity_details, days=max(days, 120)
        ),
        "efficiency": lambda: an.build_efficiency(activities),
        "pace_curve": lambda: an.build_pace_curve(activities),
        "correlations": lambda: an.build_correlations(hd, activities, days=days),
        "pillars": lambda: pl.build_pillars(hd, activities),
    }

    wanted = builders if view in ("all", "") else {view: builders.get(view)}
    if None in wanted.values():
        return JSONResponse({"error": f"Unknown view '{view}'"}, status_code=400)

    out = {}
    for key, build in wanted.items():
        try:
            out[key] = build()
        except Exception as e:
            out[key] = {"error": str(e)}

    out["history_stale"] = history.get("stale", False)
    return JSONResponse(out)


@app.post("/api/analytics/backfill")
async def api_analytics_backfill(request: Request):
    """
    Fetch the extra data the analytics charts need but older caches lack:
    sleep bed/wake timestamps (#36) and per-activity HR zones (#34).
    """
    global activity_details

    if garmin_client is None:
        return JSONResponse({"error": "Not connected to Garmin."}, status_code=409)

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    target = body.get("target", "all")
    result = {}

    if target in ("all", "sleep"):
        rows = (health_data or {}).get("sleep") or []
        updated = await asyncio.to_thread(
            backfill_sleep_times, garmin_client, rows
        )
        if updated:
            await asyncio.to_thread(dc.save_cache, health_data)
        result["sleep_nights"] = updated

    if target in ("all", "hr_zones"):
        history = await _load_activity_history()
        cutoff = (date.today() - timedelta(days=120)).isoformat()
        recent = [
            a for a in history.get("activities") or []
            if (a.get("date") or "")[:10] >= cutoff and a.get("activity_id")
        ]
        details = ac.load_activity_details()
        missing = [
            str(a["activity_id"]) for a in recent
            if str(a["activity_id"]) not in details
        ][:150]

        fetched = 0
        for activity_id in missing:
            entry = {"fetched_at": datetime.now().isoformat(timespec="seconds")}
            try:
                entry["hr_zones"] = await asyncio.to_thread(
                    garmin_client.get_activity_hr_in_timezones, activity_id
                )
                fetched += 1
            except Exception as e:
                entry["hr_zones_error"] = str(e)
            details[activity_id] = entry
            ac.save_activity_details(details)   # fault-tolerant: save each
        activity_details = details
        result["activities_enriched"] = fetched

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Insights — proactive trend alerts (#5)
# ---------------------------------------------------------------------------

@app.get("/api/insights")
async def api_insights():
    return JSONResponse({"alerts": ins.compute_alerts(health_data)})


# ---------------------------------------------------------------------------
# Goals & progress tracking (#6)
# ---------------------------------------------------------------------------

@app.get("/plan", response_class=HTMLResponse)
async def plan_page(request: Request):
    if not garmin_connected:
        return RedirectResponse("/settings")
    return templates.TemplateResponse(request, "plan.html", {"request": request})


@app.get("/goals", response_class=HTMLResponse)
async def goals_page(request: Request):
    if not garmin_connected:
        return RedirectResponse("/settings")
    return templates.TemplateResponse(request, "goals.html", {
        "request": request,
        "goal_types": gl.GOAL_TYPES,
    })


@app.get("/api/goals")
async def api_goals_list():
    progress = gl.compute_progress(
        gl.load_goals(), health_data, (health_data or {}).get("activities", []))
    return JSONResponse({"goals": progress})


@app.post("/api/goals")
async def api_goals_add(request: Request):
    data = await request.json()
    goal = gl.add_goal(data)
    _rebuild_coach()   # keep coach context aligned to the new goal
    return JSONResponse({"ok": True, "goal": goal})


@app.delete("/api/goals/{goal_id}")
async def api_goals_delete(goal_id: str):
    gl.delete_goal(goal_id)
    _rebuild_coach()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Daily check-in: habits (#19), journal (#20) and fuelling (#21)
# ---------------------------------------------------------------------------

@app.get("/daily", response_class=HTMLResponse)
async def daily_page(request: Request):
    if not garmin_connected:
        return RedirectResponse("/settings")
    return templates.TemplateResponse(request, "daily.html", {
        "request": request,
        "journal_fields": ci.JOURNAL_FIELDS,
        "suggested_habits": ci.SUGGESTED_HABITS,
    })


@app.get("/api/checkin")
async def api_checkin_get(date: str = "", days: int = 30):
    """Today's entry plus habit streaks and the recent completion grid."""
    stats = ci.habit_stats(days=max(7, min(days, 365)))
    entry = ci.get_entry(date or None)
    return JSONResponse({"entry": entry, **stats})


@app.post("/api/checkin")
async def api_checkin_save(request: Request):
    """
    Merge a partial check-in. The coach is rebuilt so advice reflects what was
    just logged rather than the state at server start.
    """
    body = await request.json()
    entry = ci.save_entry(body.get("date") or "", body)
    _rebuild_coach()
    return JSONResponse({"ok": True, "entry": entry})


@app.get("/api/checkin/journal")
async def api_checkin_journal(days: int = 90, lag: int = 1):
    """Subjective series plus their correlation against biometrics."""
    days = max(14, min(days, 365))
    return JSONResponse({
        "series": ci.journal_series(days),
        "correlations": ci.journal_correlations(
            health_data, days, lag=max(0, min(lag, 7))),
    })


@app.post("/api/habits")
async def api_habits_add(request: Request):
    habit = ci.add_habit(await request.json())
    _rebuild_coach()
    return JSONResponse({"ok": True, "habit": habit})


@app.patch("/api/habits/{habit_id}")
async def api_habits_update(habit_id: str, request: Request):
    habit = ci.update_habit(habit_id, await request.json())
    if not habit:
        return JSONResponse({"ok": False, "error": "No such habit."}, status_code=404)
    _rebuild_coach()
    return JSONResponse({"ok": True, "habit": habit})


@app.delete("/api/habits/{habit_id}")
async def api_habits_delete(habit_id: str):
    ci.delete_habit(habit_id)
    _rebuild_coach()
    return JSONResponse({"ok": True})


@app.get("/api/fueling")
async def api_fueling(days: int = 14):
    """Pre, during and post fuelling targets plus under-fuelling flags."""
    return JSONResponse(
        fl.build(health_data, nutrition_data, sm.load_settings(),
                 days=max(7, min(days, 90)))
    )


# ---------------------------------------------------------------------------
# Weekly review synthesis (#7)
# ---------------------------------------------------------------------------

def _ephemeral_ask(prompt: str) -> str:
    """One-off AI call that does NOT touch the live chat history."""
    settings = sm.load_settings()
    fresh = _make_coach(health_summary or "", user_data_dir() / "review_scratch.json")
    fresh.reset_history()
    return fresh.chat(prompt)


@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request):
    if not garmin_connected:
        return RedirectResponse("/settings")
    return templates.TemplateResponse(request, "review.html", {"request": request})


@app.get("/api/weekly-review")
async def api_weekly_review_list():
    return JSONResponse({"reviews": wr.list_reviews()})


@app.post("/api/weekly-review")
async def api_weekly_review_generate():
    if not garmin_connected or not coach:
        return JSONResponse({"ok": False, "error": "Not connected."}, status_code=400)
    try:
        review = await asyncio.to_thread(
            wr.generate,
            health_data,
            (health_data or {}).get("activities", []),
            nutrition_data,
            _ephemeral_ask,
            None,
            sm.load_settings().get("units", "mi"),
        )
        return JSONResponse({"ok": True, "review": review})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def _nutrition_status() -> dict | None:
    """Return display metadata about the stored nutrition data, or None if empty."""
    data = np_.load_nutrition()
    if not data:
        return None
    dates = sorted(data.keys())
    return {"days": len(data), "from": dates[0], "to": dates[-1]}


def _profile_weight_ctx(settings: dict) -> dict:
    """
    Body weight for the settings form. Stored canonically in kg, shown in
    whichever unit the athlete last used, defaulting to their global
    distance-unit preference so the form matches the rest of the app.
    """
    profile = (settings or {}).get("athlete_profile") or {}
    unit = profile.get("weight_unit")
    if unit not in ("kg", "lb"):
        unit = "kg" if settings.get("units") == "km" else "lb"

    kg = profile.get("weight_kg")
    shown = None
    if isinstance(kg, (int, float)) and kg > 0:
        shown = round(kg / 0.45359237, 1) if unit == "lb" else round(kg, 1)
    return {"profile_weight": shown, "profile_weight_unit": unit}


def _get_local_ip() -> str:
    """Detect the machine's primary LAN IP (the IP other devices on the network can reach)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, error: str = "", success: str = ""):
    existing = cm.load_all_credentials()
    return templates.TemplateResponse(request, "settings.html", {
        "request": request,
        "garmin_email": existing.get("garmin_email") or "",
        "has_password": bool(existing.get("garmin_password")),
        "has_api_key": bool(existing.get("anthropic_api_key")),
        "garmin_connected": garmin_connected,
        "connection_error": connection_error,
        "error": error,
        "success": success,
        "data_settings": sm.load_settings(),
        "skills": (_skills := skm.load_skills()),
        "persona_bodies": json.dumps({
            s["trigger"]: s.get("body", "") for s in _skills if s.get("type") == "persona"
        }),
        "has_digest_sender":       bool(cm.load_credential("digest_gmail_sender")),
        "has_digest_app_password": bool(cm.load_credential("digest_gmail_app_password")),
        "has_gemini_key":          bool(cm.load_credential("gemini_api_key")),
        "nutrition_status":        _nutrition_status(),
        "athlete_profile":         sm.load_settings().get("athlete_profile") or {},
        **_profile_weight_ctx(sm.load_settings()),
        "coach_memory":            mm.load_memory(),
        "lan_ip":                  _get_local_ip(),
        "app_port":                APP_PORT,
        "lan_token":               sm.load_settings().get("lan_token") or "",
    })


@app.post("/api/save-network-settings")
async def api_save_network_settings(request: Request):
    form = await request.form()
    existing = sm.load_settings()
    existing["lan_access"] = "lan_access" in form
    sm.save_settings(existing)
    if existing["lan_access"]:
        get_or_create_lan_token()   # ensure a token exists before LAN is exposed
    return RedirectResponse("/settings?success=network_saved", status_code=303)


@app.post("/api/restart")
async def api_restart():
    """Restart the app process to apply settings that require it (e.g. LAN access)."""
    import os
    import subprocess

    def _do_restart():
        import time
        time.sleep(0.8)  # let the JSON response reach the browser first

        if getattr(sys, "frozen", False):
            # Packaged exe — re-exec the exe with no extra args
            os.execv(sys.executable, [sys.executable])
        else:
            # Running from source — explicitly relaunch launcher.py
            # (avoids relying on sys.argv which can be unreliable in venv/conda setups)
            project_root = str(Path(__file__).parent.parent)
            launcher = str(Path(__file__).parent.parent / "launcher.py")
            subprocess.Popen([sys.executable, launcher], cwd=project_root)
            os._exit(0)  # exit current process; new one will take over

    threading.Thread(target=_do_restart, daemon=True).start()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Readiness probe — used by launcher.py to know when the server is up."""
    return JSONResponse({"status": "ok"})


@app.get("/api/sidebar-html", response_class=HTMLResponse)
async def api_sidebar_html(request: Request):
    """Return the rendered sidebar partial for in-place DOM refresh (no page reload)."""
    return templates.TemplateResponse(request, "sidebar_content.html", {
        "request":        request,
        "health_data":    health_data,
        "nutrition_data": nutrition_data,
    })


@app.get("/api/status")
async def api_status():
    return JSONResponse({
        "garmin_connected": garmin_connected,
        "coach_ready": coach is not None,
        "connection_error": connection_error,
    })


class ChatRequest(BaseModel):
    message: str

class PersonaRequest(BaseModel):
    trigger: str


@app.post("/api/chat")
async def api_chat(body: ChatRequest):
    import re
    if not coach:
        raise HTTPException(503, detail="Coach not ready. Please check Settings.")

    # Detect "#N" workout references and inject cached detail into this turn only
    api_message     = body.message   # what gets sent to the AI (may be enriched)
    display_message = None           # what gets stored in history (clean version)

    match = re.search(r"#(\d+)", body.message)
    if match:
        idx = int(match.group(1)) - 1   # 1-indexed → 0-indexed
        activities = (health_data or {}).get("activities", [])
        if 0 <= idx < len(activities):
            act    = activities[idx]
            act_id = act.get("activity_id", "")
            if act_id and act_id in activity_details:
                detail_text = ac.format_activity_detail_for_prompt(
                    act, activity_details[act_id], sm.load_settings()
                )
                if detail_text:
                    act_label = act.get("name") or act.get("type") or "Activity"
                    act_date  = act.get("date", "")
                    api_message = (
                        f"[WORKOUT DETAIL for {act_label} on {act_date}:\n{detail_text}]\n\n"
                        f"{body.message}"
                    )
                    display_message = body.message

    async def generate():
        try:
            async for chunk in coach.chat_stream_async(api_message, display_message=display_message):
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/reset")
async def api_reset():
    if coach:
        coach.reset_history()
    return JSONResponse({"ok": True})


# Enriched prompts are normally swapped for a clean version before being
# stored, but a stream interrupted before that swap could persist the injected
# block. Strip it on the way out so raw prompt text never reaches the screen.
_RE_INJECTED = re.compile(r"^\[WORKOUT DETAIL for .*?\]\s*", re.DOTALL)


def _history_for_display(coach_obj) -> list[dict]:
    """
    Flatten a coach's stored history into plain {role, content} bubbles.

    The two providers store turns differently — Claude keeps
    {role: user|assistant, content: str}, Gemini keeps
    {role: user|model, parts: [{text: str}]} — so the normalising happens here
    rather than making the browser understand both shapes.
    """
    out: list[dict] = []
    for turn in getattr(coach_obj, "history", None) or []:
        if not isinstance(turn, dict):
            continue
        if turn.get("content") is not None:
            text = str(turn.get("content") or "")
        else:
            text = "".join(
                str(p.get("text") or "")
                for p in (turn.get("parts") or [])
                if isinstance(p, dict)
            )
        text = _RE_INJECTED.sub("", text).strip()
        if not text:
            continue
        role = "coach" if turn.get("role") in ("assistant", "model") else "user"
        out.append({"role": role, "content": text})
    return out


@app.get("/api/chat/history")
async def api_chat_history():
    """
    The conversation as the page should redraw it.

    The chat was always persisted server-side, but the page only ever rendered
    a fresh greeting, so opening any other tab and coming back was
    indistinguishable from having the conversation thrown away. Returning it
    here lets the browser restore what was already safely on disk.
    """
    if not coach:
        return JSONResponse({"messages": [], "persona": None})
    return JSONResponse({
        "messages": _history_for_display(coach),
        "persona": getattr(coach, "persona_name", None) if coach.active_persona else None,
    })


@app.get("/api/memory")
async def api_get_memory():
    """Return current coach memory notes and metadata."""
    memory = mm.load_memory()
    return JSONResponse({
        "notes":                    memory.get("notes") or "",
        "last_updated":             memory.get("last_updated"),
        "last_extracted_from_turn": memory.get("last_extracted_from_turn", 0),
    })


@app.post("/api/memory")
async def api_save_memory(request: Request):
    """Save manually edited coach memory notes and rebuild system prompt."""
    global coach_memory, health_summary, coach
    body  = await request.json()
    notes = (body.get("notes") or "").strip()

    memory = mm.load_memory()
    memory["notes"]        = notes
    memory["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    mm.save_memory(memory)
    coach_memory = memory

    # Rebuild system prompt with updated notes
    if coach and health_data is not None:
        settings      = sm.load_settings()
        trend_summary = format_trend_summary(health_data)
        memory_notes  = mm.format_memory_for_prompt(memory)
        new_summary   = format_health_summary(
            health_data, settings, nutrition_data, nutrition_log,
            memory_notes=memory_notes,
            trend_summary=trend_summary,
        )
        health_summary = new_summary
        coach._base_system_prompt = coach._build_system_prompt(new_summary)
        if not coach.active_persona:
            coach.system_prompt = coach._base_system_prompt

    return JSONResponse({"ok": True})


@app.post("/api/memory/extract-now")
async def api_extract_memory_now():
    """Manually trigger a memory extraction pass (runs in background)."""
    if not coach:
        raise HTTPException(503, detail="Coach not ready.")
    current_memory = mm.load_memory()
    if mm.should_extract(coach.history, current_memory):
        asyncio.create_task(_extract_memory_background(coach))
        return JSONResponse({"ok": True, "message": "Extraction started in background."})
    return JSONResponse({"ok": False, "message": "Not enough new conversation turns to extract yet."})


@app.get("/api/token-usage")
async def api_token_usage():
    """Return aggregated token usage stats for the frontend monitor."""
    return JSONResponse(tt.get_usage_summary())


@app.get("/api/skills")
async def api_skills():
    return JSONResponse(skm.load_skills())


@app.post("/api/upload-skill")
async def api_upload_skill(file: UploadFile = File(...)):
    """Save an uploaded .skill (persona) or .json (prompt) skill file to the right directory."""
    from pathlib import Path
    filename = Path(file.filename).name  # strip any path components

    if not filename:
        raise HTTPException(400, detail="No filename provided.")

    content = await file.read()

    if filename.endswith(".skill"):
        dest_dir = skm.CLAUDE_DIR
        dest_dir.mkdir(exist_ok=True)
        (dest_dir / filename).write_bytes(content)
        return JSONResponse({"ok": True, "type": "persona", "filename": filename})

    elif filename.endswith(".json"):
        try:
            data = json.loads(content)
        except ValueError:
            raise HTTPException(400, detail="Invalid JSON file.")
        if "trigger" not in data or "prompt" not in data:
            raise HTTPException(400, detail='Skill JSON must have "trigger" and "prompt" fields.')
        dest_dir = skm.SKILLS_DIR
        dest_dir.mkdir(exist_ok=True)
        (dest_dir / filename).write_bytes(content)
        return JSONResponse({"ok": True, "type": "prompt", "filename": filename})

    else:
        raise HTTPException(400, detail="Unsupported file type. Upload a .skill or .json file.")


@app.post("/api/upload-nutrition")
async def api_upload_nutrition(file: UploadFile = File(...)):
    """Parse a MacroFactor CSV, merge with existing nutrition data, rebuild coach."""
    global nutrition_data, nutrition_log, health_summary, coach
    content = await file.read()
    try:
        new_totals, new_log = np_.parse_csv(content)
    except Exception as exc:
        raise HTTPException(400, detail=f"Could not parse CSV: {exc}")

    # Merge and persist daily totals
    existing_totals = np_.load_nutrition()
    merged_totals   = np_.merge_nutrition(existing_totals, new_totals)
    np_.save_nutrition(merged_totals)
    nutrition_data = merged_totals

    # Merge and persist full food log
    existing_log = np_.load_nutrition_log()
    merged_log   = np_.merge_nutrition_log(existing_log, new_log)
    np_.save_nutrition_log(merged_log)
    nutrition_log = merged_log

    # Rebuild coach context with updated nutrition data
    if health_data:
        settings      = sm.load_settings()
        trend_summary = format_trend_summary(health_data)
        memory_notes  = mm.format_memory_for_prompt(mm.load_memory())
        health_summary = format_health_summary(
            health_data, settings, nutrition_data, nutrition_log,
            memory_notes=memory_notes,
            trend_summary=trend_summary,
        )
        _provider = settings.get("ai_provider", "claude")
        coach = _make_coach(health_summary, user_data_dir() / f"chat_history_{_provider}.json")

    return JSONResponse({"ok": True, "days_imported": len(new_totals), "total_days": len(merged_totals)})


@app.post("/api/save-profile")
async def api_save_profile(request: Request):
    """Save the athlete profile and rebuild the coach context."""
    global health_summary, coach
    form = await request.form()
    settings = sm.load_settings()
    existing = settings.get("athlete_profile") or {}

    # Weight is entered in whichever unit is convenient but stored in kg, so a
    # later unit switch never silently changes what the number means.
    weight_kg = existing.get("weight_kg")
    raw_weight = (form.get("weight") or "").strip()
    if not raw_weight:
        weight_kg = None
    else:
        try:
            val = float(raw_weight)
            if (form.get("weight_unit") or "lb") == "lb":
                val *= 0.45359237
            # Only an empty field clears a stored weight; unparseable or
            # implausible input leaves the previous value alone.
            if 20 < val < 300:
                weight_kg = round(val, 2)
        except ValueError:
            pass

    age = existing.get("age")
    raw_age = (form.get("age") or "").strip()
    if not raw_age:
        age = None
    else:
        try:
            n = int(raw_age)
            if 10 < n < 100:
                age = n
        except ValueError:
            pass

    settings["athlete_profile"] = {
        "name":            (form.get("name") or "").strip(),
        "sports":          (form.get("sports") or "").strip(),
        "level":           (form.get("level") or "").strip(),
        "goal":            (form.get("goal") or "").strip(),
        "training_days":   (form.get("training_days") or "").strip(),
        "training_plan":   (form.get("training_plan") or "").strip(),
        "upcoming_events": (form.get("upcoming_events") or "").strip(),
        "health_notes":    (form.get("health_notes") or "").strip(),
        "weight_kg":       weight_kg,
        "weight_unit":     "kg" if (form.get("weight_unit") == "kg") else "lb",
        "age":             age,
    }
    sm.save_settings(settings)
    _rebuild_coach()
    return RedirectResponse("/settings?success=profile_saved#profile", status_code=303)


@app.post("/api/nutrition-settings")
async def api_save_nutrition_settings(request: Request):
    """Save nutrition AI-context toggles and rebuild coach with updated settings."""
    global health_summary, coach
    form = await request.form()
    settings = sm.load_settings()
    settings["nutrition_enabled"]     = "nutrition_enabled" in form
    settings["nutrition_log_enabled"] = "nutrition_log_enabled" in form
    sm.save_settings(settings)
    if health_data:
        trend_summary = format_trend_summary(health_data)
        memory_notes  = mm.format_memory_for_prompt(mm.load_memory())
        health_summary = format_health_summary(
            health_data, settings, nutrition_data, nutrition_log,
            memory_notes=memory_notes,
            trend_summary=trend_summary,
        )
        _provider = settings.get("ai_provider", "claude")
        coach = _make_coach(health_summary, user_data_dir() / f"chat_history_{_provider}.json")
    return RedirectResponse("/settings?success=nutrition_settings_saved#nutrition", status_code=303)


@app.post("/api/create-persona")
async def api_create_persona(request: Request):
    """Create a new .skill file from trigger, description, and persona content."""
    import io, zipfile as zf
    form        = await request.form()
    trigger     = (form.get("trigger") or "").strip().lower().replace(" ", "-")
    description = (form.get("description") or "").strip()
    content     = (form.get("content") or "").strip()

    if not trigger:
        return JSONResponse({"ok": False, "error": "Trigger name is required."}, status_code=400)
    if not content:
        return JSONResponse({"ok": False, "error": "Persona instructions are required."}, status_code=400)

    skill_md = f"---\nname: {trigger}\ndescription: {description}\n---\n{content}"

    buf = io.BytesIO()
    with zf.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", skill_md.encode("utf-8"))
    buf.seek(0)

    skm.CLAUDE_DIR.mkdir(exist_ok=True)
    (skm.CLAUDE_DIR / f"{trigger}.skill").write_bytes(buf.read())

    return JSONResponse({"ok": True, "trigger": trigger})


@app.post("/api/persona")
async def api_set_persona(body: PersonaRequest):
    if not coach:
        raise HTTPException(503, detail="Coach not ready.")
    skill = skm.get_skill_by_trigger(body.trigger)
    if not skill or skill.get("type") != "persona":
        raise HTTPException(404, detail="Persona skill not found.")
    coach.set_persona(skill["content"], name=skill["trigger"])
    return JSONResponse({"ok": True, "trigger": skill["trigger"]})


@app.post("/api/persona/clear")
async def api_clear_persona():
    if coach:
        coach.clear_persona()
    return JSONResponse({"ok": True})


@app.post("/api/refresh")
async def api_refresh():
    """Re-fetch Garmin data and rebuild the coach with fresh context."""
    await _connect()
    return JSONResponse({
        "ok": garmin_connected,
        "error": connection_error,
    })


@app.post("/api/credentials")
async def api_save_credentials(
    garmin_email: str = Form(...),
    garmin_password: str = Form(""),
    garmin_password_confirm: str = Form(""),
    anthropic_api_key: str = Form(""),
):
    existing = cm.load_all_credentials()

    # Validate password confirmation if a new password was provided
    if garmin_password and garmin_password != garmin_password_confirm:
        return RedirectResponse("/settings?error=passwords_mismatch", status_code=303)

    new_creds = {
        "garmin_email": garmin_email,
        # Use new value if provided, otherwise keep existing
        "garmin_password": garmin_password or existing.get("garmin_password") or "",
        "anthropic_api_key": anthropic_api_key or existing.get("anthropic_api_key") or "",
    }

    if not all(new_creds.values()):
        return RedirectResponse("/settings?error=missing_fields", status_code=303)

    cm.save_all_credentials(new_creds)

    # Reconnect with the new credentials
    await _connect()

    if garmin_connected:
        return RedirectResponse("/?success=connected", status_code=303)
    else:
        return RedirectResponse(
            f"/settings?error={connection_error or 'connection_failed'}",
            status_code=303,
        )


@app.post("/api/data-settings")
async def api_save_data_settings(request: Request):
    """Save data sync preferences and re-fetch Garmin data with the new config."""
    form = await request.form()

    # Load-merge-save: preserves keys managed by other settings forms
    # (athlete_profile, digest_*, ai_provider, etc.) that aren't in this form.
    existing = sm.load_settings()
    existing.update({
        "days_back": int(form.get("days_back", 7)),
        "units": "km" if form.get("units") == "km" else "mi",
        "daily_stats_enabled": "daily_stats_enabled" in form,
        "sleep_enabled": "sleep_enabled" in form,
        "activities_enabled": "activities_enabled" in form,
        "activity_count": int(form.get("activity_count", 10)),
        "hrv_enabled": "hrv_enabled" in form,
        "training_readiness_enabled": "training_readiness_enabled" in form,
        "training_status_enabled": "training_status_enabled" in form,
        "body_enabled": "body_enabled" in form,
        "metric_steps": "metric_steps" in form,
        "metric_calories_total": "metric_calories_total" in form,
        "metric_calories_active": "metric_calories_active" in form,
        "metric_stress": "metric_stress" in form,
        "metric_body_battery": "metric_body_battery" in form,
        "metric_resting_hr": "metric_resting_hr" in form,
        "metric_distance": "metric_distance" in form,
        "metric_sleep_total": "metric_sleep_total" in form,
        "metric_sleep_deep": "metric_sleep_deep" in form,
        "metric_sleep_light": "metric_sleep_light" in form,
        "metric_sleep_rem": "metric_sleep_rem" in form,
        "metric_sleep_score": "metric_sleep_score" in form,
        "metric_body_weight": "metric_body_weight" in form,
        "metric_body_fat": "metric_body_fat" in form,
        "metric_body_muscle": "metric_body_muscle" in form,
        # Activity detail enrichment toggles
        "activity_detail_hr_zones":      "activity_detail_hr_zones"      in form,
        "activity_detail_splits":        "activity_detail_splits"        in form,
        "activity_detail_exercise_sets": "activity_detail_exercise_sets" in form,
        "activity_detail_power_zones":   "activity_detail_power_zones"   in form,
    })
    sm.save_settings(existing)

    # Re-fetch Garmin data with the new configuration if we're connected
    if garmin_connected:
        await _connect()

    return RedirectResponse("/settings?success=data_saved", status_code=303)


@app.post("/api/ai-settings")
async def api_save_ai_settings(request: Request):
    """Save AI provider / model selection and reconnect the coach."""
    form = await request.form()
    settings = sm.load_settings()
    settings["ai_provider"] = form.get("ai_provider", "claude")
    settings["ai_model"]    = form.get("ai_model",    "claude-sonnet-4-6")
    sm.save_settings(settings)
    if form.get("gemini_api_key"):
        cm.save_credential("gemini_api_key", form["gemini_api_key"])
    if garmin_connected:
        await _connect()
    return RedirectResponse("/settings?success=ai_saved", status_code=303)


@app.post("/api/digest-settings")
async def api_save_digest_settings(request: Request):
    """Save Daily Digest preferences and update the Windows Task Scheduler entry."""
    form    = await request.form()
    enabled = "digest_enabled" in form   # checkbox absent from POST = unchecked

    settings = sm.load_settings()
    settings["digest_enabled"]   = enabled
    settings["digest_recipient"] = form.get("digest_recipient", "").strip()
    settings["digest_send_time"] = form.get("digest_send_time", "07:00")
    sm.save_settings(settings)

    # Persist Gmail credentials only if new values were supplied
    if form.get("digest_gmail_sender"):
        cm.save_credential("digest_gmail_sender", form["digest_gmail_sender"])
    if form.get("digest_gmail_app_password"):
        cm.save_credential("digest_gmail_app_password", form["digest_gmail_app_password"])

    # Register or remove the Task Scheduler task
    try:
        if enabled:
            _register_digest_task(settings["digest_send_time"])
        else:
            _unregister_digest_task()
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or "schtasks command failed").strip()
        return RedirectResponse(f"/settings?error={err}", status_code=303)

    return RedirectResponse("/settings?success=digest_saved", status_code=303)


@app.post("/api/digest-test")
async def api_digest_test():
    """Send a test digest email immediately, ignoring the digest_enabled toggle."""
    from datetime import date, timedelta
    from digest import run_digest   # lazy import — digest.py lives at project root
    try:
        yesterday = date.today() - timedelta(days=1)
        await asyncio.to_thread(run_digest, yesterday)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=APP_PORT, log_level="warning")
