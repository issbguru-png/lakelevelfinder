#!/usr/bin/env python3
"""data/ref/demand.json = lake id -> head search volume (the repo is public: keyword lists stay in the research folder).

Input: the research files (not shipped with the site)
  ../../data/research/validation-lakes-events/lakes-entities.csv   (entity clusters, Google Ads volume, Oct 30 2026)
Matching: an entity's words (it is stored as sorted distinctive words, e.g. "christi corpus") must equal the
distinctive words of the lake's name or one of its aliases, or an entry in EXTRA_ALIASES. When the entity has a
state, it must be one of the lake's states. An entity without a state that matches more than one lake is skipped.
Volume = the largest matched cluster (plan 1.2: never sum clusters). Also sets each lake's "demand" in lakes.json
and writes the full detail (keywords, searched aliases) and the unmatched list (entities >= 1,000/mo that match no lake)
to RESEARCH (outside the repo).

Usage: python3 tools/demand.py [--entities PATH]
"""
import argparse
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT.parent.parent / "data" / "research" / "validation-lakes-events" / "lakes-entities.csv"
RESEARCH = ROOT.parent.parent / "data" / "research" / "lakes-plan" / "demand"
GENERIC = {"lake", "lakes", "reservoir", "the", "of", "de", "dam", "and"}
# searched alias words -> lake id (aliases that are not in lakes.json names, or whose entity state is wrong)
# value: (lake id, searched alias to list, or None when it is just a short form of the name).
# These override the entity's state field (e.g. "ferry greers" is filed under SC in the research file).
EXTRA_ALIASES = {
    "mathis": ("lake-corpus-christi-tx", "Lake Mathis"),
    "hoover": ("lake-mead-nv", "Hoover Dam"),
    "hoover mead": ("lake-mead-nv", "Hoover Dam"),
    "buford": ("lake-lanier-ga", "Buford Dam"),
    "clark hill": ("thurmond-lake-ga", "Clark Hill Lake"),
    "clarks hill": ("thurmond-lake-ga", "Clarks Hill Lake"),
    "strom thurmond": ("thurmond-lake-ga", None),
    "twain": ("mark-twain-lake-mo", None),
    "o": ("lake-okeechobee-fl", "Lake O"),
    "gibson": ("fort-gibson-lake-ok", None),
    "ferry greers": ("greers-ferry-lake-ar", None),
    "pomme terre": ("pomme-de-terre-lake-mo", None),
    "lbj": ("lake-lbj-tx", None),
    "ozark": ("lake-of-the-ozarks-mo", None),
    "ozarks": ("lake-of-the-ozarks-mo", None),
    "yough": ("youghiogheny-river-lake-pa", "Yough Lake"),
    "nolin river": ("nolin-river-lake-ky", None),
    "percy priest": ("percy-priest-lake-tn", None),
    "chapman jim": ("jim-chapman-lake-tx", None),
    "sml": ("smith-mountain-lake-va", "SML"),
}


def words(s):
    s = s.lower().replace("'", "").replace("’", "").replace(".", " ")
    s = re.sub(r"\(.*?\)", " ", s)
    return " ".join(sorted(w for w in re.findall(r"[a-z0-9]+", s) if w not in GENERIC))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entities", default=str(DEFAULT))
    a = ap.parse_args()
    lakes = json.loads((ROOT / "data" / "ref" / "lakes.json").read_text())
    by_id = {l["id"]: l for l in lakes}
    keymap = {}   # words -> [(lake id, alias-or-None)]
    for l in lakes:
        keymap.setdefault(words(l["name"]), []).append((l["id"], None))
        for al in l.get("aliases") or []:
            keymap.setdefault(words(al), []).append((l["id"], al))
    extra = {}
    for k, (lid, shown) in EXTRA_ALIASES.items():
        if lid in by_id:
            extra[" ".join(sorted(k.split()))] = (lid, shown)
    out, unmatched = {}, []
    for r in csv.DictReader(open(a.entities, encoding="utf-8")):
        if r["type"] != "lake":
            continue
        vol = int(r["volume"] or 0)
        key = " ".join(sorted(w for w in r["entity"].split() if w not in GENERIC))
        if key in extra:
            cands = [extra[key]]
        else:
            cands = keymap.get(key, [])
            est = set(s for s in (r["state"] or "").split("/") if len(s) == 2)
            if est:
                cands = [c for c in cands if est & set(by_id[c[0]]["states"])]
            cands = list({c[0]: c for c in cands}.values())
        if len(cands) != 1:
            if vol >= 1000:
                unmatched.append((vol, r["entity"], r["state"], r["label"], len(cands)))
            continue
        lid, alias = cands[0]
        d = out.setdefault(lid, {"volume": 0, "keywords": [], "aliases_searched": []})
        d["volume"] = max(d["volume"], vol)
        for k in (r["top_keywords"] or "").split(" | "):
            k = k.strip()
            if k and k not in d["keywords"]:
                d["keywords"].append(k)
        if alias and alias not in d["aliases_searched"]:
            d["aliases_searched"].append(alias)
    for d in out.values():
        d["keywords"] = d["keywords"][:12]
    out = dict(sorted(out.items(), key=lambda x: -x[1]["volume"]))
    RESEARCH.mkdir(parents=True, exist_ok=True)
    (RESEARCH / "demand-full.json").write_text(json.dumps(out, indent=1) + "\n")
    (ROOT / "data" / "ref" / "demand.json").write_text(
        json.dumps({k: {"volume": d["volume"]} for k, d in out.items()}, indent=1) + "\n")
    for l in lakes:
        l["demand"] = out.get(l["id"], {}).get("volume", 0)
    (ROOT / "data" / "ref" / "lakes.json").write_text(json.dumps(lakes, indent=2, ensure_ascii=False) + "\n")
    unmatched.sort(reverse=True)
    (RESEARCH / "demand-unmatched.md").write_text(
        "# Lake entities >= 1,000/mo with no lake record\n\nGenerated by `tools/demand.py` from lakes-entities.csv "
        "(Google Ads head volume, Oct 30, 2026). Many are state phrases, operator names, river gauges or noise (plan 1.2); "
        "'matches' > 1 means the name fits more than one lake and the entity has no state.\n\n"
        "| Volume | Entity | State | Label | Matches |\n|---|---|---|---|---|\n"
        + "\n".join(f"| {v} | {e} | {s} | {lab} | {n} |" for v, e, s, lab, n in unmatched) + "\n")
    tot = sum(d["volume"] for d in out.values())
    live = sum(out.get(l["id"], {}).get("volume", 0) for l in lakes if l["page"] == "live")
    print(f"{len(out)} lakes with demand, head volume {tot}/mo ({live} on live lakes); {len(unmatched)} unmatched >= 1K")


if __name__ == "__main__":
    main()
