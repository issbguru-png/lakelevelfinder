"""California CDEC (plan §3.3.6).

Daily CSV: CSVDataServlet?Stations=SHA,FOL,ORO&SensorNums=6,15&dur_code=D&Start=..&End=..
  header STATION_ID,DURATION,SENSOR_NUMBER,SENSOR_TYPE,DATE TIME,OBS DATE,VALUE,DATA_FLAG,UNITS
  sensor 6 = RES ELE (FEET), sensor 15 = STORAGE (AF). Newest rows are '---' until filled.
Hourly JSON: JSONDataServlet?...&dur_code=H -> [{stationId, durCode, SENSOR_NUM, sensorType, date "2026-11-22 21:00",
  obsDate, value, dataFlag, units}], missing = -9999.

Reading date for daily rows: 'DATE TIME' (the day the value is for). 'OBS DATE' is often the next midnight
(SHA 20261120 0000 -> OBS 20261121 0000), so it is not used. Hourly times are local station time without a zone;
they are read as Pacific Standard Time (UTC-8) all year: CDEC's PST convention is general knowledge and is
NOT verified from a primary page (open issue; affects hourly timestamps by 1 h in summer only if wrong).
"""
import csv
import datetime as dt
import io
import json
import urllib.parse

from pipeline.fetcher import fetch as http_fetch
from pipeline.sources import SourceError, num

BASE = "https://cdec.water.ca.gov/dynamicapp/req/"
CSV_HEADER = ["STATION_ID", "DURATION", "SENSOR_NUMBER", "SENSOR_TYPE", "DATE TIME", "OBS DATE", "VALUE",
              "DATA_FLAG", "UNITS"]
SENSOR_ELEV, SENSOR_STORAGE = 6, 15
PST = dt.timezone(dt.timedelta(hours=-8), "PST")
DAILY_DAYS = 8
HOURLY_DAYS = 8
UNITS = {"FEET": "ft", "AF": "af"}


def daily_url(stations, start, end):
    q = {"Stations": ",".join(stations), "SensorNums": "6,15", "dur_code": "D",
         "Start": start.isoformat(), "End": end.isoformat()}
    return BASE + "CSVDataServlet?" + urllib.parse.urlencode(q, safe=",")


def hourly_url(stations, start, end):
    q = {"Stations": ",".join(stations), "SensorNums": "6,15", "dur_code": "H",
         "Start": start.isoformat(), "End": end.isoformat()}
    return BASE + "JSONDataServlet?" + urllib.parse.urlencode(q, safe=",")


def _empty(stations):
    return {s: {"points": [], "units": "ft", "kind": None, "extras": {"storage_points": [], "flags": {}}}
            for s in stations}


def parse_daily_csv(body, stations=None):
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or [c.strip() for c in rows[0]] != CSV_HEADER:
        raise SourceError(f"cdec: unexpected CSV header {rows[0] if rows else None}")
    out = {}
    for r in rows[1:]:
        if not r:
            continue
        if len(r) != len(CSV_HEADER):
            raise SourceError(f"cdec: bad CSV row {r}")
        st, dur, sensor, _, date_time, _, value, flag, units = [c.strip() for c in r]
        if dur != "D":
            raise SourceError(f"cdec: duration {dur!r} in a daily request")
        s = out.setdefault(st, _empty([st])[st])
        s["kind"] = "daily_average"
        v = num(value)
        if v is None:
            continue
        day = dt.datetime.strptime(date_time, "%Y%m%d %H%M").date()
        _add(s, int(sensor), units, day, v, flag)
    return _finish(out, stations)


def parse_hourly_json(body, stations=None):
    d = json.loads(body) if isinstance(body, (bytes, str)) else body
    if not isinstance(d, list):
        raise SourceError("cdec: hourly JSON is not a list")
    out = {}
    for r in d:
        if not isinstance(r, dict) or "stationId" not in r or "value" not in r:
            raise SourceError(f"cdec: bad hourly row {r!r}")
        if r.get("durCode") != "H":
            raise SourceError(f"cdec: durCode {r.get('durCode')!r} in an hourly request")
        s = out.setdefault(r["stationId"], _empty([r["stationId"]])[r["stationId"]])
        s["kind"] = "hourly"
        v = num(r["value"])
        if v is None:
            continue
        t = dt.datetime.strptime(r["date"], "%Y-%m-%d %H:%M").replace(tzinfo=PST).astimezone(dt.timezone.utc)
        _add(s, int(r["SENSOR_NUM"]), r.get("units"), t, v, r.get("dataFlag"))
    return _finish(out, stations)


def _add(s, sensor, units, ts, v, flag):
    u = UNITS.get((units or "").strip().upper())
    if sensor == SENSOR_ELEV:
        if u != "ft":
            raise SourceError(f"cdec: elevation units {units!r}, expected FEET")
        s["points"].append((ts, v))
        if flag and flag.strip():
            s["extras"]["flags"][str(ts)] = flag.strip()
    elif sensor == SENSOR_STORAGE:
        if u != "af":
            raise SourceError(f"cdec: storage units {units!r}, expected AF")
        s["extras"]["storage_points"].append((ts, v))


def _finish(out, stations):
    for s in out.values():
        s["points"].sort()
        s["extras"]["storage_points"].sort()
    for st in stations or []:
        out.setdefault(st, _empty([st])[st])
    return out


def fetch(targets, now, get=None, source=None, hourly=False):
    """One request for all stations. A station missing from the response is a per-station error."""
    get = get or http_fetch
    stations = list(dict.fromkeys(str(t["series_id"]).upper() for t in targets))
    if not stations:
        return {}, {}
    end = now.astimezone(PST).date()
    if hourly:
        body, _ = get(hourly_url(stations, end - dt.timedelta(days=HOURLY_DAYS), end), expect="json")
        series = parse_hourly_json(body, stations)
    else:
        body, _ = get(daily_url(stations, end - dt.timedelta(days=DAILY_DAYS), end), expect="csv")
        series = parse_daily_csv(body, stations)
    errors = {st: f"cdec: no valid elevation for {st}" for st in stations if not series[st]["points"]}
    return series, errors


def fetch_hourly(targets, now, get=None, source=None):
    return fetch(targets, now, get, source, hourly=True)

