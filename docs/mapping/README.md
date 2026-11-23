# Lake mapping

How each lake page is tied to one official series (plan 3.5–3.7). Generated files in this folder:

| File | What it holds |
|---|---|
| `evidence.json` | Every request URL, the value and timestamp seen, units, full-pool source and quoted text, and every rejection, as written by `tools/map_lakes.py` |
| `twdb.md`, `cwms.md`, `rise.md`, `cdec.md`, `usgs.md` | Per-source evidence tables and rejected lakes |
| `profiles.md` | High-demand lakes with no proven feed (noindex profile pages) |
| (research folder) | `tools/demand.py` writes keyword detail and unmatched entities to `data/research/lakes-plan/demand/`, outside this repo |

## Rebuild

```
python3 tools/map_lakes.py --cache /some/cache/dir    # network; ~10 min cold (TWDB full-history CSVs), seconds warm
python3 tools/demand.py                               # demand.json + each lake's "demand"
python3 -m pipeline.refdata                           # validate data/ref/*.json
```

`--offline` rebuilds from the cache only; `--refresh` ignores it.

## Acceptance rules

- **Fresh:** TWDB date ≤ 4 days old; CWMS last non-null value ≤ 48 h; RISE row ≤ 6 days; CDEC daily row ≤ 4 days.
- **Units:** feet only (CWMS `units: "ft"` with `unit=EN`; CDEC `FEET`; RISE item title "Elevation-ft").
- **Full pool in the same datum:** from the same source (TWDB `conservation_pool_elevation`; CWMS `/levels`) or from a usbr.gov page whose text is quoted in the evidence. Otherwise `full_pool_ft` is null ("level only"); CDEC lakes still get percent full from storage ÷ CDEC capacity.
- **Plausible:** CWMS value within 60 ft of full pool; every value inside `valid_min_ft`–`valid_max_ft`.
- **Seasonal pools:** CWMS seasonal levels store the highest value as `full_pool_ft` and the level id as `primary.full_pool_level`, so the pipeline can read the value in force on each run. TWDB seasonal pools are noted in `full_pool_note`; the JSON's `conservation_pool_elevation` is the value in force.
- **Validation range (`valid_range_basis` on each non-TWDB lake):** TWDB = dead pool − 5 ft (or the 20-year low − 5 ft if lower) to the record high + 10 ft from the despiked full-history CSV, kept between full pool + 10 and + 60 ft; CWMS = lowest USACE bottom level − 5 ft to top of dam (or top of flood + 10); RISE = USBR dead pool − 5 ft to USBR maximum water surface + 5 ft (else full ± margin); CDEC = daily history since 1990 (0.1th/99.9th percentile) ± 10 ft.
- **Names and ids:** plan 6.1. The searched form was chosen from `lakes-keywords.csv` (clean "lake X" counts rather than "X lake level" forms, which inflate "X lake"). Ids end with the dam's state.
