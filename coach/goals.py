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
    "baseline": <number>,            # decrease goals: value when first measured
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


# Every goal is stored in the canonical unit of its type and converted only for
# display, so changing the display preference never silently changes what a goal
# means. The unit a goal was entered in is matched case-insensitively and by
# alias: a target typed as "Mi" or "lbs" that fell through to the canonical
# branch would be compared against kilometres or kilograms and quietly report
# the wrong progress.
_UNIT_ALIASES = {
    "weekly_distance": {
        "km": 1.0, "kms": 1.0, "kilometer": 1.0, "kilometers": 1.0,
        "kilometre": 1.0, "kilometres": 1.0,
        "mi": 1.609344, "mile": 1.609344, "miles": 1.609344,
        "m": 0.001, "meter": 0.001, "meters": 0.001, "metre": 0.001, "metres": 0.001,
    },
    "weight": {
        "kg": 1.0, "kgs": 1.0, "kilo": 1.0, "kilos": 1.0,
        "kilogram": 1.0, "kilograms": 1.0,
        "lb": 0.45359237, "lbs": 0.45359237,
        "pound": 0.45359237, "pounds": 0.45359237,
    },
}


class UnknownUnit(ValueError):
    """Raised when a convertible goal names a unit we cannot interpret."""


def canonical_target(gtype: str, unit: str, target):
    """
    Convert `target` from `unit` into the canonical unit for `gtype`.

    Returns (target, canonical_unit). Unrecognised units on a convertible type
    raise rather than defaulting to canonical: storing an ambiguous number is
    how a 10-mile goal became a 10-km goal.
    """
    meta = GOAL_TYPES.get(gtype, {})
    canon = meta.get("unit", "")
    table = _UNIT_ALIASES.get(gtype)
    if table is None:
        return target, (unit or canon)
    key = (unit or canon).strip().lower().rstrip(".")
    if key not in table:
        raise UnknownUnit(
            f"Unrecognised unit {unit!r} for a {gtype.replace('_', ' ')} goal. "
            f"Use one of: {', '.join(sorted({k for k in table}))}."
        )
    factor = table[key]
    if target is not None and factor != 1.0:
        target = round(target * factor, 2)
    return target, canon


# ---------------------------------------------------------------------------
# CRUD (stored in settings)
# ---------------------------------------------------------------------------

def load_goals() -> list[dict]:
    goals = sm.load_settings().get("goals") or []
    fixed = migrate_goals(goals)
    if fixed is not goals:
        save_goals(fixed)
    return fixed


def migrate_goals(goals: list[dict]) -> list[dict]:
    """
    Repair goals stored before targets were canonicalised.

    Idempotent: a converted goal carries its canonical unit, so a second pass
    is a no-op. Only rewrites rows whose unit is convertible and non-canonical,
    which is exactly the set that was being compared against the wrong scale.
    """
    changed = False
    out = []
    for g in goals:
        gtype = g.get("type")
        table = _UNIT_ALIASES.get(gtype)
        canon = GOAL_TYPES.get(gtype, {}).get("unit", "")
        unit = (g.get("unit") or "").strip()
        if table is None or not unit or unit.lower().rstrip(".") == canon:
            out.append(g)
            continue
        try:
            target, new_unit = canonical_target(gtype, unit, _num(g.get("target")))
        except UnknownUnit:
            out.append(g)
            continue
        out.append({**g, "target": target, "unit": new_unit})
        changed = True
    return out if changed else goals


def save_goals(goals: list[dict]) -> None:
    settings = sm.load_settings()
    settings["goals"] = goals
    sm.save_settings(settings)


def add_goal(data: dict) -> dict:
    gtype = (data.get("type") or "").strip()
    meta = GOAL_TYPES.get(gtype, {})
    unit = (data.get("unit") or meta.get("unit") or "").strip()
    target = _num(data.get("target"))
    target, unit = canonical_target(gtype, unit, target)
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
        # A smart scale fills body_composition, but most athletes have none. The
        # weight typed into the athlete profile is a real measurement and is the
        # only one many users will ever have, so fall back to it rather than
        # rendering a goal that can never show progress.
        measured = _latest(hd.get("body_composition"), "weight")
        if measured is not None:
            return measured
        profile = sm.load_settings().get("athlete_profile") or {}
        return _num(profile.get("weight_kg"))
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
    baselines = {}
    for g in goals:
        cur = _current_value(g, health_data, activities)
        target = g.get("target")
        # A "decrease" goal is meaningless without a starting point. Capture it
        # the first time the metric is readable and persist it, so the number
        # can never be recomputed from a moving reference.
        if (g.get("direction") == "decrease" and g.get("baseline") is None
                and cur is not None):
            g = {**g, "baseline": cur}
            baselines[g.get("id")] = cur
        pct = None
        status = "tracking"
        if cur is not None and isinstance(target, (int, float)) and target:
            direction = g.get("direction", "hit")
            if direction == "increase":
                pct = max(0, min(100, round(cur / target * 100)))
            elif direction == "decrease":
                # A ratio of target to current is not progress: someone starting
                # at 100kg with an 82kg target would read 82% before losing a
                # gram. Measure from where they actually started.
                base = _num(g.get("baseline"))
                if base is None:
                    base = cur
                if cur <= target:
                    pct = 100
                elif base <= target:
                    pct = 0          # started at goal and drifted above it
                else:
                    pct = max(0, min(100, round((base - cur) / (base - target) * 100)))
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
    if baselines:
        stored = load_goals()
        for row in stored:
            if row.get("id") in baselines:
                row["baseline"] = baselines[row["id"]]
        save_goals(stored)
    return out


def format_for_prompt(progress: list[dict], units: str = "km") -> str:
    """Render goal progress as a compact block for the coach's system context."""
    if not progress:
        return ""

    imperial = units == "mi"

    def show(g: dict, value: float) -> str:
        # Goals are stored canonically; speak to the athlete in their own units.
        gtype = g.get("type")
        if gtype == "weekly_distance" and imperial:
            return f"{round(value / 1.609344, 1):g}mi"
        if gtype == "weight" and imperial:
            return f"{round(value / 0.45359237, 1):g}lb"
        return f"{value:g}{g.get('unit', '')}"

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
