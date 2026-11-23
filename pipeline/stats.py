"""Per-lake statistics from the stored daily series (plan §7.3, §8.3 Stats, §8.8 Honesty).

Templates only print these figures; no number is typed into copy by hand.
- record_high / record_low: only when the series has >= 365 daily values; always with the period of record
  (period_start-period_end). Never call it an all-time record unless the full history was backfilled.
- same_day_last_year: nearest valid reading within ±3 days of the date one year before period_end.
- median_this_date: median of the same calendar day across all stored years, only with >= 5 years.
- yearly[]: one row per calendar year; partial when fewer than 300 daily values.
- last_14[] / spark_30[]: one entry per calendar day; a missing day is elev_ft null (a break, not a line).
"""
import datetime as dt

from pipeline import common, series
from pipeline.common import median, r2

MIN_RECORD_DAYS = 365
MIN_MEDIAN_YEARS = 5
PARTIAL_YEAR = 300
SAME_DAY_TOL = 3
MONTHS_KEPT = 60


def _pt(r):
    return {"elev_ft": r2(r["elev_ft"]), "date": r["date"].isoformat()}


def _year_ago(d, years=1):
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 29 February
        return d.replace(year=d.year - years, day=28)


def compute(rows, today=None):
    rows = [r for r in rows if r.get("elev_ft") is not None]
    if not rows:
        return {"period_start": None, "period_end": None, "n_days": 0, "has_history": False,
                "record_high": None, "record_low": None, "year_high": None, "year_low": None,
                "same_day_last_year": None, "median_this_date": None, "yearly": [], "monthly": [],
                "last_14": [], "spark_30": [], "gaps": [], "backfilled_days": 0}
    rows.sort(key=lambda r: r["date"])
    idx = series.by_date(rows)
    start, end = rows[0]["date"], rows[-1]["date"]
    n = len(rows)
    has_hist = n >= MIN_RECORD_DAYS
    out = {"period_start": start.isoformat(), "period_end": end.isoformat(), "n_days": n, "has_history": has_hist,
           "backfilled_days": sum(1 for r in rows if r.get("flag") == "bf")}

    # Records: the highest/lowest daily value; the earliest date wins a tie.
    hi = max(rows, key=lambda r: (r["elev_ft"], -r["date"].toordinal()))
    lo = min(rows, key=lambda r: (r["elev_ft"], r["date"].toordinal()))
    out["record_high"] = _pt(hi) if has_hist else None
    out["record_low"] = _pt(lo) if has_hist else None

    last_year = [r for r in rows if r["date"] > end - dt.timedelta(days=365)]
    out["year_high"] = _pt(max(last_year, key=lambda r: (r["elev_ft"], -r["date"].toordinal())))
    out["year_low"] = _pt(min(last_year, key=lambda r: (r["elev_ft"], r["date"].toordinal())))

    ago = series.value_near(idx, _year_ago(end), SAME_DAY_TOL)
    out["same_day_last_year"] = _pt(ago) if ago else None

    md = (end.month, end.day)
    same_day = [r for r in rows if (r["date"].month, r["date"].day) == md]
    years = sorted({r["date"].year for r in same_day})
    out["median_this_date"] = ({"elev_ft": r2(median([r["elev_ft"] for r in same_day])), "years": len(years),
                                "first_year": years[0], "last_year": years[-1]}
                               if len(years) >= MIN_MEDIAN_YEARS else None)

    yearly = {}
    for r in rows:
        y = yearly.setdefault(r["date"].year, {"year": r["date"].year, "high": None, "high_date": None,
                                               "low": None, "low_date": None, "n": 0})
        y["n"] += 1
        if y["high"] is None or r["elev_ft"] > y["high"]:
            y["high"], y["high_date"] = r2(r["elev_ft"]), r["date"].isoformat()
        if y["low"] is None or r["elev_ft"] < y["low"]:
            y["low"], y["low_date"] = r2(r["elev_ft"]), r["date"].isoformat()
    for y in yearly.values():
        y["partial"] = y["n"] < PARTIAL_YEAR
    out["yearly"] = [yearly[k] for k in sorted(yearly)]

    months = {}
    for r in rows:
        months.setdefault((r["date"].year, r["date"].month), []).append(r["elev_ft"])
    keys = sorted(months)[-MONTHS_KEPT:]
    out["monthly"] = [{"year": y, "month": m, "avg": r2(sum(months[(y, m)]) / len(months[(y, m)])),
                       "n": len(months[(y, m)])} for y, m in keys]

    def day_entry(d):
        r = idx.get(d)
        prev = idx.get(d - dt.timedelta(days=1))
        e = {"date": d.isoformat(), "elev_ft": r2(r["elev_ft"]) if r else None,
             "pct_full": r.get("pct_full") if r else None, "storage_af": r.get("storage_af") if r else None,
             "change_ft": r2(r["elev_ft"] - prev["elev_ft"]) if (r and prev) else None}
        return e

    out["last_14"] = [day_entry(end - dt.timedelta(days=k)) for k in range(13, -1, -1)]
    out["spark_30"] = [{"date": (end - dt.timedelta(days=k)).isoformat(),
                        "elev_ft": r2(idx[end - dt.timedelta(days=k)]["elev_ft"]) if (end - dt.timedelta(days=k)) in idx else None}
                       for k in range(29, -1, -1)]
    out["gaps"] = [[a.isoformat(), b.isoformat()] for a, b in series.gaps(rows, start=max(start, end - dt.timedelta(days=365)))]
    return out


def change_from_series(rows, day, days, tol=0):
    """elev(day) - elev(day - days) from stored rows; None when either is missing."""
    idx = series.by_date(rows)
    a = idx.get(day)
    b = series.value_near(idx, day - dt.timedelta(days=days), tol)
    if not a or not b:
        return None
    return r2(a["elev_ft"] - b["elev_ft"])


def write(lake_id, rows, dry=False):
    st = compute(rows)
    st["lake_id"] = lake_id
    if not dry:
        common.save_json(common.DATA / "stats" / f"{lake_id}.json", st)
    return st
