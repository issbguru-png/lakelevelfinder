"""History backfill into data/series/{lake_id}.csv with flag 'bf' (plan §8.8). Resumable, polite.

  python3 tools/backfill.py                       every lake in data/ref/lakes.json, by its primary source
  python3 tools/backfill.py --lake lake-mead-nv   one lake (repeatable)
  python3 tools/backfill.py --source twdb         one source
  python3 tools/backfill.py --years 5             CWMS/CDEC/USGS: years back from today (default 5)
  python3 tools/backfill.py --from-year 2020      RISE: start here instead of the item's first year
  python3 tools/backfill.py --dry-run             fetch and report, write nothing
  python3 tools/backfill.py --restart             ignore data/backfill-state.json

Per source:
  twdb   individual/<slug>.csv once per lake; strip '#' lines; keep date, water_level, percent_full,
         reservoir_storage. Slug = the lake's primary.twdb_slug, else the lower-cased key when the key is a single
         word (the multi-word slug rule is not verified: such lakes are skipped until twdb_slug is mapped).
  rise   result/download in yearly chunks from the catalog item's temporalStartDate (Mead: 1935); a RISE storage
         item mapped in extra.storage is merged by date.
  cwms   timeseries paged with next-page, one window per year for the last N years, reduced to end-of-day values.
  cdec   daily CSV (sensors 6 and 15) per year; '---' dropped.
  usgs   the OGC 'daily' collection (statistic 00003, daily mean) per year, following next links.
At least --sleep seconds (default 1.0) between requests. Finished chunks are recorded in data/backfill-state.json
(the current year is always re-fetched). A backfilled row never replaces a live row (series.merge).
"""
import argparse
import datetime as dt
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import common, series  # noqa: E402
from pipeline.common import iso, load_json, local_date, save_json, utcnow  # noqa: E402
from pipeline.fetcher import FetchError, fetch as http_fetch  # noqa: E402
from pipeline.sources import SourceError, cdec, cwms, rise, twdb, usgs  # noqa: E402


class Throttle:
    """Wraps the getter so consecutive requests are at least `interval` seconds apart."""

    def __init__(self, get, interval):
        self.get, self.interval, self.last = get, interval, 0.0
        self.count = 0

    def __call__(self, url, **kw):
        wait = self.interval - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        try:
            return self.get(url, **kw)
        finally:
            self.last = time.monotonic()
            self.count += 1


def year_chunks(first_year, now):
    return [(y, dt.date(y, 1, 1), min(dt.date(y, 12, 31), now.date())) for y in range(first_year, now.year + 1)]


def twdb_slug(slot):
    if slot.get("twdb_slug"):
        return slot["twdb_slug"]
    key = str(slot["series_id"])
    return key.lower() if key.isalpha() else None


def rows_twdb(lake, slot, get, now, done, log):
    slug = twdb_slug(slot)
    if not slug:
        log(f"  skip: TWDB key {slot['series_id']!r} needs primary.twdb_slug (multi-word slug rule not verified)")
        return [], []
    body, _ = get(twdb.CSV_URL.format(slug=slug), expect="csv")
    p = twdb.parse_csv(body)
    gaps = twdb.missing_dates(p["rows"])
    log(f"  twdb {slug}: {len(p['rows'])} rows {p['rows'][0]['date'] if p['rows'] else ''}..; datum {p['datum']!r}; "
        f"{len(gaps)} missing date(s)")
    rows = [{"date": r["date"], "elev_ft": r["water_level"], "pct_full": r["percent_full"],
             "storage_af": r["reservoir_storage"], "src": "twdb", "flag": "bf"}
            for r in p["rows"] if r["water_level"] is not None]
    return rows, ["all"]


def rows_rise(lake, slot, get, now, done, log, from_year=None):
    item = str(slot["series_id"])
    meta = rise.parse_item(get(rise.item_url(item), expect="json", headers=rise.HEADERS)[0])
    if (meta.get("parameterUnit") or "").lower() != "ft":
        raise SourceError(f"rise item {item}: unit {meta.get('parameterUnit')!r}, expected ft")
    start = max(int((meta.get("temporalStartDate") or f"{now.year}")[:4]), from_year or 0)
    sto_slot = (lake.get("extra") or {}).get("storage") or {}
    sto_item = str(sto_slot.get("series_id")) if sto_slot.get("source") == "rise" else None
    rows, finished = [], []
    for y, a, b in year_chunks(start, now):
        key = f"rise:{item}:{y}"
        if key in done:
            continue
        _, pts = rise.parse_download(get(rise.download_url(item, a, b), expect="json")[0])
        sto = {}
        if sto_item:
            _, sp = rise.parse_download(get(rise.download_url(sto_item, a, b), expect="json")[0])
            sto = dict(sp)
        cap = lake.get("capacity_af")
        for d, v in pts:
            s = sto.get(d)
            rows.append({"date": d, "elev_ft": v, "storage_af": s,
                         "pct_full": round(100.0 * min(s, cap) / cap, 1) if (s is not None and cap) else None,
                         "src": "rise", "flag": "bf"})
        if y < now.year:
            finished.append(key)
        log(f"  rise {item} {y}: {len(pts)} values")
    return rows, finished


def rows_cwms(lake, slot, get, now, done, log, years):
    office, name = cwms.office_and_name(slot)
    tz = lake.get("timezone") or "UTC"
    today = local_date(now, tz)
    rows, finished = [], []
    for y, a, b in year_chunks(now.year - years + 1, now):
        key = f"cwms:{office}:{name}:{y}"
        if key in done:
            continue
        begin = dt.datetime(y, 1, 1, tzinfo=common.UTC) - dt.timedelta(days=1)
        end = min(dt.datetime(y + 1, 1, 1, tzinfo=common.UTC) + dt.timedelta(days=1), now)
        s = cwms.fetch_series(office, name, end, get, begin=begin, page_size=5000)  # a year of 5-minute data is ~105K points
        new = [r for r in series.rows_from_points(s["points"], tz, "cwms", "hourly", before=today, flag="bf")
               if r["date"].year == y]
        rows += new
        if y < now.year:
            finished.append(key)
        log(f"  cwms {office} {name} {y}: {len(s['points'])} points -> {len(new)} days")
    return rows, finished


def rows_cdec(lake, slot, get, now, done, log, years):
    st = str(slot["series_id"]).upper()
    rows, finished = [], []
    for y, a, b in year_chunks(now.year - years + 1, now):
        key = f"cdec:{st}:{y}"
        if key in done:
            continue
        s = cdec.parse_daily_csv(get(cdec.daily_url([st], a, b), expect="csv")[0], [st])[st]
        sto = dict(s["extras"]["storage_points"])
        cap = lake.get("capacity_af")
        for d, v in s["points"]:
            sv = sto.get(d)
            rows.append({"date": d, "elev_ft": v, "storage_af": sv,
                         "pct_full": round(100.0 * min(sv, cap) / cap, 1) if (sv is not None and cap) else None,
                         "src": "cdec", "flag": "bf"})
        if y < now.year:
            finished.append(key)
        log(f"  cdec {st} {y}: {len(s['points'])} values")
    return rows, finished


def rows_usgs(lake, slot, get, now, done, log, years):
    site, param = str(slot["series_id"]), str(slot.get("param") or "")
    rows, finished = [], []
    for y, a, b in year_chunks(now.year - years + 1, now):
        key = f"usgs:{site}:{param}:{y}"
        if key in done:
            continue
        s = usgs.daily_series(usgs.get_all(usgs.daily_url(site, param, a, b), get), param, site)
        rows += [{"date": d, "elev_ft": v, "pct_full": None, "storage_af": None, "src": "usgs", "flag": "bf"}
                 for d, v in s["points"]]
        if y < now.year:
            finished.append(key)
        log(f"  usgs {site} {y}: {len(s['points'])} daily means")
    return rows, finished


def main(argv=None, get=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lake", action="append")
    ap.add_argument("--source")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--from-year", type=int)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--lakes")
    a = ap.parse_args(argv)
    if a.sleep < 1.0 and get is None:
        ap.error("--sleep must be at least 1.0 second")
    now = utcnow()
    getter = Throttle(get or http_fetch, a.sleep)
    state_path = common.DATA / "backfill-state.json"
    state = {} if a.restart else (load_json(state_path, {}) or {})
    lakes = [lk for lk in common.load_lakes(a.lakes) if lk.get("primary")]
    if a.lake:
        lakes = [lk for lk in lakes if lk["id"] in a.lake]
    failed = 0
    for lake in lakes:
        slot = lake["primary"]
        src = slot.get("source")
        if a.source and src != a.source:
            continue
        st = state.setdefault(lake["id"], {"done": [], "complete": False})
        if st.get("complete") and src == "twdb":
            print(f"{lake['id']}: already backfilled ({st.get('rows')} rows)")
            continue
        print(f"{lake['id']} ({src} {slot.get('series_id')})")
        done = set(st.get("done") or [])
        try:
            if src == "twdb":
                rows, fin = rows_twdb(lake, slot, getter, now, done, print)
            elif src == "rise":
                rows, fin = rows_rise(lake, slot, getter, now, done, print, a.from_year)
            elif src == "cwms":
                rows, fin = rows_cwms(lake, slot, getter, now, done, print, a.years)
            elif src == "cdec":
                rows, fin = rows_cdec(lake, slot, getter, now, done, print, a.years)
            elif src == "usgs":
                rows, fin = rows_usgs(lake, slot, getter, now, done, print, a.years)
            else:
                print(f"  no backfill for source {src!r}")
                continue
        except (FetchError, SourceError, ValueError) as e:
            failed += 1
            print(f"  FAILED: {e} (finished chunks are kept; re-run to resume)")
            st["last_error"] = f"{iso(now)} {e}"
            if not a.dry_run:
                save_json(state_path, state)
            continue
        rows, dropped = series.clean_rows(sorted(rows, key=lambda r: r["date"]), lake)
        for r, why in dropped:
            print(f"  dropped {r['date']} {r['elev_ft']}: {why}")
        add, chg = series.upsert(lake["id"], rows, a.dry_run)
        print(f"  series: +{add} new day(s), {chg} replaced bf row(s)")
        st["done"] = sorted(done | set(fin))
        st["rows"] = len(series.read(lake["id"])) if not a.dry_run else None
        st["complete"] = src == "twdb" and bool(fin)
        st["updated"] = iso(now)
        st.pop("last_error", None)
        if not a.dry_run:
            save_json(state_path, state)
    print(f"{getter.count} request(s); {failed} lake(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
