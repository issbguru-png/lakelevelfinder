"""Override CLI for data/overrides.json (plan §8.9).

  python3 pipeline/override.py list
  python3 pipeline/override.py add --lake hartwell-lake-ga --action hold --reason "..." --evidence URL [--days 7]
  python3 pipeline/override.py add --lake X --action set_full_pool --full-pool 915.0 --label "top of conservation" ...
  python3 pipeline/override.py add --lake X --action note --text "Drawdown announced ..." --reason ... --evidence URL
  python3 pipeline/override.py add --lake X --action release_jump --reason "checked the operator page" --evidence URL
  python3 pipeline/override.py clear --lake X [--action hold]
  python3 pipeline/override.py prune            remove expired overrides

Every override needs --reason and --evidence (an official URL) and expires within 14 days (renew deliberately).
There is no option to type a water level: the site shows what a feed says, or says it does not know.
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import overrides as ov  # noqa: E402
from pipeline.common import iso, load_lakes, parse_ts, save_json, utcnow  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["list", "add", "clear", "prune"])
    ap.add_argument("--lake")
    ap.add_argument("--action", choices=ov.ACTIONS)
    ap.add_argument("--reason")
    ap.add_argument("--evidence")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--full-pool", type=float)
    ap.add_argument("--label")
    ap.add_argument("--text")
    ap.add_argument("--by", default="owner")
    ap.add_argument("--lakes", help="lakes file (default data/ref/lakes.json)")
    a = ap.parse_args(argv)
    now = utcnow()
    rows = ov.load()

    if a.cmd == "list":
        if not rows:
            print("no overrides")
        for o in rows:
            state = "invalid" if not ov.is_valid(o) else ("EXPIRED" if parse_ts(o["expires_at"]) <= now else "active")
            extra = o.get("full_pool_ft") if o.get("action") == "set_full_pool" else (o.get("text") or "")
            print(f"{o.get('lake_id')} {o.get('action')} until {o.get('expires_at')} [{state}] {extra} "
                  f"- {o.get('reason')} <{o.get('evidence_url')}>")
        return 0

    if a.cmd == "prune":
        keep = [o for o in rows if o not in ov.expired(now, rows)]
        save_json(ov.path(), keep)
        print(f"removed {len(rows) - len(keep)} expired override(s)")
        return 0

    if not a.lake:
        ap.error("--lake is required")
    ids = {lk["id"] for lk in load_lakes(a.lakes)}
    if a.cmd == "clear":
        keep = [o for o in rows if not (o.get("lake_id") == a.lake and (not a.action or o.get("action") == a.action))]
        save_json(ov.path(), keep)
        print(f"{a.lake}: removed {len(rows) - len(keep)}")
        return 0

    if a.lake not in ids:
        ap.error(f"unknown lake {a.lake!r} (not in the lakes file)")
    if not (a.action and a.reason and a.evidence):
        ap.error("--action, --reason and --evidence are required")
    if a.days > ov.MAX_DAYS or a.days <= 0:
        ap.error(f"--days must be between 0 and {ov.MAX_DAYS}")
    o = {"lake_id": a.lake, "action": a.action, "reason": a.reason, "evidence_url": a.evidence, "set_by": a.by,
         "set_at": iso(now), "expires_at": iso(now + dt.timedelta(days=a.days))}
    if a.action == "set_full_pool":
        if a.full_pool is None:
            ap.error("set_full_pool needs --full-pool")
        o["full_pool_ft"] = a.full_pool
        if a.label:
            o["full_pool_label"] = a.label
    if a.action == "note":
        if not a.text:
            ap.error("note needs --text")
        o["text"] = a.text
    try:
        ov.validate(o)
    except ValueError as e:
        ap.error(str(e))
    rows = [r for r in rows if not (r.get("lake_id") == a.lake and r.get("action") == a.action)] + [o]
    save_json(ov.path(), rows)
    print(f"{a.lake}: {a.action} until {o['expires_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
