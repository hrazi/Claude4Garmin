"""activity_dedupe.py — Recognising the same workout recorded twice.

The athlete wears a Garmin watch for most sessions and an Apple Watch for
others. Garmin uploads to Strava automatically, so the great majority of Strava
activities are the very same sessions already held locally. Importing Strava
without dedupe would double almost the entire history: every distance total,
every weekly bubble, every training-load figure, and the coach's own sense of
how much work the athlete is doing.

Two signals are used, strongest first.

1. Strava stamps Garmin-forwarded uploads with an external id shaped like
   "garmin_push_<garminActivityId>". That names the exact local activity, so it
   is an exact match rather than a guess.

2. Otherwise, overlap in time. The same session recorded on two wrists starts
   within a minute or two and lasts about as long. Older uploads and manual
   entries often carry no external id at all, so this fallback is what actually
   catches most of them.

The tolerances below are deliberately tight. A false positive silently discards
a real workout, which is the failure that motivated this whole feature, so the
rule requires agreement on start time AND on either duration or distance before
it will call two rows the same session.
"""

from __future__ import annotations

from datetime import datetime

# Two recordings of one session start within a couple of minutes. Fifteen
# minutes would also catch a run followed straight after by a ride, so it stays
# narrow.
START_TOLERANCE_S = 240

# Wrist devices disagree a little on both duration and distance, especially
# where one auto-pauses and the other does not. Twenty-five percent is loose
# enough for that and far too tight to merge genuinely different sessions.
RATIO_TOLERANCE = 0.25

# Below this, ratio comparisons stop meaning anything: a 40-second discrepancy
# on a two-minute walk is a huge proportion but tells us nothing.
MIN_COMPARABLE_S = 120

# Sport families that must agree before two rows can be the same session. A run
# and a ride starting together are two activities, not one.
_FAMILIES = {
    "running": "run", "treadmill_running": "run", "trail_running": "run",
    "track_running": "run", "indoor_running": "run", "virtual_run": "run",
    "cycling": "ride", "indoor_cycling": "ride", "mountain_biking": "ride",
    "gravel_cycling": "ride", "road_biking": "ride", "virtual_ride": "ride",
    "lap_swimming": "swim", "open_water_swimming": "swim", "swimming": "swim",
    "walking": "walk", "hiking": "walk",
}


def _family(activity_type: str | None) -> str:
    return _FAMILIES.get((activity_type or "").lower(), (activity_type or "").lower())


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    text = str(stamp).replace("T", " ").replace("Z", "").strip()[:19]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _num(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def _close(a, b, tolerance: float = RATIO_TOLERANCE) -> bool | None:
    """Whether two magnitudes agree within tolerance; None when uncomparable."""
    a, b = _num(a), _num(b)
    if not a or not b or a <= 0 or b <= 0:
        return None
    return abs(a - b) / max(a, b) <= tolerance


def garmin_id_from_external(external_id: str | None) -> str | None:
    """Pull the Garmin activity id out of Strava's 'garmin_push_1234' marker."""
    if not external_id:
        return None
    text = str(external_id)
    if not text.startswith("garmin_push_"):
        return None
    ident = text[len("garmin_push_"):].strip()
    # Some uploads append a file suffix, e.g. garmin_push_123456.fit
    ident = ident.split(".")[0]
    return ident or None


def find_duplicate(candidate: dict, existing: list[dict]) -> dict | None:
    """
    The already-held activity that `candidate` duplicates, or None.

    `existing` should be the rows for the candidate's own date and the day
    either side; passing the whole history works but is needlessly slow.
    """
    ext_id = garmin_id_from_external(candidate.get("external_id"))
    if ext_id:
        for row in existing:
            if str(row.get("activity_id") or "") == ext_id:
                return row

    start = _parse(candidate.get("start_time"))
    if not start:
        return None
    family = _family(candidate.get("type"))

    best, best_gap = None, None
    for row in existing:
        if _family(row.get("type")) != family:
            continue
        other = _parse(row.get("start_time"))
        if not other:
            continue
        gap = abs((start - other).total_seconds())
        if gap > START_TOLERANCE_S:
            continue

        dur_a = _num(candidate.get("duration_seconds"))
        dur_b = _num(row.get("duration_seconds"))
        long_enough = (dur_a or 0) >= MIN_COMPARABLE_S and (dur_b or 0) >= MIN_COMPARABLE_S

        dur_ok = _close(dur_a, dur_b) if long_enough else None
        dist_ok = _close(candidate.get("distance_meters"), row.get("distance_meters"))

        # Starting together is strong evidence but not proof, so at least one
        # magnitude must agree too. Where neither can be compared (a short or
        # distance-free session) the start-time match is allowed to stand on
        # its own, since two sessions of the same sport beginning within four
        # minutes of each other is not a real training pattern.
        if dur_ok is False or dist_ok is False:
            continue
        if dur_ok is None and dist_ok is None and gap > 60:
            continue

        if best_gap is None or gap < best_gap:
            best, best_gap = row, gap
    return best


def merge_imported(existing: list[dict] | None,
                   incoming: list[dict] | None) -> tuple[list[dict], dict]:
    """
    Fold imported activities into the held history, skipping duplicates.

    Returns (merged, report). The report counts what happened and keeps a small
    sample of matches so the result can be inspected rather than trusted.
    """
    held = list(existing or [])
    by_day: dict[str, list[dict]] = {}
    for row in held:
        day = str(row.get("date") or "")[:10]
        if day:
            by_day.setdefault(day, []).append(row)

    report = {"added": 0, "duplicates": 0, "skipped": 0,
              "by_external_id": 0, "by_overlap": 0, "samples": []}
    added: list[dict] = []

    for row in incoming or []:
        day = str(row.get("date") or "")[:10]
        if not day:
            report["skipped"] += 1
            continue

        # Look either side of midnight: a session starting at 23:50 on one
        # device can be stamped just after midnight on the other.
        neighbourhood: list[dict] = []
        base = _parse(day + " 00:00:00")
        if base:
            from datetime import timedelta
            for offset in (-1, 0, 1):
                neighbourhood.extend(
                    by_day.get((base + timedelta(days=offset)).date().isoformat(), []))
        else:
            neighbourhood = by_day.get(day, [])

        match = find_duplicate(row, neighbourhood)
        if match:
            report["duplicates"] += 1
            if garmin_id_from_external(row.get("external_id")):
                report["by_external_id"] += 1
            else:
                report["by_overlap"] += 1
                if len(report["samples"]) < 8:
                    report["samples"].append({
                        "kept": f"{match.get('date')} {match.get('type')} "
                                f"{(_num(match.get('distance_meters')) or 0)/1000:.2f}km",
                        "skipped": f"{row.get('date')} {row.get('type')} "
                                   f"{(_num(row.get('distance_meters')) or 0)/1000:.2f}km",
                    })
            continue

        added.append(row)
        by_day.setdefault(day, []).append(row)
        report["added"] += 1

    merged = sorted(
        held + added,
        key=lambda r: (str(r.get("date") or ""), str(r.get("start_time") or "")),
        reverse=True,
    )
    return merged, report
