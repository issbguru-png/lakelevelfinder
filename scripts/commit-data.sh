#!/bin/bash
# Commit data/ only if it changed (generic bot identity), then push. Used by the update workflow and by
# pipeline/run.py inside GitHub Actions. The message comes from .commitmsg written by pipeline/run.py.
set -euo pipefail
cd "$(dirname "$0")/.."
git config user.name "lakelevelfinder-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add data
if git diff --cached --quiet; then echo "data unchanged"; exit 0; fi
if [ -s .commitmsg ]; then git commit -q -F .commitmsg; else git commit -q -m "data: update"; fi
for i in 1 2 3; do
  if git push -q; then exit 0; fi
  echo "push rejected; rebasing on the remote (attempt $i)"
  git pull -q --rebase --autostash -X theirs || { git rebase --abort || true; sleep 5; }
done
echo "push failed after 3 attempts" >&2
exit 1
