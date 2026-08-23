"""goals.py — Structured, trackable goals and progress computation.

Goals are stored in settings.json under the "goals" key (a list of dicts), so
they persist with the rest of the user's non-sensitive preferences. Each goal
has a type that maps to a metric we can measure from Garmin / MacroFactor data,
letting us show current-vs-target progress and feed it into the coach's context.

Goal shape:
  {
    "id": "<uuid>",
    "type": "weight" | "weekly_distance" | "race" | "steps" | "sleep" | "rhr",
    "label": "<freeform description>",
    "target": <number>,             # target value (units depend on type)
    "unit": "kg" | "km" | "min/km" | "steps" | "h" | "bpm" | "",
    "date": "YYYY-MM-DD" | "",       # optional target date (races, deadlines)
    "direction": "increase" | "decrease" | "hit",  # how progress is judged
    "created": "YYYY-MM-DD"
  }
"""

import uuid
from datetime import date, datetime, timedelta

from . import settings_manager as sm


GOAL_TYPES = {
    "weight":          {"unit": "kg",    "direction": "decrease"},
    "weekly_distance": {"unit": "km",    "direction": "increase"},
    "race":            {"unit": "",      "direction": "hit"},
    "steps":           {"unit": "steps", "direction": "increase"},
    "sleep":           {"unit": "h",     "direction": "increase"},
    "rhr":             {"unit": "bpm",   "direction": "decrease"},
}


# ---------------------------------------------------------------------------
# CRUD (stored in settings)
# ---------------------------------------------------------------------------

def load_goals() -> list[dict]:
    return sm.load_settings().get("goals") or []


def save_goals(goals: list[dict]) -> None:
    settings = sm.load_settings()
    settings["goals"] = goals
    sm.save_settings(settings)


def add_goal(data: dict) -> dict:
    gtype = (data.get("type") or "").strip()
    meta = GOAL_TYPES.get(gtype, {})
    unit = (data.get("unit") or meta.get("unit") or "").strip()
    target = _num(data.get("target"))
    # Distance goals are stored canonically in km and converted for display, so
    # switching the display unit never silently changes what a goal means.
    if gtype == "weekly_distance":
        if unit == "mi" and target is not None:
            target = round(target * 1609.344 / 1000, 2)
        unit = "km"
    goal = {
        "id": uuid.uuid4().hex[:12],
        "type": gtype,
        "label": (data.get("label") or "").strip(),
        "target": target,
        "unit": unit,
        "date": (data.get("date") or "").strip(),
        "direction": (data.get("direction") or meta.get("direction") or "hit").strip(),
        "created": date.today().isoformat(),
    }
    goals = load_goals()
    goals.append(goal)
    save_goals(goals)
    return goal


def delete_goal(goal_id: str) -> None:
    save_goals([g for g in load_goals() if g.get("id") != goal_id])


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Progress computation
# ---------------------------------------------------------------------------

def _latest(rows, key):
    vals = [(r.get("date"), r.get(key)) for r in rows or [] if r.get(key) is not None]
    vals = [(d, v) for d, v in vals if d]
    if not vals:
        return None
    return max(vals, key=lambda x: x[0])[1]


def _avg(rows, key, days):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    vals = [r.get(key) for r in rows or []
            if r.get("date") and r["date"] >= cutoff and isinstance(r.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else None


def _current_value(goal: dict, health_data: dict, activities: list[dict]) -> float | None:
    hd = health_data or {}
    t = goal.get("type")
    if t == "weight":
        return _latest(hd.get("body_composition"), "weight")
    if t == "rhr":
        return _avg(hd.get("daily_stats"), "resting_hr", 7)
    if t == "steps":
        return _avg(hd.get("daily_stats"), "steps", 7)
    if t == "sleep":
        avg_s = _avg(hd.get("sleep"), "total_seconds", 7)
        return round(avg_s / 3600, 1) if avg_s else None
    if t == "weekly_distance":
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        meters = sum(
            (a.get("distance_meters") or 0)
            for a in activities or []
            if (a.get("date") or "") >= cutoff
            and "run" in (a.get("type") or "").lower()
        )
        return round(meters / 1000, 1) if meters else 0.0
    return None  # race: no auto-measure


def compute_progress(goals: list[dict], health_data: dict, activities: list[dict]) -> list[dict]:
    """Return goals annotated with current value, percent, status, and days_left."""
    out = []
    for g in goals:
        cur = _current_value(g, health_data, activities)
        target = g.get("target")
        pct = None
        status = "tracking"
        if cur is not None and isinstance(target, (int, float)) and target:
            direction = g.get("direction", "hit")
            if direction == "increase":
                pct = max(0, min(100, round(cur / target * 100)))
            elif direction == "decrease":
                # progress toward a lower target, measured from the created baseline
                pct = 100 if cur <= target else max(0, min(100, round(target / cur * 100)))
            else:
                pct = 100 if cur >= target else round(cur / target * 100)
            if pct >= 100:
                status = "achieved"
        days_left = None
        if g.get("date"):
            try:
                days_left = (datetime.fromisoformat(g["date"]).date() - date.today()).days
            except ValueError:
                pass
        out.append({**g, "current": cur, "percent": pct, "status": status, "days_left": days_left})
    return out


def format_for_prompt(progress: list[dict], units: str = "km") -> str:
    """Render goal progress as a compact block for the coach's system context."""
    if not progress:
        return ""

    def show(g: dict, value: float) -> str:
        # Distance goals are stored in km; speak to the athlete in their unit.
        unit = g.get("unit", "")
        if g.get("type") == "weekly_distance" and units == "mi":
            return f"{round(value * 1000 / 1609.344, 1):g}mi"
        return f"{value:g}{unit}"

    lines = ["ACTIVE GOALS (keep advice aligned to these):"]
    for g in progress:
        bits = [g.get("label") or g.get("type", "goal")]
        if g.get("target") is not None:
            bits.append("target " + show(g, g["target"]))
        if g.get("current") is not None:
            bits.append("current " + show(g, g["current"]))
        if g.get("percent") is not None:
            bits.append(f"{g['percent']}%")
        if g.get("days_left") is not None:
            bits.append(f"{g['days_left']}d left")
        lines.append("  - " + " · ".join(bits))
    return "\n".join(lines)
