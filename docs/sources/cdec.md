# California Data Exchange Center (CDEC, California DWR)

Verified 2026-11-23 from the build Mac. 1–3 s per call; one profile request was reset once (retry worked).

## Endpoints
- CSV: `https://cdec.water.ca.gov/dynamicapp/req/CSVDataServlet?Stations=SHA,FOL,…&SensorNums=6,15&dur_code=D&Start=YYYY-MM-DD&End=YYYY-MM-DD` (20 stations in one call works).
- JSON: `…/req/JSONDataServlet?Stations=<ID>&SensorNums=<n>&dur_code=H&Start=…&End=…`.
- Reservoir profile (capacity): `https://cdec.water.ca.gov/dynamicapp/profile?s=<ID>&type=res` → "Dam Name", "Lake Name", "Stream Name", "Capacity | N af".

## Fields
CSV header: `STATION_ID,DURATION,SENSOR_NUMBER,SENSOR_TYPE,DATE TIME,OBS DATE,VALUE,DATA_FLAG,UNITS`. Sensor 6 = `RES ELE` (FEET), sensor 15 = `STORAGE` (AF).

## Quirks
- Missing values: `---` (CSV), `-9999` (JSON). The newest rows are often blank.
- Daily rows: `DATE TIME` 20261121 0000 with `OBS DATE` 20261122 0000 for most stations, but equal for SNL, MIL, CSI, CCH, LOP. The mapping uses **`DATE TIME`** as the day the value belongs to.
- Don Pedro (DNP) and Camanche (CMN) daily rows lag 2 days more than the others.
- NAC (Nacimiento) returned no rows for sensors 6/15 in the last 8 days: not mapped.
- No full-pool elevation in CDEC; capacity comes from the profile page. Percent full = storage ÷ capacity (`capacity_source` stored per lake).
- Hourly series: use `{"source":"cdec","series_id":"SHA","dur":"H"}` in a lake slot; daily is the default.

## Terms
State data. The conditions-of-use page was not read in this pass (to do in Phase 0). Credit "California Department of Water Resources, CDEC" with the station id and link.
