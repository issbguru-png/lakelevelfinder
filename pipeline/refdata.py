"""Load and validate the reference files in data/ref/ (plan 8.2-8.3, CONTRACT.md).

    from pipeline.refdata import load_ref, validate
    ref = load_ref()                # {"lakes": [...], "sources": {...}, "states": {...}, ...}
    errors = validate(ref)          # [] when everything is consistent

CLI: python3 -m pipeline.refdata          (prints a summary, exits 1 on errors)

Standard library only; does not import the other pipeline modules.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "data" / "ref"
FILES = ["lakes", "sources", "states", "systems", "operators", "demand"]

LAKE_FIELDS = ["id", "name", "aliases", "kind", "state", "states", "county", "lat", "lon", "timezone", "operator",
               "system", "primary", "fallback", "intraday", "extra", "datum", "full_pool_ft", "full_pool_label",
               "full_pool_source", "dead_pool_ft", "capacity_af", "flood_control", "valid_min_ft", "valid_max_ft",
               "bands", "mapping_status", "verified_on", "published", "page", "demand"]
SOURCE_FIELDS = ["id", "agency", "credit", "public_url", "fetch", "value_kind", "check_every_hours",
                 "soft_stale_hours", "hard_stale_hours", "provisional", "disclaimer", "terms_checked", "last_verified"]
SOURCE_IDS = {"twdb", "cwms", "rise", "cdec", "usgs"}
VALUE_KINDS = {"daily_average", "daily_instant", "hourly", "15min"}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-[a-z]{2}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def load_ref(ref_dir=REF):
    ref_dir = Path(ref_dir)
    out = {}
    for name in FILES:
        p = ref_dir / f"{name}.json"
        out[name] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    return out


def lakes_by_id(ref):
    return {l["id"]: l for l in ref.get("lakes") or []}


def live_lakes(ref):
    return [l for l in ref.get("lakes") or [] if l.get("page") == "live"]


def _slot_errors(lid, name, slot, sources):
    e = []
    if slot is None:
        return e
    if not isinstance(slot, dict):
        return [f"{lid}: {name} must be an object or null"]
    if slot.get("source") not in sources:
        e.append(f"{lid}: {name}.source {slot.get('source')!r} not in sources.json")
    if not slot.get("series_id"):
        e.append(f"{lid}: {name}.series_id missing")
    src = slot.get("source")
    if src == "twdb" and name == "primary" and not slot.get("twdb_slug"):
        e.append(f"{lid}: twdb primary needs twdb_slug")
    if src == "cwms" and not (slot.get("office") or ":" in str(slot.get("series_id"))):
        e.append(f"{lid}: cwms {name} needs office")
    if src == "cdec" and slot.get("dur", "D") not in ("D", "H"):
        e.append(f"{lid}: cdec dur must be D or H")
    return e


def validate(ref):
    errs = []
    for name in FILES:
        if ref.get(name) is None:
            errs.append(f"missing data/ref/{name}.json")
    if errs:
        return errs
    lakes, sources, states = ref["lakes"], ref["sources"], ref["states"]
    systems, operators, demand = ref["systems"], ref["operators"], ref["demand"]
    if not isinstance(lakes, list):
        return ["lakes.json must be a list"]
    for k in ("sources", "states", "systems", "operators", "demand"):
        if not isinstance(ref[k], dict):
            errs.append(f"{k}.json must be an object")
    # sources
    for sid, s in sources.items():
        for f in SOURCE_FIELDS:
            if f not in s:
                errs.append(f"source {sid}: missing {f}")
        if s.get("id") != sid:
            errs.append(f"source {sid}: id mismatch")
        if s.get("value_kind") not in VALUE_KINDS:
            errs.append(f"source {sid}: bad value_kind")
        if not (0 < s.get("soft_stale_hours", 0) < s.get("hard_stale_hours", 0)):
            errs.append(f"source {sid}: need 0 < soft_stale_hours < hard_stale_hours")
    missing_src = SOURCE_IDS - set(sources)
    if missing_src:
        errs.append(f"sources.json lacks {sorted(missing_src)}")
    # states
    if len(states) != 51:
        errs.append(f"states.json has {len(states)} entries (want 50 + DC)")
    for code, s in states.items():
        if s.get("code") != code or not SLUG_RE.match(s.get("slug", "")):
            errs.append(f"state {code}: bad code/slug")
        for nb in s.get("neighbours", []):
            if nb not in states:
                errs.append(f"state {code}: unknown neighbour {nb}")
            elif code not in states[nb].get("neighbours", []):
                errs.append(f"state {code}: neighbour {nb} is not symmetric")
        if not s.get("timezones"):
            errs.append(f"state {code}: no timezones")
    # lakes
    seen = set()
    for l in lakes:
        lid = l.get("id", "?")
        for f in LAKE_FIELDS:
            if f not in l:
                errs.append(f"{lid}: missing field {f}")
        if lid in seen:
            errs.append(f"duplicate lake id {lid}")
        seen.add(lid)
        if not ID_RE.match(lid):
            errs.append(f"{lid}: id is not '<name>-<st>'")
        st = l.get("state")
        if st not in states:
            errs.append(f"{lid}: unknown state {st}")
        elif not lid.endswith("-" + st.lower()):
            errs.append(f"{lid}: id must end with the dam state -{st.lower()}")
        for s2 in l.get("states") or []:
            if s2 not in states:
                errs.append(f"{lid}: unknown state in states {s2}")
        if st not in (l.get("states") or []):
            errs.append(f"{lid}: state not in states")
        if l.get("operator") and l["operator"] not in operators:
            errs.append(f"{lid}: operator {l['operator']} not in operators.json")
        if l.get("system") and l["system"] not in systems:
            errs.append(f"{lid}: system {l['system']} not in systems.json")
        page = l.get("page")
        if page not in ("live", "profile", "none"):
            errs.append(f"{lid}: bad page {page}")
        if l.get("mapping_status") not in ("verified", "candidate", "none"):
            errs.append(f"{lid}: bad mapping_status")
        if page == "live":
            if l.get("mapping_status") != "verified":
                errs.append(f"{lid}: live page needs mapping_status verified")
            if not l.get("primary"):
                errs.append(f"{lid}: live page needs a primary")
            if l.get("valid_min_ft") is None or l.get("valid_max_ft") is None:
                errs.append(f"{lid}: live page needs valid_min_ft/valid_max_ft")
            elif not l["valid_min_ft"] < l["valid_max_ft"]:
                errs.append(f"{lid}: valid_min_ft >= valid_max_ft")
            if not l.get("datum"):
                errs.append(f"{lid}: datum missing")
            if not l.get("timezone"):
                errs.append(f"{lid}: timezone missing")
        if page == "profile":
            if l.get("published"):
                errs.append(f"{lid}: profile must not be published")
            if l.get("official_url") is None and l.get("operator"):
                errs.append(f"{lid}: profile names an operator without official_url")
        fp = l.get("full_pool_ft")
        if fp is not None:
            if not l.get("full_pool_label") or not l.get("full_pool_source"):
                errs.append(f"{lid}: full_pool_ft without label/source")
            if l.get("valid_min_ft") is not None and not (l["valid_min_ft"] < fp <= l["valid_max_ft"]):
                errs.append(f"{lid}: full pool {fp} outside the validation range")
        if l.get("dead_pool_ft") is not None and fp is not None and l["dead_pool_ft"] >= fp:
            errs.append(f"{lid}: dead pool >= full pool")
        for slot in ("primary", "fallback", "intraday"):
            errs += _slot_errors(lid, slot, l.get(slot), sources)
    # systems
    for slug, s in systems.items():
        if s.get("slug") != slug:
            errs.append(f"system {slug}: slug mismatch")
        if s.get("operator") and s["operator"] not in operators:
            errs.append(f"system {slug}: unknown operator {s['operator']}")
        for m in s.get("members", []):
            if m not in seen:
                errs.append(f"system {slug}: unknown member {m}")
        if not s.get("members"):
            errs.append(f"system {slug}: no members")
    for slug, o in operators.items():
        if o.get("slug") != slug or not SLUG_RE.match(slug):
            errs.append(f"operator {slug}: bad slug")
        for f in ("name", "short", "official_url", "has_public_feed"):
            if f not in o:
                errs.append(f"operator {slug}: missing {f}")
    for lid, d in demand.items():
        if lid not in seen:
            errs.append(f"demand: unknown lake {lid}")
        if not isinstance(d.get("volume"), int):
            errs.append(f"demand {lid}: volume must be an int")
    return errs


def main():
    ref = load_ref()
    errs = validate(ref)
    lakes = ref.get("lakes") or []
    live = [l for l in lakes if l.get("page") == "live"]
    by_src = {}
    for l in live:
        by_src[l["primary"]["source"]] = by_src.get(l["primary"]["source"], 0) + 1
    print(f"lakes {len(lakes)} (live {len(live)}: {by_src}; with full pool "
          f"{sum(1 for l in live if l.get('full_pool_ft') is not None)}); "
          f"profiles {sum(1 for l in lakes if l.get('page') == 'profile')}")
    for e in errs:
        print("ERROR", e)
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
