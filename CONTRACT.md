# Build contract: Lake Level Finder (lakelevelfinder.com)

Shared rules for the parallel build agents. **The spec is `../../data/docs/LAKES-BUILD-PLAN-2026-11.md`.** Read §4.2–4.5, §6, §7, §8 and §9 before writing code. Where this file and the plan disagree, this file wins.

- Brand: **Lake Level Finder**. Domain: `lakelevelfinder.com`. The domain is not registered yet and the site is not deployed.
- Stack: Python 3 standard library only (no pip). Vanilla JS. Inline SVG charts built at build time. Cloudflare Workers static assets. Copy patterns from `../burnbans/`, which is read-only: never edit it, never commit there.
- Design the site as the finished, launched product. No "Soon", "beta" or "pilot" wording anywhere.
- Honesty rules (§4.2 D, §8.4–8.6):
  - Never invent a level, a full-pool value, a record, a ramp elevation or a date.
  - Never type a water level by hand into content.
  - Never print "safe", "open", "official" (about us), or "live"/"real-time" for daily-average sources.
  - Every reading shows its own date.

## Who owns what (no agent writes outside its paths)

| Agent | Owns (paths relative to the repo root) |
|---|---|
| **A: mapping** | `data/ref/*.json`, `tools/map_lakes.py`, `tools/demand.py`, `pipeline/refdata.py`, `docs/mapping/**`, `docs/sources/*.md` |
| **B: pipeline** | `pipeline/**` except `refdata.py`, `tests/**` except `tests/test_build.py`, `tools/backfill.py`, `tools/accuracy_check.py`, `scripts/**` |
| **C: front end** | `build.py`, `charts.py`, `src/templates/**`, `src/assets/**`, `src/static/**`, `tests/test_build.py` |
| **D: infra and copy** | `.github/workflows/**`, `watchdog/**`, `wrangler.jsonc`, `deploy.sh`, `README.md`, `docs/runbook.md`, `docs/accuracy-log.md`, `src/content/**` |
| main session | `src/site.json`, `CONTRACT.md`, the `data/` outputs from running the pipeline (`current.json`, `series/`, `stats/`, `status.json`), and merging |

Each agent works in **its own scratch copy** of this repo (path given in its brief) and returns the list of files it wrote. The main session copies those files into `builds/lakelevelfinder/`. Subagents cannot write in the project folder.

## Interfaces

### Data files
`data/ref/lakes.json`, `sources.json`, `states.json`, `systems.json`, `operators.json`, `demand.json`, plus `data/current.json`, `data/series/{lake_id}.csv`, `data/stats/{lake_id}.json`, `data/status.json`, `data/notes/{lake_id}.json` and `data/overrides.json`. Schemas follow **plan §8.3 exactly** (field names included).

`lakes.json` is a JSON **list** of Lake records. `sources.json`, `states.json`, `systems.json` and `operators.json` are JSON **objects keyed by id/slug**. `current.json` is an object keyed by `lake_id`, holding Reading records.

`states.json` record: `{"code":"TX","name":"Texas","slug":"texas","neighbours":["NM","OK","AR","LA"],"timezones":["America/Chicago","America/Denver"]}`. The `published` flag is computed at build time (≥ 3 live lakes).

`systems.json` record: `{"slug":"highland-lakes","name":"Highland Lakes","operator":"lcra","members":["lake-buchanan-tx",...],"source_url":"...","intro":"src/content/systems/highland-lakes.html"}`.

`operators.json` record: `{"slug":"lcra","name":"Lower Colorado River Authority","short":"LCRA","official_url":"...","has_public_feed":false,"note":"..."}`.

`notes/{lake_id}.json`: `[{"text":"...","source_url":"...","source_name":"...","checked":"2026-11-23"}]`. Optional.

`demand.json`: `{lake_id: {"volume": int, "keywords": [..], "aliases_searched": [..]}}`.

### Python modules the build imports (B writes them; C calls them)
- `pipeline/effective.py`: `effective(reading: dict, lake: dict, source: dict, now: datetime) -> dict`. Returns the reading plus `freshness` ("current" | "delayed" | "unknown"), `age_hours`, `soft_stale_hours`, `hard_stale_hours`, and `show_badges` (False when unknown). It applies `data/overrides.json`: `hold` → unknown with a reason, `note` → `notice`.
- `pipeline/derive.py`:
  - `band(elev_ft, full_pool_ft, pct_full, flood_control, bands=None) -> str` returns one of `above_full|full|near_full|below_full|low|very_low|level_only`.
  - `in_flood_pool(...) -> bool`.
  - `trend(change_24h_ft) -> str|None` returns one of `rising_fast|rising|steady|falling|falling_fast|None`.
  - Labels: `BAND_LABEL = {"above_full":"Above full pool","full":"Full","near_full":"Near full","below_full":"Below full","low":"Low","very_low":"Very low","level_only":"Level only"}` and `TREND_LABEL = {"rising_fast":"Rising fast","rising":"Rising","steady":"Steady","falling":"Falling","falling_fast":"Falling fast"}`.
- `pipeline/common.py`: `ROOT`, `DATA`, `load_json(path, default)`, `save_json(path, obj)`, `utcnow()`, `parse_ts(s)`. Dates may be `YYYY-MM-DD`, which means the local day.

Until B's modules land, C may keep a private stub with the **same signatures** for testing. The stub must not be shipped.

### Content fragments (D writes; C's build.py renders them)
Each `src/content/**/X.html` file starts with a header comment, then the body HTML fragment (no `<html>`/`<body>`, no `<h1>`; the template adds the H1):
```
<!--
title: Data sources and methods | Lake Level Finder
description: How Lake Level Finder gets each lake level ...
h1: Data sources and methods
updated: 2026-11-23
-->
<p>...</p>
```
Paths:
- Trust pages `src/content/pages/{about,sources,disclaimer,corrections,privacy,terms,contact}.html` → `/{name}/`.
- Guides `src/content/guides/{what-is-full-pool,how-lake-levels-are-measured,reading-a-lake-level-chart}.html` → `/guides/{name}/`.
- State intros `src/content/states/{tx,ca,nv,az,ga,mo,ar,ok,sc,nm,ut}.html`: a body fragment of 1–3 paragraphs and no H1, inserted on the hub.
- System intros `src/content/systems/{slug}.html`, and operator intros `src/content/operators/{slug}.html`.

Placeholders that build.py replaces in content: `{{site.name}}`, `{{site.domain}}`, `{{site.contact_email}}`, `{{site.corrections_email}}`, `{{count.live_lakes}}`, `{{count.states}}`. No other templating in content.

### URLs (plan §6.1)
- Lake: `/lake/{lake_id}/`, history `/lake/{lake_id}/history/`.
- State: `/{state slug}/`. System: `/system/{slug}/`. Operator: `/operator/{slug}/`.
- Lists: `/lowest-lakes/`, `/fullest-lakes/`, `/rising-fastest/`, `/falling-fastest/`.
- Guides: `/guides/{slug}/`. Data: `/data/status.json`, `/data/lake/{id}.json`, `/data/lake/{id}.csv`.
- Lake ids follow plan §6.1: the common searched name plus the dam's two-letter state, e.g. `lake-travis-tx`, `canyon-lake-tx`, `lake-mead-nv`, `table-rock-lake-mo`, `choke-canyon-reservoir-tx`, `lake-corpus-christi-tx`, `medina-lake-tx`, `lake-lanier-ga`, `shasta-lake-ca`.

### Test command
`python3 -m unittest discover -s tests` (no network) and `python3 build.py --check`.
