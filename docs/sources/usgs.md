# USGS Water Data API (OGC)

Verified 2026-11-23 from the build Mac. No key used (optional key raises limits). 1–2 s per call.

## Endpoints
- Latest values: `https://api.waterdata.usgs.gov/ogcapi/v0/collections/latest-continuous/items?f=json&parameter_code=62614,62615,00062&state_name=Texas&limit=500` → GeoJSON FeatureCollection (178 features for Texas, one page). Follow a `next` link exactly as returned.
- Collections: `https://api.waterdata.usgs.gov/ogcapi/v0/collections?f=json`.

## Fields
`properties`: `time_series_id`, `monitoring_location_id` ("USGS-08179500"; some sites use another agency prefix, e.g. "TX071-08154500" for LCRA-run gauges), `parameter_code`, `statistic_id`, `time` (ISO with offset), `value` (string), `unit_of_measure` ("ft"), `approval_status` ("Provisional"), `qualifier`, `last_modified`. `geometry.coordinates` = [lon, lat].

## Parameter codes and datum
62614 = lake elevation above NGVD 1929, 62615 = above NAVD 1988, 00062 = reservoir elevation (local/project datum). The same site can carry two codes that differ by the datum shift (e.g. Waco 62614 458.68 vs 62615 458.90; Lake Travis TX071 00062 681.04 vs 62615 681.64). Store the parameter code with the series id and only compare values in the same datum.

## Quirks
- Some features are years old (e.g. a Bridgeport 62614 value from 2016): check `time`.
- Use as intraday layer only on TWDB lakes, attached when the gauge reads within 0.10 ft of TWDB (see `docs/mapping/usgs.md`).
- No lake-elevation features were returned for Tennessee, and none for Lake of the Ozarks, Smith Mountain Lake or Truman Lake.
- Legacy `waterservices.usgs.gov/nwis/iv` is reported for retirement on 2027-02-22: do not use.

## Terms
USGS data are public domain (general knowledge; not re-checked). Credit "U.S. Geological Survey" with the site number and "provisional".
