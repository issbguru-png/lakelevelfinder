"""Daily accuracy spot check (plan §8.7): re-fetch the source for N lakes and compare with data/current.json.

For a sample of lakes (seeded by the date, so a day's sample is repeatable) the primary source is fetched fresh
through the same adapter, and its latest valid point is compared with the published Reading:
  ok        same timestamp and the same level (to 0.01 ft)
  revised   same date, a different level within 1.5 ft, on a value at most 2 days old: the source revised a
            provisional value; the next run stores it
  lag       the source has a NEWER point than the last run picked up; the next run takes it
  held      the lake has a held jump or an active hold override
  stale     the published reading is past its hard threshold ("Current level unknown")
  MISMATCH  same timestamp, different level, or an older point at the source: nothing explains it
  unchecked the source request for that lake failed (reported as a problem, not as a mismatch)
With --live the published page is compared too: https://<domain>/lake/<id>/ must carry the headline value in a
<data class="elev" value="..."> element (the headline number; pattern settable with --live-pattern).

  python3 tools/accuracy_check.py [--n 30] [--date YYYY-MM-DD] [--lakes FILE] [--live] [--log] [--alert]
--log appends a row to docs/accuracy-log.md; --alert files a GitHub issue (label accuracy-mismatch) on a
MISMATCH or when a source could not be checked. Exit code 1 on any MISMATCH.
"""
import argparse
import datetime as dt
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import common  # noqa: E402
from pipeline import overrides as ov  # noqa: E402
from pipeline.common import iso, load_json, r2, utcnow  # noqa: E402
from pipeline.effective import effective  # noqa: E402
from pipeline.fetcher import USER_AGENT, FetchError, fetch as http_fetch  # noqa: E402
from pipeline.normalize import latest  # noqa: E402
from pipeline.run import ADAPTERS, series_key, slot_source  # noqa: E402
from pipeline.sources import SourceError  # noqa: E402

LOG = ROOT / "docs" / "accuracy-log.md"
LOG_HEADER = "| Date | Check | Scope | Result | Notes |\n|---|---|---|---|---|\n"
TOL_FT = 0.005


REVISE_FT = 1.5      # a same-date change this size on a recent provisional value is a revision, not an error
REVISE_DAYS = 2


def _recent(ts, pub):
    """True when the reading date is within REVISE_DAYS of the time the published reading was fetched."""
    try:
        d = dt.date.fromisoformat(str(ts)[:10])
        f = dt.date.fromisoformat(str(pub.get("fetched_at") or "")[:10])
    except ValueError:
        return False
    return 0 <= (f - d).days <= REVISE_DAYS


def classify(fresh_pt, pub, eff, held):
    """fresh_pt: (iso_ts, elev) from the source now; pub: published Reading; eff: effective(pub)."""
    if fresh_pt is None:
        return "MISMATCH", "source has no valid point now"
    ts, elev = fresh_pt
    if pub.get("reading_at") == ts:
        if abs(elev - pub["elev_ft"]) <= TOL_FT:
            return "ok", None
        if held:
            return "held", None
        if _recent(ts, pub) and abs(elev - pub["elev_ft"]) <= REVISE_FT:
            # sources revise provisional values for the last day or two (TWDB's current-day average changes hourly,
            # CDEC replaces carried-forward values); the next hourly run stores the revision (series.upsert)
            return "revised", f"source revised {ts}: {elev} ft, published {pub['elev_ft']} ft"
        return "MISMATCH", f"source {elev} ft vs published {pub['elev_ft']} ft for {ts}"
    if (ts or "") > (pub.get("reading_at") or ""):
        return "lag", f"source has {ts}, published {pub.get('reading_at')}"
    if held:
        return "held", None
    if eff["freshness"] == "unknown":
        return "stale", None
    return "MISMATCH", f"source's latest {ts} is older than the published {pub.get('reading_at')}"


def live_value(url, pattern, tries=2):
    for k in range(tries):
        try:
            req = urllib.request.Request(f"{url}?accuracy={int(time.time())}", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                m = re.search(pattern, r.read().decode("utf-8", errors="replace"))
            return float(m.group(1)) if m else None
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(5)


def check(n, day, now, lakes_file=None, live=False, pattern=r'<data class="elev" value="(-?[0-9.]+)"', get=None):
    lakes = {lk["id"]: lk for lk in common.load_lakes(lakes_file) if lk.get("primary")}
    current = load_json(common.DATA / "current.json", {}) or {}
    state = load_json(common.DATA / "pipeline-state.json", {}) or {}
    sources = common.load_sources()
    site = load_json(ROOT / "src" / "site.json", {}) or {}
    pool = sorted(lid for lid in current if lid in lakes and not current[lid].get("used_fallback"))
    sample = random.Random(f"accuracy-{day}").sample(pool, min(n, len(pool)))
    by_src = {}
    for lid in sample:
        slot = lakes[lid]["primary"]
        by_src.setdefault(slot_source(slot), []).append({**slot, "lake_id": lid, "role": "primary",
                                                         "state": lakes[lid].get("state")})
    rows, problems = [], []
    tally = {"ok": 0, "lag": 0, "revised": 0, "held": 0, "stale": 0, "MISMATCH": 0, "page error": 0, "unchecked": 0}
    for sid, tg in sorted(by_src.items()):
        try:
            smap, errs = ADAPTERS[sid](tg, now, get or http_fetch, sources.get(sid, {}))
        except (FetchError, SourceError, ValueError, KeyError) as e:
            problems.append(f"{sid}: source could not be checked ({e})")
            continue
        for t in tg:
            lid = t["lake_id"]
            lake, pub = lakes[lid], current[lid]
            key = series_key(sid, t["series_id"])
            if key in errs:
                tally["unchecked"] += 1
                problems.append(f"{lid}: source could not be checked ({errs[key]})")
                continue
            pt = latest((smap.get(key) or {}).get("points"))
            fresh = (iso(pt[0]), r2(pt[1])) if pt else None
            eff = effective(pub, lake, common.source_for(sources, pub.get("source"), pub.get("reading_kind")), now)
            held = bool((state.get("pending") or {}).get(lid)) or bool(ov.active(now, lid, "hold"))
            kind, why = classify(fresh, pub, eff, held)
            if live and kind in ("ok", "lag"):
                url = f"https://{site.get('domain')}/lake/{lid}/"
                try:
                    shown = live_value(url, pattern)
                    if shown is None or abs(shown - pub["elev_ft"]) > TOL_FT:
                        kind, why = "MISMATCH", f"page shows {shown}, current.json {pub['elev_ft']} ({url})"
                except Exception as e:  # noqa: BLE001
                    kind, why = "page error", f"{url}: {type(e).__name__}: {e}"
            tally[kind] += 1
            rows.append((lid, sid, kind, why))
            if kind in ("MISMATCH", "page error"):
                problems.append(f"{lid}: {why}")
    return rows, tally, problems


def main(argv=None, get=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--date")
    ap.add_argument("--lakes")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--live-pattern", default=r'<data class="elev" value="(-?[0-9.]+)"')
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--alert", action="store_true")
    a = ap.parse_args(argv)
    now = utcnow()
    day = a.date or now.strftime("%Y-%m-%d")
    rows, tally, problems = check(a.n, day, now, a.lakes, a.live, a.live_pattern, get)
    checked = len(rows)
    good = tally["ok"] + tally["lag"] + tally["revised"] + tally["held"]
    result = f"{good} / {checked} match" + (f", {tally['MISMATCH']} MISMATCH" if tally["MISMATCH"] else "")
    extra = ", ".join(f"{v} {k}" for k, v in tally.items() if v and k != "ok")
    print(f"{day}: {result}" + (f" ({extra})" if extra else ""))
    for lid, sid, kind, why in rows:
        if kind != "ok":
            print(f"  {lid} [{sid}] {kind}" + (f": {why}" if why else ""))
    for p in problems:
        print(f"  ! {p}")
    if a.log:
        note = "; ".join(([extra] if extra else []) + problems)[:900].replace("|", "/") or "all match"
        scope = f"{checked} lakes (seeded by date): fresh source fetch vs data/current.json" + (" and the live page" if a.live else "")
        line = f"| {day} | accuracy_check.py | {scope} | {result} | {note} |\n"
        text = LOG.read_text(encoding="utf-8") if LOG.exists() else "# Accuracy log\n\n" + LOG_HEADER
        if line not in text:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            LOG.write_text(text.rstrip("\n") + "\n" + line, encoding="utf-8")
    if a.alert and (tally["MISMATCH"] or problems):
        from pipeline.notify import file_issue
        file_issue("accuracy-mismatch", [f"Accuracy check {day}: {result}"] + problems)
    return 1 if tally["MISMATCH"] else 0


if __name__ == "__main__":
    sys.exit(main())
