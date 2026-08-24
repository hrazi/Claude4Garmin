"""strava_client.py — OAuth and activity import for Strava.

Why this exists: the athlete records some sessions on a Garmin watch and others
on an Apple Watch. Only the Garmin ones reach Garmin Connect, so the local
history has real holes — three whole weeks of running were missing, which broke
a 72-week streak into a 45-week one. Strava receives uploads from both watches,
so it is the one place the full picture already exists.

Secrets live in the OS keychain via credentials_manager, never in a file and
never in the repo. Tokens are refreshed on demand; Strava access tokens last
six hours.

Nothing here writes to Strava. The scope requested is read-only.
"""

from __future__ import annotations

import time
from datetime import datetime

import requests

from . import credentials_manager as cm

API = "https://www.strava.com/api/v3"
OAUTH_AUTHORIZE = "https://www.strava.com/oauth/authorize"
OAUTH_TOKEN = "https://www.strava.com/oauth/token"

# activity:read_all is required to see activities the athlete has marked
# private. Without it an import looks like it succeeded while silently
# skipping exactly the sessions the athlete was least likely to notice missing.
SCOPE = "activity:read_all"

CLIENT_ID_KEY = "strava_client_id"
CLIENT_SECRET_KEY = "strava_client_secret"
ACCESS_TOKEN_KEY = "strava_access_token"
REFRESH_TOKEN_KEY = "strava_refresh_token"
EXPIRES_AT_KEY = "strava_expires_at"

STRAVA_CREDENTIAL_KEYS = (CLIENT_ID_KEY, CLIENT_SECRET_KEY)

PAGE_SIZE = 200          # Strava's maximum
MAX_PAGES = 40           # 8,000 activities; far beyond any real history
TIMEOUT = 20


class StravaError(RuntimeError):
    """Raised with a message already fit to show the athlete."""


# ---------------------------------------------------------------------------
# Credentials and tokens
# ---------------------------------------------------------------------------

def app_configured() -> bool:
    """Whether a client id and secret have been saved."""
    return all(cm.load_credential(k) for k in STRAVA_CREDENTIAL_KEYS)


def is_connected() -> bool:
    """Whether we hold a refresh token, i.e. the athlete has authorised us."""
    return bool(cm.load_credential(REFRESH_TOKEN_KEY))


def save_app_credentials(client_id: str, client_secret: str) -> None:
    cm.save_credential(CLIENT_ID_KEY, client_id.strip())
    cm.save_credential(CLIENT_SECRET_KEY, client_secret.strip())


def disconnect() -> None:
    """Forget the athlete's tokens but keep the app registration."""
    for key in (ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, EXPIRES_AT_KEY):
        cm.delete_credential(key)


def authorize_url(redirect_uri: str) -> str:
    """Build the consent URL the athlete visits to grant read access."""
    client_id = cm.load_credential(CLIENT_ID_KEY)
    if not client_id:
        raise StravaError("No Strava client ID saved yet.")
    from urllib.parse import urlencode
    return OAUTH_AUTHORIZE + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        # "force" so re-connecting after a scope change actually re-prompts
        # instead of silently handing back the old, narrower grant.
        "approval_prompt": "force",
        "scope": SCOPE,
    })


def _store_token_response(payload: dict) -> None:
    if payload.get("access_token"):
        cm.save_credential(ACCESS_TOKEN_KEY, payload["access_token"])
    if payload.get("refresh_token"):
        cm.save_credential(REFRESH_TOKEN_KEY, payload["refresh_token"])
    if payload.get("expires_at"):
        cm.save_credential(EXPIRES_AT_KEY, str(payload["expires_at"]))


def _explain(resp: requests.Response) -> str:
    """Turn a Strava error body into something worth showing a human."""
    try:
        body = resp.json()
    except Exception:
        return f"Strava returned HTTP {resp.status_code}."
    errors = body.get("errors") or []
    for err in errors:
        if err.get("field") == "Status" and err.get("code") == "Inactive":
            return ("Strava reports this API application as inactive. Strava now "
                    "requires an active Strava subscription on the account that "
                    "owns the API application before it will serve any data.")
        if err.get("resource") == "AuthorizationCode":
            return "That Strava authorisation code was rejected. Try connecting again."
    return f"{body.get('message') or 'Strava request failed'} (HTTP {resp.status_code})."


def exchange_code(code: str) -> dict:
    """Swap an authorisation code for tokens and persist them."""
    client_id = cm.load_credential(CLIENT_ID_KEY)
    client_secret = cm.load_credential(CLIENT_SECRET_KEY)
    if not (client_id and client_secret):
        raise StravaError("Strava client ID and secret must be saved first.")
    resp = requests.post(OAUTH_TOKEN, timeout=TIMEOUT, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    })
    if resp.status_code != 200:
        raise StravaError(_explain(resp))
    payload = resp.json()
    _store_token_response(payload)
    return payload


def _refresh() -> str:
    refresh_token = cm.load_credential(REFRESH_TOKEN_KEY)
    client_id = cm.load_credential(CLIENT_ID_KEY)
    client_secret = cm.load_credential(CLIENT_SECRET_KEY)
    if not (refresh_token and client_id and client_secret):
        raise StravaError("Strava is not connected yet.")
    resp = requests.post(OAUTH_TOKEN, timeout=TIMEOUT, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    if resp.status_code != 200:
        raise StravaError(_explain(resp))
    payload = resp.json()
    _store_token_response(payload)
    return payload["access_token"]


def access_token() -> str:
    """
    A usable access token, refreshed if it is close to expiring.

    The 120-second margin matters: a token that passes an expiry check and then
    expires mid-import would fail partway through, leaving a half-imported
    history that looks complete.
    """
    token = cm.load_credential(ACCESS_TOKEN_KEY)
    raw_expiry = cm.load_credential(EXPIRES_AT_KEY)
    try:
        expires_at = int(raw_expiry) if raw_expiry else 0
    except (TypeError, ValueError):
        expires_at = 0
    if token and expires_at and expires_at - 120 > time.time():
        return token
    if cm.load_credential(REFRESH_TOKEN_KEY):
        return _refresh()
    if token:
        return token
    raise StravaError("Strava is not connected yet.")


def _get(path: str, **params) -> list | dict:
    resp = requests.get(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {access_token()}"},
        params=params or None,
        timeout=TIMEOUT,
    )
    if resp.status_code == 429:
        raise StravaError("Strava rate limit reached. Try again in about 15 minutes.")
    if resp.status_code != 200:
        raise StravaError(_explain(resp))
    return resp.json()


def athlete() -> dict:
    """The connected athlete, used to prove the connection works."""
    return _get("/athlete")


# ---------------------------------------------------------------------------
# Activity mapping
# ---------------------------------------------------------------------------

# Strava sport types mapped onto the Garmin type keys the rest of the app
# already groups on, so an imported run lands in the same bucket as a recorded
# one rather than falling through to "other".
SPORT_TYPE_MAP = {
    "Run": "running",
    "TrailRun": "trail_running",
    "VirtualRun": "virtual_run",
    "Treadmill": "treadmill_running",
    "Ride": "cycling",
    "VirtualRide": "indoor_cycling",
    "MountainBikeRide": "mountain_biking",
    "GravelRide": "gravel_cycling",
    "EBikeRide": "cycling",
    "Swim": "lap_swimming",
    "Walk": "walking",
    "Hike": "hiking",
    "WeightTraining": "strength_training",
    "Crossfit": "strength_training",
    "Workout": "indoor_cardio",
    "Elliptical": "elliptical",
    "StairStepper": "indoor_cardio",
    "Rowing": "rowing_v2",
    "Yoga": "yoga",
    "AlpineSki": "resort_skiing_snowboarding_ws",
    "Snowboard": "resort_skiing_snowboarding_ws",
    "NordicSki": "cross_country_skiing_ws",
}


def _local_start(act: dict) -> str:
    """
    Strava's start_date_local is wall-clock time wearing a misleading 'Z'.

    It is the athlete's local time, not UTC, despite the suffix. Stripping the
    marker rather than parsing it as UTC keeps it directly comparable with
    Garmin's startTimeLocal, which matters because duplicate detection works by
    comparing the two.
    """
    raw = (act.get("start_date_local") or "").replace("Z", "").replace("T", " ")
    return raw[:19]


def normalize_activity(act: dict) -> dict:
    """Map one Strava activity onto the app's flat activity shape."""
    sport = act.get("sport_type") or act.get("type") or ""
    mapped = SPORT_TYPE_MAP.get(sport, (sport or "other").lower())

    # Strava has no treadmill sport type; indoor runs are flagged instead.
    if mapped == "running" and act.get("trainer"):
        mapped = "treadmill_running"

    start = _local_start(act)
    avg_speed = act.get("average_speed")
    return {
        # Namespaced so a Strava id can never be mistaken for a Garmin one by
        # the detail fetcher, which would otherwise ask Garmin for an id it has
        # never heard of.
        "activity_id":      f"strava_{act.get('id')}",
        "name":             act.get("name"),
        "type":             mapped,
        "date":             start[:10],
        "start_time":       start,
        "duration_seconds": act.get("elapsed_time"),
        "moving_duration":  act.get("moving_time"),
        "distance_meters":  act.get("distance"),
        "avg_hr":           act.get("average_heartrate"),
        "max_hr":           act.get("max_heartrate"),
        "calories":         act.get("calories"),
        "elevation_gain":   act.get("total_elevation_gain"),
        "avg_power":        act.get("average_watts"),
        "avg_cadence":      (act.get("average_cadence") * 2
                             if act.get("average_cadence") and mapped.endswith("running")
                             else act.get("average_cadence")),
        "avg_speed_mps":    avg_speed,
        "source":           "strava",
        "device_name":      act.get("device_name"),
        # Strava stamps Garmin-forwarded uploads with an external id of the
        # form garmin_push_<garminActivityId>, which is the strongest possible
        # duplicate signal: it names the exact Garmin activity.
        "external_id":      act.get("external_id"),
    }


def fetch_activities(after: int | None = None, progress=None) -> list[dict]:
    """
    Every activity Strava holds, newest first, normalised to our shape.

    `after` is an epoch second; pass it to fetch only newer activities on a
    repeat import.
    """
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        params = {"per_page": PAGE_SIZE, "page": page}
        if after:
            params["after"] = int(after)
        batch = _get("/athlete/activities", **params)
        if not isinstance(batch, list) or not batch:
            break
        out.extend(normalize_activity(a) for a in batch)
        if progress:
            progress(len(out))
        if len(batch) < PAGE_SIZE:
            break
    return out


def last_import_epoch(activities: list[dict]) -> int | None:
    """Epoch of the newest Strava-sourced row already held, for incremental pulls."""
    stamps = [a.get("start_time") for a in activities or []
              if a.get("source") == "strava" and a.get("start_time")]
    if not stamps:
        return None
    try:
        return int(datetime.fromisoformat(max(stamps)).timestamp())
    except (TypeError, ValueError):
        return None
