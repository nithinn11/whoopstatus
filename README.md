# WHOOP Dashboard

A static dashboard for your own WHOOP data — recovery, strain, HRV, resting
heart rate, sleep and workouts. Fetched by GitHub Actions, hosted free on
GitHub Pages, no server anywhere.

```
WHOOP API ──▶ scripts/fetch_whoop.py ──▶ data/whoop.json ──▶ index.html
                        │                                      (Chart.js)
                        └──▶ rotated refresh token ──▶ back into repo secret
```

## The part most WHOOP dashboards get wrong

WHOOP **rotates the refresh token on every use** and invalidates the old one,
and an unused refresh token **dies after roughly 3 hours idle**.

So the obvious design — store a refresh token in a secret, run a daily cron —
breaks after exactly one run. Worse, it usually breaks *silently*: the refresh
401s, the fetch falls back to a stale access token, every endpoint errors, and
a fetcher that treats "no data" as "nothing to do" exits 0 and paints a green
checkmark over a frozen dashboard.

Three things here exist to prevent that:

1. **The rotated token is written back into the repo secret** after every
   refresh ([`.github/workflows/update.yml`](.github/workflows/update.yml),
   "Persist rotated refresh token"). This is the step that keeps it alive.
2. **The workflow runs every 30 minutes**, not daily, so the token is never
   idle long enough to expire.
3. **Failures are loud.** The fetcher exits non-zero on a dead token or a
   failed endpoint, and the page itself shows a warning banner if the data is
   more than 6 hours old.

## Setup

### 1. Create a WHOOP app

At [developer.whoop.com](https://developer.whoop.com), create an app and add
this redirect URI **exactly**:

```
http://localhost:8080/callback
```

Note the client ID and client secret.

### 2. Get a refresh token

```bash
export WHOOP_CLIENT_ID="..."
export WHOOP_CLIENT_SECRET="..."
python3 scripts/whoop_auth.py
```

This opens the WHOOP consent screen, catches the redirect locally, and prints
the three values you need. Run it once; the workflow keeps the token fresh
from then on.

### 3. Create a fine-grained PAT

The workflow has to rewrite its own `WHOOP_REFRESH_TOKEN` secret, and the
built-in `GITHUB_TOKEN` cannot write secrets. At
**Settings → Developer settings → Personal access tokens → Fine-grained**:

- Repository access: **only this repository**
- Repository permissions: **Secrets: Read and write**
- Expiration: set a calendar reminder to rotate it

### 4. Add the secrets

**Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `WHOOP_CLIENT_ID` | from developer.whoop.com |
| `WHOOP_CLIENT_SECRET` | from developer.whoop.com |
| `WHOOP_REFRESH_TOKEN` | printed by `whoop_auth.py` (rewritten automatically after this) |
| `GH_SECRETS_PAT` | the fine-grained PAT from step 3 |

### 5. Turn on Pages

**Settings → Pages → Source: GitHub Actions**.

Then **Actions → Update WHOOP data → Run workflow**. The first run fetches a
year of history and publishes the site.

## Local development

```bash
export WHOOP_CLIENT_ID="..." WHOOP_CLIENT_SECRET="..." WHOOP_REFRESH_TOKEN="..."
python3 scripts/fetch_whoop.py --days 365
python3 -m http.server 8000     # then open http://localhost:8000
```

Serve it over HTTP — opening `index.html` as a `file://` URL fails, because
the `fetch` of `data/whoop.json` is blocked by CORS.

Careful: running the fetcher locally rotates the token, which invalidates the
one stored in GitHub. Either accept that the next Actions run will repair it
from its own copy, or re-run `whoop_auth.py` afterwards.

## Data

`data/whoop.json` is one row per calendar day, joined across the cycle,
recovery and sleep endpoints:

| Field | Notes |
|---|---|
| `strain`, `avg_hr`, `max_hr`, `calories` | from `/v2/cycle` |
| `recovery`, `hrv`, `rhr`, `spo2`, `skin_temp` | from `/v2/recovery` |
| `sleep_hours`, `sleep_performance`, `sleep_efficiency` | from `/v2/activity/sleep` |

Two shaping details worth knowing:

- **Days are keyed to your local timezone**, not UTC. A cycle starting 04:00Z
  at `-04:00` belongs to the previous local day; keying on the UTC date shifts
  half the chart by one day.
- **`sleep_hours` is light + SWS + REM.** Summing every field in
  `stage_summary` also picks up `total_in_bed_time_milli` and reports
  ~20-hour nights.

`data/whoop.csv` is the same rows, flat, for spreadsheets.

## Troubleshooting

**Actions run is red, "token refresh failed (HTTP 400)"** — the refresh token
is dead. Usually the `GH_SECRETS_PAT` expired, so rotation stopped writing
back. Re-run `scripts/whoop_auth.py`, update `WHOOP_REFRESH_TOKEN`, and check
the PAT.

**Everything green but the data is old** — check that the schedule is still
enabled. GitHub disables scheduled workflows in repos with no activity for 60
days; the data commits normally keep it alive, but a long red streak will not.

**Page shows the stale banner** — that is the banner doing its job. Check the
Actions tab.

## Privacy

The data files hold your HRV, resting heart rate and sleep. GitHub Pages is
free only on public repositories, so on a public repo **this is public health
data**. On a private repo Pages requires a paid plan.
