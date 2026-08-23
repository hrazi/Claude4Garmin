"""weekly_review.py — Weekly review synthesis across the four pillars.

Aggregates the last 7 days of training, sleep, stress/HRV, and nutrition into a
compact stats block, asks the AI (via an injected `ask` callable, so this module
stays provider-agnostic and never pollutes the live chat history) for a holistic
review with 1-3 concrete focus items, and persists past reviews so the coach can
reference "last week you said…".

Reviews are stored in data/weekly_reviews.json (gitignored).
"""

import json
from datetime import date, datetime, timedelta
from statistics import mean

from .paths import user_data_dir

STORE = user_data_dir() / "weekly_reviews.json"
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Week window helpers
# ---------------------------------------------------------------------------

def week_start_for(d: date | None = None) -> date:
    """Monday of the week containing d (default: today)."""
    d = d or date.today()
    return d - timedelta(days=d.weekday())


def _in_range(day: str, start: date, end: date) -> bool:
    try:
        dd = datetime.fromisoformat(day[:10]).date()
    except (ValueError, TypeError):
        return False
    return start <= dd <= end


# ---------------------------------------------------------------------------
# Stats aggregation
# ---------------------------------------------------------------------------

def build_week_stats(health_data: dict, activities: list[dict],
                     nutrition_data: dict, start: date, end: date) -> dict:
    hd = health_data or {}

    def vals(rows, key):
        return [r.get(key) for r in rows or []
                if r.get("date") and _in_range(r["date"], start, end)
                and isinstance(r.get(key), (int, float))]

    acts = [a for a in activities or [] if _in_range(a.get("date") or "", start, end)]
    runs = [a for a in acts if "run" in (a.get("type") or "").lower()]
    run_km = round(sum((a.get("distance_meters") or 0) for a in runs) / 1000, 1)
    total_min = round(sum((a.get("moving_duration") or a.get("duration_seconds") or 0)
                          for a in acts) / 60)

    steps = vals(hd.get("daily_stats"), "steps")
    rhr = vals(hd.get("daily_stats"), "resting_hr")
    stress = vals(hd.get("daily_stats"), "stress_avg")
    sleep_h = [s / 3600 for s in vals(hd.get("sleep"), "total_seconds")]
    sleep_score = vals(hd.get("sleep"), "score")
    ready = vals(hd.get("training_readiness"), "score")

    nut = {}
    if nutrition_data:
        cals = [d.get("calories") for k, d in nutrition_data.items()
                if _in_range(k, start, end) and isinstance(d.get("calories"), (int, float))]
        prot = [d.get("protein") for k, d in nutrition_data.items()
                if _in_range(k, start, end) and isinstance(d.get("protein"), (int, float))]
        if cals:
            nut["avg_calories"] = round(mean(cals))
        if prot:
            nut["avg_protein"] = round(mean(prot))

    def avg(x):
        return round(mean(x), 1) if x else None

    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "activities": len(acts), "runs": len(runs),
        "run_km": run_km, "active_minutes": total_min,
        "avg_steps": round(mean(steps)) if steps else None,
        "avg_resting_hr": avg(rhr),
        "avg_stress": avg(stress),
        "avg_sleep_hours": avg(sleep_h),
        "avg_sleep_score": avg(sleep_score),
        "avg_readiness": avg(ready),
        "nutrition": nut or None,
    }


def _stats_text(st: dict, units: str = "km") -> str:
    # Stats are stored canonically in km; only the wording shown to the AI
    # switches, so historic reviews stay comparable.
    dist = st["run_km"]
    if units == "mi":
        dist = round(dist * 1000 / 1609.344, 1)
    lines = [f"Week {st['start']} to {st['end']}:"]
    lines.append(f"  Training: {st['activities']} activities ({st['runs']} runs), "
                 f"{dist} {units} running, {st['active_minutes']} active min")
    if st.get("avg_steps") is not None:      lines.append(f"  Steps/day avg: {st['avg_steps']}")
    if st.get("avg_resting_hr") is not None: lines.append(f"  Resting HR avg: {st['avg_resting_hr']} bpm")
    if st.get("avg_stress") is not None:     lines.append(f"  Stress avg: {st['avg_stress']}")
    if st.get("avg_sleep_hours") is not None:
        lines.append(f"  Sleep avg: {st['avg_sleep_hours']} h "
                     f"(score {st.get('avg_sleep_score')})")
    if st.get("avg_readiness") is not None:  lines.append(f"  Readiness avg: {st['avg_readiness']}/100")
    if st.get("nutrition"):
        n = st["nutrition"]
        lines.append(f"  Nutrition avg: {n.get('avg_calories','?')} kcal, "
                     f"{n.get('avg_protein','?')} g protein")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

PROMPT = (
    "You are my performance and life coach. Below are my aggregate stats for the past "
    "week across training, sleep, stress/recovery, and nutrition. Write a concise weekly "
    "review (about 150-220 words):\n"
    "1) A short read on how the week went overall.\n"
    "2) What's working and what's a risk (tie training load to recovery).\n"
    "3) Exactly 1-3 concrete, specific focus items for next week.\n"
    "Be direct and specific to the numbers. No preamble.\n\n{stats}\n"
)


def generate(health_data: dict, activities: list[dict], nutrition_data: dict,
             ask, start: date | None = None, units: str = "km") -> dict:
    """
    Build week stats, ask the AI for a review, persist and return the review.
    `ask` is a callable(prompt: str) -> str (provider-agnostic).
    """
    start = start or week_start_for()
    end = start + timedelta(days=6)
    stats = build_week_stats(health_data, activities, nutrition_data, start, end)
    text = ask(PROMPT.format(stats=_stats_text(stats, units))).strip()
    review = {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stats": stats,
        "text": text,
    }
    _save(review)
    return review


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _load_store() -> dict:
    if STORE.exists():
        try:
            raw = json.loads(STORE.read_text(encoding="utf-8"))
            if raw.get("schema_version") == SCHEMA_VERSION:
                return raw
        except Exception:
            pass
    return {"schema_version": SCHEMA_VERSION, "reviews": {}}


def _save(review: dict) -> None:
    store = _load_store()
    store["reviews"][review["week_start"]] = review
    try:
        STORE.write_text(json.dumps(store, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def list_reviews() -> list[dict]:
    """All saved reviews, newest week first."""
    store = _load_store()
    return sorted(store["reviews"].values(), key=lambda r: r["week_start"], reverse=True)


def get_review(week_start: str) -> dict | None:
    return _load_store()["reviews"].get(week_start)
