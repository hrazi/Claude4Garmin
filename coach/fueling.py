"""fueling.py — workout nutrition and hydration guidance.

Turns a session's duration and intensity into concrete carbohydrate, protein
and fluid targets, and flags days where intake looks too low for the work done.

Targets follow mainstream sports-nutrition consensus (ACSM / IOC / ISSN):

  During    < 60 min   no carbohydrate needed beyond normal meals
            60-150 min 30-60 g/h
            > 150 min  60-90 g/h, multiple transportable carbohydrate
  Before    ~1-1.5 g/kg carbohydrate 1-2 h before sessions over an hour
  After     0.3-0.4 g/kg protein, plus 1.0-1.2 g/kg/h carbohydrate when the
            next hard session is under about 8 hours away
  Daily     protein 1.6-2.2 g/kg; carbohydrate 3-5 g/kg easy, 5-7 g/kg
            moderate, 6-10 g/kg hard
  Fluid     400-800 mL/h during exercise, more in heat

Everything scales with body mass, which Garmin does not always provide. The
weight lookup falls back through several sources and, when all of them are
empty, the module says so rather than inventing a number.
"""

from datetime import date, datetime, timedelta

# Session intensity bands, keyed off average heart rate as a fraction of max.
HARD_HR_FRACTION = 0.80
MODERATE_HR_FRACTION = 0.70

# Grams of carbohydrate per hour of exercise.
CARB_PER_HOUR = {"none": (0, 0), "low": (30, 60), "high": (60, 90)}

RUN_TYPES = ("run", "treadmill", "trail")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def resolve_weight_kg(health_data: dict, nutrition_data: dict,
                      settings: dict) -> tuple[float | None, str]:
    """
    Body mass and where it came from. Ordered most to least authoritative:
    a Garmin scale reading, MacroFactor's trend weight, then whatever the
    athlete typed into their profile.
    """
    hd = health_data or {}
    for row in sorted(hd.get("body_composition") or [],
                      key=lambda r: r.get("date") or "", reverse=True):
        w = row.get("weight_kg")
        if isinstance(w, (int, float)) and 30 < w < 250:
            return float(w), "Garmin scale"

    for day in sorted((nutrition_data or {}).keys(), reverse=True):
        row = nutrition_data[day] or {}
        for key, label in (("trend_weight", "MacroFactor trend weight"),
                           ("weight", "MacroFactor weight")):
            w = row.get(key)
            if isinstance(w, (int, float)) and 30 < w < 250:
                return float(w), label

    profile = (settings or {}).get("athlete_profile") or {}
    try:
        w = float(profile.get("weight_kg"))
        if 30 < w < 250:
            return w, "profile"
    except (TypeError, ValueError):
        pass

    return None, "unknown"


def _hr_max(settings: dict, health_data: dict) -> int:
    """
    Estimated maximum heart rate. Uses age when the profile has it, otherwise
    the highest average HR actually seen in training, which is conservative but
    real, and finally a plain default.
    """
    profile = (settings or {}).get("athlete_profile") or {}
    try:
        age = int(profile.get("age"))
        if 10 < age < 100:
            return round(208 - 0.7 * age)      # Tanaka
    except (TypeError, ValueError):
        pass

    seen = [a.get("max_hr") for a in (health_data or {}).get("activities") or []
            if isinstance(a.get("max_hr"), (int, float)) and 100 < a["max_hr"] < 230]
    if seen:
        # The highest HR actually recorded is a floor for true max, not an
        # estimate of it, so allow a little headroom above the observed peak.
        return max(170, round(max(seen) * 1.02))

    avg_seen = [a.get("avg_hr") for a in (health_data or {}).get("activities") or []
                if isinstance(a.get("avg_hr"), (int, float)) and 60 < a["avg_hr"] < 220]
    if avg_seen:
        return max(180, round(max(avg_seen) / 0.92))
    return 185


def classify(activity: dict, hr_max: int) -> str:
    """
    Session intensity: easy, moderate or hard.

    Heart rate is used when present. Otherwise a run's pace-free fallback is
    its duration, since a long session is a meaningful fuelling load whatever
    the intensity.
    """
    hr = activity.get("avg_hr")
    if isinstance(hr, (int, float)) and hr > 0 and hr_max:
        frac = hr / hr_max
        if frac >= HARD_HR_FRACTION:
            return "hard"
        if frac >= MODERATE_HR_FRACTION:
            return "moderate"
        return "easy"

    minutes = (activity.get("duration_seconds") or 0) / 60
    if minutes >= 90:
        return "moderate"
    return "easy"


# ---------------------------------------------------------------------------
# Per-session plan
# ---------------------------------------------------------------------------

def _band(minutes: float) -> str:
    if minutes < 60:
        return "none"
    if minutes <= 150:
        return "low"
    return "high"


def _rng(lo: float, hi: float, unit: str) -> str:
    lo_r, hi_r = round(lo), round(hi)
    return f"{lo_r}-{hi_r} {unit}" if lo_r != hi_r else f"{lo_r} {unit}"


def session_plan(activity: dict, weight_kg: float | None, hr_max: int,
                 next_session_hours: float | None = None) -> dict:
    """Pre, during and post targets for a single session."""
    minutes = (activity.get("duration_seconds") or 0) / 60
    hours = minutes / 60
    intensity = classify(activity, hr_max)
    band = _band(minutes)
    lo_h, hi_h = CARB_PER_HOUR[band]

    plan = {
        "activity_id": activity.get("activity_id"),
        "name": activity.get("name") or activity.get("type") or "Session",
        "type": activity.get("type"),
        "date": (activity.get("date") or "")[:10],
        "minutes": round(minutes),
        "intensity": intensity,
        "carb_band": band,
    }

    # During
    if band == "none":
        plan["during"] = ("Under an hour: water is enough. No fuel needed "
                          "unless the session is at threshold or above.")
        plan["during_carbs_g"] = 0
    else:
        total_lo, total_hi = lo_h * hours, hi_h * hours
        plan["during"] = (f"{_rng(lo_h, hi_h, 'g/h')} carbohydrate "
                          f"(≈{_rng(total_lo, total_hi, 'g')} across the session)")
        plan["during_carbs_g"] = round((total_lo + total_hi) / 2)

    # Fluid: the low end for short or easy work, the high end for long or hard.
    fluid_lo, fluid_hi = (400, 600) if (hours < 1.5 and intensity != "hard") else (500, 800)
    plan["fluid_ml_per_h"] = f"{fluid_lo}-{fluid_hi} mL/h"
    plan["fluid_total_ml"] = round((fluid_lo + fluid_hi) / 2 * hours) if hours else 0
    if hours >= 1:
        plan["sodium"] = "300-600 mg sodium per litre, more if you finish salt-crusted"

    # Before and after both scale with body mass.
    if weight_kg:
        if minutes >= 60:
            plan["before"] = (f"{_rng(1.0 * weight_kg, 1.5 * weight_kg, 'g')} carbohydrate "
                              f"1-2 h before")
        else:
            plan["before"] = "Normal meals cover it. Train fasted only if that is deliberate."
        protein = _rng(0.3 * weight_kg, 0.4 * weight_kg, "g")
        if intensity == "easy" and minutes < 60:
            plan["after"] = f"{protein} protein within a couple of hours"
        else:
            carbs_after = _rng(1.0 * weight_kg, 1.2 * weight_kg, "g")
            urgent = next_session_hours is not None and next_session_hours <= 8
            plan["after"] = (
                f"{protein} protein plus {carbs_after} carbohydrate"
                + (" within the first hour: the next session is close"
                   if urgent else " over the next few hours")
            )
    else:
        plan["before"] = ("A carbohydrate-focused meal 1-2 h before"
                          if minutes >= 60 else "Normal meals cover it.")
        plan["after"] = ("Protein plus carbohydrate afterwards. Add your body "
                         "weight in Settings for gram targets.")

    return plan


# ---------------------------------------------------------------------------
# Daily targets and under-fuelling
# ---------------------------------------------------------------------------

def _load_band(minutes_today: float, hardest: str) -> str:
    if minutes_today >= 120 or hardest == "hard":
        return "hard"
    if minutes_today >= 45:
        return "moderate"
    return "easy"


DAILY_CARB_G_PER_KG = {"easy": (3, 5), "moderate": (5, 7), "hard": (6, 10)}


def daily_targets(weight_kg: float | None, minutes_today: float,
                  hardest: str) -> dict:
    band = _load_band(minutes_today, hardest)
    lo, hi = DAILY_CARB_G_PER_KG[band]
    out = {"load_band": band, "carb_g_per_kg": f"{lo}-{hi}", "protein_g_per_kg": "1.6-2.2"}
    if weight_kg:
        out["carbs"] = _rng(lo * weight_kg, hi * weight_kg, "g")
        out["protein"] = _rng(1.6 * weight_kg, 2.2 * weight_kg, "g")
        out["carbs_mid_g"] = round((lo + hi) / 2 * weight_kg)
        out["protein_mid_g"] = round(1.9 * weight_kg)
    return out


def under_fuelling(nutrition_data: dict, health_data: dict, weight_kg: float | None,
                   hr_max: int, days: int = 14) -> list[dict]:
    """
    Days where intake looks too low for the training done.

    Two independent checks, because either alone misses cases: energy intake
    well under expenditure, and protein under the minimum for an athlete.
    Only days with both nutrition and training data can be judged.
    """
    if not nutrition_data:
        return []

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    minutes_by_day: dict[str, float] = {}
    hardest_by_day: dict[str, str] = {}
    for a in (health_data or {}).get("activities") or []:
        d = (a.get("date") or "")[:10]
        if d < cutoff:
            continue
        minutes_by_day[d] = minutes_by_day.get(d, 0) + (a.get("duration_seconds") or 0) / 60
        level = classify(a, hr_max)
        order = {"easy": 0, "moderate": 1, "hard": 2}
        if order[level] > order.get(hardest_by_day.get(d, "easy"), 0):
            hardest_by_day[d] = level

    flags = []
    for day, row in sorted(nutrition_data.items(), reverse=True):
        if day < cutoff or not isinstance(row, dict):
            continue
        minutes = minutes_by_day.get(day, 0)
        if minutes < 30:
            continue                      # a rest day is allowed to be lighter

        kcal = row.get("calories")
        exp = row.get("expenditure")
        protein = row.get("protein")
        reasons = []

        if isinstance(kcal, (int, float)) and isinstance(exp, (int, float)) and exp > 0:
            deficit = exp - kcal
            if deficit > 500:
                reasons.append(f"{round(deficit)} kcal under expenditure")

        if weight_kg and isinstance(protein, (int, float)):
            floor = 1.6 * weight_kg
            if protein < floor * 0.85:
                reasons.append(f"{round(protein)} g protein, under the "
                               f"{round(floor)} g floor")

        if reasons:
            flags.append({
                "date": day,
                "minutes": round(minutes),
                "intensity": hardest_by_day.get(day, "easy"),
                "reasons": reasons,
                "calories": kcal,
                "expenditure": exp,
                "protein": protein,
            })

    return flags


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build(health_data: dict, nutrition_data: dict, settings: dict,
          days: int = 14) -> dict:
    """
    The full fuelling picture: today's sessions with per-session targets, daily
    macro targets, under-fuelling flags and any missing inputs.
    """
    hd = health_data or {}
    weight_kg, weight_source = resolve_weight_kg(hd, nutrition_data, settings)
    hr_max = _hr_max(settings, hd)
    today = date.today().isoformat()

    activities = sorted(
        (a for a in hd.get("activities") or [] if (a.get("date") or "")[:10]),
        key=lambda a: a["date"], reverse=True,
    )
    todays = [a for a in activities if (a.get("date") or "")[:10] == today]
    # With nothing logged today yet, the most recent session still tells the
    # athlete what to eat now, which is the question that actually gets asked.
    reference = todays or activities[:1]

    sessions = [session_plan(a, weight_kg, hr_max) for a in reference]
    minutes_today = sum(s["minutes"] for s in sessions) if todays else 0
    hardest = "easy"
    order = {"easy": 0, "moderate": 1, "hard": 2}
    for s in sessions if todays else []:
        if order[s["intensity"]] > order[hardest]:
            hardest = s["intensity"]

    missing = []
    if not weight_kg:
        missing.append("body weight (add it to your athlete profile in Settings, "
                       "or sync a Garmin scale)")
    if not nutrition_data:
        missing.append("nutrition data (import a MacroFactor CSV in Settings) — "
                       "without it, under-fuelling cannot be detected")

    return {
        "date": today,
        "weight_kg": round(weight_kg, 1) if weight_kg else None,
        "weight_source": weight_source,
        "hr_max": hr_max,
        "has_session_today": bool(todays),
        "sessions": sessions,
        "daily": daily_targets(weight_kg, minutes_today, hardest),
        "minutes_today": round(minutes_today),
        "flags": under_fuelling(nutrition_data, hd, weight_kg, hr_max, days),
        "missing": missing,
    }


def format_for_prompt(data: dict) -> str:
    """A compact fuelling block for the coach's system context."""
    if not data:
        return ""
    lines = []

    if data.get("sessions"):
        when = "today" if data.get("has_session_today") else "most recent session"
        lines.append(f"FUELLING ({when}):")
        for s in data["sessions"]:
            bits = [f"{s['name']} · {s['minutes']} min · {s['intensity']}"]
            if s.get("during_carbs_g"):
                bits.append(f"during: {s['during']}")
            if s.get("after"):
                bits.append(f"after: {s['after']}")
            lines.append("  - " + " · ".join(bits))

    d = data.get("daily") or {}
    if d.get("carbs"):
        lines.append(f"  Daily targets ({d['load_band']} load): {d['carbs']} carbs, "
                     f"{d['protein']} protein")
    elif d.get("load_band"):
        lines.append(f"  Daily targets ({d['load_band']} load): "
                     f"{d['carb_g_per_kg']} g/kg carbs, "
                     f"{d['protein_g_per_kg']} g/kg protein "
                     f"(body weight unknown)")

    flags = data.get("flags") or []
    if flags:
        lines.append(f"  Under-fuelled on {len(flags)} of the last training days, "
                     f"most recently {flags[0]['date']}: "
                     + "; ".join(flags[0]["reasons"]))

    return "\n".join(lines)
