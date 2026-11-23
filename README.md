# Lake Level Finder (lakelevelfinder.com)

A static site that shows US lake and reservoir levels from public agency data: the level, how it compares with full pool, the 24-hour trend, charts and records, always with the reading's own date and source. Python 3 standard library only (no pip installs), vanilla JavaScript, inline SVG charts made at build time, hosted on Cloudflare Workers static assets.

Sources: Texas Water Development Board (Water Data for Texas), U.S. Army Corps of Engineers (CWMS Data API), Bureau of Reclamation (RISE), California DWR (CDEC) and USGS (Water Data APIs). See `/sources/` on the site for cadence, datums and the exact label, trend and freshness rules.

## Run it

```bash
python3 pipeline/run.py --dry-run          # fetch and validate, print the diff, write nothing
python3 pipeline/run.py                    # fetch, validate, write data/
python3 pipeline/run.py --source cwms --lake table-rock-lake-mo --dry-run
python3 -m unittest discover -s tests      # saved fixtures only, no network
python3 build.py --check                   # build dist/, fail on rule violations
python3 -m http.server 8093 -d dist -b 127.0.0.1    # local preview
```

- `USGS_API_KEY` (optional) raises the USGS rate limit. Set it in your shell for local runs; in Actions it is a repo secret.
- `build.py --check` fails on broken links or orphan pages, a lake page missing its source link, `<time>` element, datum line or disclaimer, a band or trend string outside the allowed set, a forbidden word, a headline number that differs from `data/current.json`, or a published lake whose mapping is not verified.
- `PUBLISH_BATCH=1` (or `build.py --batch 1`) builds only release batch 1.

## Safety rules (all enforced in code and tests)

| Rule | Where |
|---|---|
| Never type a level, full pool, record or date by hand; the site shows what a feed says or says it does not know | `pipeline/`, `build.py --check` |
| One primary source per lake; one optional fallback; never blend sources | `data/ref/lakes.json`, `pipeline/effective.py` |
| Units must be feet; level inside the lake's valid range; no future timestamps; jumps over 15 ft / 24 h held until confirmed | `pipeline/validate.py` |
| Wrong count, HTML error page or changed schema rejects the whole source; last good data kept | `pipeline/validate.py` |
| Delayed / "Current level unknown" by per-source thresholds, recomputed in the browser from `data-reading-ts` | `pipeline/effective.py`, `src/assets/site.js` |
| Every reading shows its own date; no "safe", "open", "official" (about us), or "live" for daily sources | `build.py --check` |
| Overrides need a reason, evidence URL and expiry (max 14 days); no override can set a level | `pipeline/override.py` |

## Layout

| Path | What |
|---|---|
| `pipeline/` | Orchestrator, fetcher, one adapter per source in `pipeline/sources/`, validation, derived values, series and stats, freshness, overrides, GitHub Issue alerts |
| `data/ref/` | Lakes, sources, states, systems, operators, demand (reference data, built by `pipeline/refdata.py`) |
| `data/` | `current.json`, `series/{lake_id}.csv`, `stats/{lake_id}.json`, `status.json`, `notes/`, `overrides.json` (committed by the hourly run) |
| `build.py`, `charts.py` | Site generator and inline SVG charts |
| `src/templates/`, `src/assets/`, `src/static/` | Page shell, CSS, JS, `_headers`, `_redirects`, `robots.txt` |
| `src/content/` | Trust pages, guides, and state, system and operator intros (HTML fragments with a header comment) |
| `tests/` | Unit tests on saved real responses in `tests/fixtures/` |
| `tools/` | `map_lakes.py`, `demand.py`, `backfill.py`, `accuracy_check.py` |
| `.github/workflows/` | `update.yml` (hourly), `ci.yml`, `probe.yml`, `backfill.yml`, `accuracy.yml`, `watchdog-deploy.yml`, `verify-cloudflare.yml` |
| `watchdog/` | Cron Worker: starts the hourly run, starts the daily accuracy check, emails the owner when data goes stale |
| `docs/` | `runbook.md` (what to do for each alert), `accuracy-log.md`, `sources/`, `mapping/` |

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `update` | watchdog dispatch (hourly), GitHub cron as backup | pipeline → tests → `build.py --check` → alerts → commit `data/` → deploy only when repo variable `DEPLOY` is `true`. A scheduled run skips itself while the live data is under 40 minutes old |
| `ci` | push, pull request | tests + `build.py --check`, never deploys |
| `probe` | manual | fetches the source URLs from a US runner (default list = the feed checks in `docs/runbook.md`) and prints status, time and size |
| `backfill` | manual (source, optional lake) | downloads history into `data/series/` and commits it |
| `accuracy` | watchdog at 14:00 UTC, cron backup | spot-checks 30 lakes a day against the source; logs to `docs/accuracy-log.md` |
| `watchdog-deploy` | manual | deploys the watchdog Worker; fills its KV id, alert address and repo name on the runner |
| `verify-cloudflare` | manual | checks the Cloudflare token works (prints status only) |

## Deploy

```bash
./deploy.sh --dry-run     # tests + build --check + wrangler dry run, uploads nothing
./deploy.sh 1             # release batch 1 only
./deploy.sh               # everything published
```

`deploy.sh` refuses to deploy while `src/site.json` has `domain_confirmed: false`, if a test fails, or if `build.py --check` fails. The hourly workflow deploys the same way with `npx --yes wrangler@4 deploy` once the repo variable `DEPLOY` is `true`. A failed build or deploy leaves the previous version online; its pages age into "may be out of date" and then "Current level unknown" on their own. Rollback: Cloudflare dashboard → Workers & Pages → `lakelevelfinder` → Deployments.

Setup steps that need the owner (domain, secrets, DNS, email, analytics, Search Console) are in `docs/runbook.md`.

## Overrides

```bash
python3 pipeline/override.py list
python3 pipeline/override.py --help     # set and clear commands and their flags
```

Actions: `hold` (show "Current level unknown" with the reason), `release_jump` (accept a held jump after checking an agency source), `set_full_pool` (temporary full pool with its source URL), `note` (a dated, sourced notice line). Every override needs `reason`, `evidence_url`, `set_at` and `expires_at` (maximum 14 days). No override can set a water level. Commit `data/overrides.json` after a change.

## Data reuse

Files under `/data/` on the site may be reused with credit to Lake Level Finder and to the source agency named in each file. Fetch them at most once an hour.

Contact: contact@lakelevelfinder.com
