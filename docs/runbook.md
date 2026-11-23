# Runbook: Lake Level Finder

Owner actions are marked 👤. Nothing in this repo may contain a token, password, account id or personal email address: the repo may be public. Secrets live only in GitHub repo secrets and Cloudflare Worker secrets. The only address on the site is `contact@lakelevelfinder.com`.

## 1. One-time setup (in order)

1. 👤 **Domain.** Check availability and register `lakelevelfinder.com` at Cloudflare Registrar, auto-renew on.
2. 👤 **Repo.** Create the GitHub repo. Public is the plan (free Actions minutes for an hourly run; keep research and plans out of it). If it must be private, change the `update.yml` cron to every 3 hours and expect the CWMS/USGS "Delayed" threshold to need 9 hours.
3. **Domain in code.** Set `domain_confirmed: true` in `src/site.json`. The domain is already in `wrangler.jsonc`, `watchdog/wrangler.jsonc`, the `gate` job in `update.yml` and the `probe.yml` User-Agent; change all of them if the domain changes.
4. 👤 **Cloudflare API token.** Create a token limited to "Workers Scripts: Edit" (add zone "Workers Routes: Edit" if wrangler asks). Add repo secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`. Run the `verify-cloudflare` workflow to check them (it prints status only). The coding assistant never creates or types tokens.
5. 👤 **USGS key (optional).** Sign up at `https://api.waterdata.usgs.gov/signup` and add repo secret `USGS_API_KEY`.
6. **Feed checks from a US runner.** Run the `probe` workflow with an empty URL list (default = the Phase 0 list: TWDB, CWMS series/levels/catalogs, RISE, CDEC, USGS new and legacy, LCRA, TVA, Duke, Alabama Power, Georgia Power, Ameren, AEP, GRDA, Dominion, SRP, TPWD, NWPS, NWS). Set `repeat` to 50 once to test CWMS throttling. Record results in `docs/sources/`. A 403, challenge page or timeout from an operator means "no feed": do not work around it.
7. **Dry run.** Leave repo variable `DEPLOY` unset. The `update` workflow still runs the pipeline, tests and build and commits `data/` hourly. Run it like this for 7 days and read `data/status.json` and the Issues.
8. **First deploy.** Set repo variables `DEPLOY=true` and `PUBLISH_BATCH=1`. The next `update` run deploys; wrangler creates the Worker `lakelevelfinder`, attaches the custom domain and issues the certificate. Delete `PUBLISH_BATCH` to publish every state.
9. 👤 **Cloudflare zone settings.**
   - SSL/TLS → Edge Certificates → **Always Use HTTPS: On**. Check that `http://lakelevelfinder.com/` answers 301.
   - **www → apex:** DNS → add a proxied record `A www 192.0.2.1`. Rules → Redirect Rules → create from the template "Redirect from WWW to root" (301, keep path and query string). Save, then test `https://www.lakelevelfinder.com/texas/?x=1` in a browser. On an earlier site the first attempt did not save; check the rule is listed as active.
   - Caching → Configuration → **Crawler Hints: On** (👤 accept the supplemental terms).
10. 👤 **Email Routing.** Email → Email Routing → enable (Cloudflare adds MX, SPF and DKIM records). Route `contact@lakelevelfinder.com` to the owner's mailbox. Send a test message and confirm it arrives.
11. 👤 **Watchdog.**
    - Create a fine-grained GitHub token for this repo only with "Actions: Read and write". Add it as repo secret `GH_DISPATCH_TOKEN`.
    - Add repo secret `WATCHDOG_ALERT_TO`: an address that is a **verified** Email Routing destination (the `send_email` binding only delivers to verified addresses).
    - Run the `watchdog-deploy` workflow. It finds or creates the KV namespace `lakelevelfinder-watchdog-kv`, fills the KV id, alert address and repo name on the runner (never committed), deploys `lakelevelfinder-watchdog` and stores the token as Worker secret `GH_TOKEN`.
12. 👤 **Google Analytics 4** (the owner's usual Google account; check the account picker first, the default Chrome login may be a different account). Create a property `lakelevelfinder.com` in the existing account, add a web stream for `https://lakelevelfinder.com`, set event and user data retention to 14 months, and put the measurement ID in `src/site.json` (`ga4_id`). The snippet loads only on the production host, with Consent Mode v2 defaults (ads storage denied everywhere; analytics denied by default in the EEA, UK and Switzerland). The privacy page already names GA4; update its date if the setup date differs. Check Realtime shows a page view.
13. 👤 **Search Console** (same Google account). Add a Domain property `sc-domain:lakelevelfinder.com`, verify with the DNS TXT record in Cloudflare, submit `https://lakelevelfinder.com/sitemap.xml`, and request indexing for `/`, `/texas/` and the 8 largest lake pages (the daily quota is small). Add the owner's service account as a user so the Search Console connector can read the property.
14. 👤 **Bing Webmaster Tools.** Import the site from Search Console, submit the sitemap, and use URL Submission for the top 100 lake pages (about 100 a day).
15. 👤 **TWDB credit.** Before launch, write to the contact address on Water Data for Texas asking whether showing their daily figures with credit and a link is acceptable and whether they prefer a citation. Until they answer, every Texas page credits "Texas Water Development Board, Water Data for Texas" with a link.

## 2. Alerts and what to do

Alerts come from three places: GitHub Issues opened by `pipeline/notify.py` (one issue per label, new events as comments), GitHub's own workflow-failure emails, and the watchdog email (at most once every 6 hours).

| Alert | Meaning | What the site shows meanwhile | Do this |
|---|---|---|---|
| `feed-down` | A source failed 3+ runs in a row | Last reading with its date; "may be out of date" after the source's soft threshold; "Current level unknown" after the hard threshold | Open the source URL in a browser. If the agency is down, wait. If the URL or series moved, see section 3 |
| `schema-change` | The response format changed (fields, CSV header, JSON shape) | New data rejected; last good kept, ageing normally | Save a new response with `scripts/save-fixtures.sh`, update the adapter and its test, push |
| `count-mismatch` | A bulk source returned far fewer lakes than expected, or an error page | Same as above | Check the source by hand. Change the expected count only if the agency really changed its list |
| `range-reject` | One lake's reading was outside its valid range or not in feet | That lake keeps its last good reading | Compare with the agency's own page. If the feed is wrong, `hold` the lake (section 4). If the lake really moved past its range (record flood or drought), widen `valid_min_ft`/`valid_max_ft` in the mapping with evidence |
| `jump-held` | A reading moved more than 15 ft in 24 h and the next fetch has not confirmed it | Previous reading stays | Check an agency source (operator page, flood report). Real flood: `release_jump`. Bad value: let it expire or `hold` |
| `datum-mismatch` | Two sources for one lake differ by more than 0.5 ft at the same time | Lake held on its primary | Find which datum each source uses (TWDB per-lake datum line, USGS parameter code, Corps location datum) and fix the mapping. Never subtract across datums |
| `override-expired` | A manual override passed its expiry | Override ignored; feed value shown | Renew with fresh evidence or clear it |
| `workflow-failed` | The hourly run failed | Previous deploy stays online and ages into the stale states | Open the Actions log, fix, re-run with "Run workflow" |
| `accuracy-mismatch` | A live page differs from what the source says, with no lag, hold or failing source to explain it | — | Open the page and the source. Check `data/current.json` for that lake and the last update log. A real mismatch is a pipeline, build or deploy bug: fix it, then re-run `accuracy` |
| Watchdog email | `status.json` is over 3 h old, a source has 6+ failures in a row, more than 20% of lakes are "Current level unknown", or a workflow dispatch failed | Same as above | Check Actions (workflows disabled after 60 days without commits? expired Cloudflare token? expired `GH_DISPATCH_TOKEN`?). Re-run `update` by hand |

## 3. When a feed breaks

1. **Confirm it in a browser** (or with the `probe` workflow from a US runner if the host blocks your network). Note the HTTP status and what comes back.
2. **Agency outage:** do nothing. Pages age into "may be out of date" and then "Current level unknown" by design. Never fill the gap by hand.
3. **URL or series renamed** (for example a CWMS series name change): find the new series in the district catalog (`catalog/TIMESERIES?office=XXX&like=*Elev*`), fetch 7 days, check it against a second public figure for the same day, save a fixture, update the mapping, push. Do not accept a match on name alone.
4. **Format changed:** save the new response as a fixture, update the adapter and test, push.
5. **Host now answers 403, a challenge page or a login:** record it in `docs/sources/` and stop. No fake headers, proxies or headless browsers. If a fallback source exists for the lake, the pipeline uses it automatically when the primary is hard-stale. Otherwise the lake stays "Current level unknown"; after 30 days turn it into a no-feed profile that links to the operator.
6. **Source retired** (for example the legacy USGS WaterServices): remap each affected lake to the replacement service, or mark it no-feed.
7. **An agency asks us to stop or change attribution:** comply at once (hold the affected lakes), then reply from `contact@lakelevelfinder.com`.

## 4. Overrides

```bash
python3 pipeline/override.py list
python3 pipeline/override.py --help
```

Edit `data/overrides.json` only through the CLI. Actions: `hold`, `release_jump`, `set_full_pool`, `note`. Every entry needs `reason`, `evidence_url` (an agency or operator page), `set_at` and `expires_at` (at most 14 days). No override can set a water level. Commit and push `data/overrides.json`; the next hourly run applies it.

## 5. Corrections from readers

Emails to `contact@lakelevelfinder.com` about a wrong reading are answered within 24 hours (the `/corrections/` page promises this). Compare the page with the agency's own data for the same date and time; fix the cause, or `hold` the lake with the reason if the source itself is wrong; reply with what changed. Log notable cases in `docs/accuracy-log.md`.

## 6. Routine checks

- Daily: the `accuracy` workflow (30 lakes in rotation) adds a row to `docs/accuracy-log.md`.
- Weekly: read the digest issue (lakes by freshness state, source success rates, biggest movers, repo size); compare 2–3 lakes per source with the agency page by eye and note it in the log.
- Weekly (automatic): the pipeline re-downloads the last 60 days per lake and overwrites values the source revised.
- Monthly: check the repo size (growth valve at 500 MB: move old raw snapshots out of git).
- Each season: re-check full-pool values that came from operator pages (seasonal pools, new surveys).
