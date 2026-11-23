# USACE CWMS Data API

Verified 2026-11-23 from the build Mac. No key. Calls took 1–3 s.

## Endpoints
- Series: `https://cwms-data.usace.army.mil/cwms-data/timeseries?office=<OFF>&name=<series>&begin=<ISO>&end=<ISO>&unit=EN&page-size=5000` with header `Accept: application/json;version=2`.
- Pool levels: `…/levels?office=<OFF>&level-id-mask=<Location>.*&unit=EN&page-size=1000` (same Accept header). Use `<Location>.*`, not `<Location>*`, or neighbouring locations leak in (e.g. `Carters*` also returns `CartersRereg`).
- Catalog: `…/catalog/TIMESERIES?office=<OFF>&like=<pattern>&page-size=1000` (default units in the catalog are metric).

## Fields
Series: `name`, `office-id`, `units` ("ft" with `unit=EN`), `interval`, `time-zone`, `values` = `[epoch_ms, value, quality]`, `next-page` when paged. Round values to 2 dp. Null values occur; skip them. Quality codes seen: 0 and 3.

Levels: `location-level-id`, `specified-level-id` (e.g. "Top of Conservation"), `parameter-id` (`Elev`, `Elev-Pool`, `Stor`), `level-date` (effective date; several versions can exist), `constant-value` **or** `seasonal-values` [{`value`, `offset-months`, `offset-minutes`}] with `interval-origin` / `interval-months`. For a seasonal level pick the latest `level-date` ≤ now; the pipeline evaluates the current value (store the level id as `primary.full_pool_level`). Example: Table Rock "Top of Conservation" = 917.0 ft through November, 915.0 ft from Dec 1.

## Naming by district (verified series patterns)
| Office | Pool series | Level location | Full-pool level used |
|---|---|---|---|
| SWL | `<Dam>_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev` | `<Dam>_Dam` | Top of Conservation (seasonal) |
| SWT | `<CODE>.Elev.Inst.1Hour.0.Ccp-Rev` (4-letter code, e.g. KEYS) | `<CODE>` | Top of Conservation |
| SAS | `<Proj>.Elev-Pool.Inst.1Hour.0.Raw-SHEF_SAS` | `<Proj>` | Top of Conservation (seasonal) |
| SAM | `<Proj>.Elev.Inst.15Minutes.0.Raw-USGS`, Alabama Power lakes `<Proj>.Elev.Inst.1Hour.0.Raw-APCO` | `<Proj>` | Top of Conservation (seasonal) |
| LRL | `<Proj>.Elev.Inst.1Hour.0.lrldlb-comp` (irregular, a few values per day) | `<Proj>` | Top of Conservation (= summer pool; "Top of Normal" = winter pool) |
| LRN | `<NWSID>-<NAME>.Elev-Pool.Inst.1Hour.0.dcp-rev` | same | Top of Power |
| SAW | `ROA JHK.Elev.Inst.5Minutes.0.USGS-raw`, `NEU FLP…15Minutes.0.USGS-raw` | same | Top of Conservation (Falls only; Kerr and Jordan have only storage levels) |
| MVK | `<Name> Lake.Elev.Inst.1Hour.0.DCP-rev` | same | Top of Conservation |
| MVS | `<Name> Lk-<River>.Elev.Inst.30Minutes.0.lrgsShef-rev` | same | max(Top of Conservation, Summer Guide Curve) |
| LRH | `<Name>-Lake.Elev.Inst.15Minutes.0.OBS` | same | Top of Recreation |
| LRP | `<Name>-Lake.Elev.Inst.1Hour.0.OBS` | same | Top of Conservation (seasonal) |

## Quirks and traps
- **Always `unit=EN`**; `unit=ft` is a 400.
- Catalog `latest-time` is unreliable; freshness is read from the series values only.
- `Elev-Head` is the head (pool − tailwater), not the pool: Hartwell's `Elev-Head` read 172 ft while `Elev-Pool` read 647 ft.
- `Decodes-raw` LRL series (`Rough-Lake…Decodes-raw`, 40.5 ft) are stage, not elevation. Use `lrldlb-comp`.
- NWK (Truman, Stockton, Pomme de Terre), NWO, NWW, NWP, NAN returned 0 elevation series in the catalog on 2026-11-23.
- SWT `Ccp-Rev` series lag ~12–14 h (last value 18:00 UTC the previous day at 07:30 UTC).
- MVS `lrgsShef-rev` last value was ~13 h old; LRL `lrldlb-comp` ~20 h old.
- Blue Mountain (SWL) last value was 2026-11-20 (stale): rejected.
- The location vertical datum is not given in the series; pool levels and readings come from the same office and project, so they are compared as published.

## Terms
US federal data; no terms page found. Credit "U.S. Army Corps of Engineers, <District>" with the office code; say "provisional".
