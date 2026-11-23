"""Series point lists -> one candidate Reading per lake (plan §8.3 Reading, §8.4 step 4, §4.2 B).

Changes:
  hourly sources  change_24h = median(last 3 readings) - median(the 3 readings nearest to 24 h before the
                  latest, all 20-28 h before it); None when there are none. change_7d = latest - the reading
                  nearest to 7 days before (within 3 h). change_30d from the stored daily series.
  daily sources   change_24h = value(day) - value(day - 1); change_7d = value(day) - value(day - 7) (±1 day);
                  change_30d = value(day) - value(day - 30) (±2 days); from the fetched window and the stored series.
"""
import datetime as dt

from pipeline import common, derive
from pipeline.common import iso, local_date, median, r2

HOURLY_TREND_MIN_H, HOURLY_TREND_MAX_H = 20, 28
SEVEN_DAY_TOL_H = 3
STORAGE_MATCH_H = 2


def latest(points):
    for t, v in reversed(points or []):
        if v is not None:
            return t, v
    return None


def change_hourly_24h(points):
    pts = [(t, v) for t, v in points or [] if v is not None]
    if len(pts) < 2:
        return None
    t_last = pts[-1][0]
    last3 = [v for _, v in pts[-3:]]
    lo, hi = t_last - dt.timedelta(hours=HOURLY_TREND_MAX_H), t_last - dt.timedelta(hours=HOURLY_TREND_MIN_H)
    target = t_last - dt.timedelta(hours=24)
    cands = sorted((p for p in pts if lo <= p[0] <= hi), key=lambda p: abs((p[0] - target).total_seconds()))[:3]
    if not cands:
        return None
    return r2(median(last3) - median([v for _, v in cands]))


def change_hourly_at(points, delta, tol_h=SEVEN_DAY_TOL_H):
    pts = [(t, v) for t, v in points or [] if v is not None]
    if not pts:
        return None
    t_last, v_last = pts[-1]
    target = t_last - delta
    near = [p for p in pts if abs((p[0] - target).total_seconds()) <= tol_h * 3600]
    if not near:
        return None
    _, v = min(near, key=lambda p: abs((p[0] - target).total_seconds()))
    return r2(v_last - v)


def _daily_index(points, rows):
    idx = {r["date"]: r["elev_ft"] for r in rows or [] if r.get("elev_ft") is not None}
    idx.update({d: v for d, v in points or [] if v is not None})
    return idx


def _near(idx, day, tol):
    if day in idx:
        return idx[day]
    for k in range(1, tol + 1):
        for d in (day - dt.timedelta(days=k), day + dt.timedelta(days=k)):
            if d in idx:
                return idx[d]
    return None


def daily_change(idx, day, days, tol=0):
    a, b = idx.get(day), _near(idx, day - dt.timedelta(days=days), tol)
    return None if a is None or b is None else r2(a - b)


def _value_at(points, t, daily):
    """Value of an auxiliary series (storage, % full) for the reading's day or time."""
    if not points:
        return None
    if daily:
        return dict(points).get(t)
    near = [(abs((pt - t).total_seconds()), v) for pt, v in points
            if v is not None and abs((pt - t).total_seconds()) <= STORAGE_MATCH_H * 3600]
    return min(near)[1] if near else None


def to_reading(lake, slot, s, source, now, rows=None, storage=None):
    """Candidate reading for `lake` from series entry `s` of the slot {source, series_id, param}.
    rows: the lake's stored daily series; storage: optional extra storage series entry (RISE item).
    Returns None when the series has no valid point."""
    last = latest(s.get("points"))
    if not last:
        return None
    t, elev = last
    kind = s.get("kind") or (source or {}).get("value_kind")
    daily = kind in common.DAILY_KINDS or isinstance(t, dt.date) and not isinstance(t, dt.datetime)
    ex = s.get("extras") or {}
    tz = lake.get("timezone") or "UTC"
    r = {"lake_id": lake["id"], "source": slot["source"], "series_id": str(slot["series_id"]),
         "units": s.get("units", "ft"), "elev_ft": r2(elev), "reading_at": iso(t), "reading_kind": kind,
         "provisional": bool((source or {}).get("provisional", True)), "quality": ex.get("quality"),
         "pct_full": None, "pct_full_basis": None, "storage_af": None, "conservation_storage_af": None,
         "area_acres": None, "full_pool_ft": None}
    if slot["source"] == "twdb":
        r["pct_full"] = ex.get("percent_full")
        r["pct_full_basis"] = "source" if r["pct_full"] is not None else None
        r["storage_af"] = None if ex.get("volume") is None else int(round(ex["volume"]))
        r["conservation_storage_af"] = (None if ex.get("conservation_storage") is None
                                        else int(round(ex["conservation_storage"])))
        r["area_acres"] = ex.get("area")
        r["full_pool_ft"] = ex.get("conservation_pool_elevation")
    else:
        r["full_pool_ft"] = ex.get("full_pool_ft")  # CWMS /levels value when the slot maps one
        sto_pts = ex.get("storage_points") or (storage or {}).get("points")
        sto = _value_at(sto_pts, t, daily)
        r["storage_af"] = None if sto is None else int(round(sto))
        cap = lake.get("capacity_af")
        if sto is not None and cap:
            r["pct_full"] = round(100.0 * min(sto, cap) / cap, 1)
            r["pct_full_basis"] = "computed_storage"
    if daily:
        idx = _daily_index(s.get("points"), rows)
        r["change_24h_ft"] = daily_change(idx, t, 1)
        r["change_7d_ft"] = daily_change(idx, t, 7, tol=1)
        r["change_30d_ft"] = daily_change(idx, t, 30, tol=2)
    else:
        r["change_24h_ft"] = change_hourly_24h(s.get("points"))
        r["change_7d_ft"] = change_hourly_at(s.get("points"), dt.timedelta(days=7))
        idx = {row["date"]: row["elev_ft"] for row in rows or [] if row.get("elev_ft") is not None}
        b = _near(idx, local_date(t, tz) - dt.timedelta(days=30), 2)
        r["change_30d_ft"] = None if b is None else r2(elev - b)
    return r


def derive_fields(r, lake):
    """vs_full_pool_ft, band, in_flood_pool, trend on a reading (full pool: the feed's own, else the lake's)."""
    fp = r.get("full_pool_ft") if r.get("full_pool_ft") is not None else lake.get("full_pool_ft")
    r["full_pool_ft"] = fp
    r["vs_full_pool_ft"] = derive.vs_full_pool(r["elev_ft"], fp)
    r["band"] = derive.band(r["elev_ft"], fp, r.get("pct_full"), lake.get("flood_control"), lake.get("bands"))
    r["in_flood_pool"] = derive.in_flood_pool(r["elev_ft"], fp, lake.get("flood_control"), lake.get("bands"))
    r["trend"] = derive.trend(r.get("change_24h_ft"))
    return r


def intraday_layer(lake, slot, s):
    """The labelled 'latest gauge reading' line for a daily-average primary (never the headline)."""
    last = latest((s or {}).get("points"))
    if not last or not isinstance(last[0], dt.datetime):
        return None
    return {"elev_ft": r2(last[1]), "at": iso(last[0]), "source": slot["source"], "series_id": str(slot["series_id"]),
            "param": str(slot.get("param") or ""), "quality": ((s.get("extras") or {}).get("quality"))}
