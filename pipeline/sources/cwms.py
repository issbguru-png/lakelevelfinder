"""USACE CWMS Data API (plan §3.3.4, §8.6).

timeseries?office=SWL&name=<series>&begin=..&end=..&unit=EN  ->  {name, office-id, units, interval, time-zone,
total, page, next-page, page-size, values: [[epoch_ms, value, quality_code], ...], vertical-datum-info}

Rules: always unit=EN; reject any series whose units is not 'ft' (unit=SI returns 'm'); drop null values;
round to 2 dp (raw values look like 909.7599999999999); 8-day window; follow next-page until it is absent.
Lake targets carry the office in "office", or the series id is written "OFFICE:Series.Name".
Optional "full_pool_level": a location-level id such as
"Table_Rock_Dam.Elev-Pool.Inst.0.Top of Conservation"; its value at fetch time (seasonal values are applied:
Table Rock is 917.0 ft in November and 915.0 ft in January per /levels) is returned as extras.full_pool_ft,
so the full pool comes from the same source as the series (plan §3.5 rule 4).
"""
import calendar
import datetime as dt
import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from pipeline.common import UTC, from_epoch_ms, iso, r2
from pipeline.fetcher import FetchError, fetch as http_fetch
from pipeline.sources import SourceError, num

BASE = "https://cwms-data.usace.army.mil/cwms-data/"
WINDOW_DAYS = 8
MAX_PAGES = 120
WORKERS = 4
HEADERS = {"Accept": "application/json;version=2"}


def office_and_name(target):
    sid = target["series_id"]
    office = target.get("office")
    if target.get("name"):
        sid = target["name"]
    elif ":" in sid and sid.split(":", 1)[0].isupper() and len(sid.split(":", 1)[0]) == 3:
        pre, rest = sid.split(":", 1)
        if not office or office == pre:
            office, sid = pre, rest
    if not office:
        raise SourceError(f"cwms: no office for series {sid!r}")
    return office, sid


def ts_url(office, name, begin, end, page=None, page_size=None):
    q = {"office": office, "name": name, "begin": iso(begin), "end": iso(end), "unit": "EN"}
    if page_size:
        q["page-size"] = page_size
    if page:
        q["page"] = page
    return BASE + "timeseries?" + urllib.parse.urlencode(q)


def parse_page(body):
    """One timeseries page -> (points, meta). Raises SourceError on wrong units or shape."""
    try:
        d = json.loads(body) if isinstance(body, (bytes, str)) else body
    except ValueError as e:
        raise SourceError(f"cwms: body is not JSON ({e})") from None
    if not isinstance(d, dict) or "values" not in d:
        raise SourceError("cwms: response has no 'values'")
    units = d.get("units")
    if units != "ft":
        raise SourceError(f"cwms: units {units!r}, expected 'ft' (unit=EN)")
    pts, quality = [], {}
    for row in d.get("values") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            raise SourceError(f"cwms: bad value row {row!r}")
        v = num(row[1])
        if row[0] is None or v is None:
            continue
        pts.append((from_epoch_ms(row[0]), r2(v)))
        q = row[2] if len(row) > 2 else None
        quality[q] = quality.get(q, 0) + 1
    meta = {k: d.get(k) for k in ("name", "office-id", "units", "interval", "time-zone", "total", "next-page")}
    meta["quality_codes"] = quality
    meta["vertical_datum"] = d.get("vertical-datum-info")
    return pts, meta


def fetch_series(office, name, now, get=None, days=WINDOW_DAYS, begin=None, page_size=None):
    get = get or http_fetch
    begin = begin or (now - dt.timedelta(days=days))
    pts, meta, page, quality = [], None, None, {}
    for _ in range(MAX_PAGES):
        body, _ = get(ts_url(office, name, begin, now, page, page_size), expect="json", headers=HEADERS)
        p, m = parse_page(body)
        pts += p
        meta = meta or m
        for q, n in m["quality_codes"].items():
            quality[q] = quality.get(q, 0) + n
        page = m.get("next-page")
        if not page:
            break
    else:
        raise SourceError(f"cwms: more than {MAX_PAGES} pages for {name}")
    pts = sorted({t: v for t, v in pts}.items())
    return {"points": pts, "units": "ft", "kind": "hourly",
            "extras": {"office": office, "time_zone": meta.get("time-zone"), "interval": meta.get("interval"),
                       "quality_codes": quality, "vertical_datum": meta.get("vertical_datum")}}


def fetch(targets, now, get=None, source=None):
    """One request (plus pages) per series, 4 at a time. A failing series never stops the others."""
    series, errors = {}, {}

    def one(t):
        office, name = office_and_name(t)
        s = fetch_series(office, name, now, get)
        lvl = t.get("full_pool_level")
        if lvl:
            try:
                body, _ = (get or http_fetch)(levels_url(office, lvl), expect="json", headers=HEADERS)
                spec = lvl.rsplit(".", 1)[-1]
                val = parse_levels(body, now).get(spec)
                s["extras"].update(full_pool_ft=val, full_pool_label=spec, full_pool_level=lvl)
                if val is None:
                    s["extras"]["full_pool_error"] = f"level {lvl!r} not found"
            except (FetchError, SourceError, ValueError) as e:
                s["extras"]["full_pool_error"] = str(e)
        return t["series_id"], s

    uniq = list({t["series_id"]: t for t in targets}.values())
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {t["series_id"]: ex.submit(one, t) for t in uniq}
        for sid, f in futs.items():
            try:
                _, s = f.result()
                if not s["points"]:
                    errors[sid] = "cwms: no values in the 8-day window"
                series[sid] = s
            except (FetchError, SourceError) as e:
                errors[sid] = str(e)
    return series, errors


# ---- pool levels (/levels): "Top of Conservation" etc., used by mapping tools and checks

def levels_url(office, mask, when=None):
    q = {"office": office, "level-id-mask": mask, "unit": "EN"}
    if when:
        q["effective-date"] = iso(when)
    return BASE + "levels?" + urllib.parse.urlencode(q)


def _add_months(t, months):
    y, m = divmod(t.month - 1 + months, 12)
    y += t.year
    m += 1
    day = min(t.day, calendar.monthrange(y, m)[1])
    return t.replace(year=y, month=m, day=day)


def level_value(rec, when):
    """Value of one location-level record at `when`: constant-value, or seasonal-values interpolated
    (interpolate-string 'T') or stepped within the yearly interval starting at interval-origin."""
    if rec.get("constant-value") is not None:
        return r2(rec["constant-value"])
    sv = rec.get("seasonal-values") or []
    if not sv:
        return None
    origin = dt.datetime.fromisoformat(rec.get("interval-origin", "2001-01-01T00:00:00Z").replace("Z", "+00:00"))
    months = int(rec.get("interval-months") or 12)
    start = origin.replace(year=when.year)
    while start > when:
        start = _add_months(start, -months)
    while _add_months(start, months) <= when:
        start = _add_months(start, months)
    marks = [(_add_months(start, int(s.get("offset-months") or 0)) + dt.timedelta(minutes=int(s.get("offset-minutes") or 0)),
              float(s["value"])) for s in sv]
    marks.sort()
    marks.append((_add_months(start, months), marks[0][1]))  # wraps to the next interval's first value
    prev = marks[0]
    for t, v in marks[1:]:
        if when < t:
            if rec.get("interpolate-string", "T") == "T" and t > prev[0] and when >= prev[0]:
                frac = (when - prev[0]) / (t - prev[0])
                return r2(prev[1] + (v - prev[1]) * frac)
            return r2(prev[1])
        prev = (t, v)
    return r2(prev[1])


def parse_levels(body, when, parameter="Elev-Pool"):
    """/levels response -> {specified-level-id: value_ft} at `when`, latest level-date <= when per level.
    Only records whose level-units-id is 'ft' and whose parameter matches are used."""
    d = json.loads(body) if isinstance(body, (bytes, str)) else body
    if not isinstance(d, dict) or "levels" not in d:
        raise SourceError("cwms levels: response has no 'levels'")
    best = {}
    for rec in d["levels"]:
        if rec.get("level-units-id") != "ft" or parameter not in (rec.get("location-level-id") or ""):
            continue
        ld = dt.datetime.fromisoformat(rec["level-date"].replace("Z", "+00:00"))
        if ld > when:
            continue
        k = rec.get("specified-level-id")
        if k not in best or ld > best[k][0]:
            best[k] = (ld, rec)
    return {k: level_value(rec, when.astimezone(UTC)) for k, (_, rec) in best.items()}
