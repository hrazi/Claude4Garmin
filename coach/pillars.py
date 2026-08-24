"""
pillars.py — Five-pillar fitness radar (#45).

Scores aerobic fitness, recovery, sleep, consistency and activity on a common
0-100 scale so one shape says what a dozen separate charts say, and compares
the current window against an earlier one to show which direction each pillar
is moving.

Three decisions shape everything here:

1. **Fixed anchors, not self-referenced percentiles.** Scoring each window
   against its own distribution would force both epochs to look identical
   regardless of what actually changed, which defeats the entire comparison.
   Anchors come from published guidance where it exists (step counts, sleep
   duration, training frequency).

2. **Where population norms are meaningless, the baseline still spans both
   epochs.** HRV and running efficiency have no useful absolute scale — 45 ms
   is excellent for one person and poor for another. Those are scored against
   the athlete's own history, but that baseline is deliberately computed over
   a window enclosing *both* epochs. A baseline drawn from each window
   separately would measure each one with a different ruler and quietly
   invent movement that never happened.

3. **Recovery excludes sleep.** Garmin's training-readiness score is absent
   for this athlete and gets estimated locally from sleep, HRV and stress, so
   feeding it into Recovery would draw the Sleep axis twice under two names
   and make the radar look coherent while saying half as much. Recovery uses
   only signals independent of the sleep score: HRV, resting HR and body
   battery.

Every pillar reports the number of days behind it. Thin data returns None
rather than a confident-looking number on a chart that invites comparison.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# A pillar needs this fraction of its window populated before it is scored at
# all. Below it the axis is reported as unscored: a radar leg drawn from three
# days of data is indistinguishable from one drawn from thirty, which is
# exactly the kind of false confidence a single-glance chart encourages.
MIN_COVERAGE = 0.4

# An overall score needs this many pillars before it means anything. Averaging
# one or two surviving axes and printing it as "overall fitness" would let a
# gap in the data masquerade as a whole-athlete verdict.
MIN_PILLARS_FOR_OVERALL = 3

# Aerobic and HRV baselines are drawn from this many days back, which comfortably
# encloses both comparison windows (see decision 2 above).
BASELINE_DAYS = 730
HRV_BASELINE_DAYS = 180

# Physiologically implausible values, filtered before any averaging.
HR_MIN, HR_MAX = 30, 220
HRV_MIN, HRV_MAX = 5, 250

PILLARS = ("aerobic", "recovery", "sleep", "consistency", "activity")

PILLAR_LABELS = {
    "aerobic":     "Aerobic fitness",
    "recovery":    "Recovery",
    "sleep":       "Sleep",
    "consistency": "Consistency",
    "activity":    "Daily activity",
}


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _interp(value: float, anchors: list[tuple[float, float]]) -> float:
    """
    Map a raw metric onto 0-100 through documented anchor points.

    Anchors are (raw, score) pairs in ascending raw order; values between them
    interpolate linearly and values outside clamp to the end scores. Written
    out as anchors rather than a formula so each threshold can be argued with
    on its own terms instead of hiding inside a curve nobody can inspect.
    """
    if value is None:
        return None
    lo_raw, lo_score = anchors[0]
    if value <= lo_raw:
        return lo_score
    for hi_raw, hi_score in anchors[1:]:
        if value <= hi_raw:
            span = hi_raw - lo_raw
            if span <= 0:
                return hi_score
            return lo_score + (value - lo_raw) / span * (hi_score - lo_score)
        lo_raw, lo_score = hi_raw, hi_score
    return anchors[-1][1]


def _num(v):
    """Coerce to float, rejecting bools and anything unparseable."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None      # NaN check


def _mean(vals: list):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _median(vals: list):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def _pctile(vals: list, p: float):
    """Nearest-rank percentile; adequate for the sample sizes involved here."""
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    idx = min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))
    return vals[idx]


def _day(value) -> str:
    """Normalise assorted date shapes to YYYY-MM-DD."""
    if not value:
        return ""
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return str(value)[:10]


def _window(rows: list, start: str, end: str, key: str = "date") -> list:
    """Rows whose date falls in [start, end) — half-open so windows can abut."""
    return [r for r in rows or []
            if isinstance(r, dict) and start <= _day(r.get(key)) < end]


# ---------------------------------------------------------------------------
# Individual pillars
# ---------------------------------------------------------------------------

def _score_aerobic(runs: list, baseline: dict, days: int) -> dict:
    """
    Aerobic fitness from running efficiency: metres covered per heartbeat.

    Speed alone rewards a hard day and punishes an easy one, so it says more
    about intent than fitness. Distance per heartbeat is close to effort-neutral
    — covering more ground for the same cardiac cost is the thing that actually
    improves as aerobic fitness improves.

    Scored against the athlete's own two-year p10-p90 range, since metres per
    beat has no meaningful absolute scale across people. The range maps to
    25-90 rather than 0-100 deliberately: matching your own two-year best is
    excellent but not perfection, and your own worst day is not the absence of
    fitness.
    """
    vals = [r["eff"] for r in runs]
    if not vals:
        return {"score": None, "reason": "No runs with heart rate in this window.",
                "n": 0, "coverage": 0.0}

    lo, hi = baseline.get("p10"), baseline.get("p90")
    cur = _median(vals)

    if lo is None or hi is None or hi <= lo:
        return {"score": None, "n": len(vals), "coverage": 1.0,
                "reason": "Not enough running history to establish a baseline."}

    score = _interp(cur, [(lo, 25.0), (hi, 90.0)])
    return {
        "score": round(score, 1),
        "n": len(vals),
        "coverage": 1.0,
        "raw": round(cur, 3),
        "unit": "m/beat",
        "detail": f"{cur:.2f} m per heartbeat across {len(vals)} run"
                  f"{'' if len(vals) == 1 else 's'}",
    }


def _score_recovery(stats: list, hrv_rows: list, hrv_baseline: float,
                    rhr_baseline: float, days: int) -> dict:
    """
    Recovery from HRV and resting heart rate — deliberately not sleep, and
    deliberately not body battery.

    Both signals are scored as drift from the athlete's own baseline, because
    an absolute HRV or resting HR means nothing across people. The pillar is
    the mean of whichever have data, so a missing chest strap degrades
    confidence rather than silently dragging the axis down.

    Body battery is excluded despite looking like the obvious fit. The cached
    field is Garmin's ``bodyBatteryMostRecentValue``, which for any past day is
    the *last* reading of that day — an end-of-evening drained number that
    bottoms out at 5 on most days here. Averaging it scores how tired the
    athlete was at bedtime, not how recovered they are, and it would have
    pinned this axis near the floor no matter what the body was actually doing.
    """
    parts, detail = [], []

    hrv_vals = [v for v in (_num(r.get("last_night_avg")) for r in hrv_rows)
                if v is not None and HRV_MIN <= v <= HRV_MAX]
    if hrv_vals and hrv_baseline:
        ratio = _median(hrv_vals) / hrv_baseline
        # HRV is meaningful only as drift from your own norm: sustained
        # suppression is the classic overreaching signal.
        parts.append(_interp(ratio, [(0.80, 15.0), (0.92, 45.0), (1.00, 68.0),
                                     (1.10, 88.0), (1.25, 98.0)]))
        detail.append(f"HRV {_median(hrv_vals):.0f} ms vs {hrv_baseline:.0f} baseline")

    rhr_vals = [v for v in (_num(r.get("resting_hr")) for r in stats)
                if v is not None and HR_MIN <= v <= HR_MAX]
    if rhr_vals and rhr_baseline:
        # Inverted: an elevated resting heart rate means less recovered.
        delta = _median(rhr_vals) - rhr_baseline
        parts.append(_interp(delta, [(-6.0, 98.0), (-2.0, 85.0), (0.0, 68.0),
                                     (3.0, 42.0), (7.0, 12.0)]))
        detail.append(f"resting HR {_median(rhr_vals):.0f} vs {rhr_baseline:.0f} baseline")

    covered = max(len(hrv_vals), len(rhr_vals))
    coverage = covered / days if days else 0.0
    if not parts or coverage < MIN_COVERAGE:
        return {"score": None, "n": covered, "coverage": round(coverage, 2),
                "reason": "Not enough recovery data in this window."}

    return {"score": round(_mean(parts), 1), "n": covered,
            "coverage": round(coverage, 2), "detail": ", ".join(detail)}


def _score_sleep(rows: list, days: int) -> dict:
    """
    Sleep from Garmin's own score plus duration.

    The device score already folds in stages and disturbances, but it grades
    generously on nights that were simply too short, so duration is scored
    separately and weighted equally. Duration anchors follow the standard adult
    7-9 hour recommendation.
    """
    scores = [v for v in (_num(r.get("score")) for r in rows) if v is not None]
    hours = [v / 3600.0 for v in (_num(r.get("total_seconds")) for r in rows)
             if v is not None and v > 0]

    parts, detail = [], []
    if scores:
        parts.append(_mean(scores))
        detail.append(f"score {_mean(scores):.0f}/100")
    if hours:
        parts.append(_interp(_mean(hours), [(4.0, 10.0), (5.0, 30.0), (6.0, 55.0),
                                            (7.0, 85.0), (8.0, 100.0)]))
        h = _mean(hours)
        detail.append(f"{int(h)}h {int(round((h % 1) * 60)):02d}m average")

    covered = max(len(scores), len(hours))
    coverage = covered / days if days else 0.0
    if not parts or coverage < MIN_COVERAGE:
        return {"score": None, "n": covered, "coverage": round(coverage, 2),
                "reason": "Not enough nights recorded in this window."}

    return {"score": round(_mean(parts), 1), "n": covered,
            "coverage": round(coverage, 2), "detail": ", ".join(detail)}


def _score_consistency(acts: list, days: int, start: str, end: str,
                       has_history: bool = True) -> dict:
    """
    Consistency as training days per week, plus how many weeks were touched.

    Counts days rather than sessions so a triple-session Saturday cannot pass
    for a well-spread week, and blends in the fraction of weeks containing any
    activity so a single heroic block followed by nothing scores like what it
    is. Anchors treat 4-5 training days a week as strong, matching the usual
    endurance-training guidance.

    "Weeks" here are 7-day blocks measured back from the end of the window
    rather than calendar weeks, so the score cannot swing on which weekday the
    window happens to start.

    An empty window scores a real zero — for this pillar, absence of training
    IS the measurement. But that only holds when an activity history exists at
    all: if the log is entirely empty the cache never loaded, and scoring that
    as "zero training" would invent a fact from a missing file.
    """
    if days <= 0:
        return {"score": None, "n": 0, "coverage": 0.0, "reason": "Empty window."}
    if not has_history:
        return {"score": None, "n": 0, "coverage": 0.0,
                "reason": "No activity history available."}

    active_days = {_day(a.get("date")) for a in acts
                   if isinstance(a, dict) and _day(a.get("date"))}
    per_week = len(active_days) / days * 7.0

    # Weeks are 7-day blocks counted back from the end of the window, NOT
    # calendar weeks. A 30-day window straddles five or six ISO weeks
    # depending only on which weekday it happens to begin, so keying on the
    # calendar made an identical training pattern score 72.9 in one window and
    # 80.9 in another. Blocks anchored to the window end are the same shape
    # every time, so the number moves only when the athlete does.
    weeks = max(1, days // 7)
    touched = set()
    try:
        end_d = date.fromisoformat(end)
        for d in active_days:
            try:
                offset = (end_d - date.fromisoformat(d)).days
            except ValueError:
                continue
            if offset < 0:
                continue
            # Days in the leftover remainder at the far end fold into the
            # oldest block rather than forming a stub block of their own: a
            # 2-day "week" is far less likely to contain training and would
            # read as a gap the athlete never had.
            touched.add(min(offset // 7, weeks - 1))
    except ValueError:
        pass
    week_frac = min(1.0, len(touched) / weeks)

    day_score = _interp(per_week, [(0.0, 0.0), (1.0, 22.0), (2.0, 45.0),
                                   (3.0, 66.0), (4.0, 83.0), (5.0, 93.0), (7.0, 100.0)])
    score = day_score * 0.75 + week_frac * 100.0 * 0.25

    return {
        "score": round(score, 1),
        "n": len(active_days),
        "coverage": 1.0,      # absence of activity is data, not a gap
        "raw": round(per_week, 2),
        "unit": "days/week",
        "detail": f"{per_week:.1f} training days per week, "
                  f"{len(touched)} of {weeks} weeks active",
    }


def _score_activity(stats: list, days: int) -> dict:
    """
    Daily activity from step count — the movement outside deliberate training.

    Kept separate from Consistency on purpose: a person can train hard four
    times a week and still spend the rest of it sitting, and those are
    different problems with different fixes. Anchors follow the step-count
    literature, where the mortality curve bends sharply up to roughly 7-8k
    and flattens well before the folk-wisdom 10k.
    """
    steps = [v for v in (_num(r.get("steps")) for r in stats) if v is not None and v > 0]
    coverage = len(steps) / days if days else 0.0

    if not steps or coverage < MIN_COVERAGE:
        return {"score": None, "n": len(steps), "coverage": round(coverage, 2),
                "reason": "Not enough step data in this window."}

    avg = _mean(steps)
    score = _interp(avg, [(1000.0, 5.0), (3000.0, 25.0), (5000.0, 45.0),
                          (7500.0, 70.0), (10000.0, 88.0), (12500.0, 96.0),
                          (15000.0, 100.0)])
    return {"score": round(score, 1), "n": len(steps), "coverage": round(coverage, 2),
            "raw": round(avg), "unit": "steps/day",
            "detail": f"{avg:,.0f} steps per day"}


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------

def _run_efficiency(activities: list, since: str) -> list:
    """
    Metres per heartbeat for every qualifying run since a given date.

    Mirrors analytics.build_efficiency's filters — runs only, at least ten
    minutes, plausible heart rate — so the radar and the efficiency chart can
    never disagree about the same runs.
    """
    from .analytics import _is_run, _run_speed

    out = []
    for a in activities or []:
        # Rows arrive from a cache file, so a malformed entry is a data
        # problem, not a reason to take the whole radar down with it.
        if not isinstance(a, dict) or not _is_run(a):
            continue
        day = _day(a.get("date"))
        if not day or day < since:
            continue
        speed = _run_speed(a)
        hr = _num(a.get("avg_hr"))
        dur = _num(a.get("duration_seconds")) or 0
        if not speed or not hr or not (HR_MIN <= hr <= HR_MAX) or dur < 600:
            continue
        out.append({"date": day, "eff": speed / hr * 60.0})
    return out


def _earliest_health_day(hd: dict) -> str:
    """Oldest date present in any day-indexed health series."""
    days = []
    for key in ("daily_stats", "sleep", "hrv"):
        for row in hd.get(key) or []:
            d = _day(row.get("date")) if isinstance(row, dict) else ""
            if d:
                days.append(d)
    return min(days) if days else ""


def build_pillars(health_data: dict, activities: list,
                  days: int = 30, lookback: int = 60) -> dict:
    """
    Score the five pillars for the current window and for one `lookback` days
    earlier, so the radar can draw both.

    days     — length of each comparison window (default 30).
    lookback — how far back the comparison window starts (default 60, so it
               covers days 60-90 ago).

    The default is 60 rather than the issue's literal "90 days ago" because the
    health archive holds exactly 90 days: a window starting 90 days back sits
    entirely before the oldest record, and recovery, sleep and activity all
    came back unscored while the chart still looked willing to draw. Sixty puts
    the comparison on the oldest 30 days actually held, which is the furthest
    honest comparison available.

    If the requested window still reaches past the archive it is shifted
    forward onto real data and flagged with ``shifted``, so the page can say
    which period it actually compared rather than quietly labelling the wrong
    dates.

    Windows are half-open and never overlap: sharing even a few days would damp
    every delta toward zero and make real change look like noise.
    """
    hd = health_data or {}
    days = max(7, min(int(days or 30), 120))
    lookback = max(days, min(int(lookback or 60), 365))

    today = date.today()
    def iso(n): return (today - timedelta(days=n)).isoformat()

    prev_start, prev_end = iso(lookback + days), iso(lookback)
    earliest = _earliest_health_day(hd)
    shifted = False
    if earliest and prev_start < earliest:
        # Slide the comparison forward so it sits on real data, but never let
        # it touch the current window: an overlap would compare the period
        # against itself and report change that cannot exist.
        cap = iso(days)
        new_start = min(earliest, cap)
        new_end = min(
            (date.fromisoformat(new_start) + timedelta(days=days)).isoformat(), cap
        )
        if new_end > new_start:
            prev_start, prev_end, shifted = new_start, new_end, True

    windows = {
        "current": (iso(days), iso(0)),
        "previous": (prev_start, prev_end),
    }

    # Baselines span both windows on purpose — see decision 2 in the module
    # docstring. Drawing them per-window would measure each epoch with its own
    # ruler and manufacture movement that never happened.
    eff_all = _run_efficiency(activities, iso(BASELINE_DAYS))
    eff_baseline = {
        "p10": _pctile([r["eff"] for r in eff_all], 0.10),
        "p90": _pctile([r["eff"] for r in eff_all], 0.90),
        "n": len(eff_all),
    }
    hrv_baseline = _median([
        v for v in (_num(r.get("last_night_avg"))
                    for r in _window(hd.get("hrv"), iso(HRV_BASELINE_DAYS), iso(0)))
        if v is not None and HRV_MIN <= v <= HRV_MAX
    ])
    rhr_baseline = _median([
        v for v in (_num(r.get("resting_hr"))
                    for r in _window(hd.get("daily_stats"), iso(HRV_BASELINE_DAYS), iso(0)))
        if v is not None and HR_MIN <= v <= HR_MAX
    ])

    epochs = {}
    has_history = any(isinstance(a, dict) and _day(a.get("date"))
                      for a in activities or [])
    for name, (start, end) in windows.items():
        stats = _window(hd.get("daily_stats"), start, end)
        sleep = _window(hd.get("sleep"), start, end)
        hrv = _window(hd.get("hrv"), start, end)
        acts = _window(activities, start, end)
        runs = [r for r in eff_all if start <= r["date"] < end]

        epochs[name] = {
            "start": start,
            "end": end,
            "aerobic":     _score_aerobic(runs, eff_baseline, days),
            "recovery":    _score_recovery(stats, hrv, hrv_baseline, rhr_baseline, days),
            "sleep":       _score_sleep(sleep, days),
            "consistency": _score_consistency(acts, days, start, end, has_history),
            "activity":    _score_activity(stats, days),
        }

    # Deltas only where both epochs actually scored. Treating an unscored
    # pillar as zero would render a missing chest strap as a collapse in
    # fitness, which is precisely the false story this chart must not tell.
    deltas, scored, common = {}, [], []
    for key in PILLARS:
        cur = epochs["current"][key].get("score")
        prev = epochs["previous"][key].get("score")
        deltas[key] = round(cur - prev, 1) if cur is not None and prev is not None else None
        if cur is not None:
            scored.append(cur)
        if cur is not None and prev is not None:
            common.append((cur, prev))

    # An "overall" drawn from one or two surviving pillars is not an overall.
    # Below the gate, report nothing rather than a number that looks whole.
    overall = round(_mean(scored), 1) if len(scored) >= MIN_PILLARS_FOR_OVERALL else None
    prev_scored = [epochs["previous"][k]["score"] for k in PILLARS
                   if epochs["previous"][k].get("score") is not None]
    overall_prev = (round(_mean(prev_scored), 1)
                    if len(prev_scored) >= MIN_PILLARS_FOR_OVERALL else None)

    # The overall delta is computed over the pillars scored in BOTH epochs, not
    # by subtracting the two published means. Those means can rest on different
    # pillar sets, so differencing them would attribute a change of coverage to
    # a change of fitness.
    overall_delta = None
    if len(common) >= MIN_PILLARS_FOR_OVERALL:
        overall_delta = round(_mean([c for c, _ in common]) - _mean([p for _, p in common]), 1)

    return {
        "pillars": list(PILLARS),
        "labels": PILLAR_LABELS,
        "current": epochs["current"],
        "previous": epochs["previous"],
        "deltas": deltas,
        "overall": overall,
        "overall_previous": overall_prev,
        "overall_delta": overall_delta,
        "overall_basis": len(common),
        "window_days": days,
        "lookback_days": lookback,
        "comparison_shifted": shifted,
        "baseline": {
            "efficiency_runs": eff_baseline["n"],
            "hrv_ms": round(hrv_baseline, 1) if hrv_baseline else None,
            "resting_hr": round(rhr_baseline, 1) if rhr_baseline else None,
        },
        "unscored": [k for k in PILLARS if epochs["current"][k].get("score") is None],
    }
