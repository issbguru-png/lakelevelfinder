"""Validation (plan §8.4 steps 5-6, §8.5, §8.6). A failed lake keeps its last good reading.

Per lake, a candidate reading is:
  rejected  units not ft; no elevation; outside valid_min_ft..valid_max_ft (or dead pool - 5 .. full pool + 60
            when no range is set); pct_full outside 0-100; timestamp more than 2 h in the future (a date: its
            local start); older than the last accepted reading from the same source
  held      |change| vs the last accepted reading > 15 ft per 24 h. It is accepted when the NEXT fetch shows
            the same level within 0.5 ft, or when a release_jump override is in force. Never dropped silently.
  same      same timestamp and level as the last accepted reading (only fetched_at moves)
  accepted  everything else (a revised value for the same day is accepted, subject to the jump guard)
Per source (raised by the adapters as SourceError): wrong body type, HTML, count under min_count.
"""
import datetime as dt

from pipeline import common
from pipeline.common import parse_ts, r2
from pipeline.sources import SourceError

JUMP_FT_PER_DAY = 15.0
CONFIRM_TOL_FT = 0.5
FUTURE_HOURS = 2
RANGE_BELOW_DEAD_POOL = 5.0
RANGE_ABOVE_FULL_POOL = 60.0


def valid_range(lake):
    lo, hi = lake.get("valid_min_ft"), lake.get("valid_max_ft")
    if lo is None and lake.get("dead_pool_ft") is not None:
        lo = lake["dead_pool_ft"] - RANGE_BELOW_DEAD_POOL
    if hi is None and lake.get("full_pool_ft") is not None:
        hi = lake["full_pool_ft"] + RANGE_ABOVE_FULL_POOL
    return lo, hi


def in_range(elev, lake):
    lo, hi = valid_range(lake)
    return (lo is None or elev >= lo) and (hi is None or elev <= hi)


def ts_bounds(reading, lake):
    """(start, end) of the reading's time: equal for an instant, the local day for a date."""
    at = reading.get("reading_at")
    tz = lake.get("timezone") or "UTC"
    if common.is_date_str(at):
        return parse_ts(at, tz), parse_ts(at, tz, end_of_day=True)
    t = parse_ts(at)
    return t, t


def check_reading(cand, prev, lake, now):
    """Return None when the candidate passes the hard checks, else (alert_label, reason)."""
    if cand.get("units", "ft") != "ft":
        return "range-reject", f"units {cand.get('units')!r}, expected ft"
    elev = cand.get("elev_ft")
    if elev is None:
        return "range-reject", "no elevation"
    if not cand.get("reading_at"):
        return "schema-change", "no timestamp"
    if not in_range(elev, lake):
        lo, hi = valid_range(lake)
        return "range-reject", f"{elev} ft outside the valid range {lo}..{hi}"
    pct = cand.get("pct_full")
    if pct is not None and not (0.0 <= pct <= 100.0):
        return "range-reject", f"pct_full {pct} outside 0-100"
    start, _ = ts_bounds(cand, lake)
    if start > now + dt.timedelta(hours=FUTURE_HOURS):
        return "range-reject", f"timestamp {cand['reading_at']} is in the future"
    if prev and prev.get("reading_at") and prev.get("source") == cand.get("source"):
        _, p_end = ts_bounds(prev, lake)
        _, c_end = ts_bounds(cand, lake)
        if c_end < p_end:
            return "range-reject", f"timestamp {cand['reading_at']} older than the accepted {prev['reading_at']}"
    return None


def is_jump(cand, prev, lake):
    if not prev or prev.get("elev_ft") is None:
        return False
    _, c = ts_bounds(cand, lake)
    _, p = ts_bounds(prev, lake)
    days = max(1.0, (c - p).total_seconds() / 86400)
    return abs(cand["elev_ft"] - prev["elev_ft"]) > JUMP_FT_PER_DAY * days


def decide(cand, prev, pending, lake, now, release=False):
    """Return (action, reason, alert_label, new_pending). action: accept | same | hold | reject."""
    bad = check_reading(cand, prev, lake, now)
    if bad:
        return "reject", bad[1], bad[0], pending
    if prev and prev.get("reading_at") == cand["reading_at"] and r2(prev.get("elev_ft")) == r2(cand["elev_ft"]) \
            and prev.get("source") == cand.get("source"):
        return "same", None, None, None
    if is_jump(cand, prev, lake):
        if release:
            return "accept", "jump released by override", None, None
        if pending and pending.get("elev_ft") is not None and abs(cand["elev_ft"] - pending["elev_ft"]) <= CONFIRM_TOL_FT:
            return "accept", f"jump confirmed by a second fetch ({pending['elev_ft']} -> {cand['elev_ft']})", None, None
        reason = (f"jump {r2(cand['elev_ft'] - prev['elev_ft']):+} ft vs the accepted {prev['elev_ft']} "
                  f"({prev['reading_at']}); held until the next fetch confirms it")
        new = {"elev_ft": cand["elev_ft"], "reading_at": cand["reading_at"], "source": cand.get("source"),
               "first_seen": (pending or {}).get("first_seen") or common.iso(now),
               "seen": ((pending or {}).get("seen") or 0) + 1}
        return "hold", reason, "jump-held", new
    return "accept", None, None, None


def check_count(n, source, name=None):
    """Bulk sources: count >= min_count (plan §8.4 step 6)."""
    m = (source or {}).get("min_count")
    if m is not None and n < m:
        raise SourceError(f"{name or (source or {}).get('id')}: {n} records, fewer than min_count {m}")
