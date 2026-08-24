"""manual_activities.py — Hand-entered sessions that no connected device holds.

Not every workout reaches Garmin Connect. Sessions recorded on a second watch,
a gym treadmill, or nothing at all simply never arrive, and until now the app
had no way to represent them. That is not a cosmetic gap: three missing weeks
of running reported a 72-week streak as 45, and the coach was reasoning about
training load from an incomplete picture.

These live in their own file rather than inside data/training_log.json on
purpose. The training log is a CACHE of Garmin and is rebuilt from Garmin
whenever it is missing or stale, so anything stored there is disposable by
design. Hand-typed data is the opposite of disposable: it exists nowhere else
and cannot be re-fetched. Keeping it separate means a cache wipe, a schema
bump, or a bad refresh can never destroy it.

The file is gitignored (data/*.json) — activity history, never credentials.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from .paths import user_data_dir

STORE = user_data_dir() / "manual_activities.json"
SCHEMA_VERSION = 1

SOURCE = "manual"
ID_PREFIX = "manual_"

# Mirrors the Garmin type keys so a manual run groups, charts, and counts
# toward streaks exactly like a recorded one.
KNOWN_TYPES = (
    "running", "treadmill_running", "trail_running", "track_running",
    "cycling", "indoor_cycling", "mountain_biking",
    "lap_swimming", "open_water_swimming",
    "walking", "hiking",
    "strength_training", "indoor_cardio", "elliptical", "rowing_v2", "yoga",
    "other",
)

# Guards against a typo turning into a corrupt row that then poisons pace
# curves and efficiency charts, the way three bad Garmin records once did.
MAX_DURATION_S = 86_400          # 24 hours
MAX_DISTANCE_M = 500_000         # 500 km
MAX_HR = 240
MIN_HR = 20


class ManualEntryError(ValueError):
    """Raised with a message already fit to show the athlete."""


def _num(value, field: str, *, low: float, high: float, required=False):
    """Parse an optional numeric field, rejecting values that cannot be real."""
    if value is None or value == "":
        if required:
            raise ManualEntryError(f"{field} is required.")
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ManualEntryError(f"{field} must be a number.")
    if out != out:
        raise ManualEntryError(f"{field} must be a number.")
    if not (low <= out <= high):
        raise ManualEntryError(f"{field} must be between {low:g} and {high:g}.")
    return out


def load_manual() -> list[dict]:
    """Every hand-entered activity, or an empty list."""
    if not STORE.exists():
        return []
    try:
        raw = json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if raw.get("schema_version") != SCHEMA_VERSION:
        return []
    rows = raw.get("activities")
    return rows if isinstance(rows, list) else []


def _save(rows: list[dict]) -> None:
    STORE.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "activities": rows}, indent=2, default=str),
        encoding="utf-8",
    )


def build_row(fields: dict) -> dict:
    """
    Validate raw form input and return a row in the app's activity shape.

    Raises ManualEntryError with a human-readable message on bad input; the
    caller can hand that straight to the athlete.
    """
    date_str = (fields.get("date") or "").strip()
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise ManualEntryError("Date must be in YYYY-MM-DD form.")
    if day > datetime.now().date():
        raise ManualEntryError("That date is in the future.")

    time_str = (fields.get("start_time") or "").strip() or "12:00"
    try:
        clock = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        raise ManualEntryError("Start time must be in HH:MM form.")

    activity_type = (fields.get("type") or "running").strip().lower()
    if activity_type not in KNOWN_TYPES:
        raise ManualEntryError(f"Unknown activity type '{activity_type}'.")

    # Duration is the one field with no sensible default: without it nothing
    # downstream can compute pace, load, or efficiency.
    hours = _num(fields.get("hours"), "Hours", low=0, high=24) or 0
    minutes = _num(fields.get("minutes"), "Minutes", low=0, high=59) or 0
    seconds = _num(fields.get("seconds"), "Seconds", low=0, high=59) or 0
    duration = int(hours * 3600 + minutes * 60 + seconds)
    if duration <= 0:
        raise ManualEntryError("Duration is required.")
    if duration > MAX_DURATION_S:
        raise ManualEntryError("Duration cannot exceed 24 hours.")

    distance = _num(fields.get("distance_meters"), "Distance",
                    low=0, high=MAX_DISTANCE_M)
    avg_hr = _num(fields.get("avg_hr"), "Average heart rate", low=MIN_HR, high=MAX_HR)
    max_hr = _num(fields.get("max_hr"), "Max heart rate", low=MIN_HR, high=MAX_HR)
    calories = _num(fields.get("calories"), "Calories", low=0, high=30_000)
    elevation = _num(fields.get("elevation_gain"), "Elevation gain", low=0, high=20_000)

    start = datetime.combine(day, clock)
    row = {
        "activity_id": f"{ID_PREFIX}{uuid.uuid4().hex[:12]}",
        "name": (fields.get("name") or "").strip() or "Manual entry",
        "type": activity_type,
        "date": day.isoformat(),
        "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": duration,
        # No auto-pause information exists for a hand-entered session, so
        # elapsed and moving time are necessarily the same number.
        "moving_duration": duration,
        "distance_meters": distance,
        "avg_hr": avg_hr,
        "max_hr": max_hr,
        "calories": calories,
        "elevation_gain": elevation,
        "avg_power": None,
        "avg_cadence": None,
        "avg_speed_mps": (distance / duration) if distance and duration else None,
        "source": SOURCE,
        "device_name": (fields.get("device_name") or "").strip() or None,
        "note": (fields.get("note") or "").strip() or None,
    }
    return row


def add(fields: dict) -> dict:
    """Validate, persist, and return one hand-entered activity."""
    row = build_row(fields)
    rows = load_manual()
    rows.append(row)
    _save(rows)
    return row


def delete(activity_id: str) -> bool:
    """Remove a hand-entered activity. Returns whether anything was removed."""
    rows = load_manual()
    kept = [r for r in rows if str(r.get("activity_id")) != str(activity_id)]
    if len(kept) == len(rows):
        return False
    _save(kept)
    return True


def is_manual(activity_id: str | None) -> bool:
    return str(activity_id or "").startswith(ID_PREFIX)


def merge_into(activities: list[dict] | None) -> list[dict]:
    """
    Fold hand-entered activities into a Garmin history, newest first.

    Applied at every read of the training log, so a manual session counts
    toward streaks, charts, analytics and the coach's context exactly like a
    recorded one. Manual rows are keyed with a prefix Garmin can never emit, so
    they cannot collide with a real activity id.
    """
    rows = list(activities or [])
    known = {str(r.get("activity_id") or "") for r in rows}
    for row in load_manual():
        if str(row.get("activity_id") or "") not in known:
            rows.append(row)
    return sorted(
        rows,
        key=lambda r: (str(r.get("date") or ""), str(r.get("start_time") or "")),
        reverse=True,
    )
