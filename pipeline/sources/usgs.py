"""USGS Water Data OGC API (plan §3.3.3, §8.6).

latest-continuous/items?f=json&parameter_code=62614&state_name=Texas&limit=..  (intraday layer, bulk by state)
continuous/items?f=json&monitoring_location_id=USGS-08179500&parameter_code=62614&datetime=A/B  (primary lakes)
daily/items?...&statistic_id=00003  (daily mean; backfill)
Feature properties: monitoring_location_id, parameter_code, statistic_id, time, value (a STRING), unit_of_measure,
approval_status, qualifier, time_series_id, last_modified. Paging: follow the 'next' link exactly as given
(it points at /ogcapi/v1/ with a cursor). Optional key: USGS_API_KEY env, sent as X-Api-Key.

Refuses: values whose parameter_code differs from the mapped code (a datum change), units other than ft.
"""
import datetime as dt
import json
import os
import urllib.parse

from pipeline.common import iso
from pipeline.fetcher import FetchError, fetch as http_fetch
from pipeline.sources import SourceError, num

BASE = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/"
MAX_PAGES = 30
PAGE_LIMIT = 1000
WINDOW_DAYS = 8
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}


def headers():
    h = {"Accept": "application/geo+json, application/json"}
    if os.environ.get("USGS_API_KEY"):
        h["X-Api-Key"] = os.environ["USGS_API_KEY"]
    return h


def latest_url(state_name, params, limit=PAGE_LIMIT):
    q = {"f": "json", "parameter_code": ",".join(sorted(params)), "state_name": state_name, "limit": limit}
    return BASE + "latest-continuous/items?" + urllib.parse.urlencode(q, safe=",")


def continuous_url(site, param, begin, end, limit=PAGE_LIMIT):
    q = {"f": "json", "monitoring_location_id": site, "parameter_code": param,
         "datetime": f"{iso(begin)}/{iso(end)}", "limit": limit}
    return BASE + "continuous/items?" + urllib.parse.urlencode(q, safe=",/:")


def daily_url(site, param, start, end, statistic="00003", limit=PAGE_LIMIT):
    q = {"f": "json", "monitoring_location_id": site, "parameter_code": param, "statistic_id": statistic,
         "datetime": f"{start.isoformat()}/{end.isoformat()}", "limit": limit}
    return BASE + "daily/items?" + urllib.parse.urlencode(q, safe=",/:")


def parse_page(body):
    """FeatureCollection page -> (rows, next_url). rows: dicts of the properties we use."""
    d = json.loads(body) if isinstance(body, (bytes, str)) else body
    if not isinstance(d, dict) or d.get("type") != "FeatureCollection" or not isinstance(d.get("features"), list):
        raise SourceError("usgs: response is not a GeoJSON FeatureCollection")
    rows = []
    for f in d["features"]:
        p = (f or {}).get("properties") or {}
        if "monitoring_location_id" not in p or "value" not in p or "time" not in p:
            raise SourceError("usgs: feature without monitoring_location_id/time/value")
        rows.append({k: p.get(k) for k in ("monitoring_location_id", "parameter_code", "statistic_id", "time",
                                            "value", "unit_of_measure", "approval_status", "qualifier",
                                            "time_series_id")})
    nxt = next((ln.get("href") for ln in d.get("links") or [] if ln.get("rel") == "next"), None)
    return rows, nxt


def get_all(url, get=None):
    get = get or http_fetch
    rows, seen = [], set()
    while url:
        if url in seen:
            raise SourceError("usgs: the next link repeats a page")
        seen.add(url)
        if len(seen) > MAX_PAGES:
            raise SourceError(f"usgs: more than {MAX_PAGES} pages")
        body, _ = get(url, expect="json", headers=headers())
        r, url = parse_page(body)
        rows += r
        if not r:
            break
    return rows


def _time(s):
    if len(s) == 10:
        return dt.date.fromisoformat(s)
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def to_series(rows, param, site=None, kind="15min"):
    """Rows of one site -> series entry. Rows with another parameter_code or units != ft raise SourceError."""
    pts, approval = {}, {}
    for r in rows:
        if site and r["monitoring_location_id"] != site:
            continue
        if str(r.get("parameter_code")) != str(param):
            raise SourceError(f"usgs: {r['monitoring_location_id']} parameter_code {r.get('parameter_code')} "
                              f"differs from the mapped {param} (datum change?)")
        if (r.get("unit_of_measure") or "").strip().lower() not in ("ft", "feet"):
            raise SourceError(f"usgs: {r['monitoring_location_id']} unit {r.get('unit_of_measure')!r}, expected ft")
        v = num(r["value"])
        if v is None:
            continue
        t = _time(r["time"])
        pts[t] = v
        approval[iso(t) if isinstance(t, dt.datetime) else t.isoformat()] = r.get("approval_status")
    pts = sorted(pts.items())
    last_q = approval.get(iso(pts[-1][0]) if pts and isinstance(pts[-1][0], dt.datetime)
                          else (pts[-1][0].isoformat() if pts else None))
    return {"points": pts, "units": "ft", "kind": kind, "extras": {"param": str(param), "quality": last_q}}


def parse_latest(rows, wanted):
    """latest-continuous rows -> {series_id: series} for the wanted {site: param}; errors for the rest."""
    by_site = {}
    for r in rows:
        by_site.setdefault(r["monitoring_location_id"], []).append(r)
    series, errors = {}, {}
    for site, param in wanted.items():
        rs = by_site.get(site, [])
        same = [r for r in rs if str(r.get("parameter_code")) == str(param)]
        if not same:
            errors[site] = (f"usgs: {site} reports parameter {sorted({r.get('parameter_code') for r in rs})}, "
                            f"not the mapped {param}") if rs else f"usgs: {site} not in latest-continuous"
            continue
        try:
            series[site] = to_series(same, param, site)
        except SourceError as e:
            errors[site] = str(e)
    return series, errors


def fetch(targets, now, get=None, source=None):
    """Intraday targets: one paged latest-continuous request per state. Primary/fallback targets: the
    continuous collection for the last 8 days, one paged request per site (trend needs a window)."""
    series, errors = {}, {}
    latest_by_state, window = {}, {}
    for t in targets:
        param = str(t.get("param") or "")
        if not param.isdigit():
            errors[t["series_id"]] = f"usgs: no parameter code for {t['series_id']}"
            continue
        if t.get("role") == "intraday":
            latest_by_state.setdefault(t.get("state"), {})[t["series_id"]] = param
        else:
            window[t["series_id"]] = param
    for st, wanted in latest_by_state.items():
        name = STATE_NAMES.get(st)
        if not name:
            errors.update({s: f"usgs: unknown state {st!r}" for s in wanted})
            continue
        try:
            rows = get_all(latest_url(name, set(wanted.values())), get)
            s, e = parse_latest(rows, wanted)
            series.update(s)
            errors.update(e)
        except (FetchError, SourceError) as e:
            errors.update({s: str(e) for s in wanted})
    begin = now - dt.timedelta(days=WINDOW_DAYS)
    for site, param in window.items():
        try:
            s = to_series(get_all(continuous_url(site, param, begin, now), get), param, site)
            if not s["points"]:
                errors[site] = f"usgs: no values for {site} in {WINDOW_DAYS} days"
            series[site] = s
        except (FetchError, SourceError) as e:
            errors[site] = str(e)
    return series, errors


def daily_series(rows, param, site):
    return to_series([r for r in rows if r.get("statistic_id") in (None, "00003")], param, site, kind="daily_average")
