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

The order matters: WHOOP's app form asks for a privacy policy URL, and that
URL only resolves once your site is live. So publish first, then register.

### 1. Publish the site

Create the GitHub repo, push this code, then **Settings → Pages → Source:
GitHub Actions**. Run the workflow once from the Actions tab. It will fail at
the fetch step -- there are no credentials yet -- but the `deploy` job still
runs, so the site goes live.

Your privacy policy is now at:

```
https://nithinn11.github.io/whoopstatus/privacy.html
```

Replace `REPLACE_WITH_YOUR_EMAIL` in [`privacy.html`](privacy.html) with a real
contact address before you submit it to WHOOP.

### 2. Create a WHOOP app

At [developer.whoop.com](https://developer.whoop.com), create an app and add
this redirect URI **exactly**:

```
http://localhost:1111/callback
```

If you registered a different port, set `WHOOP_REDIRECT_URI` when running the
auth script instead of editing it.

For the **privacy policy URL**, use the Pages link from step 1.

Note the client ID and client secret.

### 3. Get a refresh token

```bash
python3 scripts/whoop_auth.py
```

It prompts for your client id and secret **without echoing them**, opens the
WHOOP consent screen, catches the redirect locally, and writes all three
values to `.env.local` (chmod 600, gitignored).

Nothing is printed to the terminal on purpose. Open `.env.local` yourself and
copy each value into GitHub. Run this once; the workflow keeps the token fresh
from then on.

### 4. Create a fine-grained PAT

The workflow has to rewrite its own `WHOOP_REFRESH_TOKEN` secret, and the
built-in `GITHUB_TOKEN` cannot write secrets. At
**Settings → Developer settings → Personal access tokens → Fine-grained**:

- Repository access: **only this repository**
- Repository permissions: **Secrets: Read and write**
- Expiration: set a calendar reminder to rotate it

### 5. Add the secrets

**Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `WHOOP_CLIENT_ID` | from developer.whoop.com |
| `WHOOP_CLIENT_SECRET` | from developer.whoop.com |
| `WHOOP_REFRESH_TOKEN` | printed by `whoop_auth.py` (rewritten automatically after this) |
| `GH_SECRETS_PAT` | the fine-grained PAT from step 4 |

### 6. Run it

**Actions → Update WHOOP data → Run workflow**. This run fetches a year of
history, rotates the token, and republishes the site with your data. From here
the 30-minute schedule takes over.

## Local development

```bash
python3 scripts/fetch_whoop.py --days 365   # reads .env.local automatically
python3 -m http.server 8000                 # then open http://localhost:8000
```

Serve it over HTTP — opening `index.html` as a `file://` URL fails, because
the `fetch` of `data/whoop.json` is blocked by CORS.

Careful: running the fetcher locally rotates the token, which invalidates the
copy stored in GitHub. The local run writes the replacement back into
`.env.local`, so local runs keep working -- but the next Actions run will fail
auth until you paste the new `WHOOP_REFRESH_TOKEN` from `.env.local` into the
repository secret. Prefer running the workflow over running the fetcher
locally once things are live.

## Where credentials live

| | Holds | Notes |
|---|---|---|
| GitHub Actions secrets | all three | encrypted, exposed only to the running workflow, never logged |
| `.env.local` | all three | your machine only, chmod 600, gitignored |
| Repository / site | none | no credential is ever committed or served |

Two ways this goes wrong, both avoidable:

- **`export`ing secrets into your shell.** They land in your shell history and
  in the environment of every command you run afterwards. Use `.env.local`;
  both scripts read it automatically.
- **Echoing them.** Terminal scrollback gets screen-shared, pasted into issues,
  and read by anything watching the session. `whoop_auth.py` uses `getpass` and
  writes to a file rather than printing.

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
