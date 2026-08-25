"""
Training plan — adaptive periodized plan generator (GitHub issue #23).

Pure functions over already-cached data (no network calls), plus a small
JSON store for the generated plan.

Design notes that are load-bearing:

1. ONE volume simulator. `_progress_weeks()` is used by BOTH the feasibility
   verdict and the plan builder. If they used separate maths the app could
   tell you a race was reachable and then hand you a plan that never reaches
   it (or the reverse). They must move together.

2. The FEASIBILITY VERDICT IS THE PRODUCT. A plan that ramps someone from
   4 mi/week to a marathon in ten weeks is not a plan, it is an injury with a
   calendar. When the numbers do not support the race we say so plainly and
   offer concrete alternatives (a shorter distance that IS reachable, a
   finish-focused run/walk, or a later date).

3. Daily adaptation reads RAW SIGNALS — sleep, HRV against its own rolling
   baseline, resting HR against a trailing median, stress. It deliberately
   does NOT read `training_readiness`, because every stored readiness score is
   `estimated: true` (derived by fill_readiness_estimates from those same
   inputs). Using it would double-count sleep and dress a derivation up as a
   measurement.

4. `body_battery` is never used. The cached field is
   `bodyBatteryMostRecentValue` — for any past day that is the LAST reading,
   an end-of-evening drained number (observed mean ~14). It scores bedtime
   exhaustion, not recovery.

All distances are stored canonically in KILOMETRES. Formatting into the
athlete's preferred unit happens at the edges.
"""

from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
PLAN_FILE = os.path.join(DATA_DIR, "training_plan.json")

KM_PER_MI = 1.609344

RUN_TYPES = ("running", "treadmill_running", "trail_running", "track_running",
             "indoor_running", "virtual_run")

# Same corrupt-record guards analytics.py uses: the history holds a handful of
# records with bad GPS or a mis-set distance (one "run" at 15.6 m/s, faster
# than the world record). They must not inflate the volume the plan builds on.
MIN_RUN_SPEED_MPS = 1.5
MAX_RUN_SPEED_MPS = 6.5

# --- progression safety limits ------------------------------------------------
SAFE_WEEKLY_RAMP = 1.10      # the "10% rule"
RECOVERY_EVERY = 4           # every 4th week backs off
RECOVERY_FACTOR = 0.70
LONG_RUN_STEP_KM = 1.6        # ~1 mile added per build week, at most
MIN_START_WEEKLY_KM = 8.0     # floor so a near-zero base still produces a plan

# The 10% rule is a percentage, and percentages of a small number are a rounding
# error: 10% of a 5 mi week is half a mile. Below LOW_VOLUME_KM we also allow a
# modest ABSOLUTE step, because at these volumes the limiting factor is the long
# run and total load, not the ratio.
MIN_WEEKLY_STEP_KM = 2.4      # ~1.5 mi
LOW_VOLUME_KM = 32.0          # ~20 mi/week
MIN_LONG_RUN_KM = 3.0
# A prescribed session below this is noise. Days that can't be filled become
# rest rather than a fake 0.3 mi jog.
MIN_SESSION_KM = 1.6

# Long run as a share of the week. The textbook 30-35% assumes a structured,
# higher-volume athlete. A 3-day-a-week runner legitimately carries a much
# larger share in the long run, and applying the high-volume number to them
# produces the absurd result of a plan PRESCRIBING LESS than they already run.
LONG_RUN_FRACTION = ((25.0, 0.50), (40.0, 0.45), (60.0, 0.40))
LONG_RUN_FRACTION_MIN = 0.35
# Above this share the week is long-run-heavy: survivable, but fragile. Worth
# saying out loud rather than silently planning it.
LONG_RUN_SHARE_WARN = 0.45


def _long_fraction(weekly_km: float) -> float:
    for ceiling, frac in LONG_RUN_FRACTION:
        if (weekly_km or 0) < ceiling:
            return frac
    return LONG_RUN_FRACTION_MIN


RACES = {
    "5k": {
        "label": "5K", "distance_km": 5.0,
        "peak_weekly_km": 25.0, "min_peak_weekly_km": 11.0,
        "long_run_km": 10.0, "min_long_run_km": 5.0,
        "min_weeks": 6, "taper_weeks": 1,
    },
    "10k": {
        "label": "10K", "distance_km": 10.0,
        "peak_weekly_km": 32.0, "min_peak_weekly_km": 16.0,
        "long_run_km": 13.0, "min_long_run_km": 8.0,
        "min_weeks": 8, "taper_weeks": 1,
    },
    "half": {
        "label": "Half marathon", "distance_km": 21.0975,
        "peak_weekly_km": 45.0, "min_peak_weekly_km": 28.0,
        "long_run_km": 19.0, "min_long_run_km": 14.0,
        "min_weeks": 12, "taper_weeks": 2,
    },
    "marathon": {
        "label": "Marathon", "distance_km": 42.195,
        "peak_weekly_km": 60.0, "min_peak_weekly_km": 45.0,
        "long_run_km": 32.0, "min_long_run_km": 26.0,
        "min_weeks": 16, "taper_weeks": 3,
    },
}

# Longest first: "half marathon" must not match the "marathon" rule.
_RACE_PATTERNS = (
    ("half", ("half marathon", "half-marathon", "halfmarathon", "13.1", "21k", "half")),
    ("marathon", ("marathon", "26.2", "42k")),
    ("10k", ("10k", "10 k", "ten k", "6.2")),
    ("5k", ("5k", "5 k", "five k", "parkrun", "3.1")),
)


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return float(value)


def _day(value) -> str:
    return (value or "")[:10]


def _is_run(activity: dict) -> bool:
    return (activity.get("type") or "").lower() in RUN_TYPES


def _run_km(activity: dict):
    """Distance of a run in km, or None when the record is implausible."""
    dist = _num(activity.get("distance_meters"))
    dur = _num(activity.get("moving_duration")) or _num(activity.get("duration_seconds"))
    if not dist or dist <= 0:
        return None
    if dur and dur > 0:
        speed = dist / dur
        if not (MIN_RUN_SPEED_MPS <= speed <= MAX_RUN_SPEED_MPS):
            return None
    return dist / 1000.0


def _parse_date(value):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------------------
# Race goal
# ---------------------------------------------------------------------------

def infer_race(goals: list[dict] | None, profile: dict | None = None) -> dict | None:
    """
    Find the athlete's target race from their goals.

    Returns None when there is no dated race goal — the caller should then ask
    for one rather than inventing a race.
    """
    best = None
    for g in goals or []:
        if (g.get("type") or "") != "race":
            continue
        when = _parse_date(g.get("date"))
        if not when:
            continue
        if best is None or when < best[0]:
            best = (when, g)
    if not best:
        return None

    when, goal = best
    label = (goal.get("label") or "")
    key = _match_distance(label)
    if not key and profile:
        key = _match_distance(profile.get("goal") or "")
    if not key:
        key = _match_distance(profile.get("upcoming_events") or "") if profile else None

    spec = dict(RACES[key]) if key else None
    return {
        "goal_id": goal.get("id"),
        "name": label or "Race",
        "date": when.isoformat(),
        "distance_key": key,
        "spec": spec,
        "target_time": _match_time(label) or (_match_time(profile.get("goal") or "") if profile else None),
    }


def _match_distance(text: str):
    low = (text or "").lower()
    for key, needles in _RACE_PATTERNS:
        if any(n in low for n in needles):
            return key
    return None


def _match_time(text: str):
    """Pull an H:MM or H:MM:SS goal time out of free text like '4:30 Marathon'."""
    import re
    m = re.search(r"\b(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\b", text or "")
    if not m:
        return None
    h, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 12:
        return None
    return {"text": m.group(0), "seconds": h * 3600 + mm * 60 + ss}


# ---------------------------------------------------------------------------
# Current fitness
# ---------------------------------------------------------------------------

def assess_fitness(activities: list[dict] | None, today: date | None = None) -> dict:
    """Measure the base the plan has to start from. All distances in km."""
    today = today or date.today()
    runs = []
    for a in activities or []:
        if not isinstance(a, dict) or not _is_run(a):
            continue
        d = _parse_date(_day(a.get("date")))
        km = _run_km(a)
        if d and km:
            runs.append((d, km, a))

    def window_km(days: int) -> float:
        cutoff = today - timedelta(days=days)
        return sum(km for d, km, _ in runs if cutoff < d <= today)

    def longest(days: int) -> float:
        cutoff = today - timedelta(days=days)
        vals = [km for d, km, _ in runs if cutoff < d <= today]
        return max(vals) if vals else 0.0

    km_4 = window_km(28)
    km_8 = window_km(56)
    km_12 = window_km(84)

    # Weekly buckets over the last 8 weeks, for consistency.
    start = _monday(today) - timedelta(weeks=7)
    buckets = {}
    for d, km, _ in runs:
        if d >= start:
            buckets.setdefault(_monday(d).isoformat(), 0.0)
            buckets[_monday(d).isoformat()] += km
    weeks_with_runs = sum(1 for v in buckets.values() if v > 0)

    return {
        "has_history": bool(runs),
        "weekly_km_4wk": round(km_4 / 4, 2),
        "weekly_km_8wk": round(km_8 / 8, 2),
        "weekly_km_12wk": round(km_12 / 12, 2),
        "longest_run_km_8wk": round(longest(56), 2),
        "longest_run_km_1yr": round(longest(365), 2),
        "runs_last_8wk": sum(1 for d, _, _ in runs if today - timedelta(days=56) < d <= today),
        "active_weeks_of_8": weeks_with_runs,
        "consistency": round(weeks_with_runs / 8, 2),
        "last_run_date": max((d for d, _, _ in runs), default=None).isoformat() if runs else None,
    }


def starting_volume(fitness: dict) -> float:
    """
    The weekly volume a plan may safely open at.

    Uses the more conservative of the 4- and 8-week averages: a single big
    week inside an otherwise thin block is not a base, and opening a plan on
    top of it is exactly how people get hurt in week one.
    """
    a = fitness.get("weekly_km_4wk") or 0.0
    b = fitness.get("weekly_km_8wk") or 0.0
    base = min(a, b) if (a and b) else max(a, b)
    return max(base, MIN_START_WEEKLY_KM) if base else MIN_START_WEEKLY_KM


# ---------------------------------------------------------------------------
# Progression simulator — shared by the verdict and the builder
# ---------------------------------------------------------------------------

def _progress_weeks(start_weekly: float, start_long: float, weeks: int,
                    weekly_cap: float, long_cap: float) -> list[dict]:
    """
    Simulate `weeks` of safe progression.

    Volume rises by the greater of SAFE_WEEKLY_RAMP or MIN_WEEKLY_STEP_KM (the
    absolute step only while under LOW_VOLUME_KM). Every RECOVERY_EVERY-th week
    backs off to RECOVERY_FACTOR and does NOT advance the ramp.

    The long run rises by at most LONG_RUN_STEP_KM per build week and is capped
    by the volume-dependent share of the week — but NEVER falls below what the
    athlete has already demonstrated. Prescribing someone less than they
    already run is not caution, it is a bug.
    """
    out = []
    build_weekly = max(start_weekly, 0.0)
    # `start_long` is the athlete's DEMONSTRATED capability. It is a target the
    # progression may climb back to, not a floor under every week: a single big
    # run inside an otherwise thin block is a one-off, not a weekly habit, and
    # anchoring week 1 to it leaves no volume for anything else.
    peak_long = min(max(start_long, 0.0), long_cap)
    long_km = max(min(peak_long, build_weekly * _long_fraction(build_weekly)), MIN_LONG_RUN_KM)
    for i in range(1, max(int(weeks), 0) + 1):
        recovery = (i % RECOVERY_EVERY == 0)
        if i > 1 and not recovery:
            step = build_weekly * (SAFE_WEEKLY_RAMP - 1.0)
            if build_weekly < LOW_VOLUME_KM:
                step = max(step, MIN_WEEKLY_STEP_KM)
            build_weekly = min(build_weekly + step, weekly_cap)
            long_km += LONG_RUN_STEP_KM
        week_km = build_weekly * RECOVERY_FACTOR if recovery else build_weekly
        ceiling = min(max(week_km * _long_fraction(week_km), peak_long), long_cap)
        long_km = min(long_km, ceiling)
        week_long = long_km * RECOVERY_FACTOR if recovery else long_km
        out.append({
            "index": i,
            "weekly_km": round(week_km, 2),
            "long_run_km": round(week_long, 2),
            "recovery": recovery,
            "long_share": round(week_long / week_km, 3) if week_km else None,
        })
    return out


def weeks_to_reach(start_weekly: float, start_long: float,
                   need_weekly: float, need_long: float, limit: int = 104):
    """How many safe weeks until BOTH targets are met? None if not within limit."""
    sim = _progress_weeks(start_weekly, start_long, limit,
                          weekly_cap=max(need_weekly, start_weekly) * 1.5,
                          long_cap=max(need_long, start_long) * 1.5)
    for w in sim:
        if w["recovery"]:
            continue
        if w["weekly_km"] >= need_weekly and w["long_run_km"] >= need_long:
            return w["index"]
    return None


# ---------------------------------------------------------------------------
# Feasibility
# ---------------------------------------------------------------------------

def assess_feasibility(fitness: dict, race: dict | None, today: date | None = None) -> dict:
    """
    Can this race be reached safely from this base in the time available?

    Returns a verdict plus the numbers behind it and concrete alternatives.
    """
    today = today or date.today()
    if not race:
        return {"status": "no_race", "headline": "No race goal set.",
                "detail": "Add a race goal with a date and I can build a plan backwards from it.",
                "reasons": [], "alternatives": []}

    spec = race.get("spec")
    race_date = _parse_date(race.get("date"))
    if not race_date:
        return {"status": "no_race", "headline": "Race goal has no date.",
                "detail": "A plan is built backwards from race day, so the date is required.",
                "reasons": [], "alternatives": []}

    days_out = (race_date - today).days
    weeks_out = days_out / 7.0

    if not spec:
        return {"status": "unknown_distance",
                "headline": f"Couldn't tell what distance \u201c{race['name']}\u201d is.",
                "detail": "Rename the goal to include the distance (5K, 10K, half, marathon) "
                          "and I'll build a plan for it.",
                "reasons": [], "alternatives": [], "days_out": days_out}

    if not fitness.get("has_history"):
        return {"status": "insufficient_data",
                "headline": "No run history to plan from.",
                "detail": "Sync activities or add a few runs by hand, then regenerate.",
                "reasons": [], "alternatives": [], "days_out": days_out}

    if days_out < 0:
        return {"status": "past", "headline": f"{race['name']} was {abs(days_out)} days ago.",
                "detail": "Set a new race goal to build the next block.",
                "reasons": [], "alternatives": [], "days_out": days_out}

    start_weekly = starting_volume(fitness)
    start_long = max(fitness.get("longest_run_km_8wk") or 0.0, start_weekly * 0.35, 3.0)
    taper = int(spec["taper_weeks"])
    build_weeks = max(int(weeks_out) - taper, 0)

    sim = _progress_weeks(start_weekly, start_long, build_weeks,
                          weekly_cap=spec["peak_weekly_km"],
                          long_cap=spec["long_run_km"])
    proj_weekly = max((w["weekly_km"] for w in sim), default=start_weekly)
    proj_long = max((w["long_run_km"] for w in sim), default=start_long)

    reasons = []
    reasons.append(f"You're averaging {_mi(fitness.get('weekly_km_8wk'))} mi/week over 8 weeks "
                   f"({fitness.get('active_weeks_of_8')} of 8 weeks with a run).")
    reasons.append(f"Longest run in the last year: {_mi(fitness.get('longest_run_km_1yr'))} mi.")
    reasons.append(f"{int(weeks_out)} weeks to race day \u2014 {taper} of them taper, "
                   f"so {build_weeks} weeks of building.")
    reasons.append(f"At a safe 10%/week ramp that peaks around {_mi(proj_weekly)} mi/week "
                   f"with a longest run of ~{_mi(proj_long)} mi.")
    reasons.append(f"A {_phrase(spec['label'])} wants roughly {_mi(spec['peak_weekly_km'])} mi/week "
                   f"peak and a {_mi(spec['long_run_km'])} mi long run "
                   f"({_mi(spec['min_peak_weekly_km'])} / {_mi(spec['min_long_run_km'])} mi bare minimum).")

    meets_rec = proj_weekly >= spec["peak_weekly_km"] * 0.95 and proj_long >= spec["long_run_km"] * 0.95
    meets_min = proj_weekly >= spec["min_peak_weekly_km"] and proj_long >= spec["min_long_run_km"]

    if meets_rec:
        status = "ready"
        headline = f"{race['name']} is reachable from where you are."
        detail = "The safe ramp gets you to full preparation with time to spare."
    elif meets_min:
        status = "tight"
        headline = f"{race['name']} is tight but doable if nothing goes wrong."
        detail = ("You'll arrive under-prepared relative to the textbook build. "
                  "Treat the goal time as optional and the finish as the win.")
    else:
        status = "not_safe"
        headline = f"{race['name']} is not safely reachable in {int(weeks_out)} weeks."
        detail = (f"Getting from {_mi(fitness.get('weekly_km_8wk'))} to "
                  f"{_mi(spec['min_peak_weekly_km'])} mi/week in {build_weeks} weeks is roughly a "
                  f"{_ratio(spec['min_peak_weekly_km'], max(fitness.get('weekly_km_8wk') or 0.1, 0.1))}x jump. "
                  "That is how people arrive at a start line injured, or don't arrive at all.")

    return {
        "status": status,
        "headline": headline,
        "detail": detail,
        "reasons": reasons,
        "race": {"name": race["name"], "date": race["date"], "label": spec["label"]},
        "days_out": days_out,
        "weeks_out": round(weeks_out, 1),
        "build_weeks": build_weeks,
        "taper_weeks": taper,
        "start_weekly_km": round(start_weekly, 2),
        "projected_peak_weekly_km": round(proj_weekly, 2),
        "projected_long_run_km": round(proj_long, 2),
        "required_peak_weekly_km": spec["peak_weekly_km"],
        "required_long_run_km": spec["long_run_km"],
        "minimum_peak_weekly_km": spec["min_peak_weekly_km"],
        "minimum_long_run_km": spec["min_long_run_km"],
        "alternatives": _alternatives(fitness, spec, race, proj_weekly, proj_long,
                                      start_weekly, start_long, today, race_date),
    }


def _alternatives(fitness, spec, race, proj_weekly, proj_long,
                  start_weekly, start_long, today, race_date) -> list[dict]:
    """Concrete options when the named race doesn't fit the available runway."""
    out = []
    if proj_weekly >= spec["min_peak_weekly_km"] and proj_long >= spec["min_long_run_km"]:
        return out

    # 1. The longest distance the projection actually supports.
    order = ["marathon", "half", "10k", "5k"]
    for key in order:
        alt = RACES[key]
        if alt["distance_km"] >= spec["distance_km"]:
            continue
        if proj_weekly >= alt["min_peak_weekly_km"] and proj_long >= alt["min_long_run_km"]:
            out.append({
                "kind": "shorter",
                "distance_key": key,
                "title": f"Train for a {_phrase(alt['label'])} instead",
                "body": (f"Your projected {_mi(proj_weekly)} mi/week and {_mi(proj_long)} mi long run "
                         f"clear what a {_phrase(alt['label'])} asks for. Find a {_phrase(alt['label'])} "
                         f"around that date, or use {race['name']} day for the effort \u2014 "
                         f"a distance you'll actually be trained for."),
            })
            break

    # 2. Run/walk the original distance, finish-focused.
    if proj_long >= spec["distance_km"] * 0.45:
        out.append({
            "kind": "finish",
            "distance_key": race.get("distance_key"),
            "title": f"Run/walk the {_phrase(spec['label'])}, finish-focused",
            "body": ("Drop any time goal, plan walk breaks from the first mile, and accept a long day. "
                     "Still demanding and still a real risk, but survivable if you're honest about pace."),
        })

    # 3. Give it the runway it needs.
    need = weeks_to_reach(start_weekly, start_long,
                          spec["peak_weekly_km"], spec["long_run_km"])
    if need:
        when = _monday(today) + timedelta(weeks=need + spec["taper_weeks"])
        out.append({
            "kind": "defer",
            "title": f"Give the {_phrase(spec['label'])} the runway it needs",
            "body": (f"From this base, a full build takes about {need + spec['taper_weeks']} weeks \u2014 "
                     f"a race around {when.strftime('%B %Y')}. "
                     f"Keep {race['name']} as a supported long run or a training day."),
        })
    return out


def _phrase(label: str) -> str:
    """Lowercase a race name in prose, but never mangle an acronym: 'Marathon'
    reads better as 'marathon', while '10K' must not become '10k'."""
    label = label or ""
    return label if label[:1].isdigit() else label.lower()


def _mi(km) -> str:
    v = _num(km) or 0.0
    return f"{v / KM_PER_MI:.1f}"


def _ratio(a, b) -> str:
    try:
        return f"{(a / b):.1f}"
    except ZeroDivisionError:
        return "?"


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------

SESSION_TYPES = {
    "rest":     {"label": "Rest", "intensity": 0},
    "easy":     {"label": "Easy run", "intensity": 1},
    "long":     {"label": "Long run", "intensity": 2},
    "steady":   {"label": "Steady run", "intensity": 2},
    "tempo":    {"label": "Tempo", "intensity": 3},
    "intervals": {"label": "Intervals", "intensity": 4},
    "strides":  {"label": "Easy + strides", "intensity": 2},
    "race":     {"label": "RACE", "intensity": 5},
    "cross":    {"label": "Cross-train", "intensity": 1},
}

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Which weekdays get used, by weekly frequency. Long run lands last (weekend).
_DAY_PATTERNS = {
    2: [1, 6],
    3: [1, 3, 6],
    4: [1, 3, 5, 6],
    5: [0, 1, 3, 4, 6],
    6: [0, 1, 2, 3, 4, 6],
    7: [0, 1, 2, 3, 4, 5, 6],
}


def _phase_for(week_index: int, total_build: int, taper_weeks: int, total_weeks: int) -> str:
    if week_index > total_build:
        return "taper"
    if total_build <= 0:
        return "taper"
    frac = week_index / total_build
    if total_build >= 6 and week_index >= total_build - 1:
        return "peak"
    if frac <= 0.45:
        return "base"
    return "build"


def _quality_for(phase: str) -> str:
    return {"base": "strides", "build": "tempo", "peak": "intervals", "taper": "strides"}.get(phase, "easy")


def build_plan(fitness: dict, race: dict, feasibility: dict,
               profile: dict | None = None, today: date | None = None,
               mode: str = "auto") -> dict:
    """
    Build a week-by-week plan working backwards from race day.

    `mode` selects what the plan actually trains for:
      "auto"    — follow the feasibility verdict (a shorter distance when the
                  named race isn't safely reachable)
      "race"    — train for the named race anyway (honest but risky)
      "<key>"   — train for a specific distance from RACES
    """
    today = today or date.today()
    race_date = _parse_date(race.get("date"))
    spec = race.get("spec") or RACES["10k"]

    target_key = race.get("distance_key")
    if mode == "auto" and feasibility.get("status") == "not_safe":
        for alt in feasibility.get("alternatives") or []:
            if alt.get("kind") == "shorter" and alt.get("distance_key"):
                target_key = alt["distance_key"]
                break
    elif mode in RACES:
        target_key = mode
    target = RACES.get(target_key) or spec

    taper_weeks = int(target["taper_weeks"])
    first_monday = _monday(today)
    race_monday = _monday(race_date)
    total_weeks = max(int((race_monday - first_monday).days / 7) + 1, 1)
    build_weeks = max(total_weeks - taper_weeks - 1, 0)

    try:
        days_per_week = int(str((profile or {}).get("training_days") or "3").strip()[:1])
    except (ValueError, IndexError):
        days_per_week = 3
    days_per_week = max(2, min(days_per_week, 7))

    start_weekly = starting_volume(fitness)
    start_long = max(fitness.get("longest_run_km_8wk") or 0.0, start_weekly * 0.35, 3.0)

    sim = _progress_weeks(start_weekly, start_long, build_weeks,
                          weekly_cap=target["peak_weekly_km"],
                          long_cap=target["long_run_km"])
    # A back-off week immediately before the taper would put the peak three
    # weeks from race day and leave the athlete detrained on the start line.
    # Promote it back to a build week.
    if len(sim) > 1 and sim[-1]["recovery"]:
        prev = max((w for w in sim[:-1] if not w["recovery"]),
                   key=lambda w: w["weekly_km"], default=None)
        if prev:
            sim[-1] = {**sim[-1], "weekly_km": prev["weekly_km"],
                       "long_run_km": prev["long_run_km"], "recovery": False}
    peak_weekly = max((w["weekly_km"] for w in sim), default=start_weekly)
    peak_long = max((w["long_run_km"] for w in sim), default=start_long)

    weeks = []
    for w in sim:
        phase = _phase_for(w["index"], build_weeks, taper_weeks, total_weeks)
        weeks.append(_make_week(first_monday, w["index"], phase, w["weekly_km"],
                                w["long_run_km"], w["recovery"], days_per_week, target))

    # Taper: shed volume, keep a little intensity, arrive fresh.
    taper_curve = {1: [0.60], 2: [0.75, 0.55], 3: [0.80, 0.62, 0.45]}.get(taper_weeks, [0.70])
    for i, factor in enumerate(taper_curve):
        idx = build_weeks + i + 1
        if idx >= total_weeks:
            break
        weeks.append(_make_week(first_monday, idx, "taper", peak_weekly * factor,
                                min(peak_long * factor,
                                    peak_weekly * factor * _long_fraction(peak_weekly * factor)),
                                False, days_per_week, target, taper=True))

    weeks.append(_race_week(first_monday, total_weeks, race, target, spec,
                            race_date, peak_weekly,
                            retargeted=bool(target_key != race.get("distance_key"))))

    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "race": {**{k: race.get(k) for k in ("name", "date", "goal_id")},
                 "distance_key": race.get("distance_key"),
                 "label": spec.get("label")},
        "target": {"distance_key": target_key, "label": target["label"],
                   "distance_km": target["distance_km"]},
        "mode": mode,
        "retargeted": bool(target_key != race.get("distance_key")),
        "days_per_week": days_per_week,
        "total_weeks": len(weeks),
        "build_weeks": build_weeks,
        "taper_weeks": taper_weeks,
        "start_weekly_km": round(start_weekly, 2),
        "peak_weekly_km": round(peak_weekly, 2),
        "peak_long_run_km": round(peak_long, 2),
        "weeks": weeks,
        "feasibility": feasibility,
    }


def _shares(remainder: float, n: int) -> tuple[float, float]:
    """Split the non-long volume across n supporting days; quality gets more."""
    if n <= 0:
        return (0.0, 0.0)
    if n == 1:
        return (max(remainder, 0.0), 0.0)
    q = max(remainder, 0.0) * 0.45
    return (q, (max(remainder, 0.0) - q) / (n - 1))


def _min_share(remainder: float, n: int) -> float:
    q, each = _shares(remainder, n)
    return q if n <= 1 else min(q, each)


def _make_week(first_monday: date, index: int, phase: str, weekly_km: float,
               long_km: float, recovery: bool, days: int, target: dict,
               taper: bool = False) -> dict:
    start = first_monday + timedelta(weeks=index - 1)
    slots = _DAY_PATTERNS.get(days, _DAY_PATTERNS[3])
    # The caller already applied the share ceiling and the demonstrated-capability
    # floor. Only guard the absurd case of a long run exceeding the whole week.
    long_km = max(min(long_km, weekly_km), 0.0)
    remainder = max(weekly_km - long_km, 0.0)
    other = [s for s in slots if s != slots[-1]]

    sessions = []
    # Only schedule as many supporting days as the remaining volume can fill
    # with a real session. Fewer, honest days beat a week of 0.3 mi jogs.
    use = list(other)
    while use and _min_share(remainder, len(use)) < MIN_SESSION_KM:
        use.pop()
    rest_slots = [s for s in other if s not in use]

    if use:
        q_km, each = _shares(remainder, len(use))
        quality_slot = use[0]
        for slot in use:
            is_q = (slot == quality_slot)
            kind = _quality_for(phase) if (is_q and not recovery) else "easy"
            sessions.append(_session(start, slot, kind, q_km if is_q else each, phase))
    else:
        # Nothing left over: the long run IS the week.
        long_km = weekly_km

    for slot in rest_slots:
        sessions.append(_session(start, slot, "rest", 0.0, phase))

    sessions.append(_session(start, slots[-1], "long", long_km, phase))
    sessions.sort(key=lambda s: s["weekday"])

    return {
        "index": index,
        "start": start.isoformat(),
        "end": (start + timedelta(days=6)).isoformat(),
        "phase": "recovery" if recovery else phase,
        "recovery": recovery,
        "taper": taper,
        "weekly_km": round(weekly_km, 2),
        "long_run_km": round(long_km, 2),
        "long_share": round(long_km / weekly_km, 3) if weekly_km else None,
        # Flag only when the long run exceeds the share this volume actually
        # allows — comparing against a fixed threshold below that share would
        # mark every low-volume week, which is noise, not a warning.
        "long_heavy": bool(weekly_km and (long_km / weekly_km) > _long_fraction(weekly_km) * 1.02),
        "sessions": sessions,
    }


def _race_week(first_monday: date, index: int, race: dict, target: dict, spec: dict,
               race_date: date, peak_weekly: float, retargeted: bool = False) -> dict:
    start = first_monday + timedelta(weeks=index - 1)
    sessions = []
    for wd in (1, 3):
        d = start + timedelta(days=wd)
        if d < race_date:
            sessions.append(_session(start, wd, "easy", max(peak_weekly * 0.08, 3.0), "taper"))

    if retargeted:
        # Do NOT print the athlete's marathon at the target distance. The named
        # race is still whatever it is; this plan simply does not prepare for it.
        label = f"{target['label']} effort"
        note = (f"This block builds {target['label']} fitness. {race.get('name')} is a "
                f"{_phrase(spec['label'])} ({_mi(spec['distance_km'])} mi) — it does not "
                f"prepare you for that distance. Race a {_phrase(target['label'])} instead, "
                f"or treat the day as a training run.")
    else:
        label = race.get("name") or "RACE"
        note = "Race day. Nothing you do this week makes you fitter — it only makes you tireder."

    sessions.append({
        "date": race_date.isoformat(),
        "weekday": race_date.weekday(),
        "day": DAY_NAMES[race_date.weekday()],
        "type": "race",
        "label": label,
        "km": round(target["distance_km"], 2),
        "note": note,
    })
    sessions.sort(key=lambda s: s["weekday"])
    return {
        "index": index,
        "start": start.isoformat(),
        "end": (start + timedelta(days=6)).isoformat(),
        "phase": "race",
        "recovery": False,
        # Race week is not a taper week: it carries the race itself, so counting
        # it as one would double-count the taper block and report a 26.2 mi week
        # as reduced volume. `phase == "race"` is the unambiguous marker.
        "taper": False,
        "weekly_km": round(sum(s["km"] for s in sessions), 2),
        "long_run_km": round(target["distance_km"], 2),
        "sessions": sessions,
    }


_NOTES = {
    "easy": "Conversational. If you can't talk in sentences, slow down.",
    "long": "Time on feet is the point, not pace. Walk breaks are allowed.",
    "steady": "Comfortably hard but controlled — not a race.",
    "tempo": "Sustained effort you could hold for about an hour.",
    "intervals": "Hard reps with full recoveries. Quality over quantity.",
    "strides": "Easy running, then 4-6 x 20s relaxed accelerations.",
    "rest": "Rest is where the adaptation happens.",
    "cross": "Aerobic, low impact — bike, swim, or row.",
}


def _session(week_start: date, weekday: int, kind: str, km: float, phase: str) -> dict:
    d = week_start + timedelta(days=weekday)
    return {
        "date": d.isoformat(),
        "weekday": weekday,
        "day": DAY_NAMES[weekday],
        "type": kind,
        "label": SESSION_TYPES.get(kind, {}).get("label", kind.title()),
        "km": round(max(km, 0.0), 2),
        "note": _NOTES.get(kind, ""),
    }


# ---------------------------------------------------------------------------
# Daily adaptation
# ---------------------------------------------------------------------------

def readiness_signals(health_data: dict | None, day: date | None = None) -> dict:
    """
    Raw recovery signals for a day, with the baselines they should be judged
    against. Deliberately excludes training_readiness (always estimated) and
    body_battery (end-of-day drained value for past dates).
    """
    day = day or date.today()
    iso = day.isoformat()
    hd = health_data or {}

    def rows(key):
        return [r for r in (hd.get(key) or []) if isinstance(r, dict) and not r.get("error")]

    sleep = next((r for r in rows("sleep") if _day(r.get("date")) == iso), None)
    hrv = next((r for r in rows("hrv") if _day(r.get("date")) == iso), None)
    stats = next((r for r in rows("daily_stats") if _day(r.get("date")) == iso), None)

    rhr_hist = []
    for r in rows("daily_stats"):
        d = _parse_date(_day(r.get("date")))
        v = _num(r.get("resting_hr"))
        if d and v and day - timedelta(days=30) <= d < day:
            rhr_hist.append(v)
    rhr_base = sorted(rhr_hist)[len(rhr_hist) // 2] if rhr_hist else None

    return {
        "date": iso,
        "sleep_hours": round((_num((sleep or {}).get("total_seconds")) or 0) / 3600, 2) if sleep else None,
        "sleep_score": _num((sleep or {}).get("score")) if sleep else None,
        "hrv_last_night": _num((hrv or {}).get("last_night_avg")) if hrv else None,
        "hrv_baseline": _num((hrv or {}).get("weekly_avg")) if hrv else None,
        "hrv_status": (hrv or {}).get("status") if hrv else None,
        "resting_hr": _num((stats or {}).get("resting_hr")) if stats else None,
        "resting_hr_baseline": rhr_base,
        "stress_avg": _num((stats or {}).get("stress_avg")) if stats else None,
    }


def adapt_session(session: dict | None, signals: dict) -> dict:
    """
    Adjust today's session to how the body actually is.

    Two or more strong flags -> rest. One strong (or two mild) -> ease off.
    Every decision names the signal that drove it, so it can be argued with.
    """
    strong, mild = [], []

    hrv, hrv_base = signals.get("hrv_last_night"), signals.get("hrv_baseline")
    if hrv and hrv_base:
        if hrv < hrv_base * 0.85:
            strong.append(f"HRV {hrv:.0f} is {round((1 - hrv / hrv_base) * 100)}% below your {hrv_base:.0f} baseline")
        elif hrv < hrv_base * 0.93:
            mild.append(f"HRV {hrv:.0f} is a little under your {hrv_base:.0f} baseline")

    hours, score = signals.get("sleep_hours"), signals.get("sleep_score")
    if hours is not None and hours < 5:
        strong.append(f"only {hours:.1f}h sleep")
    elif hours is not None and hours < 6.5:
        mild.append(f"{hours:.1f}h sleep is short")
    if score is not None and score < 40:
        strong.append(f"sleep score {score:.0f}")
    elif score is not None and score < 60:
        mild.append(f"sleep score {score:.0f}")

    rhr, rhr_base = signals.get("resting_hr"), signals.get("resting_hr_baseline")
    if rhr and rhr_base:
        if rhr >= rhr_base + 7:
            strong.append(f"resting HR {rhr:.0f} vs {rhr_base:.0f} normal")
        elif rhr >= rhr_base + 4:
            mild.append(f"resting HR {rhr:.0f} slightly up")

    stress = signals.get("stress_avg")
    if stress is not None and stress >= 60:
        mild.append(f"average stress {stress:.0f}")

    have_any = any(signals.get(k) is not None for k in
                   ("sleep_hours", "hrv_last_night", "resting_hr", "stress_avg"))
    if not have_any:
        return {"action": "unknown", "session": session, "adjusted": None,
                "headline": "No recovery data for today yet.",
                "reasons": [], "flags": {"strong": [], "mild": []}}

    if session is None or session.get("type") == "rest":
        return {"action": "keep", "session": session, "adjusted": session,
                "headline": "Rest day — nothing to adapt.",
                "reasons": strong + mild, "flags": {"strong": strong, "mild": mild}}

    if session.get("type") == "race":
        return {"action": "keep", "session": session, "adjusted": session,
                "headline": "Race day. Run it.",
                "reasons": strong + mild, "flags": {"strong": strong, "mild": mild}}

    adjusted = dict(session)
    if len(strong) >= 2:
        action = "rest"
        adjusted.update({"type": "rest", "label": "Rest", "km": 0.0,
                         "note": _NOTES["rest"]})
        headline = "Take today off."
    elif len(strong) == 1 or len(mild) >= 3:
        action = "ease"
        if session.get("type") in ("tempo", "intervals", "steady", "strides"):
            adjusted.update({"type": "easy", "label": "Easy run",
                             "km": round(session.get("km", 0) * 0.8, 2),
                             "note": _NOTES["easy"]})
            headline = "Drop the quality — run it easy."
        else:
            adjusted.update({"km": round(session.get("km", 0) * 0.75, 2),
                             "note": _NOTES.get(session.get("type"), "")})
            headline = "Shorten it and keep the effort easy."
    elif mild:
        action = "caution"
        headline = "Go as planned, but start conservatively."
    else:
        action = "keep"
        headline = "Green light — run it as written."

    return {"action": action, "session": session, "adjusted": adjusted,
            "headline": headline, "reasons": strong + mild,
            "flags": {"strong": strong, "mild": mild}}


def session_for(plan: dict | None, day: date) -> dict | None:
    iso = day.isoformat()
    for week in (plan or {}).get("weeks") or []:
        for s in week.get("sessions") or []:
            if s.get("date") == iso:
                return s
    # A day inside the plan with no session is a rest day, not a gap.
    for week in (plan or {}).get("weeks") or []:
        if week.get("start", "") <= iso <= week.get("end", "zzz"):
            return {"date": iso, "weekday": day.weekday(), "day": DAY_NAMES[day.weekday()],
                    "type": "rest", "label": "Rest", "km": 0.0, "note": _NOTES["rest"]}
    return None


def current_week(plan: dict | None, day: date | None = None) -> dict | None:
    day = day or date.today()
    iso = day.isoformat()
    for week in (plan or {}).get("weeks") or []:
        if week.get("start", "") <= iso <= week.get("end", "zzz"):
            return week
    return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_plan() -> dict | None:
    try:
        with open(PLAN_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and data.get("weeks") else None
    except (OSError, ValueError):
        return None


def save_plan(plan: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PLAN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)
    os.replace(tmp, PLAN_FILE)


def clear_plan() -> bool:
    try:
        os.remove(PLAN_FILE)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Coach context
# ---------------------------------------------------------------------------

def format_for_prompt(plan: dict | None, adaptation: dict | None,
                      units: str = "km", today: date | None = None) -> str:
    """Compact block describing the plan for the coach's system context."""
    if not plan:
        return ""
    today = today or date.today()

    def dist(km):
        v = _num(km) or 0.0
        return f"{v / KM_PER_MI:.1f}mi" if units == "mi" else f"{v:.1f}km"

    lines = ["TRAINING PLAN (the athlete is following this — keep advice consistent with it):"]
    tgt = plan.get("target") or {}
    race = plan.get("race") or {}
    lines.append(f"  Target: {tgt.get('label')} \u2014 {race.get('name')} on {race.get('date')}")
    if plan.get("retargeted"):
        lines.append(f"  NOTE: plan trains for a {tgt.get('label')}, NOT the "
                     f"{race.get('label')} the athlete originally named. "
                     f"Their base does not safely support that distance in the time available.")

    fz = plan.get("feasibility") or {}
    if fz.get("status") in ("not_safe", "tight"):
        lines.append(f"  Feasibility: {fz.get('headline')}")

    week = current_week(plan, today)
    if week:
        lines.append(f"  This week (#{week['index']} of {plan.get('total_weeks')}, "
                     f"{week['phase']}): {dist(week['weekly_km'])} planned, "
                     f"long run {dist(week['long_run_km'])}")
        for s in week.get("sessions") or []:
            mark = "<- today" if s.get("date") == today.isoformat() else ""
            lines.append(f"    {s['day']}: {s['label']} {dist(s['km'])} {mark}".rstrip())

    if adaptation and adaptation.get("action") not in (None, "unknown"):
        lines.append(f"  Today's adjustment: {adaptation.get('headline')}")
        # Only justify an actual change. "Rest day - nothing to adapt, because
        # you slept badly" reads as though the rest day were a response.
        if adaptation.get("reasons") and adaptation["action"] in ("rest", "ease", "caution"):
            lines.append("    because " + "; ".join(adaptation["reasons"]))
    return "\n".join(lines)
