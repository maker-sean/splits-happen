"""Discovery spike: call every Garmin Connect Personal API read endpoint and save raw responses.

Usage:  uv run python -I scripts/discover.py [--skip-probes] [--skip-burst]

Writes data/raw/<endpoint>/<date>.json envelopes (status, headers, body). data/raw/ is gitignored.
The API key and bearer token are never printed, logged, or written into envelopes.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv
from garmin_personal_api import GarminPersonalApiClient, PersonalApiAuthenticator
from garmin_personal_api.constants import TOKEN_PATH
from garmin_personal_api.errors import GarminPersonalApiError, HttpError

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
PACE_SECONDS = 0.35
TODAY = date.today()
WEEK = [TODAY - timedelta(days=i) for i in range(6, -1, -1)]  # 7 days, oldest first


# --------------------------------------------------------------------------- recording


class Recorder:
    """Response hook that captures every HTTP exchange the session makes."""

    def __init__(self) -> None:
        self.last: requests.Response | None = None
        self.log: list[dict[str, Any]] = []

    def hook(self, response: requests.Response, *args: Any, **kwargs: Any) -> None:
        self.last = response
        self.log.append(
            {
                "t": time.time(),
                "method": response.request.method,
                "path": requests.utils.urlparse(response.url).path,
                "status": response.status_code,
                "rate_headers": {
                    k: v
                    for k, v in response.headers.items()
                    if any(s in k.lower() for s in ("rate", "retry", "limit", "quota", "throttl"))
                },
            }
        )


def envelope(resp: requests.Response, slug: str, *, binary_path: str | None = None) -> dict:
    req = resp.request
    parsed = requests.utils.urlparse(resp.url)
    env: dict[str, Any] = {
        "endpoint": slug,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "request": {"method": req.method, "path": parsed.path, "query": parsed.query},
        "status": resp.status_code,
        "reason": resp.reason,
        "elapsed_ms": round(resp.elapsed.total_seconds() * 1000, 1),
        "headers": dict(resp.headers),
        "content_length": len(resp.content),
    }
    if parsed.path == TOKEN_PATH:
        env["body"] = "<redacted: bearer token>"
    elif binary_path:
        env["body"] = f"<binary saved to {binary_path}>"
    else:
        try:
            env["body"] = resp.json() if resp.content else None
        except ValueError:
            env["body"] = resp.text
    return env


def save(slug: str, name: str, env: dict) -> Path:
    path = RAW / slug / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(env, indent=2, default=str))
    return path


SUMMARY: list[dict[str, Any]] = []


def capture(
    rec: Recorder, slug: str, name: str, fn: Callable[[], Any], *, retry_on_429: bool = True
) -> requests.Response | None:
    """Run one client call, save whatever HTTP response came back (success or error)."""
    rec.last = None
    error = None
    try:
        fn()
    except HttpError as exc:
        error = f"HTTP {exc.status_code}"
    except (GarminPersonalApiError, requests.RequestException, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    resp = rec.last
    if resp is None:
        SUMMARY.append({"slug": slug, "name": name, "status": None, "error": error})
        print(f"  {slug:<34} {name:<24} NO RESPONSE  {error}")
        return None
    if resp.status_code == 429 and retry_on_429:
        wait = float(resp.headers.get("Retry-After", "30"))
        save(slug, f"{name}__429", envelope(resp, slug))
        print(f"  {slug:<34} {name:<24} 429 -> sleeping {wait}s (Retry-After={resp.headers.get('Retry-After')})")
        time.sleep(min(wait, 120))
        return capture(rec, slug, name, fn, retry_on_429=False)
    env = envelope(resp, slug)
    save(slug, name, env)
    SUMMARY.append(
        {
            "slug": slug,
            "name": name,
            "status": resp.status_code,
            "bytes": len(resp.content),
            "count": count_items(env["body"]),
            "error": error,
        }
    )
    print(f"  {slug:<34} {name:<24} {resp.status_code}  {len(resp.content):>8}B  count={count_items(env['body'])}")
    time.sleep(PACE_SECONDS)
    return resp


def count_items(body: Any) -> int | None:
    """Rough record count: top-level list length, or the longest list one level down."""
    if isinstance(body, list):
        return len(body)
    if isinstance(body, dict):
        lens = [len(v) for v in body.values() if isinstance(v, list)]
        return max(lens) if lens else None
    return None


# --------------------------------------------------------------------------- endpoints


@dataclass
class RangeEndpoint:
    slug: str
    path: str
    call: Callable[[GarminPersonalApiClient, date, date], Any]
    documented_cap_days: int | None
    wrapped: bool = True


@dataclass
class DateEndpoint:
    slug: str
    path: str
    call: Callable[[GarminPersonalApiClient, date], Any]
    wrapped: bool = True


@dataclass
class StaticEndpoint:
    slug: str
    path: str
    call: Callable[[GarminPersonalApiClient], Any]
    wrapped: bool = True


def raw(path: str, params: dict | None = None) -> Callable[[GarminPersonalApiClient], Any]:
    return lambda c: c.get(path, params=params)


RANGE_ENDPOINTS = [
    RangeEndpoint("stress_daily", "/usersummary-service/stats/stress/daily/{s}/{e}", lambda c, s, e: c.get_daily_stress(s, e), 28),
    RangeEndpoint("steps_daily", "/usersummary-service/stats/steps/daily/{s}/{e}", lambda c, s, e: c.get_daily_steps(s, e), 28),
    RangeEndpoint("hydration_daily", "/usersummary-service/stats/hydration/daily/{s}/{e}", lambda c, s, e: c.get_daily_hydration(s, e), 31),
    RangeEndpoint("weight_goals_effective", "/goal-service/goal/user/effective/weightgoal/{s}/{e}", lambda c, s, e: c.get_effective_weight_goals(s, e), None),
    RangeEndpoint("weight_range", "/weight-service/weight/range/{s}/{e}?includeAll=true", lambda c, s, e: c.get_weight_measurements(s, e), None),
    RangeEndpoint("weight_all", "/weight-service/weight/all/{s}/{e}", lambda c, s, e: c.get(f"/weight-service/weight/all/{s}/{e}"), 364, wrapped=False),
    RangeEndpoint("sleep_summaries", "/sleep-service/sleep/dailySleepSummariesByDate", lambda c, s, e: c.get_daily_sleep_summaries(s, e), 31),
    RangeEndpoint("body_battery_daily", "/wellness-service/wellness/bodyBattery/reports/daily", lambda c, s, e: c.get_daily_body_battery(s, e), 31),
    RangeEndpoint("daily_summaries_v2", "/usersummary-service/v2/dailysummaries", lambda c, s, e: c.get_daily_summaries(s, e), 366),
    RangeEndpoint("hrv_daily", "/hrv-service/hrv/daily/{s}/{e}", lambda c, s, e: c.get_daily_hrv_summaries(s, e), None),
    RangeEndpoint("vo2max_daily", "/metrics-service/metrics/maxmet/daily/{s}/{e}", lambda c, s, e: c.get_daily_vo2_max(s, e), None),
    RangeEndpoint("wellness_activity_summaries", "/wellnessactivity-service/activity/summary/range/{s}/{e}", lambda c, s, e: c.get_wellness_activity_summaries(s, e), None),
    RangeEndpoint("activities_search", "/activitylist-service/activities/search/activities", lambda c, s, e: c.search_activities(start_date=s, end_date=e, limit=100), None),
]

DATE_ENDPOINTS = [
    DateEndpoint("food_logs", "/nutrition-service/food/logs/{d}", lambda c, d: c.get_daily_food_logs(d)),
    DateEndpoint("sleep_daily", "/sleep-service/sleep/dailySleep", lambda c, d: c.get_daily_sleep(d)),
    DateEndpoint("hrv", "/hrv-service/hrv/{d}", lambda c, d: c.get_daily_hrv(d)),
    DateEndpoint("fitness_age", "/fitnessage-service/fitnessage/v2/{d}", lambda c, d: c.get_fitness_age(d)),
    DateEndpoint("heart_rate_daily", "/wellness-service/wellness/dailyHeartRate", lambda c, d: c.get_daily_heart_rate(d)),
    DateEndpoint("menstrual_summary", "/periodichealth-service/menstrualcycle/summary/{d}", lambda c, d: c.get_menstrual_cycle_summary(d)),
    DateEndpoint("menstrual_dayview", "/periodichealth-service/menstrualcycle/dayview/{d}", lambda c, d: c.get_menstrual_cycle_day_view(d, TODAY)),
    DateEndpoint("spo2_daily", "/wellness-service/wellness/daily/spo2acclimation/{d}", lambda c, d: c.get_daily_spo2(d)),
    DateEndpoint("user_summary_daily", "/usersummary-service/usersummary/daily", lambda c, d: c.get("/usersummary-service/usersummary/daily", params={"calendarDate": d.isoformat()}), wrapped=False),
]

STATIC_ENDPOINTS = [
    StaticEndpoint("profile_settings", "/userprofile-service/userprofile/settings", lambda c: c.get_user_profile_settings()),
    StaticEndpoint("heart_rate_zones", "/biometric-service/heartRateZones/v2", lambda c: c.get_heart_rate_zones()),
    StaticEndpoint("activity_types", "/activity-service/activity/activityTypes/simple", lambda c: c.get_activity_types(force_refresh=True)),
]

PROBE_LENGTHS = [14, 28, 29, 31, 32, 60, 92, 183, 364, 365, 366, 367, 731, 1826]


# --------------------------------------------------------------------------- phases


def get_token(session: requests.Session, rec: Recorder, key: str) -> str:
    cache = RAW / "_auth" / "bearer-token-cache.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    with PersonalApiAuthenticator(session=session, bearer_token_cache_file=cache) as auth:
        rec.last = None
        token = auth.obtain_token(key, force_refresh=True)
    resp = rec.last
    env = envelope(resp, "_token")
    header_b64, payload_b64, _sig = token.access_token.split(".")
    dec = lambda b: json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))  # noqa: E731
    claims = dec(payload_b64)
    env["jwt"] = {
        "header": dec(header_b64),
        "claim_names": sorted(claims),
        "iat": claims.get("iat"),
        "exp": claims.get("exp"),
        "lifetime_seconds": (claims["exp"] - claims["iat"]) if "iat" in claims else None,
        "body_is_raw_jwt": resp.text.strip().count(".") == 2 and not resp.text.strip().startswith("{"),
        "content_type": resp.headers.get("Content-Type"),
    }
    save("_token", TODAY.isoformat(), env)
    print(f"  token: HTTP {resp.status_code}, raw JWT={env['jwt']['body_is_raw_jwt']}, lifetime={env['jwt']['lifetime_seconds']}s, claims={env['jwt']['claim_names']}")
    return token.access_token


def phase_week(c: GarminPersonalApiClient, rec: Recorder) -> None:
    s, e = WEEK[0], WEEK[-1]
    print(f"\n== range endpoints, {s}..{e}")
    for ep in RANGE_ENDPOINTS:
        capture(rec, ep.slug, f"{s}_{e}", lambda ep=ep: ep.call(c, s, e))
    print("\n== single-date endpoints, each day")
    for ep in DATE_ENDPOINTS:
        for d in WEEK:
            capture(rec, ep.slug, d.isoformat(), lambda ep=ep, d=d: ep.call(c, d))
    print("\n== static endpoints")
    for ep in STATIC_ENDPOINTS:
        capture(rec, ep.slug, TODAY.isoformat(), lambda ep=ep: ep.call(c))


def phase_range_probes(c: GarminPersonalApiClient, rec: Recorder) -> dict[str, Any]:
    """For each range endpoint, find the longest range (ending today) that returns 2xx."""
    print("\n== range-length probes")
    results: dict[str, Any] = {}
    for ep in RANGE_ENDPOINTS:
        slug = f"{ep.slug}/_range_probe"
        trail: list[dict[str, Any]] = []

        def probe(days: int) -> requests.Response | None:
            s = TODAY - timedelta(days=days - 1)
            resp = capture(rec, slug, f"{days:04d}d_{s}_{TODAY}", lambda: ep.call(c, s, TODAY))
            trail.append(
                {"days": days, "status": resp.status_code if resp is not None else None,
                 "bytes": len(resp.content) if resp is not None else None,
                 "count": count_items(resp.json()) if resp is not None and resp.ok and resp.content and "json" in resp.headers.get("Content-Type", "") else None}
            )
            return resp

        last_ok, first_fail = 7, None
        for days in PROBE_LENGTHS:
            r = probe(days)
            if r is not None and r.ok:
                last_ok = days
            else:
                first_fail = days
                break
        while first_fail is not None and first_fail - last_ok > 1:  # bisect to the exact cap
            mid = (last_ok + first_fail) // 2
            r = probe(mid)
            if r is not None and r.ok:
                last_ok = mid
            else:
                first_fail = mid
        results[ep.slug] = {
            "documented_cap_days": ep.documented_cap_days,
            "max_ok_days": last_ok,
            "first_fail_days": first_fail,
            "trail": trail,
        }
        print(f"  -> {ep.slug}: max_ok={last_ok} first_fail={first_fail} (documented {ep.documented_cap_days})")

    # activities_search page size
    for limit in (100, 200, 500, 1000, 5000):
        capture(rec, "activities_search/_limit_probe", f"limit_{limit}",
                lambda limit=limit: c.search_activities(start=0, limit=limit))
    save("_meta", "range_probes", {"today": TODAY.isoformat(), "results": results})
    return results


def phase_errors(c: GarminPersonalApiClient, rec: Recorder, session: requests.Session) -> None:
    print("\n== deliberate error probes")
    capture(rec, "_errors", "stress_bad_date_format", lambda: c.get_daily_stress("2026-13-45", TODAY))
    capture(rec, "_errors", "stress_end_before_start", lambda: c.get_daily_stress(TODAY, TODAY - timedelta(days=3)))
    capture(rec, "_errors", "stress_future", lambda: c.get_daily_stress(TODAY + timedelta(days=1), TODAY + timedelta(days=5)))
    capture(rec, "_errors", "sleep_daily_no_date", lambda: c.get("/sleep-service/sleep/dailySleep"))
    capture(rec, "_errors", "activity_nonexistent", lambda: c.get_activity(1))
    capture(rec, "_errors", "activity_non_numeric", lambda: c.get("/activity-service/activity/abc"))
    capture(rec, "_errors", "unknown_path", lambda: c.get("/usersummary-service/does-not-exist"))
    capture(rec, "_errors", "search_bad_activity_type", lambda: c.search_activities(activity_type="not_a_type"))
    with GarminPersonalApiClient("not.a.jwt", session=session) as bad:
        capture(rec, "_errors", "bad_bearer_token", lambda: bad.get_user_profile_settings())
    capture(rec, "_errors", "no_auth_header",
            lambda: session.get(c.base_url + "/userprofile-service/userprofile/settings", timeout=30))
    capture(rec, "_errors", "token_bad_api_key",
            lambda: session.post(c.base_url + TOKEN_PATH, headers={"Authorization": "ApiKey invalid-key"}, timeout=30))
    capture(rec, "_errors", "post_to_data_endpoint",
            lambda: session.post(c.base_url + "/userprofile-service/userprofile/settings",
                                 headers=c._headers, timeout=30))  # noqa: SLF001


def phase_burst(c: GarminPersonalApiClient, rec: Recorder, n: int = 40) -> None:
    """Modest unpaced burst on one cheap endpoint to look for rate-limit signals."""
    print(f"\n== burst: {n} unpaced GETs to profile settings")
    start = len(rec.log)
    t0 = time.time()
    for _ in range(n):
        try:
            c.get_user_profile_settings()
        except GarminPersonalApiError:
            pass
        if rec.last is not None and rec.last.status_code == 429:
            save("_burst", "first_429", envelope(rec.last, "_burst"))
            break
    burst = rec.log[start:]
    out = {
        "requests": len(burst),
        "seconds": round(time.time() - t0, 2),
        "statuses": {str(s): sum(1 for b in burst if b["status"] == s) for s in {b["status"] for b in burst}},
        "rate_headers_seen": [b["rate_headers"] for b in burst if b["rate_headers"]][:5],
        "headers_last": dict(rec.last.headers) if rec.last is not None else None,
    }
    save("_burst", TODAY.isoformat(), out)
    print(f"  {out['requests']} requests in {out['seconds']}s, statuses={out['statuses']}, rate headers={bool(out['rate_headers_seen'])}")


def phase_activity(c: GarminPersonalApiClient, rec: Recorder) -> None:
    print("\n== today's run")
    found = c.search_activities(start_date=TODAY, end_date=TODAY, activity_type="running", limit=20)
    runs = found if isinstance(found, list) else []
    picked_today = bool(runs)
    if not runs:
        print("  no run today; falling back to most recent run")
        recent = c.search_activities(activity_type="running", limit=1)
        runs = recent if isinstance(recent, list) else []
    if not runs:
        print("  no runs found at all")
        return
    act = runs[-1] if picked_today else runs[0]  # earliest run today (results sorted desc)
    aid = act["activityId"]
    day = str(act.get("startTimeLocal", TODAY))[:10]
    print(f"  activity {aid} '{act.get('activityName')}' start={act.get('startTimeLocal')} today={picked_today}")
    save("_meta", "picked_activity", {"activityId": aid, "startTimeLocal": act.get("startTimeLocal"), "is_today": picked_today})
    capture(rec, "activity", f"{day}_{aid}", lambda: c.get_activity(aid))
    capture(rec, "activity_workouts", f"{day}_{aid}", lambda: c.get_activity_workouts(aid))

    # download: keep ZIP and extracted FIT in their own directory, separate from scripts
    rec.last = None
    try:
        blob = c.download_activity(aid)
    except GarminPersonalApiError as exc:
        blob = None
        print(f"  download failed: {exc}")
    dl_dir = RAW / "activity_download" / f"{day}_{aid}"
    dl_dir.mkdir(parents=True, exist_ok=True)
    if rec.last is not None:
        save("activity_download", f"{day}_{aid}",
             envelope(rec.last, "activity_download", binary_path=str(dl_dir.relative_to(ROOT))))
    if not blob:
        return
    zip_path = dl_dir / f"{aid}.zip"
    zip_path.write_bytes(blob)
    print(f"  zip: {len(blob)} bytes, magic={blob[:4]!r}")
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = Path(info.filename).name  # flatten: never trust archive paths
            if not name or info.is_dir():
                continue
            target = dl_dir / name
            target.write_bytes(zf.read(info))
            print(f"  extracted {name} ({info.file_size} bytes)")


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-probes", action="store_true")
    ap.add_argument("--skip-burst", action="store_true")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    key = os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()
    if not key:
        print("GARMIN_PERSONAL_API_KEY missing from .env", file=sys.stderr)
        return 1

    rec = Recorder()
    session = requests.Session()
    session.hooks["response"].append(rec.hook)

    print("== token exchange")
    bearer = get_token(session, rec, key)
    del key

    with GarminPersonalApiClient(bearer, session=session) as c:
        phase_week(c, rec)
        phase_activity(c, rec)
        if not args.skip_probes:
            phase_range_probes(c, rec)
        phase_errors(c, rec, session)
        if not args.skip_burst:
            phase_burst(c, rec)

    save("_meta", "summary", {"today": TODAY.isoformat(), "calls": SUMMARY, "request_log": rec.log})
    statuses: dict[str, int] = {}
    for entry in rec.log:
        statuses[str(entry["status"])] = statuses.get(str(entry["status"]), 0) + 1
    print(f"\n== done: {len(rec.log)} HTTP requests, statuses {statuses}")
    print(f"   rate-limit-ish headers ever seen: {sorted({k for e in rec.log for k in e['rate_headers']})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
