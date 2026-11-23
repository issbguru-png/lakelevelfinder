# Accuracy log

Daily spot check (`tools/accuracy_check.py`, run by the `accuracy` workflow at about 14:00 UTC): 30 lakes a day in rotation, each re-fetched from its source and compared with the elevation and reading date on the live lake page. Differences explained by a source update since the last hourly run (`lag`), a hold or override (`held`) or a failing source (`stale`) are not mismatches. Weekly, also compare 2–3 lakes per source with the agency's own page by eye and note it here.

| Date (UTC) | Who | Lakes checked (by source) | Result | Notes |
|---|---|---|---|---|
