"""checkin.py — daily habits and subjective journal.

Garmin measures what the body did. This module records what the athlete chose
to do and how they actually felt, which is the other half of coaching.

Two related things live here because they are captured in the same daily
moment and are stored in the same file:

  Habits  — user-defined behaviours (hydration, mobility, meditation) with a
            daily or weekly cadence, checked off each day, scored as streaks
            and completion rates.

  Journal — mood, energy, motivation and soreness on a 1-5 scale plus a free
            note, correlated against sleep, HRV and readiness.

Storage: checkin.json
  {
    "schema_version": 1,
    "habits":  [{id, name, icon, cadence, target_per_week, created, archived}],
    "entries": {"YYYY-MM-DD": {habits: {id: bool}, mood, energy,
                               motivation, soreness, note, updated}}
  }

All functions are pure with respect to the network and take plain dicts, so
they can be exercised directly against cached data.
"""

import json
import math
import uuid
from datetime import date, datetime, timedelta

from .paths import user_data_dir

STORE = user_data_dir() / "checkin.json"
SCHEMA_VERSION = 1

# Subjective scales. Soreness is inverse-valenced: 5 means very sore, which is
# bad, whereas 5 mood is good. Anything that ranks or colours these has to
# respect `good_high`, or it will cheerfully report the wrong conclusion.
JOURNAL_FIELDS = {
    "mood":       {"label": "Mood",       "good_high": True,  "icon": "🙂"},
    "energy":     {"label": "Energy",     "good_high": True,  "icon": "⚡"},
    "motivation": {"label": "Motivation", "good_high": True,  "icon": "🎯"},
    "soreness":   {"label": "Soreness",   "good_high": False, "icon": "💢"},
}

# Biometrics worth testing subjective ratings against.
BIOMETRIC_FIELDS = [
    ("sleep_score",  "Sleep score"),
    ("sleep_hours",  "Sleep hours"),
    ("hrv",          "HRV"),
    ("resting_hr",   "Resting HR"),
    ("readiness",    "Readiness"),
    ("body_battery", "Body battery"),
    ("stress",       "Stress"),
    ("steps",        "Steps"),
]

SUGGESTED_HABITS = [
    {"name": "Drink 2L water",   "icon": "💧", "cadence": "daily"},
    {"name": "Mobility / stretch", "icon": "🧘", "cadence": "daily"},
    {"name": "Strength session", "icon": "🏋️", "cadence": "weekly", "target_per_week": 2},
    {"name": "Meditate",         "icon": "🌿", "cadence": "daily"},
    {"name": "In bed by 22:30",  "icon": "🌙", "cadence": "daily"},
    {"name": "No alcohol",       "icon": "🚫", "cadence": "daily"},
]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _empty() -> dict:
    return {"schema_version": SCHEMA_VERSION, "habits": [], "entries": {}}


def _load() -> dict:
    if STORE.exists():
        try:
            raw = json.loads(STORE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("schema_version") == SCHEMA_VERSION:
                raw.setdefault("habits", [])
                raw.setdefault("entries", {})
                return raw
        except Exception:
            pass
    return _empty()


def _save(store: dict) -> None:
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(store, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def _today() -> str:
    return date.today().isoformat()


def _clamp_rating(value):
    """Ratings are 1-5 integers. Anything else becomes None rather than noise."""
    if value in (None, "", "null"):
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 5 else None


# ---------------------------------------------------------------------------
# Habit definitions
# ---------------------------------------------------------------------------

def load_habits(include_archived: bool = False) -> list[dict]:
    habits = _load()["habits"]
    if include_archived:
        return habits
    return [h for h in habits if not h.get("archived")]


def add_habit(data: dict) -> dict:
    cadence = "weekly" if (data.get("cadence") == "weekly") else "daily"
    target = data.get("target_per_week")
    try:
        target = int(target)
    except (TypeError, ValueError):
        target = None
    if cadence == "weekly":
        target = min(7, max(1, target or 3))
    else:
        target = 7

    habit = {
        "id": uuid.uuid4().hex[:12],
        "name": (data.get("name") or "").strip()[:80] or "Untitled habit",
        "icon": (data.get("icon") or "✅").strip()[:4],
        "cadence": cadence,
        "target_per_week": target,
        "created": _today(),
        "archived": False,
    }
    store = _load()
    store["habits"].append(habit)
    _save(store)
    return habit


def update_habit(habit_id: str, data: dict) -> dict | None:
    store = _load()
    for h in store["habits"]:
        if h["id"] == habit_id:
            if "name" in data:
                h["name"] = (data.get("name") or "").strip()[:80] or h["name"]
            if "icon" in data:
                h["icon"] = (data.get("icon") or h["icon"]).strip()[:4]
            if "archived" in data:
                h["archived"] = bool(data["archived"])
            if data.get("cadence") in ("daily", "weekly"):
                h["cadence"] = data["cadence"]
                if h["cadence"] == "daily":
                    h["target_per_week"] = 7
            if "target_per_week" in data and h["cadence"] == "weekly":
                try:
                    h["target_per_week"] = min(7, max(1, int(data["target_per_week"])))
                except (TypeError, ValueError):
                    pass
            _save(store)
            return h
    return None


def delete_habit(habit_id: str) -> None:
    """Archive rather than erase, so past completions stay in the record."""
    store = _load()
    for h in store["habits"]:
        if h["id"] == habit_id:
            h["archived"] = True
    _save(store)


# ---------------------------------------------------------------------------
# Daily entries
# ---------------------------------------------------------------------------

def get_entry(day: str | None = None) -> dict:
    day = day or _today()
    entry = _load()["entries"].get(day) or {}
    return {
        "date": day,
        "habits": entry.get("habits", {}),
        "mood": entry.get("mood"),
        "energy": entry.get("energy"),
        "motivation": entry.get("motivation"),
        "soreness": entry.get("soreness"),
        "note": entry.get("note", ""),
        "updated": entry.get("updated"),
    }


def save_entry(day: str, data: dict) -> dict:
    """
    Merge a partial check-in into the day's entry. Callers send only what
    changed, so a habit tick must never wipe the journal written earlier.
    """
    day = (day or _today())[:10]
    store = _load()
    entry = store["entries"].get(day) or {}

    if isinstance(data.get("habits"), dict):
        merged = dict(entry.get("habits") or {})
        for hid, done in data["habits"].items():
            merged[str(hid)] = bool(done)
        entry["habits"] = merged

    for field in JOURNAL_FIELDS:
        if field in data:
            entry[field] = _clamp_rating(data[field])

    if "note" in data:
        entry["note"] = (data.get("note") or "").strip()[:2000]

    entry["updated"] = datetime.now().isoformat(timespec="seconds")
    store["entries"][day] = entry
    _save(store)
    return get_entry(day)


def entries_between(start: str, end: str) -> dict:
    """Entries with `start` <= date <= `end`, keyed by ISO date."""
    return {d: e for d, e in _load()["entries"].items() if start <= d <= end}


# ---------------------------------------------------------------------------
# Streaks and adherence
# ---------------------------------------------------------------------------

def _days_back(n: int, end: date | None = None) -> list[str]:
    end = end or date.today()
    return [(end - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _iso_week(day: str) -> str:
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def _daily_streak(done_days: set, created: str, today: date) -> tuple[int, int]:
    """
    Current and best run of consecutive completed days.

    Today is given grace: an unchecked today does not break a streak, because
    the day is not over yet. Miss yesterday and the streak is gone.
    """
    cur = 0
    cursor = today
    if today.isoformat() not in done_days:
        cursor = today - timedelta(days=1)
    while cursor.isoformat() in done_days and cursor.isoformat() >= created:
        cur += 1
        cursor -= timedelta(days=1)

    best = 0
    run = 0
    if done_days:
        start = date.fromisoformat(min(done_days))
        cursor = start
        while cursor <= today:
            if cursor.isoformat() in done_days:
                run += 1
                best = max(best, run)
            else:
                run = 0
            cursor += timedelta(days=1)
    return cur, best


def _weekly_streak(done_days: set, target: int, today: date) -> tuple[int, int]:
    """
    Consecutive ISO weeks that met the weekly target.

    The current week is given grace the same way today is for daily habits: it
    is only counted once the target is met, and never counted as a miss.
    """
    per_week: dict[str, int] = {}
    for d in done_days:
        per_week[_iso_week(d)] = per_week.get(_iso_week(d), 0) + 1

    def week_key(offset: int) -> str:
        return _iso_week((today - timedelta(days=7 * offset)).isoformat())

    cur = 0
    offset = 0
    if per_week.get(week_key(0), 0) < target:
        offset = 1                      # this week is still in progress
    while per_week.get(week_key(offset), 0) >= target:
        cur += 1
        offset += 1

    best = 0
    run = 0
    if per_week:
        weeks = sorted(per_week)
        first = datetime.strptime(weeks[0] + "-1", "%G-W%V-%u").date()
        cursor = first
        while cursor <= today:
            if per_week.get(_iso_week(cursor.isoformat()), 0) >= target:
                run += 1
                best = max(best, run)
            else:
                run = 0
            cursor += timedelta(days=7)
    return cur, best


def habit_stats(days: int = 30, end: date | None = None) -> dict:
    """
    Per-habit streaks, completion rate and a recent grid for the UI, plus an
    overall adherence figure across every active habit.
    """
    end = end or date.today()
    store = _load()
    window = _days_back(days, end)
    entries = store["entries"]

    out = []
    total_done = total_expected = 0
    for h in load_habits():
        done_days = {
            d for d, e in entries.items()
            if (e.get("habits") or {}).get(h["id"]) and d <= end.isoformat()
        }
        created = h.get("created") or window[0]
        # Only days the habit actually existed count against it.
        eligible = [d for d in window if d >= created]
        hits = [d for d in eligible if d in done_days]

        if h.get("cadence") == "weekly":
            target = h.get("target_per_week") or 3
            cur, best = _weekly_streak(done_days, target, end)
            expected = max(1, round(len(eligible) * target / 7))
            unit = "wk"
        else:
            target = 7
            cur, best = _daily_streak(done_days, created, end)
            expected = len(eligible)
            unit = "d"

        rate = round(len(hits) / expected * 100) if expected else None
        total_done += len(hits)
        total_expected += expected

        out.append({
            **h,
            "streak": cur,
            "best_streak": best,
            "streak_unit": unit,
            "completed": len(hits),
            "expected": expected,
            "rate": rate,
            "grid": [{"date": d, "done": d in done_days, "eligible": d >= created}
                     for d in window],
        })

    out.sort(key=lambda h: (-(h["streak"] or 0), h["name"].lower()))
    return {
        "days": days,
        "dates": window,
        "habits": out,
        "adherence": round(total_done / total_expected * 100) if total_expected else None,
        "today": get_entry(end.isoformat()),
    }


# ---------------------------------------------------------------------------
# Journal series and correlations
# ---------------------------------------------------------------------------

def journal_series(days: int = 90, end: date | None = None) -> dict:
    """Dates plus one array per subjective field, for charting."""
    end = end or date.today()
    window = _days_back(days, end)
    entries = _load()["entries"]
    series = {
        f: [(entries.get(d) or {}).get(f) for d in window]
        for f in JOURNAL_FIELDS
    }
    counts = {f: sum(1 for v in series[f] if v is not None) for f in JOURNAL_FIELDS}
    rated = sum(
        1 for i, _ in enumerate(window)
        if any(series[f][i] is not None for f in JOURNAL_FIELDS)
    )
    return {"dates": window, "series": series, "counts": counts,
            "entries": rated}


def _biometric_columns(health_data: dict, dates: list[str]) -> dict:
    """Line up daily biometrics with the journal's date axis."""
    hd = health_data or {}
    by_day: dict[str, dict] = {d: {} for d in dates}

    for row in hd.get("daily_stats") or []:
        d = (row.get("date") or "")[:10]
        if d in by_day:
            by_day[d]["steps"] = row.get("steps")
            by_day[d]["stress"] = row.get("stress_avg")
            by_day[d]["resting_hr"] = row.get("resting_hr")
            by_day[d]["body_battery"] = row.get("body_battery")

    for row in hd.get("sleep") or []:
        d = (row.get("date") or "")[:10]
        if d in by_day:
            by_day[d]["sleep_score"] = row.get("score")
            secs = row.get("total_seconds")
            by_day[d]["sleep_hours"] = round(secs / 3600, 2) if secs else None

    for row in hd.get("hrv") or []:
        d = (row.get("date") or "")[:10]
        if d in by_day:
            by_day[d]["hrv"] = row.get("last_night_avg") or row.get("weekly_avg")

    for row in hd.get("training_readiness") or []:
        d = (row.get("date") or "")[:10]
        if d in by_day:
            by_day[d]["readiness"] = row.get("score")

    return {key: [by_day[d].get(key) for d in dates] for key, _ in BIOMETRIC_FIELDS}


def _pearson(xs: list, ys: list):
    """Pearson r over pairs where both values are present. Returns (r, n)."""
    pairs = [(x, y) for x, y in zip(xs, ys)
             if isinstance(x, (int, float)) and isinstance(y, (int, float))]
    n = len(pairs)
    if n < 5:
        return None, n
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = sum((x - mx) ** 2 for x, _ in pairs)
    dy = sum((y - my) ** 2 for _, y in pairs)
    if dx <= 0 or dy <= 0:
        return None, n
    return num / math.sqrt(dx * dy), n


def _strength(r: float) -> str:
    a = abs(r)
    if a >= 0.7:
        return "strong"
    if a >= 0.4:
        return "moderate"
    if a >= 0.2:
        return "weak"
    return "negligible"


def journal_correlations(health_data: dict, days: int = 90,
                         lag: int = 0, end: date | None = None) -> dict:
    """
    Correlate each subjective rating against each biometric.

    `lag` shifts the biometric earlier than the rating, so lag=1 answers "does
    last night's sleep predict how I feel today?" rather than merely describing
    the same day twice.
    """
    end = end or date.today()
    js = journal_series(days, end)
    dates = js["dates"]

    if lag:
        shifted = [(date.fromisoformat(d) - timedelta(days=lag)).isoformat()
                   for d in dates]
        bio = _biometric_columns(health_data, shifted)
    else:
        bio = _biometric_columns(health_data, dates)

    rows = []
    findings = []
    for f, meta in JOURNAL_FIELDS.items():
        cells = []
        for key, label in BIOMETRIC_FIELDS:
            r, n = _pearson(js["series"][f], bio[key])
            cells.append({"metric": key, "label": label,
                          "r": None if r is None else round(r, 3), "n": n})
            if r is not None and abs(r) >= 0.3 and n >= 10:
                findings.append({
                    "subjective": meta["label"], "metric": label,
                    "r": round(r, 3), "n": n, "strength": _strength(r),
                    "direction": "higher" if r > 0 else "lower",
                    "good_high": meta["good_high"],
                })
        rows.append({"field": f, "label": meta["label"],
                     "good_high": meta["good_high"], "cells": cells})

    findings.sort(key=lambda f: -abs(f["r"]))
    return {
        "days": days, "lag": lag, "rows": rows,
        "metrics": [{"key": k, "label": l} for k, l in BIOMETRIC_FIELDS],
        "findings": findings[:6],
        "entries": js["entries"],
        # Below this, correlations are noise dressed up as insight.
        "enough_data": js["entries"] >= 10,
    }


# ---------------------------------------------------------------------------
# Summaries for the coach and the weekly review
# ---------------------------------------------------------------------------

def _avg(values: list):
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 1) if vals else None


def week_summary(start: date, end: date) -> dict:
    """Habit adherence and subjective averages for one week."""
    entries = entries_between(start.isoformat(), end.isoformat())
    stats = habit_stats(days=(end - start).days + 1, end=end)
    return {
        "habit_adherence": stats["adherence"],
        "habits": [
            {"name": h["name"], "completed": h["completed"],
             "expected": h["expected"], "streak": h["streak"]}
            for h in stats["habits"]
        ],
        "checkins": len(entries),
        **{f"avg_{f}": _avg([e.get(f) for e in entries.values()])
           for f in JOURNAL_FIELDS},
        "notes": [e["note"] for e in entries.values() if e.get("note")],
    }


def format_for_prompt(days: int = 14) -> str:
    """Habit adherence and recent subjective state, for the coach's context."""
    stats = habit_stats(days=days)
    js = journal_series(days)
    if not stats["habits"] and not js["entries"]:
        return ""

    lines = []
    if stats["habits"]:
        lines.append(f"HABITS (last {days} days, {stats['adherence']}% adherence):")
        for h in stats["habits"]:
            streak = (f"{h['streak']}{h['streak_unit']} streak"
                      if h["streak"] else "no active streak")
            lines.append(f"  - {h['name']}: {h['completed']}/{h['expected']} "
                         f"· {streak} · best {h['best_streak']}{h['streak_unit']}")

    if js["entries"]:
        avgs = []
        for f, meta in JOURNAL_FIELDS.items():
            a = _avg(js["series"][f])
            if a is not None:
                avgs.append(f"{meta['label'].lower()} {a}/5")
        if avgs:
            lines.append(f"SUBJECTIVE CHECK-INS ({js['entries']} of last {days} days): "
                         + ", ".join(avgs)
                         + " (soreness: higher is worse)")
        recent = [(d, (_load()["entries"].get(d) or {}).get("note"))
                  for d in reversed(js["dates"])]
        notes = [f'{d}: "{n}"' for d, n in recent if n][:3]
        if notes:
            lines.append("  Recent notes: " + " | ".join(notes))

    return "\n".join(lines)
