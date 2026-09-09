#!/usr/bin/env python3
"""Fetch WHOOP data and build data/whoop.json + data/whoop.csv.

Two things this does that a naive fetcher does not:

  1. WHOOP ROTATES REFRESH TOKENS. Every refresh returns a *new* refresh_token
     and invalidates the one you sent. If you do not persist the new one, run
     #2 authenticates with a dead credential. We write it to $WHOOP_TOKEN_OUT
     so the workflow can push it back into the repo secret.

  2. IT FAILS LOUDLY. If the refresh dies or every endpoint errors, this exits
     non-zero so the Actions run goes red. A fetcher that swallows auth
     failures and exits 0 leaves you with a green checkmark and frozen data.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_BASE = "https://api.prod.whoop.com/developer/v2"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
UA = "whoop-dashboard/1.0 (+github-actions)"
PAGE_LIMIT = 25          # WHOOP's maximum
MAX_PAGES = 400          # ~10k records; a hard stop against runaway pagination


def die(msg):
    print("ERROR: %s" % msg, file=sys.stderr)
    sys.exit(1)


def log(msg):
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

def refresh_access_token(refresh_token, client_id, client_secret):
    """Exchange a refresh token. Returns (access_token, new_refresh_token)."""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "offline",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        try:
            code = json.loads(detail).get("error", "")
        except ValueError:
            code = ""
        # These two failures look alike and have opposite fixes. Reconnecting
        # cannot repair bad client credentials -- the auth script signs its own
        # exchange with the same id/secret, so it would fail identically.
        if e.code == 401 or code == "invalid_client":
            hint = (
                "WHOOP rejected the CLIENT credentials, not your refresh token.\n"
                "  Check WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET against your app\n"
                "  at developer.whoop.com. Re-running whoop_auth.py will NOT help."
            )
        elif code in ("invalid_grant", "invalid_request", "invalid_token"):
            hint = (
                "The stored refresh token is dead. WHOOP rotates the token on\n"
                "  every use and expires it after roughly 3 hours idle, so this\n"
                "  means rotation broke -- check that GH_SECRETS_PAT is still\n"
                "  valid. Re-run scripts/whoop_auth.py and update the secret."
            )
        else:
            hint = "Unrecognised OAuth error; see the response body above."
        die("token refresh failed (HTTP %s): %s\n  %s" % (e.code, detail, hint))
    except Exception as e:
        die("token refresh network error: %s" % e)

    access = payload.get("access_token")
    if not access:
        die("token response contained no access_token")
    # May be absent if WHOOP ever stops rotating; keep using the old one then.
    return access, payload.get("refresh_token") or refresh_token


def persist_refresh_token(token):
    """Store the rotated token: the workflow's handoff file in CI, .env.local
    locally. Skipping either one means the *next* run presents a token this
    run already invalidated."""
    out = os.environ.get("WHOOP_TOKEN_OUT")
    if out:
        with open(out, "w") as f:
            f.write(token)
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
        log("Wrote rotated refresh token to %s" % out)
        return

    if not os.path.exists(".env.local"):
        log("WARNING: refresh token rotated but there is nowhere to store it. "
            "The next run will fail unless you update WHOOP_REFRESH_TOKEN.")
        return

    with open(".env.local") as f:
        lines = f.readlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("WHOOP_REFRESH_TOKEN="):
            lines[i] = "WHOOP_REFRESH_TOKEN=%s\n" % token
            replaced = True
    if not replaced:
        lines.append("WHOOP_REFRESH_TOKEN=%s\n" % token)
    with open(".env.local", "w") as f:
        f.writelines(lines)
    os.chmod(".env.local", 0o600)
    log("Updated WHOOP_REFRESH_TOKEN in .env.local")


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------

def api_get(path, token, params=None):
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer %s" % token,
        "Accept": "application/json",
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise RuntimeError("HTTP %s on %s: %s" % (e.code, path, detail))


def paginate(path, token, start_iso, end_iso):
    records = []
    next_token = None
    for _ in range(MAX_PAGES):
        params = {"limit": PAGE_LIMIT, "start": start_iso, "end": end_iso}
        if next_token:
            params["nextToken"] = next_token
        page = api_get(path, token, params)
        batch = page.get("records") or []
        records.extend(batch)
        next_token = page.get("next_token")
        if not next_token or not batch:
            break
    return records


# --------------------------------------------------------------------------
# shaping
# --------------------------------------------------------------------------

def local_date(record):
    """Calendar date in the wearer's own timezone, not UTC.

    A cycle starting 04:00Z with offset -04:00 belongs to the previous local
    day; keying on the UTC date would shift half the chart by one day.
    """
    start = record.get("start") or record.get("created_at")
    if not start:
        return None
    try:
        ts = datetime.strptime(start[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    offset = record.get("timezone_offset") or "+00:00"
    try:
        sign = -1 if offset[0] == "-" else 1
        ts += sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
    except (ValueError, IndexError):
        pass
    return ts.date().isoformat()


def scored(record):
    return (record or {}).get("score") or {}


def round_or_none(value, digits=1):
    return round(value, digits) if isinstance(value, (int, float)) else None


def build_series(cycles, recovery, sleep):
    """Join cycles + recovery + sleep into one row per calendar day."""
    recovery_by_cycle = {r.get("cycle_id"): r for r in recovery}

    # Longest non-nap sleep wins the night; naps would otherwise overwrite it.
    sleep_by_date = {}
    for s in sleep:
        if s.get("nap"):
            continue
        date = local_date(s)
        if not date:
            continue
        stage = scored(s).get("stage_summary") or {}
        asleep = (
            (stage.get("total_light_sleep_time_milli") or 0)
            + (stage.get("total_slow_wave_sleep_time_milli") or 0)
            + (stage.get("total_rem_sleep_time_milli") or 0)
        )
        prev = sleep_by_date.get(date)
        if prev is None or asleep > prev[0]:
            sleep_by_date[date] = (asleep, s)

    rows = {}
    for c in cycles:
        date = local_date(c)
        if not date:
            continue
        cs = scored(c)
        strain = cs.get("strain")
        # An unscored or still-open cycle reports strain 0 with no heart rate.
        # Charting those puts a fake trough at today's date every morning.
        if not isinstance(strain, (int, float)) or strain <= 0:
            continue
        if not cs.get("average_heart_rate"):
            continue

        rs = scored(recovery_by_cycle.get(c.get("id")))
        asleep_ms, sleep_rec = sleep_by_date.get(date, (0, None))
        ss = scored(sleep_rec)

        rows[date] = {
            "date": date,
            "strain": round_or_none(strain, 2),
            "avg_hr": cs.get("average_heart_rate"),
            "max_hr": cs.get("max_heart_rate"),
            "calories": round(cs["kilojoule"] / 4.184) if cs.get("kilojoule") else None,
            "recovery": round_or_none(rs.get("recovery_score"), 0),
            "hrv": round_or_none(rs.get("hrv_rmssd_milli"), 1),
            "rhr": rs.get("resting_heart_rate"),
            "spo2": round_or_none(rs.get("spo2_percentage"), 1),
            "skin_temp": round_or_none(rs.get("skin_temp_celsius"), 1),
            "sleep_hours": round(asleep_ms / 3600000.0, 2) if asleep_ms else None,
            "sleep_performance": round_or_none(ss.get("sleep_performance_percentage"), 0),
            "sleep_efficiency": round_or_none(ss.get("sleep_efficiency_percentage"), 0),
            "sleep_consistency": round_or_none(ss.get("sleep_consistency_percentage"), 0),
        }
    return [rows[d] for d in sorted(rows)]


def build_workouts(workouts):
    out = []
    for w in workouts:
        ws = scored(w)
        strain = ws.get("strain")
        if not isinstance(strain, (int, float)):
            continue
        start, end = w.get("start"), w.get("end")
        minutes = None
        if start and end:
            try:
                fmt = "%Y-%m-%dT%H:%M:%S"
                minutes = round(
                    (datetime.strptime(end[:19], fmt)
                     - datetime.strptime(start[:19], fmt)).total_seconds() / 60
                )
            except ValueError:
                pass
        out.append({
            "date": local_date(w),
            "sport": w.get("sport_name") or "Activity",
            "strain": round(strain, 1),
            "minutes": minutes,
            "avg_hr": ws.get("average_heart_rate"),
            "max_hr": ws.get("max_heart_rate"),
            "calories": round(ws["kilojoule"] / 4.184) if ws.get("kilojoule") else None,
        })
    out.sort(key=lambda w: w.get("date") or "", reverse=True)
    return out


CSV_COLUMNS = [
    ("date", "Date"), ("strain", "Strain"), ("recovery", "Recovery %"),
    ("hrv", "HRV (ms)"), ("rhr", "RHR"), ("sleep_hours", "Sleep (h)"),
    ("sleep_performance", "Sleep Perf %"), ("sleep_efficiency", "Sleep Eff %"),
    ("avg_hr", "Avg HR"), ("max_hr", "Max HR"), ("calories", "Calories"),
    ("spo2", "SpO2 %"), ("skin_temp", "Skin Temp C"),
]


def write_csv(path, series):
    with open(path, "w") as f:
        f.write(",".join(label for _, label in CSV_COLUMNS) + "\n")
        for row in series:
            f.write(",".join(
                "" if row.get(key) is None else str(row[key])
                for key, _ in CSV_COLUMNS
            ) + "\n")


# --------------------------------------------------------------------------

def load_env_file(path=".env.local"):
    """Read KEY=value pairs from .env.local into the environment.

    Keeps credentials out of your shell history and out of the environment of
    every other command in the session. Absent in CI, where Actions supplies
    the same variables from repository secrets.
    """
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main():
    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=365,
                        help="how far back to fetch (default: 365)")
    parser.add_argument("--out-dir", default="data")
    args = parser.parse_args()

    client_id = os.environ.get("WHOOP_CLIENT_ID")
    client_secret = os.environ.get("WHOOP_CLIENT_SECRET")
    stored_refresh = os.environ.get("WHOOP_REFRESH_TOKEN")
    if not (client_id and client_secret and stored_refresh):
        die("WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET and WHOOP_REFRESH_TOKEN must "
            "all be set.\n  Locally: run scripts/whoop_auth.py to create "
            ".env.local.\n  In CI: check the repository secrets.")

    access_token, new_refresh = refresh_access_token(
        stored_refresh, client_id, client_secret)
    log("Refreshed access token.")
    if new_refresh != stored_refresh:
        log("Refresh token rotated -- persisting the new one.")
    persist_refresh_token(new_refresh)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    start_iso, end_iso = start.strftime(fmt), end.strftime(fmt)

    endpoints = [
        ("cycles", "/cycle"),
        ("recovery", "/recovery"),
        ("sleep", "/activity/sleep"),
        ("workouts", "/activity/workout"),
    ]
    raw, failures = {}, []
    for name, path in endpoints:
        try:
            raw[name] = paginate(path, access_token, start_iso, end_iso)
            log("  %-9s %4d records" % (name, len(raw[name])))
        except Exception as e:
            raw[name] = []
            failures.append("%s: %s" % (name, e))
            log("  %-9s FAILED: %s" % (name, e))

    if len(failures) == len(endpoints):
        die("every endpoint failed -- refusing to overwrite good data.\n  "
            + "\n  ".join(failures))

    series = build_series(raw["cycles"], raw["recovery"], raw["sleep"])
    workouts = build_workouts(raw["workouts"])
    if not series:
        die("no scored days in the last %d days -- not writing an empty "
            "dashboard." % args.days)

    os.makedirs(args.out_dir, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "days": len(series),
        "series": series,
        "workouts": workouts[:100],
    }
    json_path = os.path.join(args.out_dir, "whoop.json")
    with open(json_path, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    write_csv(os.path.join(args.out_dir, "whoop.csv"), series)

    log("Wrote %s -- %d days, %d workouts (%s .. %s)"
        % (json_path, len(series), len(workouts),
           series[0]["date"], series[-1]["date"]))

    # Partial failure is still worth a red run: the data is stale in a way the
    # dashboard cannot show you.
    if failures:
        die("some endpoints failed:\n  " + "\n  ".join(failures))


if __name__ == "__main__":
    main()
