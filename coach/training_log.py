"""training_log.py — Persistence for the full run/activity history shown on the
Training Log page.

This is a separate cache from data_cache.py: the coach only needs a small window
of recent activities (activity_count), but the Training Log page wants the full
multi-year history. Fetching that is expensive, so we cache it to
data/training_log.json and only refetch on demand or when it goes stale.

The file is gitignored (data/*.json) — it contains activity history, never
credentials or tokens.
"""

import json
from datetime import datetime
from pathlib import Path

from .paths import user_data_dir

CACHE_FILE = user_data_dir() / "training_log.json"
SCHEMA_VERSION = 1

# How long the cached history is considered fresh before it is refetched.
# This is the maximum delay before an activity you just finished shows up, so
# it has to stay short: at six hours a morning run was still missing from the
# grid (and from the weekly streak) all afternoon. A full refetch is ~9 paged
# GETs and about two seconds, so hourly is cheap.
STALE_AFTER_HOURS = 1


def load_training_log() -> dict | None:
    """
    Load the cached activity history envelope.

    Returns {schema_version, fetched_at, activities}, or None if missing,
    unreadable, or from an incompatible schema version.
    """
    if not CACHE_FILE.exists():
        return None
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if raw.get("schema_version") != SCHEMA_VERSION:
            return None
        return raw
    except Exception:
        return None


def merge_activities(
    cached: list[dict] | None,
    fetched: list[dict] | None,
) -> list[dict]:
    """
    Merge a freshly fetched history into the cached one, keyed by activity_id.

    A refresh used to replace the cache outright, which quietly trusted every
    fetch to be complete. It isn't guaranteed to be: fetch_activity_history
    stops as soon as a page comes back shorter than page_size, so a transient
    short or empty page part-way through pagination looks exactly like the end
    of the history. Replacing on that would have overwritten years of records
    with a truncated list — and the weekly grid and streaks would have silently
    reported the truncation as fact.

    Merging removes that whole class of failure: the fetched copy of a row wins
    (so renames and type corrections made on Garmin still propagate), and any
    cached row the fetch didn't return is kept. The tradeoff is that an activity
    deleted on Garmin lingers locally, which is the far better bug to have.

    Returns the merged list, newest first.
    """
    merged: dict[str, dict] = {}
    for row in cached or []:
        key = str(row.get("activity_id") or "")
        if key:
            merged[key] = row
    for row in fetched or []:
        key = str(row.get("activity_id") or "")
        if key:
            merged[key] = row
    return sorted(
        merged.values(),
        key=lambda r: (str(r.get("date") or ""), str(r.get("activity_id") or "")),
        reverse=True,
    )


def save_training_log(activities: list[dict]) -> dict:
    """Persist the activity history and return the saved envelope."""
    cache = {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "activities": activities,
    }
    try:
        CACHE_FILE.write_text(
            json.dumps(cache, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass    # Non-fatal; the page still renders from the in-memory list
    return cache


def is_stale(cache: dict | None) -> bool:
    """Whether the cached history is missing or older than STALE_AFTER_HOURS."""
    if not cache or not cache.get("activities"):
        return True
    fetched_at = cache.get("fetched_at")
    if not fetched_at:
        return True
    try:
        age = datetime.now() - datetime.fromisoformat(fetched_at)
        return age.total_seconds() > STALE_AFTER_HOURS * 3600
    except Exception:
        return True
