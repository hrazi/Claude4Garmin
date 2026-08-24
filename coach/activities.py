"""
Activity browsing: filtering and paging over the full training history, plus
structured parsers that turn Garmin's raw enrichment payloads into something a
page can render directly.

Everything here is a pure function over already-fetched data, so it can be
tested against the real cache without touching the network.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# Distance-based activities where pace (time per unit) is the natural readout.
# For everything else speed is more meaningful, and for some there is no
# distance at all.
PACE_TYPES = {
    "running", "treadmill_running", "trail_running", "track_running",
    "walking", "hiking", "lap_swimming", "open_water_swimming", "swimming",
}

SPEED_TYPES = {
    "cycling", "indoor_cycling", "mountain_biking", "gravel_cycling",
    "road_biking", "virtual_ride", "rowing_v2", "rowing",
    "resort_skiing_snowboarding_ws", "skiing", "elliptical",
}

# Broad groups so the filter bar stays short even with 18 raw types.
GROUPS: dict[str, tuple[str, ...]] = {
    "run":      ("running", "treadmill_running", "trail_running", "track_running"),
    "ride":     ("cycling", "indoor_cycling", "mountain_biking", "gravel_cycling",
                 "road_biking", "virtual_ride"),
    "swim":     ("lap_swimming", "open_water_swimming", "swimming"),
    "walk":     ("walking", "hiking"),
    "strength": ("strength_training", "indoor_cardio", "elliptical", "yoga", "pilates"),
}

GROUP_LABELS = {
    "run": "Run", "ride": "Ride", "swim": "Swim",
    "walk": "Walk & hike", "strength": "Gym",
}

ICONS = {
    "run": "🏃", "ride": "🚴", "swim": "🏊", "walk": "🥾",
    "strength": "🏋️", "other": "•",
}

# Plausible average speed in m/s per group. Garmin occasionally logs a
# distance and duration that cannot both be true (a 3 km "run" in 213 s), and
# a pace derived from those is fiction. Such rows are flagged rather than
# hidden, because it is still a real activity the athlete did.
PLAUSIBLE_MPS = {
    "run":  (0.7, 7.0),
    "ride": (0.7, 25.0),
    "swim": (0.2, 3.0),
    "walk": (0.2, 3.5),
}


def group_of(activity_type: str) -> str:
    """Which broad group a raw Garmin activity type belongs to."""
    t = (activity_type or "").lower()
    for group, members in GROUPS.items():
        if t in members:
            return group
    return "other"


def pretty_type(activity_type: str) -> str:
    """'treadmill_running' -> 'Treadmill running'."""
    t = (activity_type or "").replace("_ws", "").replace("_v2", "")
    return t.replace("_", " ").strip().capitalize() or "Activity"


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def _pick_timed(dist_m: float, moving: float, elapsed: float, group: str) -> float:
    """
    Choose the duration that pace and speed should be derived from.

    Moving time is the better number when it is trustworthy, but Garmin's
    movingDuration is unreliable on older GPS files: one open-water swim
    records 3,432 s of "moving" inside a 6h50m session, which would turn a real
    swim into a 16 km/h one. So prefer moving time only while it yields a
    plausible speed, and fall back to elapsed when that rescues the figure.
    """
    if moving <= 0:
        return elapsed
    if dist_m <= 0 or elapsed <= 0:
        return moving
    lo, hi = PLAUSIBLE_MPS.get(group, (0.0, 1e9))
    if lo <= dist_m / moving <= hi:
        return moving
    if lo <= dist_m / elapsed <= hi:
        return elapsed
    # Neither is plausible; keep moving time and let the suspect flag speak.
    return moving


def summarize(a: dict) -> dict:
    """
    One activity, enriched with the derived numbers a list row needs.

    Pace uses moving time when Garmin recorded it and elapsed time otherwise,
    because for more than half of this history moving_duration is missing and
    silently falling back to zero would invent implausibly fast paces.
    """
    dist_m = _num(a.get("distance_meters")) or 0.0
    elapsed = _num(a.get("duration_seconds")) or 0.0
    moving = _num(a.get("moving_duration")) or 0.0

    group = group_of(a.get("type"))
    timed = _pick_timed(dist_m, moving, elapsed, group)
    pace_min_per_km = None
    speed_kph = None
    if dist_m > 0 and timed > 0:
        speed_kph = (dist_m / 1000.0) / (timed / 3600.0)
        pace_min_per_km = (timed / 60.0) / (dist_m / 1000.0)

    raw_type = a.get("type") or ""
    if raw_type in SPEED_TYPES:
        readout = "speed"
    elif group == "swim":
        # Nobody reads swim pace in minutes per mile. The universal convention
        # is time per 100 m, so swims get their own readout regardless of the
        # athlete's distance preference.
        readout = "swim_pace"
    elif raw_type in PACE_TYPES:
        readout = "pace"
    else:
        readout = "pace" if dist_m > 0 else "none"

    suspect = False
    if dist_m > 0 and timed > 0:
        lo, hi = PLAUSIBLE_MPS.get(group, (0.0, 1e9))
        suspect = not (lo <= dist_m / timed <= hi)

    return {
        "activity_id":     str(a.get("activity_id") or ""),
        "name":            a.get("name") or pretty_type(raw_type),
        "type":            raw_type,
        "type_label":      pretty_type(raw_type),
        "group":           group,
        "icon":            ICONS.get(group, ICONS["other"]),
        "date":            (a.get("date") or "")[:10],
        "start_time":      a.get("start_time"),
        "distance_m":      dist_m or None,
        "duration_s":      elapsed or None,
        "moving_s":        moving or None,
        "timed_s":         timed or None,
        "used_elapsed":    bool(dist_m > 0 and moving <= 0 < elapsed),
        "pace_min_per_km": round(pace_min_per_km, 4) if pace_min_per_km else None,
        "speed_kph":       round(speed_kph, 3) if speed_kph else None,
        "readout":         readout,
        "suspect":         suspect,
        "avg_hr":          _num(a.get("avg_hr")),
        "max_hr":          _num(a.get("max_hr")),
        "calories":        _num(a.get("calories")),
        "elevation_gain":  _num(a.get("elevation_gain")),
        "avg_power":       _num(a.get("avg_power")),
        "avg_cadence":     _num(a.get("avg_cadence")),
        # Hand-entered sessions must stay visibly distinguishable from recorded
        # ones: they carry no device measurement behind them, and the athlete
        # should never have to guess which figures were typed in.
        "source":          a.get("source") or "garmin",
        "device_name":     a.get("device_name"),
        "note":            a.get("note"),
    }


def facets(activities: list[dict]) -> dict:
    """Group and year counts, so the filter bar only offers what actually exists."""
    groups: dict[str, int] = {}
    years: dict[str, int] = {}
    for a in activities:
        groups[group_of(a.get("type"))] = groups.get(group_of(a.get("type")), 0) + 1
        y = (a.get("date") or "")[:4]
        if y:
            years[y] = years.get(y, 0) + 1
    return {
        "groups": [{"key": k, "label": GROUP_LABELS.get(k, "Other"),
                    "icon": ICONS.get(k, ICONS["other"]), "count": v}
                   for k, v in sorted(groups.items(), key=lambda kv: -kv[1])],
        "years": [{"year": y, "count": n} for y, n in sorted(years.items(), reverse=True)],
    }


# Below this distance a pace figure is noise, not a performance: the history
# holds "runs" of 6, 7 and 10 metres, which are accidental starts. Swimming
# gets a lower floor because a 150 m pool session is a real, if short, swim.
MIN_RANKABLE_M = {"swim": 100}
MIN_RANKABLE_DEFAULT = 200


def _unrankable_pace(row: dict) -> bool:
    """True when this row's pace should not be allowed to win a ranking."""
    if row["suspect"] or not row["distance_m"]:
        return True
    floor = MIN_RANKABLE_M.get(row["group"], MIN_RANKABLE_DEFAULT)
    return row["distance_m"] < floor


SORTS = {    "date":     lambda s: s["date"] or "",
    "distance": lambda s: s["distance_m"] or 0,
    "duration": lambda s: s["duration_s"] or 0,
    "pace":     lambda s: s["pace_min_per_km"] or 0,
    "hr":       lambda s: s["avg_hr"] or 0,
}


def query(activities: list[dict], *, group: str = "", year: str = "",
          search: str = "", sort: str = "date", desc: bool = True,
          limit: int = 50, offset: int = 0) -> dict:
    """Filter, sort and page the history. Returns the page plus the total."""
    rows = [summarize(a) for a in activities]

    if group and group != "all":
        rows = [r for r in rows if r["group"] == group]
    if year and year != "all":
        rows = [r for r in rows if r["date"][:4] == year]
    if search:
        q = search.strip().lower()
        rows = [r for r in rows
                if q in r["name"].lower() or q in r["type_label"].lower()]

    key = SORTS.get(sort, SORTS["date"])
    # Two demotions, both so a ranking never gets won by a number that isn't
    # trustworthy. First: rows missing the sort value sink to the bottom either
    # way, rather than masquerading as the fastest or the longest. Second: pace
    # is derived from distance ÷ time, so it is only meaningful when those two
    # agree and the distance is long enough to mean anything. Demoted rows stay
    # in the list, flagged — they just don't get to top a pace ranking.
    demote = _unrankable_pace if sort == "pace" else (lambda r: False)
    rows.sort(key=lambda r: (demote(r), key(r) is None or key(r) == 0, key(r))
              if not desc
              else (demote(r), key(r) is None or key(r) == 0, -_sortable(key(r))))

    total = len(rows)
    offset = max(0, offset)
    page = rows[offset:offset + max(1, limit)]
    return {"total": total, "offset": offset, "limit": limit, "activities": page}


def _sortable(v):
    """Numeric key for descending sort; dates sort lexically so map them."""
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        # Turn an ISO date into a sortable integer, missing dates to zero.
        digits = v.replace("-", "")
        return int(digits) if digits.isdigit() else 0
    return 0


# ── detail parsers ──────────────────────────────────────────────────────────

def parse_hr_zones(detail: dict) -> list[dict]:
    """Garmin's HR zone payload -> rows with seconds and share of total."""
    raw = (detail or {}).get("hr_zones")
    if not isinstance(raw, list):
        return []
    zones = []
    for z in raw:
        if not isinstance(z, dict):
            continue
        secs = _num(z.get("secsInZone")) or 0.0
        zones.append({
            "zone":     int(z.get("zoneNumber") or 0),
            "seconds":  round(secs),
            "low_bpm":  _num(z.get("zoneLowBoundary")),
        })
    total = sum(z["seconds"] for z in zones)
    for z in zones:
        z["pct"] = round(z["seconds"] / total * 100, 1) if total else 0.0
    return sorted(zones, key=lambda z: z["zone"])


def parse_power_zones(detail: dict) -> list[dict]:
    raw = (detail or {}).get("power_zones")
    if not isinstance(raw, list):
        return []
    zones = []
    for z in raw:
        if not isinstance(z, dict):
            continue
        secs = _num(z.get("secsInZone")) or 0.0
        zones.append({
            "zone":       int(z.get("zoneNumber") or 0),
            "seconds":    round(secs),
            "low_watts":  _num(z.get("zoneLowBoundary")),
        })
    total = sum(z["seconds"] for z in zones)
    for z in zones:
        z["pct"] = round(z["seconds"] / total * 100, 1) if total else 0.0
    return sorted(zones, key=lambda z: z["zone"])


def parse_splits(detail: dict, group: str = "other") -> list[dict]:
    """Lap rows, with pace derived per lap rather than inherited from the whole."""
    splits = (detail or {}).get("splits")
    laps = (splits or {}).get("lapDTOs") if isinstance(splits, dict) else None
    if not isinstance(laps, list):
        return []

    rows = []
    for i, lap in enumerate(laps, start=1):
        if not isinstance(lap, dict):
            continue
        dist = _num(lap.get("distance")) or 0.0
        moving = _num(lap.get("movingDuration")) or 0.0
        elapsed = _num(lap.get("duration")) or 0.0
        timed = _pick_timed(dist, moving, elapsed, group)
        pace = (timed / 60.0) / (dist / 1000.0) if dist > 0 and timed > 0 else None
        rows.append({
            "lap":        i,
            "distance_m": dist or None,
            "duration_s": elapsed or None,
            "moving_s":   moving or None,
            "pace_min_per_km": round(pace, 4) if pace else None,
            "speed_kph":  round((dist / 1000.0) / (timed / 3600.0), 3)
                          if dist > 0 and timed > 0 else None,
            "avg_hr":     _num(lap.get("averageHR")),
            "max_hr":     _num(lap.get("maxHR")),
            "calories":   _num(lap.get("calories")),
            "elev_gain":  _num(lap.get("elevationGain")),
            "cadence":    _num(lap.get("averageRunCadence"))
                          or _num(lap.get("averageBikeCadence")),
        })
    return rows


def parse_exercise_sets(detail: dict) -> list[dict]:
    """Strength sets, skipping rest blocks which carry no exercise name."""
    raw = (detail or {}).get("exercise_sets")
    sets_list = raw.get("exerciseSets") if isinstance(raw, dict) else raw
    if not isinstance(sets_list, list):
        return []

    rows = []
    for s in sets_list:
        if not isinstance(s, dict):
            continue
        if (s.get("setType") or "").upper() == "REST":
            continue
        names = s.get("exercises") or []
        name = ""
        if isinstance(names, list) and names and isinstance(names[0], dict):
            name = (names[0].get("name") or names[0].get("category") or "")
            name = name.replace("_", " ").title()
        rows.append({
            "exercise":   name or "Set",
            "reps":       _num(s.get("repetitionCount")),
            "weight_kg":  round(_num(s.get("weight")) / 1000, 1)
                          if _num(s.get("weight")) else None,
            "duration_s": _num(s.get("duration")),
        })
    return rows


def detail_payload(activity: dict, detail: dict | None) -> dict:
    """Everything the detail view needs, with a flag per section."""
    detail = detail or {}
    summary = summarize(activity)
    zones = parse_hr_zones(detail)
    power = parse_power_zones(detail)
    laps = parse_splits(detail, summary["group"])
    sets_ = parse_exercise_sets(detail)

    errors = [k.replace("_error", "") for k in detail if k.endswith("_error")]

    return {
        "summary":       summary,
        "hr_zones":      zones,
        "power_zones":   power,
        "splits":        laps,
        "exercise_sets": sets_,
        "fetched_at":    detail.get("fetched_at"),
        "has_detail":    bool(zones or power or laps or sets_),
        "errors":        errors,
    }


def find(activities: list[dict], activity_id: str) -> dict | None:
    """Locate one activity by id."""
    target = str(activity_id)
    for a in activities:
        if str(a.get("activity_id")) == target:
            return a
    return None


# ── weekly grid + streaks ───────────────────────────────────────────────────

def _monday_of(day: date) -> date:
    """The Monday that starts this day's week."""
    return day - timedelta(days=day.weekday())


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def weekly_buckets(rows: list[dict]) -> list[dict]:
    """
    Group summarised activities into Monday-start weeks, newest first.

    Each week carries its activities already split across seven day slots, so
    the grid can render straight from this without doing date maths in the
    browser. Weeks with nothing in them are omitted; the page decides how to
    draw the gaps between them.
    """
    weeks: dict[date, dict] = {}
    for r in rows:
        day = _parse_day(r.get("date"))
        if day is None:
            continue
        mon = _monday_of(day)
        wk = weeks.get(mon)
        if wk is None:
            wk = weeks[mon] = {
                "monday": mon.isoformat(),
                "year": mon.year,
                "days": [[] for _ in range(7)],
                "count": 0,
                "distance_m": 0.0,
                "duration_s": 0.0,
            }
        wk["days"][day.weekday()].append(r)
        wk["count"] += 1
        wk["distance_m"] += r.get("distance_m") or 0.0
        wk["duration_s"] += r.get("duration_s") or 0.0

    out = [weeks[m] for m in sorted(weeks, reverse=True)]
    for wk in out:
        # Longest first within a day looks tidiest in the grid.
        for slot in wk["days"]:
            slot.sort(key=lambda r: r.get("distance_m") or 0, reverse=True)
    return out


def weekly_streaks(rows: list[dict], today: date | None = None) -> dict:
    """
    Consecutive-week activity streaks over the given (already filtered) rows.

    A week counts once it holds at least one activity, so this measures
    consistency rather than volume — which is the habit actually worth keeping.

    The current week is treated as grace, not as a break: it is still in
    progress, so an empty Monday morning should not appear to end a streak that
    is in fact intact. The same rule the daily habit tracker uses.
    """
    today = today or date.today()
    this_monday = _monday_of(today)

    active = set()
    for r in rows:
        day = _parse_day(r.get("date"))
        if day is not None:
            active.add(_monday_of(day))
    if not active:
        return {"current": 0, "longest": 0, "longest_start": None,
                "longest_end": None, "active_weeks": 0, "this_week": False,
                "in_grace": False}

    # Longest run of consecutive weeks anywhere in the history.
    ordered = sorted(active)
    longest = run = 1
    run_start = best_start = best_end = ordered[0]
    for prev, cur in zip(ordered, ordered[1:]):
        if cur - prev == timedelta(days=7):
            run += 1
        else:
            run, run_start = 1, cur
        if run > longest:
            longest, best_start, best_end = run, run_start, cur

    # Current streak, counting back from this week or the one before it.
    this_week = this_monday in active
    anchor = this_monday if this_week else this_monday - timedelta(days=7)
    current = 0
    cursor = anchor
    while cursor in active:
        current += 1
        cursor -= timedelta(days=7)

    return {
        "current": current,
        "longest": max(longest, current),
        "longest_start": best_start.isoformat(),
        "longest_end": best_end.isoformat(),
        "active_weeks": len(active),
        "this_week": this_week,
        # True when the streak is being carried by last week while this one is
        # still open, so the page can say so instead of implying it is banked.
        "in_grace": bool(current and not this_week),
    }
