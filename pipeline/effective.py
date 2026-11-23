"""What a lake page shows at build time (plan §4.2 C, §7.6, §8.5, §8.9).

effective(reading, lake, source, now) -> a copy of the reading plus:
  freshness         "current" | "delayed" | "unknown"
  age_hours         now - the reading's own timestamp (daily values: END of the covered day, lake time zone)
  reading_ts        that timestamp as ISO UTC (for data-reading-ts; the page script re-checks it in the browser)
  soft_stale_hours  hard_stale_hours   the thresholds used (for data-soft-h / data-hard-h)
  show_badges       False when freshness is "unknown" (band and trend badges must be removed)
  unknown_reason    None, "stale", "no reading", the pipeline's hold reason, or the override reason
  notice            a `note` override {text, evidence_url, set_at, expires_at} or None
  override          the `hold` override in force, or None
  full_pool_ft      the full pool used (a `set_full_pool` override replaces the lake's value and the
                    band / vs_full_pool_ft / in_flood_pool are recomputed from it)
A page can never look current on stale data, and no override can supply a level.
"""
import os

from pipeline import common, derive, overrides as ov
from pipeline.common import parse_ts

_CACHE = {"mtime": None, "data": []}


def _overrides():
    p = ov.path()
    try:
        st = os.stat(p)
    except OSError:
        return []
    m = (str(p), st.st_mtime_ns, st.st_size)
    if _CACHE["mtime"] != m:
        _CACHE.update(mtime=m, data=ov.load())
    return _CACHE["data"]


def reading_time(reading, lake):
    """Aware UTC datetime of the reading: a date means the END of that local day."""
    at = (reading or {}).get("reading_at")
    if not at:
        return None
    return parse_ts(at, tz=(lake or {}).get("timezone") or "UTC", end_of_day=common.is_date_str(at))


def thresholds(reading, source):
    src = source or {}
    rid = (reading or {}).get("source")
    kind = (reading or {}).get("reading_kind")
    if (rid and rid != src.get("id")) or (rid == "cdec" and kind == "hourly"):
        src = common.source_for(common.load_sources(), rid, kind)
    return float(src.get("soft_stale_hours", 6)), float(src.get("hard_stale_hours", 48))


def effective(reading, lake, source, now, overrides=None):
    lake = lake or {}
    out = dict(reading or {})
    out.setdefault("lake_id", lake.get("id"))
    soft, hard = thresholds(reading, source)
    st = lake.get("stale_hours") or {}
    if (not out.get("used_fallback")) and st.get("soft") and st.get("hard"):
        soft, hard = float(st["soft"]), float(st["hard"])
    t = reading_time(reading, lake)
    age = None if t is None else round(max(0.0, (now - t).total_seconds() / 3600), 2)
    if age is None or out.get("elev_ft") is None:
        fresh, why = "unknown", "no reading"
    elif age > hard:
        fresh, why = "unknown", "stale"
    elif age > soft:
        fresh, why = "delayed", None
    else:
        fresh, why = "current", None
    if out.get("hold_reason"):
        fresh, why = "unknown", out["hold_reason"]

    acts = ov.for_lake(now, lake.get("id") or out.get("lake_id"), data=_overrides() if overrides is None else overrides)
    full_pool = lake.get("full_pool_ft")
    if out.get("full_pool_ft") is not None:  # the feed's own value (TWDB conservation_pool_elevation)
        full_pool = out["full_pool_ft"]
    if "set_full_pool" in acts:
        o = acts["set_full_pool"]
        full_pool = float(o["full_pool_ft"])
        out["full_pool_override"] = {k: o.get(k) for k in ("full_pool_ft", "full_pool_label", "reason",
                                                           "evidence_url", "set_at", "expires_at")}
        elev = out.get("elev_ft")
        out["vs_full_pool_ft"] = derive.vs_full_pool(elev, full_pool)
        out["band"] = derive.band(elev, full_pool, out.get("pct_full"), lake.get("flood_control"), lake.get("bands"))
        out["in_flood_pool"] = derive.in_flood_pool(elev, full_pool, lake.get("flood_control"), lake.get("bands"))
    out["full_pool_ft"] = full_pool
    out["override"] = None
    if "hold" in acts:
        fresh, why = "unknown", acts["hold"]["reason"]
        out["override"] = acts["hold"]
    note = acts.get("note")
    out["notice"] = ({k: note.get(k) for k in ("text", "evidence_url", "set_at", "expires_at")} if note else None)
    out.update(freshness=fresh, age_hours=age, reading_ts=common.iso(t), soft_stale_hours=soft,
               hard_stale_hours=hard, show_badges=fresh != "unknown", unknown_reason=why)
    return out
