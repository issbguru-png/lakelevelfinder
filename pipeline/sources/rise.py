"""USBR RISE (plan §3.3.5).

result?itemId=6123&dateTime[after]=YYYY-MM-DD&itemsPerPage=50  (ALWAYS with a date filter: unfiltered calls time out)
  -> {"meta": {"totalItems"}, "data": [{"attributes": {"itemId", "dateTime": "2026-11-21T07:00:00+00:00",
      "result": 1041.41, "resultAttributes": {"timeStep": "day", "resultType": "observed"}, "lastUpdate"}}]}
catalog-item/6123 -> data.attributes.parameterUnit ("ft"), parameterTimestep ("daily"), disclaimer.
result/download?type=json&itemId=..&after=..&before=.. -> {"Units": "ft", "Timezone": "MT", "0": {"dateTime":
  "2026-11-01 07:00:00", "result": ...}, ...}  (backfill)

Daily values are stamped at local midnight in UTC (07:00Z = 00:00 MST). The value's day is the UTC calendar
date of that stamp, which is the local date for any US zone (local midnight falls 04:00-11:00Z) and for a
plain UTC-midnight stamp.
"""
import datetime as dt
import json
import urllib.parse

from pipeline.fetcher import FetchError, fetch as http_fetch
from pipeline.sources import SourceError, num

BASE = "https://data.usbr.gov/rise/api/"
LOOKBACK_DAYS = 10
UNIT_OK = {"ft", "af", "acre-feet", "acre-ft", "ac-ft"}
HEADERS = {"Accept": "application/vnd.api+json"}


def result_url(item_id, after, per_page=50):
    q = [("itemId", str(item_id)), ("dateTime[after]", after.isoformat()), ("itemsPerPage", str(per_page))]
    return BASE + "result?" + urllib.parse.urlencode(q)


def item_url(item_id):
    return f"{BASE}catalog-item/{item_id}"


def download_url(item_id, after, before):
    q = {"type": "json", "itemId": item_id, "after": after.isoformat(), "before": before.isoformat()}
    return BASE + "result/download?" + urllib.parse.urlencode(q)


def _day(stamp):
    t = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00").replace(" ", "T"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)  # the download form says "Datetime values are presented as UTC"
    return t.astimezone(dt.timezone.utc).date()


def parse_result(body, item_id=None):
    """result JSON:API page -> sorted daily points [(date, value)]."""
    d = json.loads(body) if isinstance(body, (bytes, str)) else body
    if not isinstance(d, dict) or not isinstance(d.get("data"), list):
        raise SourceError("rise: response has no 'data' list")
    pts = {}
    for row in d["data"]:
        a = (row or {}).get("attributes") or {}
        if item_id is not None and a.get("itemId") is not None and int(a["itemId"]) != int(item_id):
            raise SourceError(f"rise: row for item {a.get('itemId')} in a request for {item_id}")
        if "dateTime" not in a or "result" not in a:
            raise SourceError("rise: row without dateTime/result (not a result listing)")
        step = (a.get("resultAttributes") or {}).get("timeStep")
        if step not in (None, "day"):
            raise SourceError(f"rise: timeStep {step!r}, expected 'day'")
        v = num(a["result"])
        if v is not None:
            pts[_day(a["dateTime"])] = v
    return sorted(pts.items())


def parse_item(body):
    d = json.loads(body) if isinstance(body, (bytes, str)) else body
    a = ((d or {}).get("data") or {}).get("attributes")
    if not a:
        raise SourceError("rise: catalog item has no attributes")
    return {k: a.get(k) for k in ("_id", "itemTitle", "parameterName", "parameterUnit", "parameterTimestep",
                                  "temporalStartDate", "temporalEndDate", "disclaimer", "verticalDatum")}


def parse_download(body):
    """result/download JSON -> (meta, [(date, value)])."""
    d = json.loads(body) if isinstance(body, (bytes, str)) else body
    if not isinstance(d, dict):
        raise SourceError("rise download: not an object")
    meta = {k: d.get(k) for k in ("Units", "Timezone", "Timestep", "Aggregation")}
    pts = {}
    for k, v in d.items():
        if k.isdigit() and isinstance(v, dict) and "dateTime" in v:
            val = num(v.get("result"))
            if val is not None:
                pts[_day(v["dateTime"])] = val
    return meta, sorted(pts.items())


def fetch_item(item_id, now, get=None, days=LOOKBACK_DAYS):
    get = get or http_fetch
    meta = parse_item(get(item_url(item_id), expect="json", headers=HEADERS)[0])
    unit = (meta.get("parameterUnit") or "").lower()
    if unit not in UNIT_OK:
        raise SourceError(f"rise: item {item_id} unit {meta.get('parameterUnit')!r} not supported")
    if (meta.get("parameterTimestep") or "daily") != "daily":
        raise SourceError(f"rise: item {item_id} timestep {meta.get('parameterTimestep')!r}")
    after = (now - dt.timedelta(days=days)).date()
    pts = parse_result(get(result_url(item_id, after), expect="json", headers=HEADERS)[0], item_id)
    units = "ft" if unit == "ft" else "af"
    return {"points": pts, "units": units, "kind": "daily_instant",
            "extras": {"title": meta.get("itemTitle"), "disclaimer": meta.get("disclaimer"),
                       "vertical_datum": meta.get("verticalDatum")}}


def fetch(targets, now, get=None, source=None):
    series, errors = {}, {}
    for sid in dict.fromkeys(str(t["series_id"]) for t in targets):
        try:
            s = fetch_item(sid, now, get)
            if not s["points"]:
                errors[sid] = f"rise: item {sid} returned no values in {LOOKBACK_DAYS} days"
            series[sid] = s
        except (FetchError, SourceError, ValueError) as e:
            errors[sid] = str(e)
    return series, errors
