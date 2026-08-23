"""insights.py — Proactive trend alerts and daily nudges.

Scans the recent daily health data (steps, resting HR, sleep, stress, HRV,
training readiness) for meaningful threshold crossings and streaks, and returns
a list of alerts. These power the dashboard banner, the /api/insights endpoint,
and are injected into the coach's context so advice is grounded in what's
actually happening right now.

Pure functions, no I/O — takes the in-memory health_data dict.
"""

from statistics import median


SEVERITY_ORDER = {"warning": 0, "caution": 1, "positive": 2, "info": 3}


def _series(rows: list[dict], key: str) -> list[tuple[str, float]]:
    """Return [(date, value)] for rows that have a non-null numeric value, sorted by date."""
    out = []
    for r in rows or []:
        d, v = r.get("date"), r.get(key)
        if d and isinstance(v, (int, float)):
            out.append((d, float(v)))
    out.sort(key=lambda x: x[0])
    return out


def _alert(severity: str, metric: str, title: str, detail: str) -> dict:
    return {"severity": severity, "metric": metric, "title": title, "detail": detail}


def compute_alerts(health_data: dict | None) -> list[dict]:
    """
    Return a list of alert dicts, most severe first.

    Rules (all use strictly recent windows vs a longer baseline):
      - Resting HR up >=5 bpm (recent 3d avg vs prior 14d median)
      - Sleep score < 60 for 2+ consecutive nights
      - Average stress rising: recent 3d avg >= 10 pts over prior 14d median
      - HRV status Unbalanced/Low on the latest night
      - Training readiness LOW (or < 40) for 2+ consecutive days
      - Positive: readiness HIGH streak, or sleep 3 nights >= 80
    """
    hd = health_data or {}
    daily = hd.get("daily_stats") or []
    sleep = hd.get("sleep") or []
    hrv = hd.get("hrv") or []
    ready = hd.get("training_readiness") or []
    alerts: list[dict] = []

    # --- Resting HR climbing ---------------------------------------------
    rhr = _series(daily, "resting_hr")
    if len(rhr) >= 7:
        recent = [v for _, v in rhr[-3:]]
        baseline = [v for _, v in rhr[-17:-3]] or [v for _, v in rhr[:-3]]
        if baseline:
            delta = sum(recent) / len(recent) - median(baseline)
            if delta >= 5:
                alerts.append(_alert(
                    "warning", "resting_hr", "Resting heart rate is elevated",
                    f"Your last 3 days average {sum(recent)/len(recent):.0f} bpm — "
                    f"{delta:.0f} bpm above your recent baseline. This often signals "
                    f"fatigue, illness, or under-recovery. Consider an easier day."))

    # --- Sleep score low streak ------------------------------------------
    ss = _series(sleep, "score")
    low_nights = 0
    for _, v in reversed(ss):
        if v < 60:
            low_nights += 1
        else:
            break
    if low_nights >= 2:
        alerts.append(_alert(
            "warning", "sleep", f"Poor sleep {low_nights} nights running",
            f"Sleep score has been under 60 for {low_nights} consecutive nights. "
            f"Recovery and readiness will suffer — prioritise an earlier, longer night."))

    # --- Stress rising ----------------------------------------------------
    stress = _series(daily, "stress_avg")
    if len(stress) >= 7:
        recent = [v for _, v in stress[-3:]]
        baseline = [v for _, v in stress[-17:-3]] or [v for _, v in stress[:-3]]
        if baseline:
            delta = sum(recent) / len(recent) - median(baseline)
            if delta >= 10:
                alerts.append(_alert(
                    "caution", "stress", "Average stress is trending up",
                    f"Your last 3 days average {sum(recent)/len(recent):.0f} stress — "
                    f"{delta:.0f} points above baseline. Watch load, caffeine, and wind-down."))

    # --- HRV status -------------------------------------------------------
    if hrv:
        latest = sorted((h for h in hrv if h.get("date")), key=lambda h: h["date"])
        if latest:
            st = (latest[-1].get("status") or "").upper()
            if st in ("UNBALANCED", "LOW", "POOR"):
                alerts.append(_alert(
                    "caution", "hrv", f"HRV status: {st.title()}",
                    "Overnight HRV is outside your balanced range, a sign your nervous "
                    "system is still recovering. Favour easy aerobic work today."))

    # --- Training readiness LOW streak -----------------------------------
    low_ready = 0
    rs = sorted((r for r in ready if r.get("date")), key=lambda r: r["date"])
    for r in reversed(rs):
        lvl = (r.get("level") or "").upper()
        score = r.get("score")
        is_low = lvl == "LOW" or (isinstance(score, (int, float)) and score < 40)
        if is_low:
            low_ready += 1
        else:
            break
    if low_ready >= 2:
        alerts.append(_alert(
            "warning", "readiness", f"Low training readiness {low_ready} days",
            f"Readiness has been LOW for {low_ready} days running. Your body is asking "
            f"for recovery — keep intensity down until it rebounds."))

    # --- Positives --------------------------------------------------------
    high_ready = 0
    for r in reversed(rs):
        if (r.get("level") or "").upper() in ("HIGH", "VERY_HIGH"):
            high_ready += 1
        else:
            break
    if high_ready >= 2:
        alerts.append(_alert(
            "positive", "readiness", f"Primed to train ({high_ready}-day streak)",
            f"Readiness has been HIGH for {high_ready} days — a great window for a "
            f"quality session or a hard effort if it fits your plan."))
    elif len(ss) >= 3 and all(v >= 80 for _, v in ss[-3:]):
        alerts.append(_alert(
            "positive", "sleep", "Sleep is dialled in",
            "Three straight nights of 80+ sleep score. Recovery is working in your favour."))

    alerts.sort(key=lambda a: SEVERITY_ORDER.get(a["severity"], 9))
    return alerts


def format_for_prompt(alerts: list[dict]) -> str:
    """Render alerts as a compact block for the coach's system context."""
    if not alerts:
        return ""
    lines = ["ACTIVE ALERTS (proactively raise these when relevant):"]
    for a in alerts:
        lines.append(f"  [{a['severity'].upper()}] {a['title']} — {a['detail']}")
    return "\n".join(lines)
