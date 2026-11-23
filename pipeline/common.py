"""Shared helpers: paths, time, JSON I/O, reference data. Python standard library only.

Environment (tests and local runs):
  LLF_DATA   data directory (default <repo>/data)
  LLF_LAKES  path of the lakes file (default <LLF_DATA>/ref/lakes.json)
  LLF_NOW    fake current time, e.g. 2026-11-23T08:00:00Z
"""
import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("LLF_DATA") or (ROOT / "data"))
REF = DATA / "ref"
UTC = dt.timezone.utc

# Plan §7.6 (freshness) and §8.4 (check interval). data/ref/sources.json overrides any key; these fill the gaps.
SOURCE_DEFAULTS = {
    "twdb": {"id": "twdb", "agency": "Texas Water Development Board",
             "credit": "Texas Water Development Board, Water Data for Texas",
             "public_url": "https://www.waterdatafortexas.org/reservoirs/statewide",
             "value_kind": "daily_average", "check_every_hours": 3, "soft_stale_hours": 48,
             "hard_stale_hours": 96, "expected_count": 123, "min_count": 118, "provisional": True},
    "rise": {"id": "rise", "agency": "US Bureau of Reclamation", "credit": "US Bureau of Reclamation, RISE",
             "public_url": "https://data.usbr.gov/", "value_kind": "daily_instant", "check_every_hours": 6,
             "soft_stale_hours": 60, "hard_stale_hours": 120, "provisional": True},
    "cdec": {"id": "cdec", "agency": "California Department of Water Resources",
             "credit": "California Department of Water Resources, CDEC", "public_url": "https://cdec.water.ca.gov/",
             "value_kind": "daily_average", "check_every_hours": 3, "soft_stale_hours": 48,
             "hard_stale_hours": 96, "provisional": True},
    "cdec_hourly": {"id": "cdec_hourly", "agency": "California Department of Water Resources",
                    "credit": "California Department of Water Resources, CDEC",
                    "public_url": "https://cdec.water.ca.gov/", "value_kind": "hourly", "check_every_hours": 1,
                    "soft_stale_hours": 6, "hard_stale_hours": 48, "provisional": True},
    "cwms": {"id": "cwms", "agency": "US Army Corps of Engineers", "credit": "US Army Corps of Engineers, CWMS",
             "public_url": "https://water.usace.army.mil/", "value_kind": "hourly", "check_every_hours": 1,
             "soft_stale_hours": 6, "hard_stale_hours": 48, "provisional": True},
    "usgs": {"id": "usgs", "agency": "US Geological Survey", "credit": "US Geological Survey",
             "public_url": "https://waterdata.usgs.gov/", "value_kind": "15min", "check_every_hours": 1,
             "soft_stale_hours": 6, "hard_stale_hours": 48, "provisional": True},
}
DAILY_KINDS = ("daily_average", "daily_instant")


def utcnow():
    """Current time (UTC, whole seconds). LLF_NOW fakes it for tests."""
    fake = os.environ.get("LLF_NOW")
    if fake:
        return parse_ts(fake)
    return dt.datetime.now(UTC).replace(microsecond=0)


def iso(t):
    if t is None:
        return None
    if isinstance(t, dt.datetime):
        return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return t.isoformat()  # a date


def is_date_str(s):
    return isinstance(s, str) and len(s) == 10 and s[4] == "-" and s[7] == "-"


def parse_ts(s, tz=None, end_of_day=False):
    """ISO date-time -> aware UTC datetime. A bare 'YYYY-MM-DD' means the LOCAL day in `tz` (default UTC):
    returns the start of that day, or its end (next midnight) with end_of_day=True. None -> None."""
    if s is None or s == "":
        return None
    if isinstance(s, dt.datetime):
        return s.astimezone(UTC) if s.tzinfo else s.replace(tzinfo=UTC)
    if isinstance(s, dt.date):
        s = s.isoformat()
    if is_date_str(s):
        d = dt.date.fromisoformat(s)
        if end_of_day:
            d = d + dt.timedelta(days=1)
        z = zone(tz)
        return dt.datetime(d.year, d.month, d.day, tzinfo=z).astimezone(UTC)
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC)


def zone(tz):
    if tz is None:
        return UTC
    if isinstance(tz, dt.tzinfo):
        return tz
    return ZoneInfo(tz)


def local_date(t, tz):
    """Local calendar date of an aware datetime (or a date, returned unchanged)."""
    if isinstance(t, dt.datetime):
        return t.astimezone(zone(tz)).date()
    return t


def from_epoch_ms(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, UTC)


def load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj, indent=1):
    """Atomic write (temp file, then rename) so a crash never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def lakes_path():
    return Path(os.environ.get("LLF_LAKES") or (REF / "lakes.json"))


def load_lakes(path=None):
    """data/ref/lakes.json is a JSON list of Lake records (plan §8.3)."""
    lakes = load_json(path or lakes_path(), []) or []
    if isinstance(lakes, dict):  # tolerate {id: record}
        lakes = [dict(v, id=v.get("id", k)) for k, v in lakes.items()]
    return lakes


def load_sources():
    """Source records keyed by id: data/ref/sources.json on top of SOURCE_DEFAULTS."""
    ref = load_json(REF / "sources.json", {}) or {}
    if isinstance(ref, list):
        ref = {s["id"]: s for s in ref}
    out = {k: dict(v) for k, v in SOURCE_DEFAULTS.items()}
    for k, v in ref.items():
        out[k] = {**out.get(k, {}), **v, "id": k}
    return out


def source_for(sources, source_id, reading_kind=None):
    """The source record whose thresholds apply. A CDEC hourly reading uses 'cdec_hourly'."""
    if source_id == "cdec" and reading_kind == "hourly":
        source_id = "cdec_hourly"
    return sources.get(source_id) or SOURCE_DEFAULTS.get(source_id) or {"id": source_id}


def r2(x):
    return None if x is None else round(float(x) + 0.0, 2)


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
