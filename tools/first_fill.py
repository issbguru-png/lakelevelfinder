"""Find reservoirs whose stored history starts with their first filling, and set `records_from` in lakes.json.

  python3 tools/first_fill.py            report only
  python3 tools/first_fill.py --write    set records_from / records_from_reason in data/ref/lakes.json

Why: a series that starts when the dam closed (Lake Mead 1935, Lake Powell 1963, Lake Travis 1940) holds levels
from the months or years the reservoir was first filling. Those values are real, but calling them the "record low"
misleads: the lake's record low is a drought level after it first filled. Charts and the yearly table keep every
value; records and the same-date median count from `records_from` (pipeline/stats.py).

Rule, from the data only (no typed dates):
  1. The series starts with a filling signature: the lowest value in its first 3 calendar years is more than
     10% of the whole range (and more than 5 ft) below the lowest value from year 4 on.
  2. records_from = the first day the level came within 10% of the drawdown range of full pool
     (full_pool_ft - 0.1 * (full_pool_ft - lowest value)); the series maximum stands in when full pool is unknown.
Lakes that do not match rule 1 get no records_from (an existing one is removed).
"""
import argparse
import csv
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
LAKES = ROOT / "data" / "ref" / "lakes.json"
SERIES = ROOT / "data" / "series"
REASON = "first filling"
MIN_ROWS = 3000


def first_fill(values, full_pool):
    """values: [(date_str, elev)] sorted by date -> records_from date string, or None."""
    if len(values) < MIN_ROWS:
        return None
    y0 = int(values[0][0][:4])
    early = [x for d, x in values if int(d[:4]) < y0 + 3]
    later = [x for d, x in values if int(d[:4]) >= y0 + 3]
    if not early or not later:
        return None
    lo, hi = min(x for _, x in values), max(x for _, x in values)
    gap = min(later) - min(early)
    if not (gap > 0.1 * (hi - lo) and gap > 5):
        return None
    top = full_pool if full_pool is not None else hi
    threshold = top - 0.1 * (top - lo)
    return next((d for d, x in values if x >= threshold), None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    lakes = json.loads(LAKES.read_text())
    changed = 0
    for lk in lakes:
        f = SERIES / f"{lk['id']}.csv"
        rf = None
        if f.exists():
            with f.open() as fh:
                vals = [(r["date"], float(r["elev_ft"])) for r in csv.DictReader(fh) if r.get("elev_ft")]
            vals.sort()
            rf = first_fill(vals, lk.get("full_pool_ft"))
            if rf:
                after = [(d, x) for d, x in vals if d >= rf]
                was = min(vals, key=lambda t: t[1])
                now = min(after, key=lambda t: t[1])
                print(f"{lk['id']:32} records_from {rf}  low {was[1]} ({was[0]}) -> {now[1]} ({now[0]})")
        if lk.get("records_from") != rf:
            changed += 1
            if rf:
                lk["records_from"], lk["records_from_reason"] = rf, REASON
            else:
                lk.pop("records_from", None)
                lk.pop("records_from_reason", None)
    print(f"{changed} lake(s) change")
    if a.write and changed:
        LAKES.write_text(json.dumps(lakes, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {LAKES.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
