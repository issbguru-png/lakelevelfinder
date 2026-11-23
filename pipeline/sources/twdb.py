"""TWDB Water Data for Texas (plan §3.3.1, §3.3.2).

recent-conditions.json: one object keyed by TWDB lake key ('Travis', "Lake O' the Pines", ...), 123 keys.
Per lake: timestamp (a date: the day the average covers), elevation, percent_full,
conservation_pool_elevation, dead_pool_elevation, conservation_storage, conservation_capacity, volume,
area, flood_control_lake ('Y' or null), gauge_location, full_name ...

percent_full is capped at 100 by TWDB (storage capped at capacity), so 'above full pool' must come from
elevation - conservation_pool_elevation. percent_full is null for flood-detention reservoirs (Addicks, Barker).
Note (seen Nov 23, 2026): some lakes carry TODAY's date, i.e. a partial-day average that TWDB revises.
"""
import csv
import datetime as dt
import io
import json

from pipeline.fetcher import fetch as http_fetch
from pipeline.sources import SourceError, num

URL = "https://www.waterdatafortexas.org/reservoirs/recent-conditions.json"
CSV_URL = "https://www.waterdatafortexas.org/reservoirs/individual/{slug}.csv"
CSV_30DAY_URL = "https://www.waterdatafortexas.org/reservoirs/individual/{slug}-30day.csv"
EXTRA_FIELDS = ("percent_full", "conservation_pool_elevation", "dead_pool_elevation", "conservation_storage",
                "conservation_capacity", "volume", "area", "flood_control_lake", "full_name")
STALE_DAYS = 4
MAX_STALE_LAKES = 10


def parse(body):
    """recent-conditions.json -> series map. Raises SourceError on a non-object body."""
    try:
        data = json.loads(body)
    except ValueError as e:
        raise SourceError(f"twdb: body is not JSON ({e})") from None
    if not isinstance(data, dict):
        raise SourceError(f"twdb: expected a JSON object, got {type(data).__name__}")
    out = {}
    for key, rec in data.items():
        if not isinstance(rec, dict):
            raise SourceError(f"twdb: record {key!r} is not an object")
        elev = num(rec.get("elevation"))
        ts = rec.get("timestamp")
        try:
            day = dt.date.fromisoformat(ts) if ts else None
        except ValueError:
            raise SourceError(f"twdb: {key!r} timestamp {ts!r} is not a date") from None
        pts = [(day, elev)] if (day and elev is not None) else []
        extras = {f: rec.get(f) for f in EXTRA_FIELDS}
        for f in EXTRA_FIELDS:
            if f not in ("flood_control_lake", "full_name"):
                extras[f] = num(extras[f])
        extras["gauge_location"] = (rec.get("gauge_location") or {}).get("coordinates")
        if pts and extras["percent_full"] is not None:
            extras["pct_full_points"] = [(day, extras["percent_full"])]
        if pts and extras["volume"] is not None:
            extras["storage_points"] = [(day, extras["volume"])]
        out[key] = {"points": pts, "units": "ft", "kind": "daily_average", "extras": extras}
    return out


def check(series, source, now):
    """Per-source checks (plan §3.3.1): count >= min_count; at most 10 lakes older than 4 days."""
    n = len(series)
    min_count = (source or {}).get("min_count", 118)
    if n < min_count:
        raise SourceError(f"twdb: {n} lakes, fewer than min_count {min_count}")
    today = now.date()
    stale = [k for k, s in series.items() if not s["points"] or (today - s["points"][-1][0]).days > STALE_DAYS]
    if len(stale) > MAX_STALE_LAKES:
        raise SourceError(f"twdb: {len(stale)} lakes have no reading in the last {STALE_DAYS} days")


def fetch(targets, now, get=None, source=None):
    body, _ = (get or http_fetch)(URL, expect="json")
    series = parse(body)
    check(series, source, now)
    errors = {}
    for t in targets:
        sid = t["series_id"]
        if sid not in series:
            errors[sid] = f"twdb key {sid!r} not in recent-conditions.json"
        elif not series[sid]["points"]:
            errors[sid] = f"twdb key {sid!r} has no elevation"
    return series, errors


def parse_csv(text):
    """TWDB per-lake CSV (full history or 30-day) -> dict with rows and header facts.

    Rows: {date, water_level, surface_area, reservoir_storage, conservation_storage, percent_full,
    conservation_capacity, dead_pool_capacity} with missing values as None. Header facts from '#' lines:
    datum, conservation_pool (list of strings as printed), dead_pool, generated."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    head, body = [], []
    for line in text.splitlines():
        (head if line.startswith("#") else body).append(line)
    facts = {"datum": None, "conservation_pool": [], "dead_pool": [], "generated": None}
    section = None
    for line in head:
        s = line[1:].strip()
        low = s.lower()
        if low.startswith("conservation pool elevation"):
            section = "conservation_pool"
        elif low.startswith("dead pool elevation"):
            section = "dead_pool"
        elif low.startswith("vertical datum"):
            section = "datum"
        elif low.startswith("generated:"):
            facts["generated"] = s.split(":", 1)[1].strip()
            section = None
        elif not s or s.endswith(":"):
            section = None
        elif section == "datum" and facts["datum"] is None:
            facts["datum"] = s
        elif section in ("conservation_pool", "dead_pool"):
            facts[section].append(s)
    rows = []
    reader = csv.DictReader(io.StringIO("\n".join(body)))
    if not reader.fieldnames or "date" not in reader.fieldnames or "water_level" not in reader.fieldnames:
        raise SourceError(f"twdb csv: unexpected header {reader.fieldnames}")
    for r in reader:
        try:
            d = dt.date.fromisoformat((r.get("date") or "").strip())
        except ValueError:
            continue
        row = {"date": d}
        for k in reader.fieldnames:
            if k != "date":
                row[k] = num(r.get(k))
        rows.append(row)
    rows.sort(key=lambda r: r["date"])
    return {"rows": rows, **facts}


def csv_series(text):
    """Per-lake CSV -> one series entry (daily points) usable like parse() output."""
    p = parse_csv(text)
    pts = [(r["date"], r["water_level"]) for r in p["rows"] if r["water_level"] is not None]
    pct = [(r["date"], r["percent_full"]) for r in p["rows"] if r["percent_full"] is not None]
    sto = [(r["date"], r["reservoir_storage"]) for r in p["rows"] if r["reservoir_storage"] is not None]
    return {"points": pts, "units": "ft", "kind": "daily_average",
            "extras": {"pct_full_points": pct, "storage_points": sto, "datum": p["datum"],
                       "conservation_pool": p["conservation_pool"]}}


def missing_dates(rows):
    """Dates absent between the first and last row (plan §3.3.2: check every file for gaps)."""
    have = {r["date"] for r in rows}
    if not have:
        return []
    d, end, out = min(have), max(have), []
    while d < end:
        d += dt.timedelta(days=1)
        if d not in have:
            out.append(d)
    return out

