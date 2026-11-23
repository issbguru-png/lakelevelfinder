#!/bin/bash
# Build and upload dist/ to Cloudflare (Workers static assets; config in wrangler.jsonc).
#   ./deploy.sh            -> every published state
#   ./deploy.sh 1          -> release batch 1 only (same as PUBLISH_BATCH=1)
#   ./deploy.sh --dry-run  -> run every check and a wrangler dry run; upload nothing
#   ./deploy.sh 1 --dry-run
# Refuses to deploy if the domain is not confirmed in src/site.json, a unit test fails, or build.py --check fails.
set -euo pipefail
cd "$(dirname "$0")"

DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    [0-9]*) export PUBLISH_BATCH="$a" ;;
    *) echo "Unknown argument: $a"; exit 2 ;;
  esac
done

if [ "$DRY" = 0 ] && ! python3 -c 'import json,sys; sys.exit(0 if json.load(open("src/site.json")).get("domain_confirmed") else 1)'; then
  echo "src/site.json: domain_confirmed is false. Register the domain, set it in site.json and wrangler.jsonc, then retry."
  exit 1
fi

LOG="${TMPDIR:-/tmp}/lakelevelfinder-tests.log"
python3 -m unittest discover -s tests > "$LOG" 2>&1 || { echo "Unit tests failed (see $LOG)"; exit 1; }
python3 build.py --check || { echo "Build check failed: not deploying"; exit 1; }

if [ "$DRY" = 1 ]; then
  echo "Checks passed. Dry run: wrangler will not upload anything."
  npx --yes wrangler@4 deploy --dry-run
  exit 0
fi
npx --yes wrangler@4 deploy
