#!/usr/bin/env python3
"""Remove impossible values from data/series/*.csv (range + isolated spikes) and log them.

  python3 tools/clean_series.py            clean every series, write docs/mapping/series-cleaning.md
  python3 tools/clean_series.py --dry-run  report only
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline import common, series  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    lakes = {lk["id"]: lk for lk in common.load_lakes()}
    log = ["# Series cleaning log", "", "Rows removed from stored history by `tools/clean_series.py` "
           "(outside the lake's valid range, or an isolated one-day spike in backfilled data).", "",
           "| Lake | Date | Value (ft) | Reason |", "|---|---|---|---|"]
    total = 0
    for p in sorted((common.DATA / "series").glob("*.csv")):
        lake = lakes.get(p.stem)
        if not lake:
            continue
        rows = series.read(p.stem)
        kept, dropped = series.clean_rows(rows, lake)
        for r, why in dropped:
            log.append(f"| {p.stem} | {r['date']} | {r['elev_ft']} | {why} |")
        total += len(dropped)
        if dropped and not a.dry_run:
            series.write(p.stem, kept)
    print(f"removed {total} row(s)")
    if not a.dry_run:
        out = common.ROOT / "docs" / "mapping" / "series-cleaning.md"
        out.write_text("\n".join(log) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
