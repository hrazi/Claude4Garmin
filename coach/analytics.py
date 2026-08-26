"""
Analytics — derived series for the visual analytics page.

Pure functions over already-cached data (no network calls). Each builder takes
plain dicts/lists and returns JSON-serialisable structures ready for Chart.js.

Covers GitHub issues:
  #33 year contribution heatmap
  #34 HR zone distribution
  #36 sleep/wake consistency bands
  #38 pace-per-heartbeat efficiency trend
  #42 pace-duration (best effort) curve
  #46 metric correlation matrix
  #49 intraday stress bands + longest waking rest window
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

RUN_TYPES = ("running", "treadmill_running", "trail_running", "track_running",
             "indoor_running", "virtual_run")


def _is_run(activity: dict) -> bool:
    return (activity.get("type") or "").lower() in RUN_TYPES


# Garmin history contains a handful of corrupt records (bad GPS, mis-set
# distance) — e.g. a "run" at 15.6 m/s, faster than the world record. Bound
# running speed to a humanly possible range before using it for any best-effort
# or efficiency calculation.
MIN_RUN_SPEED_MPS = 1.5   # ~11:07 /km — slower than this is a walk
MAX_RUN_SPEED_MPS = 6.5   # ~2:34 /km — faster than this is corrupt data
MIN_HR = 30
MAX_HR = 220


def _run_speed(activity: dict):
    """Average speed of a run in m/s, or None when the record is implausible."""
    dist = _num(activity.get("distance_meters"))
    dur = _num(activity.get("moving_duration")) or _num(activity.get("duration_seconds"))
    if not dist or not dur or dur <= 0:
        return None
    speed = dist / dur
    if not (MIN_RUN_SPEED_MPS <= speed <= MAX_RUN_SPEED_MPS):
        return None
    return speed


def _num(value):
    """Return value as a float, or None when it is missing/non-numeric/NaN."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return float(value)


def _day(value) -> str:
    return (value or "")[:10]


# ---------------------------------------------------------------------------
# #33 — Year contribution heatmap
# ---------------------------------------------------------------------------

HEATMAP_METRICS = {
    "distance":  {"label": "Distance",  "unit": "km",   "source": "activity"},
    "duration":  {"label": "Duration",  "unit": "min",  "source": "activity"},
    "steps":     {"label": "Steps",     "unit": "steps", "source": "daily"},
    "readiness": {"label": "Readiness", "unit": "score", "source": "daily"},
}


def build_heatmap(activities: list, daily_stats: list, readiness: list) -> dict:
    """
    Daily totals per metric keyed by ISO date, plus the list of years that hold
    data. The template renders the 53x7 grid; here we only supply the values.
    """
    distance: dict[str, float] = {}
    duration: dict[str, float] = {}
    counts: dict[str, int] = {}
    types: dict[str, dict[str, int]] = {}

    for a in activities or []:
        day = _day(a.get("date") or a.get("start_time"))
        if not day:
            continue
        dist = _num(a.get("distance_meters")) or 0.0
        dur = _num(a.get("duration_seconds")) or 0.0
        distance[day] = distance.get(day, 0.0) + dist / 1000.0
        duration[day] = duration.get(day, 0.0) + dur / 60.0
        counts[day] = counts.get(day, 0) + 1
        kind = (a.get("type") or "other").replace("_", " ")
        types.setdefault(day, {})
        types[day][kind] = types[day].get(kind, 0) + 1

    steps = {_day(r.get("date")): _num(r.get("steps"))
             for r in daily_stats or [] if _num(r.get("steps"))}
    ready = {_day(r.get("date")): _num(r.get("score"))
             for r in readiness or [] if _num(r.get("score"))}

    values = {
        "distance": {d: round(v, 2) for d, v in distance.items() if v > 0},
        "duration": {d: round(v, 1) for d, v in duration.items() if v > 0},
        "steps": steps,
        "readiness": ready,
    }

    years = sorted({d[:4] for metric in values.values() for d in metric},
                   reverse=True)

    return {
        "metrics": HEATMAP_METRICS,
        "values": values,
        "counts": counts,
        "types": {d: sorted(t.items(), key=lambda kv: -kv[1]) for d, t in types.items()},
        "years": years,
        "streaks": _streaks(sorted(counts)),
    }


def _streaks(active_days: list) -> dict:
    """Longest and current run of consecutive active days."""
    if not active_days:
        return {"longest": 0, "current": 0}
    days = [date.fromisoformat(d) for d in active_days if len(d) == 10]
    if not days:
        return {"longest": 0, "current": 0}

    longest = run = 1
    for prev, cur in zip(days, days[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        longest = max(longest, run)

    today = date.today()
    current = 0
    if (today - days[-1]).days <= 1:
        current = 1
        for prev, cur in zip(reversed(days[:-1]), reversed(days[1:])):
            if (cur - prev).days == 1:
                current += 1
            else:
                break
    return {"longest": longest, "current": current}


# ---------------------------------------------------------------------------
# #36 — Sleep / wake consistency bands
# ---------------------------------------------------------------------------

def _minutes_from_midnight(iso: str | None, *, anchor_evening: bool) -> float | None:
    """
    Convert an ISO datetime to minutes on a night-centred axis where 0 = noon.
    Bedtimes late in the evening and wake times the next morning then sit on a
    single continuous scale, so a band never wraps around the axis.
    """
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    minutes = dt.hour * 60 + dt.minute
    # Axis runs noon -> noon. Anything before noon belongs to the next day.
    shifted = minutes - 720
    if shifted < 0:
        shifted += 1440
    if anchor_evening and shifted > 1080:  # bedtime after ~06:00 is unusual
        shifted -= 1440
    return float(shifted)


def build_sleep_bands(sleep_rows: list) -> dict:
    """
    One band per night (bedtime -> wake) on a noon-to-noon axis, with medians so
    schedule drift is visible. Nights without timestamps are returned as nulls
    to keep the date axis continuous.

    The wake end is derived by adding the true elapsed time to the bedtime
    rather than mapping it independently. Mapping both ends separately puts a
    daytime nap's start and end on different noon-cycles, which fabricates
    windows of 24 hours or more.
    """
    rows = sorted(sleep_rows or [], key=lambda r: r.get("date") or "")
    dates, starts, ends, scores, labels, naps = [], [], [], [], [], []

    for r in rows:
        day = _day(r.get("date"))
        if not day:
            continue

        bed = _minutes_from_midnight(r.get("bedtime_local"), anchor_evening=True)
        elapsed = _elapsed_minutes(r.get("bedtime_local"), r.get("wake_local"))
        wake = bed + elapsed if (bed is not None and elapsed is not None) else None

        dates.append(day)
        starts.append(bed)
        ends.append(wake)
        scores.append(_num(r.get("score")))
        # A short window starting after 05:00 is a nap, not a night's sleep.
        naps.append(bool(elapsed is not None and elapsed < 300
                         and bed is not None and bed < 0))
        labels.append({
            "bed": (r.get("bedtime_local") or "")[11:],
            "wake": (r.get("wake_local") or "")[11:],
            "hours": round(elapsed / 60, 1) if elapsed else None,
        })

    # Medians describe the normal routine, so naps are excluded from them.
    nightly = [i for i in range(len(dates))
               if starts[i] is not None and ends[i] is not None and not naps[i]]
    med_bed = _median([starts[i] for i in nightly])
    med_wake = _median([ends[i] for i in nightly])

    # Consistency = how tightly bedtimes cluster (lower spread is better).
    spread = None
    if len(nightly) > 1 and med_bed is not None:
        spread = round(
            sum(abs(starts[i] - med_bed) for i in nightly) / len(nightly), 1
        )

    return {
        "dates": dates,
        "bed": starts,
        "wake": ends,
        "scores": scores,
        "labels": labels,
        "naps": naps,
        "median_bed": med_bed,
        "median_wake": med_wake,
        "bed_spread_min": spread,
        "nights_with_times": len(nightly),
        "axis_origin_hour": 12,
    }


def _elapsed_minutes(start_iso: str | None, end_iso: str | None):
    """True minutes between two ISO timestamps, or None if either is missing."""
    if not start_iso or not end_iso:
        return None
    try:
        delta = datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
    except ValueError:
        return None
    minutes = delta.total_seconds() / 60
    if minutes <= 0 or minutes > 24 * 60:
        return None
    return minutes


def _median(vals: list):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    s = sorted(vals)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


# ---------------------------------------------------------------------------
# #34 — HR zone distribution
# ---------------------------------------------------------------------------

ZONE_LABELS = {
    1: "Z1 Warm up", 2: "Z2 Easy", 3: "Z3 Aerobic",
    4: "Z4 Threshold", 5: "Z5 Max",
}


def build_hr_zones(activities: list, details: dict, days: int = 120) -> dict:
    """
    Per-activity and per-week time-in-zone, plus the easy/hard split.

    `details` is the activity_details cache keyed by activity_id; only
    activities with a cached `hr_zones` payload can be charted.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    details = details or {}

    per_activity, weekly = [], {}
    easy = hard = 0.0

    rows = sorted(
        (a for a in activities or [] if _day(a.get("date")) >= cutoff),
        key=lambda a: a.get("date") or "",
    )

    for a in rows:
        entry = details.get(str(a.get("activity_id"))) or {}
        zones = entry.get("hr_zones")
        if not isinstance(zones, list) or not zones:
            continue

        mins = {}
        total = 0.0
        for z in zones:
            num = z.get("zoneNumber")
            secs = _num(z.get("secsInZone")) or 0.0
            if num is None:
                continue
            mins[int(num)] = round(secs / 60.0, 1)
            total += secs
        if total <= 0:
            continue

        day = _day(a.get("date"))
        per_activity.append({
            "activity_id": str(a.get("activity_id")),
            "date": day,
            "name": a.get("name") or "Activity",
            "type": (a.get("type") or "").replace("_", " "),
            "zones": [mins.get(i, 0.0) for i in range(1, 6)],
            "total_min": round(total / 60.0, 1),
        })

        week = _week_start(day)
        bucket = weekly.setdefault(week, [0.0] * 5)
        for i in range(1, 6):
            bucket[i - 1] = round(bucket[i - 1] + mins.get(i, 0.0), 1)

        easy += sum(mins.get(i, 0.0) for i in (1, 2))
        hard += sum(mins.get(i, 0.0) for i in (3, 4, 5))

    total_min = easy + hard
    ratio = round(easy / total_min * 100, 1) if total_min else None

    return {
        "zone_labels": [ZONE_LABELS[i] for i in range(1, 6)],
        "activities": per_activity,
        "weeks": sorted(weekly),
        "weekly": [weekly[w] for w in sorted(weekly)],
        "easy_min": round(easy, 1),
        "hard_min": round(hard, 1),
        "easy_pct": ratio,
        "target_easy_pct": 80,
        "covered": len(per_activity),
        "in_range": len(rows),
        "days": days,
    }


def _week_start(day: str) -> str:
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day
    return (d - timedelta(days=d.weekday())).isoformat()


# ---------------------------------------------------------------------------
# #38 — Aerobic efficiency trend (pace per heartbeat)
# ---------------------------------------------------------------------------

def build_efficiency(activities: list, days: int = 730,
                     min_duration_s: int = 600) -> dict:
    """
    Efficiency index = metres covered per heartbeat (speed / HR * 60). Rising
    values mean more speed for the same effort. Short efforts are excluded so
    warm-ups and strides do not distort the trend.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    points = []

    for a in activities or []:
        if not _is_run(a):
            continue
        day = _day(a.get("date"))
        if not day or day < cutoff:
            continue
        speed = _run_speed(a)
        hr = _num(a.get("avg_hr"))
        dur = _num(a.get("duration_seconds")) or 0
        if not speed or not hr or not (MIN_HR <= hr <= MAX_HR):
            continue
        if dur < min_duration_s:
            continue
        dist = _num(a.get("distance_meters")) or 0
        points.append({
            "x": day,
            "y": round(speed / hr * 60.0, 2),      # metres per heartbeat
            "pace": round(1000.0 / speed / 60.0, 2) if speed else None,
            "hr": round(hr),
            "km": round(dist / 1000.0, 2),
            "name": a.get("name") or "Run",
            "activity_id": str(a.get("activity_id")),
            "long_run": dist >= 15000,
        })

    points.sort(key=lambda p: p["x"])

    # Rolling median over the trailing 30 days: robust against one-off outliers.
    trend = []
    for i, p in enumerate(points):
        cur = date.fromisoformat(p["x"])
        window = [
            q["y"] for q in points[: i + 1]
            if (cur - date.fromisoformat(q["x"])).days <= 30
        ]
        if window:
            s = sorted(window)
            mid = len(s) // 2
            med = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
            trend.append({"x": p["x"], "y": round(med, 2)})

    change = None
    if len(points) >= 6:
        head = [p["y"] for p in points[: max(3, len(points) // 5)]]
        tail = [p["y"] for p in points[-max(3, len(points) // 5):]]
        base = sum(head) / len(head)
        if base:
            change = round((sum(tail) / len(tail) - base) / base * 100, 1)

    return {"points": points, "trend": trend, "change_pct": change,
            "count": len(points), "days": days}


# ---------------------------------------------------------------------------
# #42 — Pace / duration best-effort curve
# ---------------------------------------------------------------------------

DISTANCE_BUCKETS = [
    ("1K", 1000, 0.90, 1.35),
    ("2K", 2000, 0.90, 1.30),
    ("5K", 5000, 0.92, 1.25),
    ("10K", 10000, 0.93, 1.20),
    ("15K", 15000, 0.93, 1.15),
    ("Half", 21097, 0.95, 1.10),
    ("30K", 30000, 0.94, 1.12),
    ("Marathon", 42195, 0.96, 1.08),
]

# Riegel exponent: predicted time scales with distance^1.06
RIEGEL = 1.06


def build_pace_curve(activities: list, recent_days: int = 365) -> dict:
    """
    Best sustained pace for each classic race distance, using every run whose
    distance falls in that band. Also projects race times from the strongest
    single effort using the Riegel formula, so the goal gap is visible.
    """
    cutoff = (date.today() - timedelta(days=recent_days)).isoformat()
    buckets = []
    best_overall = None

    runs = [a for a in activities or [] if _is_run(a)]

    for label, target, lo, hi in DISTANCE_BUCKETS:
        low, high = target * lo, target * hi
        all_time = recent = None

        for a in runs:
            dist = _num(a.get("distance_meters"))
            dur = _num(a.get("moving_duration")) or _num(a.get("duration_seconds"))
            day = _day(a.get("date"))
            if not dist or not dur or not (low <= dist <= high):
                continue
            if _run_speed(a) is None:      # skip corrupt distance/duration pairs
                continue
            # Normalise the effort to the exact race distance before comparing.
            norm = dur * (target / dist) ** RIEGEL
            cand = {
                "seconds": round(norm),
                "date": day,
                "name": a.get("name") or "Run",
                "actual_km": round(dist / 1000.0, 2),
                "activity_id": str(a.get("activity_id")),
            }
            if all_time is None or norm < all_time["seconds"]:
                all_time = cand
            if day >= cutoff and (recent is None or norm < recent["seconds"]):
                recent = cand

        buckets.append({
            "label": label,
            "meters": target,
            "best": all_time,
            "recent": recent,
            "best_pace": _pace(all_time, target),
            "recent_pace": _pace(recent, target),
        })

        if all_time:
            score = all_time["seconds"] / (target ** RIEGEL)
            if best_overall is None or score < best_overall["score"]:
                best_overall = {"score": score, "label": label, **all_time,
                                "meters": target}

    # A true best-effort curve gets slower as distance grows. Where a shorter
    # distance is clearly slower than a longer one, the athlete has never
    # actually run that distance flat out — it appears in the history only as
    # warm-ups and cool-downs. Require a real margin so that two near-identical
    # paces are not mistaken for a signal.
    untested_margin = 0.05
    for i, bucket in enumerate(buckets):
        pace = bucket["best_pace"]
        faster_longer = [
            b["label"] for b in buckets[i + 1:]
            if b["best_pace"] is not None and pace is not None
            and b["best_pace"] < pace * (1 - untested_margin)
        ]
        bucket["untested"] = bool(faster_longer)
        bucket["untested_vs"] = faster_longer[0] if faster_longer else None

    # Project every distance from the single strongest effort.
    projections = []
    if best_overall:
        for label, target, _lo, _hi in DISTANCE_BUCKETS:
            secs = best_overall["seconds"] * (target / best_overall["meters"]) ** RIEGEL
            projections.append({
                "label": label,
                "meters": target,
                "seconds": round(secs),
                "pace": round(secs / (target / 1000.0) / 60.0, 2),
            })

    return {
        "buckets": buckets,
        "projections": projections,
        "anchor": best_overall,
        "recent_days": recent_days,
        "runs_considered": len(runs),
    }


def _pace(effort, meters) -> float | None:
    """Minutes per kilometre for a normalised effort."""
    if not effort or not meters:
        return None
    return round(effort["seconds"] / (meters / 1000.0) / 60.0, 2)


# ---------------------------------------------------------------------------
# #46 — Metric correlation matrix
# ---------------------------------------------------------------------------

CORRELATION_METRICS = [
    ("sleep_score", "Sleep score"),
    ("sleep_hours", "Sleep hours"),
    ("steps", "Steps"),
    ("stress", "Stress"),
    ("resting_hr", "Resting HR"),
    ("hrv", "HRV"),
    ("body_battery", "Body battery"),
    ("readiness", "Readiness"),
    ("load", "Training load"),
]


def build_correlations(health_data: dict, activities: list,
                       days: int = 90) -> dict:
    """
    Pearson correlation between daily metrics over the requested window, plus
    the scatter points behind each pair and the strongest relationships found.
    """
    hd = health_data or {}
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    by_day: dict[str, dict] = {}

    def collect(rows, mapping):
        for r in rows or []:
            day = _day(r.get("date"))
            if not day or day < cutoff:
                continue
            slot = by_day.setdefault(day, {})
            for key, fn in mapping.items():
                val = fn(r)
                if val is not None:
                    slot[key] = val

    collect(hd.get("daily_stats"), {
        "steps": lambda r: _num(r.get("steps")),
        "stress": lambda r: _num(r.get("stress_avg")),
        "resting_hr": lambda r: _num(r.get("resting_hr")),
        "body_battery": lambda r: _num(r.get("body_battery")),
    })
    collect(hd.get("sleep"), {
        "sleep_score": lambda r: _num(r.get("score")),
        "sleep_hours": lambda r: (
            round(_num(r.get("total_seconds")) / 3600, 2)
            if _num(r.get("total_seconds")) else None
        ),
    })
    collect(hd.get("hrv"), {"hrv": lambda r: _num(r.get("last_night_avg"))})
    collect(hd.get("training_readiness"), {
        "readiness": lambda r: _num(r.get("score")),
    })

    # Training load proxy: duration-weighted intensity for the day.
    for a in activities or []:
        day = _day(a.get("date"))
        if not day or day < cutoff:
            continue
        dur = _num(a.get("duration_seconds")) or 0
        hr = _num(a.get("avg_hr")) or 0
        load = dur / 60.0 * (hr / 100.0 if hr else 1.0)
        slot = by_day.setdefault(day, {})
        slot["load"] = round(slot.get("load", 0.0) + load, 1)

    for day, slot in by_day.items():
        slot.setdefault("load", 0.0)

    keys = [k for k, _ in CORRELATION_METRICS]
    labels = [lbl for _, lbl in CORRELATION_METRICS]
    dates = sorted(by_day)

    columns = {k: [by_day[d].get(k) for d in dates] for k in keys}

    matrix, pairs = [], []
    for i, a_key in enumerate(keys):
        row = []
        for j, b_key in enumerate(keys):
            r, n = _pearson(columns[a_key], columns[b_key])
            row.append(None if r is None else round(r, 3))
            if i < j and r is not None and n >= 10:
                pairs.append({
                    "a": labels[i], "b": labels[j],
                    "a_key": a_key, "b_key": b_key,
                    "r": round(r, 3), "n": n,
                    "strength": _strength(r),
                })
        matrix.append(row)

    pairs.sort(key=lambda p: -abs(p["r"]))

    return {
        "keys": keys,
        "labels": labels,
        "matrix": matrix,
        "dates": dates,
        "columns": columns,
        "top": pairs[:6],
        "days": days,
        "sample_days": len(dates),
    }


def _pearson(xs: list, ys: list):
    """Pearson r over pairs where both values are present. Returns (r, n)."""
    pairs = [(x, y) for x, y in zip(xs, ys)
             if isinstance(x, (int, float)) and isinstance(y, (int, float))]
    n = len(pairs)
    if n < 3:
        return None, n

    mean_x = sum(p[0] for p in pairs) / n
    mean_y = sum(p[1] for p in pairs) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    den_x = sum((x - mean_x) ** 2 for x, y in pairs)
    den_y = sum((y - mean_y) ** 2 for x, y in pairs)
    if den_x <= 0 or den_y <= 0:
        return None, n
    return num / math.sqrt(den_x * den_y), n


def _strength(r: float) -> str:
    a = abs(r)
    if a >= 0.7:
        return "strong"
    if a >= 0.4:
        return "moderate"
    if a >= 0.2:
        return "weak"
    return "negligible"


# ------------------------------------------------------------ #49 stress bands

#: Garmin's own four buckets. Reusing the watch's language means a band on this
#: chart means the same thing as a band on the athlete's wrist.
STRESS_BANDS = [
    {"key": "rest",   "label": "Rest",   "min": 0,  "max": 25},
    {"key": "low",    "label": "Low",    "min": 26, "max": 50},
    {"key": "medium", "label": "Medium", "min": 51, "max": 75},
    {"key": "high",   "label": "High",   "min": 76, "max": 100},
]

STRESS_INTERVAL_MIN = 3   # Garmin samples every 3 minutes: 480 slots per day.
_REST_CEILING = 25        # Top of the rest band.
_REST_GAP_TOL_MIN = 9     # Missing samples that still count as one window.
_MIN_REST_WINDOW_MIN = 15 # Shorter than this is noise, not a recovery window.
_SLOTS_PER_DAY = 1440 // STRESS_INTERVAL_MIN


def encode_stress_samples(pairs: list) -> str:
    """
    Pack a day's [minute, level] samples into one comma-separated line.

    Samples sit on a fixed 3-minute grid, so the minute is implied by position
    and only the level needs storing; an empty field means the watch did not
    score that slot. This exists because the cache is written with indent=2,
    which puts every element of a list on its own line: kept as pairs, 90 days
    of stress is 67,000 lines and 1.4 MB of mostly whitespace, which buries the
    rest of the cache when reading it by hand. As one string per day it is
    ~130 KB and stays greppable.
    """
    slots = [""] * _SLOTS_PER_DAY
    for minute, level in pairs or []:
        idx = int(minute) // STRESS_INTERVAL_MIN
        if 0 <= idx < _SLOTS_PER_DAY:
            slots[idx] = str(int(level))
    return ",".join(slots).rstrip(",")


def decode_stress_samples(value) -> list:
    """
    Unpack encode_stress_samples back into [minute, level] pairs.

    Also accepts the raw pair list, so a cache written before the packed format
    still renders instead of silently showing an empty chart.
    """
    if isinstance(value, list):
        return [[int(m), int(v)] for m, v in value
                if m is not None and v is not None]
    if not isinstance(value, str) or not value:
        return []
    out = []
    for idx, field in enumerate(value.split(",")):
        field = field.strip()
        if not field:
            continue
        try:
            out.append([idx * STRESS_INTERVAL_MIN, int(field)])
        except ValueError:
            continue
    return out


def _band_for(level: int) -> str:
    for band in STRESS_BANDS:
        if level <= band["max"]:
            return band["key"]
    return "high"


def _sleep_mask(sleep_rows: list):
    """
    Map each date to the minute ranges the athlete was asleep, plus the set of
    dates whose preceding night is actually known.

    A night that begins before midnight is split across the two dates it
    actually covers, because the stress axis is a single calendar day. Without
    the split, a 22:40 bedtime would leave the pre-midnight hours looking like
    waking calm.

    The known-nights set is what keeps the waking-rest figure honest. Some
    nights are missing from Garmin entirely, and on those days the small hours
    are indistinguishable from a calm morning, which is how an untreated day
    ends up reporting seven hours of "waking rest".
    """
    mask: dict = {}
    known: set = set()
    for row in sleep_rows or []:
        bed, wake = row.get("bedtime_local"), row.get("wake_local")
        if not bed or not wake:
            continue
        try:
            b = datetime.fromisoformat(str(bed))
            w = datetime.fromisoformat(str(wake))
        except ValueError:
            continue
        if w <= b:
            continue
        bd, wd = b.date().isoformat(), w.date().isoformat()
        bm, wm = b.hour * 60 + b.minute, w.hour * 60 + w.minute
        if bd == wd:
            mask.setdefault(bd, []).append((bm, wm))
        else:
            mask.setdefault(bd, []).append((bm, 1440))
            mask.setdefault(wd, []).append((0, wm))
        # The night that *ends* on a date is the one covering its small hours.
        known.add(wd)
    return mask, known


def _in_mask(minute: int, ranges: list) -> bool:
    return any(start <= minute <= end for start, end in ranges)


def _longest_rest_window(samples: list, asleep: list):
    """
    Longest unbroken waking stretch spent in the rest band.

    Sleep is excluded on purpose. Every athlete's longest calm stretch is the
    night, so including it would make this field report bedtime 365 days a year
    and say nothing about the waking day, which is the part they can change.

    An unmeasured gap ends the window rather than bridging it: claiming calm
    across minutes the watch never scored would be inventing the very thing the
    chart exists to show.
    """
    best = current = None
    for minute, level in samples:
        if level > _REST_CEILING or _in_mask(minute, asleep):
            current = None
            continue
        if current is not None and (minute - current[1]) <= _REST_GAP_TOL_MIN:
            current = (current[0], minute)
        else:
            current = (minute, minute)
        if best is None or (current[1] - current[0]) > (best[1] - best[0]):
            best = current

    if not best:
        return None
    # Each sample represents the interval that follows it, so the window runs to
    # the end of its final sample rather than to that sample's start. Clamp that
    # tail if it would reach into sleep, so a wind-down window does not appear
    # to overlap the night it ended in.
    end = best[1] + STRESS_INTERVAL_MIN
    for start, stop in asleep:
        if best[1] < start < end:
            end = start
    minutes = end - best[0]
    if minutes < _MIN_REST_WINDOW_MIN:
        return None
    return {"start": best[0], "end": end, "minutes": minutes}


def build_stress_bands(stress_rows: list, sleep_rows: list | None = None,
                       days: int = 28) -> dict:
    """
    Intraday stress timelines, banded, one entry per day (newest first).

    Returns raw samples rather than a pre-binned series so the UI can draw both
    the full-detail single-day timeline and the week grid from one payload.

    Coverage is reported per day because it is not incidental: a day the watch
    spent on the charger has a flattering average built from whatever fraction
    it did measure, and the chart should be able to say so instead of showing a
    calm day that never happened.
    """
    rows = [r for r in (stress_rows or []) if r.get("date")]
    rows.sort(key=lambda r: str(r["date"]), reverse=True)
    rows = rows[:max(1, days)]

    mask, known_nights = _sleep_mask(sleep_rows)
    slots_per_day = _SLOTS_PER_DAY
    days_out, all_rest = [], []

    for row in rows:
        samples = decode_stress_samples(row.get("samples"))
        counts = {b["key"]: 0 for b in STRESS_BANDS}
        for _, level in samples:
            counts[_band_for(level)] += 1

        measured = len(samples)
        levels = [v for _, v in samples]
        asleep = mask.get(row["date"], [])
        # Without the night, the small hours read as waking calm and the figure
        # becomes a measure of how long the athlete slept. Report nothing rather
        # than something confidently wrong.
        sleep_known = row["date"] in known_nights
        rest_window = (_longest_rest_window(samples, asleep)
                       if samples and sleep_known else None)
        if rest_window:
            all_rest.append(rest_window["minutes"])

        days_out.append({
            "date": row["date"],
            "samples": samples,
            "measured_samples": measured,
            # Percent of the 24h day actually scored. Off-wrist and
            # unmeasurable slots both count as missing.
            "coverage_pct": round(100 * measured / slots_per_day) if measured else 0,
            "off_wrist_pct": round(
                100 * (row.get("off_wrist_samples") or 0) / slots_per_day),
            "band_minutes": {
                k: v * STRESS_INTERVAL_MIN for k, v in counts.items()
            },
            "band_pct": {
                k: (round(100 * v / measured) if measured else 0)
                for k, v in counts.items()
            },
            # The cached average is Garmin's own; fall back to the samples when
            # a day was captured before that field existed.
            "avg": _num(row.get("avg")) if row.get("avg") is not None else (
                round(sum(levels) / measured) if measured else None),
            "max": _num(row.get("max")) if row.get("max") is not None else (
                max(levels) if levels else None),
            "rest_window": rest_window,
            "sleep_known": sleep_known,
            "asleep": asleep,
            "error": row.get("error"),
        })

    with_data = [d for d in days_out if d["measured_samples"]]
    return {
        "days": days_out,
        "bands": STRESS_BANDS,
        "interval_min": STRESS_INTERVAL_MIN,
        "rest_ceiling": _REST_CEILING,
        "days_with_data": len(with_data),
        "days_missing_sleep": len([d for d in with_data if not d["sleep_known"]]),
        "median_rest_minutes": _median(all_rest),
        "median_coverage_pct": _median([d["coverage_pct"] for d in with_data]),
        "has_sleep_mask": bool(mask),
    }
