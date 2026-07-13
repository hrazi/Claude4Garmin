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

# How long the cached history is considered fresh before a background refresh is
# suggested. History rarely changes except for brand-new activities, so a few
# hours is plenty for a single-user app.
STALE_AFTER_HOURS = 6


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
