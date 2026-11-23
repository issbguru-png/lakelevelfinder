"""Daily series files data/series/{lake_id}.csv (plan §8.3 Series row, §8.8).

Columns: date,elev_ft,pct_full,storage_af,src,flag   one row per LOCAL calendar day.
  flag ''    daily-average/daily source value as published
  flag 'eod' hourly source: last valid reading of the local day
  flag 'bf'  backfilled from the source's history download
A live value replaces a row whose value differs (sources revise provisional data); a backfilled value never
replaces a live row. Missing days stay missing (charts break, stats skip them).
"""
import csv
import datetime as dt
import io
import os
import tempfile
from pathlib import Path

from pipeline import common
from pipeline.common import local_date, r2

HEADER = ["date", "elev_ft", "pct_full", "storage_af", "src", "flag"]
FLAGS = ("", "eod", "bf")


def path(lake_id):
    return common.DATA / "series" / f"{lake_id}.csv"


def _num(s, cast=float):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return cast(float(s)) if cast is int else cast(s)
    except ValueError:
        return None


def read(lake_id=None, p=None):
    """Rows sorted by date: {date, elev_ft, pct_full, storage_af, src, flag}. Missing file -> []."""
    p = Path(p) if p else path(lake_id)
    if not p.exists():
        return []
    rows = {}
    with open(p, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                d = dt.date.fromisoformat(r["date"])
            except (KeyError, ValueError):
                continue
            rows[d] = {"date": d, "elev_ft": _num(r.get("elev_ft")), "pct_full": _num(r.get("pct_full")),
                       "storage_af": _num(r.get("storage_af"), int), "src": (r.get("src") or "").strip(),
                       "flag": (r.get("flag") or "").strip()}
    return [rows[d] for d in sorted(rows)]


def _fmt(v, dp=None):
    if v is None:
        return ""
    if dp is None:
        return str(int(round(v)))
    return f"{round(float(v), dp):.{dp}f}"


def to_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(HEADER)
    for r in sorted(rows, key=lambda r: r["date"]):
        w.writerow([r["date"].isoformat(), _fmt(r.get("elev_ft"), 2), _fmt(r.get("pct_full"), 1),
                    _fmt(r.get("storage_af")), r.get("src") or "", r.get("flag") or ""])
    return buf.getvalue()


def write(lake_id, rows, p=None):
    p = Path(p) if p else path(lake_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(to_csv(rows))
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)


def _same(a, b):
    return (r2(a.get("elev_ft")) == r2(b.get("elev_ft")) and
            (None if a.get("pct_full") is None else round(a["pct_full"], 1)) ==
            (None if b.get("pct_full") is None else round(b["pct_full"], 1)) and
            (None if a.get("storage_af") is None else int(round(a["storage_af"]))) ==
            (None if b.get("storage_af") is None else int(round(b["storage_af"]))))


def merge(old_rows, new_rows):
    """Merge rows by date. Returns (rows, added, changed). bf never replaces a live row."""
    by = {r["date"]: dict(r) for r in old_rows}
    added = changed = 0
    for r in new_rows:
        if r.get("elev_ft") is None:
            continue
        cur = by.get(r["date"])
        if cur is None:
            by[r["date"]] = dict(r)
            added += 1
        elif r.get("flag") == "bf" and cur.get("flag") != "bf":
            continue
        elif not _same(cur, r) or (cur.get("flag") == "bf" and r.get("flag") != "bf"):
            by[r["date"]] = dict(r)
            changed += 1
    return [by[d] for d in sorted(by)], added, changed


def upsert(lake_id, new_rows, dry=False):
    """Merge new rows into the lake's file. Returns (added, changed)."""
    old = read(lake_id)
    rows, added, changed = merge(old, new_rows)
    if (added or changed) and not dry:
        write(lake_id, rows)
    return added, changed


def daily_last(points, tz, before=None):
    """Instantaneous points -> {local_date: (ts, value)} with the LAST valid reading of each local day.
    `before`: only days strictly before this local date (complete days)."""
    out = {}
    for t, v in sorted(points):
        if v is None:
            continue
        d = local_date(t, tz)
        if before is not None and d >= before:
            continue
        out[d] = (t, v)
    return out


def _on_day(points, tz):
    return {d: v for d, (_, v) in daily_last(points or [], tz).items()}


def rows_from_points(points, tz, src, kind, storage_points=None, pct_points=None, before=None, flag=None):
    """Build series rows. Daily sources: one row per dated point (flag ''). Hourly sources: last valid
    reading of each complete local day (flag 'eod')."""
    daily = kind in common.DAILY_KINDS
    if flag is None:
        flag = "" if daily else "eod"
    elev = {d: v for d, v in points if (before is None or d < before)} if daily else \
        {d: v for d, (_, v) in daily_last(points, tz, before).items()}
    sto = dict(storage_points or []) if daily else _on_day(storage_points, tz)
    pct = dict(pct_points or []) if daily else _on_day(pct_points, tz)
    return [{"date": d, "elev_ft": r2(v), "pct_full": pct.get(d), "storage_af": sto.get(d), "src": src,
             "flag": flag} for d, v in sorted(elev.items())]


def gaps(rows, start=None, end=None):
    """Missing days as [(first_missing, last_missing), ...] between the first and last row (or start/end)."""
    have = sorted(r["date"] for r in rows if r.get("elev_ft") is not None)
    if not have:
        return []
    d, stop = start or have[0], end or have[-1]
    have = set(have)
    out, run = [], None
    while d <= stop:
        if d not in have:
            run = (run[0], d) if run else (d, d)
        elif run:
            out.append(run)
            run = None
        d += dt.timedelta(days=1)
    if run:
        out.append(run)
    return out


def by_date(rows):
    return {r["date"]: r for r in rows if r.get("elev_ft") is not None}


def value_near(index, day, tol_days=0):
    """Row for `day`, else the nearest within ±tol_days (earlier wins a tie). None when nothing is near."""
    if day in index:
        return index[day]
    for k in range(1, tol_days + 1):
        for d in (day - dt.timedelta(days=k), day + dt.timedelta(days=k)):
            if d in index:
                return index[d]
    return None


def clean_rows(rows, lake, spike_ft=5.0, agree_ft=2.0, max_gap_days=3):
    """Drop impossible history values before they reach charts and records.

    Returns (kept, dropped). A row is dropped when its elevation is outside the lake's
    valid_min_ft/valid_max_ft, or when it is an isolated spike: it differs from both
    neighbours by more than spike_ft while the neighbours (each within max_gap_days)
    agree with each other within agree_ft. Live rows (flag != 'bf') are never touched
    by the spike rule; the live pipeline has its own jump hold.
    """
    lo, hi = lake.get("valid_min_ft"), lake.get("valid_max_ft")
    kept, dropped = [], []
    for r in rows:
        e = r.get("elev_ft")
        if e is not None and ((lo is not None and e < lo) or (hi is not None and e > hi)):
            dropped.append((r, "outside valid range"))
        else:
            kept.append(r)
    out = []
    for i, r in enumerate(kept):
        e = r.get("elev_ft")
        if e is None or r.get("flag") != "bf" or i == 0 or i == len(kept) - 1:
            out.append(r)
            continue
        p, n = kept[i - 1], kept[i + 1]
        pe, ne = p.get("elev_ft"), n.get("elev_ft")
        if (pe is not None and ne is not None
                and (r["date"] - p["date"]).days <= max_gap_days and (n["date"] - r["date"]).days <= max_gap_days
                and abs(pe - ne) <= agree_ft and abs(e - pe) > spike_ft and abs(e - ne) > spike_ft):
            dropped.append((r, f"isolated spike vs {pe} / {ne}"))
            continue
        out.append(r)
    return out, dropped
