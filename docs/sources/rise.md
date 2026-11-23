# USBR RISE (Reclamation Information Sharing Environment)

Verified 2026-11-23 from the build Mac. No key. 1–7 s per call.

## Endpoints (send `Accept: application/vnd.api+json`)
- Data: `https://data.usbr.gov/rise/api/result?itemId=<id>&dateTime[after]=YYYY-MM-DD&itemsPerPage=50&order[dateTime]=DESC`. **Always send a date filter** (unfiltered calls time out).
- Item: `https://data.usbr.gov/rise/api/catalog-item/<id>` → `data.attributes.itemTitle`, `temporalEndDate` (catalog end dates lag; read the result rows).
- Locations: `https://data.usbr.gov/rise/api/location?itemsPerPage=100&page=N` (838 locations, 9 pages) or `location?search=<text>` (works). `location/<id>` → `relationships.catalogRecords`; `catalog-record/<id>` → `relationships.catalogItems`. **`catalog-item?locationId=` and `?search=` do not filter** (they return the whole catalog), so item ids are found through the location → record → item chain.

## Result row fields (fixture: tests/fixtures/rise/)
`attributes`: `_id`, `itemId`, `locationId`, `sourceCode` (e.g. `lchdb2`, `uchdb2`), `dateTime` (e.g. `2026-11-19T07:00:00+00:00` = midnight Mountain time, the day the value belongs to), `result` (number, ft), `status`, `parameterId`, `resultAttributes` {`timeStep`: "day", `resultType`: "observed"} (null on some Great Plains items), `lastUpdate`, `createDate`.

## Item ids mapped (elevation / storage / release)
Mead 6123 / 6124 / 6125 · Powell 508 / 509 · Flaming Gorge 341 / 337 · Navajo 612 / 613 · Cheney 133978 / 133977 · Havasu 6128 / 6129 · Mohave 6133 / 6134 · Blue Mesa 78 / 76 · Jordanelle 471 / 468 · Caballo 212 / 209 · Willard Bay 869 / 866 · Horsetooth 87 / 86 · Granby 384 / 383 · Pueblo 682 / 681 · Canyon Ferry 14 / 13 · Pathfinder 645 / 644 · Cachuma 10786 / 10787 · Millerton 10808 / 10807 (mapped to CDEC instead).
No daily elevation item: Theodore Roosevelt (location 3713), Palisades (7193), Jackson Lake (7185), American Falls (7167).

## Latency
Lower Colorado and Great Plains items: latest row = 2 days ago (2026-11-21 on 11-23). Upper Colorado items (Powell, Flaming Gorge, Blue Mesa, Jordanelle): 4 days (2026-11-19). The plan's 60 h / 120 h thresholds will mark Upper Colorado lakes "delayed" much of the time; consider 108 h / 168 h for `uchdb2` items after the Phase 0 run.

## Full pool
RISE carries no pool levels. Values come from usbr.gov pages (dam pages `https://www.usbr.gov/projects/index.php?id=<n>` "Dimensions" table, GP allocation sheets `https://www.usbr.gov/gp/aop/resaloc/<name>.pdf`, the Hoover Dam lake FAQ, the Flaming Gorge Dam page, and the 2007 shortage FEIS Appendix B for Powell). Evidence with quoted text in `docs/mapping/rise.md`.

## Terms
Catalog disclaimer: "Data are provisional and subject to revision unless otherwise noted." Credit "U.S. Bureau of Reclamation" with a link to the item.
