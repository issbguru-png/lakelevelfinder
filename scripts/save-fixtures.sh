#!/bin/bash
# Save fresh real responses for every source into tests/fixtures/_fresh/<date>/<source>/ for review.
# The committed fixtures in tests/fixtures/<source>/ are what the tests assert against, so this script never
# overwrites them: compare the fresh files by eye, then copy a file over when a source's format changes.
# Small requests only; honest User-Agent; a 403 or a challenge page is recorded and not retried.
# Usage: scripts/save-fixtures.sh            (run from the repo root or anywhere)
set -u
cd "$(dirname "$0")/.."
DAY=$(date -u +%Y-%m-%d)
UA="LakeLevelFinder/0.1 (+https://lakelevelfinder.com)"
OUT="tests/fixtures/_fresh/$DAY"
END=$(date -u +%Y-%m-%dT%H:00:00Z)
BEGIN=$(python3 -c "import datetime as d;print((d.datetime.now(d.timezone.utc)-d.timedelta(days=8)).strftime('%Y-%m-%dT00:00:00Z'))")
AFTER=$(python3 -c "import datetime as d;print((d.date.today()-d.timedelta(days=10)).isoformat())")
START=$(python3 -c "import datetime as d;print((d.date.today()-d.timedelta(days=7)).isoformat())")
TODAY=$(date -u +%Y-%m-%d)
mkdir -p "$OUT"/{twdb,cwms,rise,cdec,usgs}

get() { # url file [extra curl args...]
  local url="$1" file="$2"; shift 2
  curl -sS -m 60 -A "$UA" "$@" "$url" -o "$OUT/$file" -w "%{http_code} %{size_download}B %{time_total}s  $file\n"
  sleep 1
}

get "https://www.waterdatafortexas.org/reservoirs/recent-conditions.json" twdb/recent-conditions.json
for s in travis medina canyon; do
  get "https://www.waterdatafortexas.org/reservoirs/individual/$s-30day.csv" "twdb/$s-30day.csv"
done

C="https://cwms-data.usace.army.mil/cwms-data"
H=(-H "Accept: application/json;version=2")
get "$C/timeseries?office=SWL&name=Table_Rock_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev&begin=$BEGIN&end=$END&unit=EN" cwms/table-rock-ts.json "${H[@]}"
get "$C/timeseries?office=SAM&name=Buford.Elev.Inst.15Minutes.0.Raw-USGS&begin=$BEGIN&end=$END&unit=EN" cwms/lanier-ts.json "${H[@]}"
get "$C/levels?office=SWL&level-id-mask=Table_Rock*&unit=EN" cwms/table-rock-levels.json "${H[@]}"

R="https://data.usbr.gov/rise/api"
get "$R/catalog-item/6123" rise/mead-item.json -H "Accept: application/vnd.api+json"
get "$R/result?itemId=6123&dateTime%5Bafter%5D=$AFTER&itemsPerPage=50" rise/mead-result.json -H "Accept: application/vnd.api+json"
get "$R/result?itemId=6124&dateTime%5Bafter%5D=$AFTER&itemsPerPage=50" rise/mead-storage-result.json -H "Accept: application/vnd.api+json"

D="https://cdec.water.ca.gov/dynamicapp/req"
get "$D/CSVDataServlet?Stations=SHA,FOL,ORO&SensorNums=6,15&dur_code=D&Start=$START&End=$TODAY" cdec/daily.csv
get "$D/JSONDataServlet?Stations=SHA,FOL,ORO&SensorNums=6,15&dur_code=H&Start=$START&End=$TODAY" cdec/hourly.json

U="https://api.waterdata.usgs.gov/ogcapi/v0/collections"
get "$U/latest-continuous/items?f=json&parameter_code=62614&state_name=Texas&limit=1000" usgs/latest-continuous-tx.json
get "$U/continuous/items?f=json&monitoring_location_id=USGS-08179500&parameter_code=62614&datetime=$BEGIN/$END&limit=1000" usgs/continuous-medina.json

echo "saved under $OUT (not used by the tests until copied into tests/fixtures/<source>/)"
