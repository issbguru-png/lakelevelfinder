#!/usr/bin/env python3
"""Build the lake mapping (plan section 3.6) and write data/ref/*.json plus evidence.

Every lake is accepted only on a fresh, dated reading fetched by this script, and a
full-pool value that comes from the same source (TWDB, CWMS /levels) or from an official
page whose URL is stored with it. Unknown values stay null. Nothing is typed by hand
except the curated mapping tables below (series ids, names, aliases, official URLs).

Usage:
  python3 tools/map_lakes.py --cache /path/to/cache          # full run (network)
  python3 tools/map_lakes.py --cache DIR --offline           # rebuild from cached responses only
  python3 tools/map_lakes.py --cache DIR --refresh           # ignore cached responses

Outputs (repo-relative):
  data/ref/lakes.json, sources.json, states.json, systems.json, operators.json
  docs/mapping/evidence.json and docs/mapping/{twdb,cwms,rise,cdec,usgs,profiles}.md

Then run tools/demand.py (adds demand.json and each lake's "demand" field) and
python3 -m pipeline.refdata (validator).

Network rules: honest User-Agent, timeouts, retries, >= 1 s between TWDB/RISE/CDEC
requests, no proxies or header tricks. A host that times out or answers 403 is recorded
and skipped.
"""
import argparse
import csv
import datetime as dt
import hashlib
import html
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "data" / "ref"
DOCS = ROOT / "docs" / "mapping"
UA = "LakeLevelFinder-build/0.1 (+https://lakelevelfinder.com)"
TODAY = dt.date.today().isoformat()
NOW = dt.datetime.now(dt.timezone.utc)

CACHE = None
OFFLINE = False
REFRESH = False
LOG = []          # (url, status, note)
_last_hit = {}


# ----------------------------------------------------------------------------- HTTP

TIME_PARAMS = {"begin", "end", "dateTime[after]", "Start", "End", "after", "before"}


def stable_key(url, accept):
    """Cache key without time-window parameters, so --offline can reuse the latest response."""
    u = urllib.parse.urlparse(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k not in TIME_PARAMS]
    return hashlib.sha1((accept + "|" + u._replace(query=urllib.parse.urlencode(q)).geturl()).encode()).hexdigest()


def http(url, accept="*/*", timeout=60, retries=2, spacing=0.0, binary=False):
    """GET with cache. Returns text (or bytes). Raises RuntimeError on failure."""
    key = hashlib.sha1((accept + "|" + url).encode()).hexdigest()
    path = CACHE / "http" / key[:2] / key if CACHE else None
    if path and path.exists() and not REFRESH:
        data = path.read_bytes()
        return data if binary else data.decode("utf-8", "replace")
    if OFFLINE:
        alias = CACHE / "latest" / stable_key(url, accept) if CACHE else None
        if alias and alias.exists():
            data = alias.read_bytes()
            return data if binary else data.decode("utf-8", "replace")
        raise RuntimeError(f"offline and not cached: {url}")
    host = urllib.parse.urlparse(url).netloc
    if spacing:
        wait = spacing - (time.time() - _last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
    err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            _last_hit[host] = time.time()
            LOG.append((url, 200, ""))
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                alias = CACHE / "latest" / stable_key(url, accept)
                alias.parent.mkdir(parents=True, exist_ok=True)
                alias.write_bytes(data)
            return data if binary else data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            _last_hit[host] = time.time()
            err = f"HTTP {e.code}"
            if e.code in (400, 403, 404):
                break
        except Exception as e:  # timeout, reset, DNS
            err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(3 * (attempt + 1))
    LOG.append((url, err, "failed"))
    raise RuntimeError(f"{err} for {url}")


def http_json(url, accept="application/json", **kw):
    return json.loads(http(url, accept=accept, **kw))


def page_text(raw):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw, flags=re.S)
    t = html.unescape(re.sub(r"<[^>]+>", " | ", t))
    t = re.sub(r"\s+", " ", t)
    return re.sub(r"(\| )+", "| ", t)


def slugify(s):
    s = s.lower().replace("'", "").replace("’", "").replace(".", " ")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def r2(x):
    return None if x is None else round(float(x), 2)


def hours_since(ts):
    return (NOW - ts).total_seconds() / 3600.0


# ----------------------------------------------------------------------------- TWDB

TWDB_JSON = "https://www.waterdatafortexas.org/reservoirs/recent-conditions.json"
TWDB_STATEWIDE = "https://www.waterdatafortexas.org/reservoirs/statewide"
TWDB_IND = "https://www.waterdatafortexas.org/reservoirs/individual/"

# key -> (lake id, display name, aliases). Default: slug(full_name) + "-tx", full_name.
# Choices follow the searched form in lakes-keywords.csv (clean "lake X" counts) and plan 6.1.
TWDB_NAMES = {
    "Amistad": ("lake-amistad-tx", "Lake Amistad", ["Amistad Reservoir"]),
    "Austin": ("lake-austin-tx", "Lake Austin", []),
    "BASteinhagen": ("b-a-steinhagen-lake-tx", "B. A. Steinhagen Lake", ["Steinhagen Lake"]),
    "Bridgeport": ("lake-bridgeport-tx", "Lake Bridgeport", ["Bridgeport Reservoir"]),
    "Caddo": ("caddo-lake-la", "Caddo Lake", []),
    "CedarCreek": ("cedar-creek-reservoir-tx", "Cedar Creek Reservoir", ["Cedar Creek Lake"]),
    "Cherokee": ("lake-cherokee-tx", "Lake Cherokee", []),
    "ChokeCanyon": ("choke-canyon-reservoir-tx", "Choke Canyon Reservoir", ["Choke Canyon Lake"]),
    "CorpusChristi": ("lake-corpus-christi-tx", "Lake Corpus Christi", ["Lake Mathis"]),
    "EagleMountain": ("eagle-mountain-lake-tx", "Eagle Mountain Lake", ["Eagle Mountain Reservoir"]),
    "ElephantButte": ("elephant-butte-lake-nm", "Elephant Butte Lake", ["Elephant Butte Reservoir"]),
    "Falcon": ("falcon-lake-tx", "Falcon Lake", ["Falcon Reservoir", "Falcon International Reservoir"]),
    "Fork": ("lake-fork-tx", "Lake Fork", ["Lake Fork Reservoir"]),
    "FortPhantomHill": ("lake-fort-phantom-hill-tx", "Lake Fort Phantom Hill", ["Fort Phantom Hill Reservoir"]),
    "HubbardCreek": ("hubbard-creek-reservoir-tx", "Hubbard Creek Reservoir", ["Hubbard Creek Lake"]),
    "HubertHMoss": ("moss-lake-tx", "Moss Lake", ["Hubert H. Moss Lake"]),
    "JBThomas": ("lake-j-b-thomas-tx", "Lake J. B. Thomas", []),
    "JimChapman": ("jim-chapman-lake-tx", "Jim Chapman Lake", ["Cooper Lake"]),
    "Lake O' the Pines": ("lake-o-the-pines-tx", "Lake O' the Pines", []),
    "Lavon": ("lake-lavon-tx", "Lake Lavon", ["Lavon Lake"]),
    "Leon": ("lake-leon-tx", "Lake Leon", ["Leon Reservoir"]),
    "Lewisville": ("lake-lewisville-tx", "Lake Lewisville", ["Lewisville Lake"]),
    "Livingston": ("lake-livingston-tx", "Lake Livingston", ["Livingston Reservoir"]),
    "LyndonBJohnson": ("lake-lbj-tx", "Lake LBJ", ["Lake Lyndon B. Johnson"]),
    "Mackenzie": ("lake-mackenzie-tx", "Lake Mackenzie", ["Mackenzie Reservoir"]),
    "Martin": ("martin-lake-tx", "Martin Lake", []),
    "Nocona": ("lake-nocona-tx", "Lake Nocona", ["Farmers Creek Reservoir"]),
    "OHIvie": ("lake-o-h-ivie-tx", "Lake O. H. Ivie", ["O. H. Ivie Reservoir"]),
    "SamRayburn": ("sam-rayburn-reservoir-tx", "Sam Rayburn Reservoir", ["Sam Rayburn Lake"]),
    "Somerville": ("lake-somerville-tx", "Lake Somerville", ["Somerville Lake"]),
    "Texoma": ("lake-texoma-tx", "Lake Texoma", []),
    "ToledoBend": ("toledo-bend-reservoir-tx", "Toledo Bend Reservoir", ["Toledo Bend Lake"]),
    "Waco": ("lake-waco-tx", "Lake Waco", ["Waco Lake"]),
    "Whitney": ("lake-whitney-tx", "Lake Whitney", ["Whitney Lake"]),
}
# Dam state and every state touched (only where a lake crosses a state line).
TWDB_STATES = {
    "Caddo": ("LA", ["LA", "TX"]),
    "ElephantButte": ("NM", ["NM"]),
    "Texoma": ("TX", ["TX", "OK"]),
    "ToledoBend": ("TX", ["TX", "LA"]),
}
TWDB_TZ = {"ElephantButte": "America/Denver"}
TWDB_SYSTEM = {k: "highland-lakes" for k in ["Buchanan", "Inks", "LyndonBJohnson", "MarbleFalls", "Travis", "Austin"]}
TWDB_SYSTEM.update({"ChokeCanyon": "corpus-christi-water-supply", "CorpusChristi": "corpus-christi-water-supply",
                    "Texana": "corpus-christi-water-supply"})
TWDB_OPERATOR = {k: "lcra" for k in ["Buchanan", "Inks", "LyndonBJohnson", "MarbleFalls", "Travis", "Austin"]}


def twdb_key_to_slug(key, slugs):
    if key == "BoisDArc":
        cand = "bois-darc"
    else:
        s = key.replace("'", "")
        s = re.sub(r"([a-z])([A-Z])", r"\1-\2", s)
        s = re.sub(r"([A-Z])([A-Z])", r"\1-\2", s)
        s = re.sub(r"([A-Z])([A-Z])", r"\1-\2", s)
        cand = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return cand if cand in slugs else None


def parse_twdb_header(text):
    hdr = [l for l in text.splitlines() if l.startswith("#")]

    def block(name):
        out, on = [], False
        for l in hdr:
            if name in l:
                on = True
                continue
            if on:
                s = l.lstrip("#").strip()
                if not s:
                    break
                out.append(s)
        return out

    def current(lines):
        # the open-ended entry: no qualifier, "(Start date: X)", or a current seasonal entry
        cur = [l for l in lines if "End date" not in l and " to " not in l.split("(", 1)[-1]]
        cur = cur or lines
        return cur

    dat = block("Vertical datum(s) used")
    cp = block("Conservation pool elevation(s) used")
    dp = block("Dead pool elevation(s) used")
    datum = None
    if dat:
        c = current(dat)
        datum = re.sub(r"\s*\(.*\)$", "", c[0]).strip() if c else None
    seasonal = [l for l in current(cp) if "Seasonal" in l]
    gen = re.search(r"generated:\s*(\S+)", text)
    return {"datum": datum, "datum_lines": dat, "pool_lines": cp, "dead_lines": dp,
            "seasonal_pool_lines": seasonal, "generated": gen.group(1) if gen else None}


def despike(vals, ev, window=3, limit=15.0):
    """Drop isolated spikes: a value more than `limit` ft from the median of its neighbours
    (window on each side). TWDB history has some (e.g. Elephant Butte 3147.97 ft on 2026-09-09,
    Oak Creek 246200.00 on 2003-01-23)."""
    vals = sorted(vals, key=lambda t: t[1])
    out, spikes = [], []
    for i, (x, d) in enumerate(vals):
        nb = [v for v, _ in vals[max(0, i - window):i] + vals[i + 1:i + 1 + window]]
        if nb:
            nb.sort()
            med = nb[len(nb) // 2]
            if abs(x - med) > limit:
                spikes.append((d, x))
                continue
        out.append((x, d))
    if spikes:
        ev["history_spikes_dropped"] = spikes[:20]
        ev["history_spikes_count"] = len(spikes)
    return out


def twdb_lakes(evidence):
    data = http_json(TWDB_JSON, spacing=1)
    sw = http(TWDB_STATEWIDE, spacing=1)
    slugs = sorted(set(re.findall(r"reservoirs/individual/([a-z0-9\-]+)", sw)))
    if len(data) < 118:
        raise SystemExit(f"TWDB returned {len(data)} lakes (< 118); stop")
    lakes, rejected = [], []
    for key in sorted(data):
        v = data[key]
        slug = twdb_key_to_slug(key, set(slugs))
        ev = {"source": "twdb", "key": key, "json_url": TWDB_JSON, "slug": slug}
        if not slug:
            rejected.append({"key": key, "reason": "no matching per-lake slug on the statewide page"})
            continue
        url30 = f"{TWDB_IND}{slug}-30day.csv"
        try:
            t30 = http(url30, spacing=1)
        except RuntimeError as e:
            rejected.append({"key": key, "reason": f"30-day CSV failed: {e}"})
            continue
        hdr = parse_twdb_header(t30)
        rows30 = [r for r in csv.reader(io.StringIO("\n".join(l for l in t30.splitlines() if not l.startswith("#"))))]
        ev.update({"csv30_url": url30, "csv_header": hdr})
        # full history: min / max of water_level for the validation range
        urlf = f"{TWDB_IND}{slug}.csv"
        hist_max = hist_min = hist_max_date = hist_first = None
        try:
            tf = http(urlf, spacing=1, timeout=180)
            vals = []
            for line in tf.splitlines():
                if line.startswith("#") or line.startswith("date") or not line.strip():
                    continue
                p = line.split(",")
                if len(p) > 1 and p[1]:
                    try:
                        vals.append((float(p[1]), p[0]))
                    except ValueError:
                        pass
            vals = despike([(x, d) for x, d in vals if x > 0], ev)
            if vals:
                hist_max, hist_max_date = max(vals)
                hist_first = min(d for _, d in vals)
                # record low over the last 20 years only: first-filling values are not plausible today
                cutoff = (NOW.date() - dt.timedelta(days=365 * 20)).isoformat()
                recent = [x for x, d in vals if d >= cutoff]
                hist_min = min(recent) if recent else None
        except RuntimeError as e:
            ev["full_csv_error"] = str(e)
        ev.update({"full_csv_url": urlf, "history_max_ft": hist_max, "history_max_date": hist_max_date,
                   "history_min_20y_ft": hist_min, "history_first_date": hist_first})
        elev = v.get("elevation")
        ts = v.get("timestamp")
        ev.update({"reading": {"elevation_ft": elev, "date": ts, "percent_full": v.get("percent_full"),
                               "conservation_pool_elevation": v.get("conservation_pool_elevation")}})
        # freshness: timestamp is a local date; accept if within 4 days
        try:
            age_days = (NOW.date() - dt.date.fromisoformat(ts)).days
        except Exception:
            age_days = 99
        if elev is None or age_days > 4:
            rejected.append({"key": key, "reason": f"no fresh reading (date {ts})"})
            continue
        if hdr["datum"] is None:
            rejected.append({"key": key, "reason": "no vertical datum in the 30-day CSV header"})
            continue
        lid, name, aliases = TWDB_NAMES.get(key, (None, None, []))
        name = name or re.sub(r"\s*\(.*?\)", "", v["full_name"]).strip()
        lid = lid or slugify(name) + "-tx"
        st, sts = TWDB_STATES.get(key, ("TX", ["TX"]))
        lon, lat = (v.get("gauge_location") or {}).get("coordinates", [None, None])
        cp = v.get("conservation_pool_elevation")
        dead = v.get("dead_pool_elevation")
        if dead is not None and dead <= 0:   # TWDB uses 0 when it has no dead-pool elevation ("None" in the CSV header)
            dead = None
        detention = v.get("percent_full") is None and v.get("conservation_capacity") is None
        full = None if detention else cp
        vmin = (dead - 5) if (dead is not None and not detention) else (
            hist_min - 10 if hist_min is not None else ((cp - 80) if cp else None))
        vmax = (hist_max + 10) if hist_max is not None else (cp + 60 if cp else None)
        if vmax is not None and cp is not None and not detention:
            # plan 3.6 step 5: at most full pool + 60 ft (history can hold bad multi-day values, e.g. Marble Falls
            # 820.98 ft in 1995), at least full pool + 10 ft (some lakes have never filled)
            vmax = max(min(vmax, cp + 60), cp + 10)
        if vmin is not None and hist_min is not None and hist_min < vmin:
            vmin = hist_min - 5   # never reject a level the lake has actually reached
        lake = {
            "id": lid, "name": name, "aliases": aliases,
            "kind": "lake" if key == "Caddo" else "reservoir",
            "state": st, "states": sts, "county": None,
            "lat": lat, "lon": lon,
            "timezone": TWDB_TZ.get(key, "America/Chicago"),
            "operator": TWDB_OPERATOR.get(key),
            "system": TWDB_SYSTEM.get(key),
            "primary": {"source": "twdb", "series_id": key, "param": "elevation",
                        "twdb_slug": slug, "csv_slug": slug},
            "fallback": None, "intraday": None,
            "extra": {"storage": None, "water_temp": None, "release": None},
            "datum": hdr["datum"],
            "full_pool_ft": full,
            "full_pool_label": None if full is None else "conservation pool",
            "full_pool_source": None if full is None else "twdb:conservation_pool_elevation",
            "dead_pool_ft": None if detention else dead,
            "capacity_af": v.get("conservation_capacity"),
            "flood_control": v.get("flood_control_lake") == "Y",
            "valid_min_ft": r2(vmin), "valid_max_ft": r2(vmax),
            "bands": None,
            "mapping_status": "verified", "verified_on": TODAY,
            "published": True, "page": "live", "demand": 0,
            "source_url": f"{TWDB_IND}{slug}",
        }
        if hdr["seasonal_pool_lines"]:
            lake["full_pool_note"] = "TWDB applies a seasonal conservation pool: " + "; ".join(hdr["seasonal_pool_lines"])
        if detention:
            lake["full_pool_note"] = ("Flood-detention reservoir: TWDB lists no conservation storage or percent full, "
                                      "so no full pool is shown.")
        ev["accepted_id"] = lid
        evidence["twdb"].append(ev)
        lakes.append(lake)
    return lakes, rejected, data


def usgs_intraday(lakes, twdb_data, evidence):
    """Attach a USGS gauge as the intraday layer where it sits at the TWDB gauge location
    and reads within 0.10 ft of TWDB's latest value (same datum by construction)."""
    url = ("https://api.waterdata.usgs.gov/ogcapi/v0/collections/latest-continuous/items?"
           + urllib.parse.urlencode({"f": "json", "parameter_code": "62614,62615,00062",
                                     "state_name": "Texas", "limit": 500}))
    feats, nxt, pages = [], url, 0
    while nxt and pages < 10:
        d = http_json(nxt, accept="application/geo+json, application/json", spacing=1)
        feats += d.get("features", [])
        links = [l["href"] for l in d.get("links", []) if l.get("rel") == "next"]
        nxt = links[0] if links else None
        pages += 1

    def dist(a, b):
        la1, lo1, la2, lo2 = map(math.radians, (a[1], a[0], b[1], b[0]))
        h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
        return 12742 * math.asin(math.sqrt(h))

    by_key = {l["primary"]["series_id"]: l for l in lakes if l["primary"]["source"] == "twdb"}
    for key, lake in by_key.items():
        v = twdb_data[key]
        g = (v.get("gauge_location") or {}).get("coordinates")
        if not g:
            continue
        best = None
        for f in feats:
            p = f.get("properties", {})
            mid = p.get("monitoring_location_id", "")
            if not mid.startswith("USGS-") or not f.get("geometry"):
                continue
            try:
                val = float(p["value"])
                t = dt.datetime.fromisoformat(p["time"])
            except Exception:
                continue
            dk = dist(g, f["geometry"]["coordinates"])
            if dk > 5 or hours_since(t) > 48:
                continue
            diff = abs(val - v["elevation"])
            if diff <= 0.10 and (best is None or (dk, diff) < (best[0], best[1])):
                best = (dk, diff, mid, p["parameter_code"], val, p["time"], p.get("approval_status"))
        if best:
            lake["intraday"] = {"source": "usgs", "series_id": best[2], "param": best[3]}
            evidence["usgs"].append({"lake_id": lake["id"], "twdb_key": key, "site": best[2], "param": best[3],
                                     "usgs_value_ft": best[4], "usgs_time": best[5], "approval": best[6],
                                     "twdb_value_ft": v["elevation"], "twdb_date": v["timestamp"],
                                     "distance_km": round(best[0], 2), "diff_ft": round(best[1], 2),
                                     "request": url})
    return feats


# ----------------------------------------------------------------------------- CWMS

CWMS = "https://cwms-data.usace.army.mil/cwms-data/"
CWMS_OPERATOR = {"SWL": "usace-little-rock", "SWT": "usace-tulsa", "SAS": "usace-savannah", "SAM": "usace-mobile",
                 "LRL": "usace-louisville", "LRN": "usace-nashville", "SAW": "usace-wilmington",
                 "MVK": "usace-vicksburg", "MVS": "usace-st-louis", "LRH": "usace-huntington",
                 "LRP": "usace-pittsburgh"}

POOLS = {  # office -> (level names in priority order, "first" | "max", label)
    "SWL": (["Top of Conservation"], "first", "top of conservation pool"),
    "SAS": (["Top of Conservation"], "first", "top of conservation pool"),
    "SAM": (["Top of Conservation"], "first", "top of conservation pool"),
    "SWT": (["Top of Conservation"], "first", "top of conservation pool"),
    "LRL": (["Top of Conservation"], "first", "top of conservation pool (summer pool)"),
    "LRN": (["Top of Power", "Top of Normal", "Top of Conservation"], "first", None),
    "SAW": (["Top of Conservation"], "first", "top of conservation pool"),
    "MVK": (["Top of Conservation"], "first", "top of conservation pool"),
    "MVS": (["Top of Conservation", "Summer Guide Curve"], "max", None),
    "LRH": (["Top of Recreation"], "first", "top of recreation pool (summer pool)"),
    "LRP": (["Top of Conservation"], "first", "top of conservation pool"),
}

# id, name, aliases, dam state, states, office, [series candidates], level location, operator override, pools override
CWMS_LAKES = [
    ("table-rock-lake-mo", "Table Rock Lake", [], "MO", ["MO", "AR"], "SWL", ["Table_Rock_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev"], "Table_Rock_Dam"),
    ("bull-shoals-lake-ar", "Bull Shoals Lake", [], "AR", ["AR", "MO"], "SWL", ["Bull_Shoals_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev"], "Bull_Shoals_Dam"),
    ("beaver-lake-ar", "Beaver Lake", [], "AR", ["AR"], "SWL", ["Beaver_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev"], "Beaver_Dam"),
    ("norfork-lake-ar", "Norfork Lake", [], "AR", ["AR", "MO"], "SWL", ["Norfork_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev"], "Norfork_Dam"),
    ("greers-ferry-lake-ar", "Greers Ferry Lake", [], "AR", ["AR"], "SWL", ["GreersFerry_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev"], "GreersFerry_Dam"),
    ("millwood-lake-ar", "Millwood Lake", [], "AR", ["AR"], "SWL", ["Millwood_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev"], "Millwood_Dam"),
    ("clearwater-lake-mo", "Clearwater Lake", [], "MO", ["MO"], "SWL", ["Clearwater_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev"], "Clearwater_Dam"),
    ("blue-mountain-lake-ar", "Blue Mountain Lake", [], "AR", ["AR"], "SWL", ["Blue_Mtn_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev"], "Blue_Mtn_Dam"),
    ("lake-hartwell-ga", "Lake Hartwell", ["Hartwell Lake"], "GA", ["GA", "SC"], "SAS", ["Hartwell.Elev-Pool.Inst.1Hour.0.Raw-SHEF_SAS"], "Hartwell"),
    ("thurmond-lake-ga", "J. Strom Thurmond Lake", ["Clarks Hill Lake", "Clark Hill Lake", "Lake Thurmond"], "GA", ["GA", "SC"], "SAS", ["Thurmond.Elev-Pool.Inst.1Hour.0.Raw-SHEF_SAS"], "Thurmond"),
    ("lake-russell-ga", "Richard B. Russell Lake", ["Lake Russell"], "GA", ["GA", "SC"], "SAS", ["Russell.Elev-Pool.Inst.1Hour.0.Raw-SHEF_SAS"], "Russell"),
    ("lake-lanier-ga", "Lake Lanier", ["Lake Sidney Lanier", "Buford Dam"], "GA", ["GA"], "SAM", ["Buford.Elev.Inst.15Minutes.0.Raw-USGS", "Buford.Elev.Inst.1Hour.0.Raw-USGS"], "Buford"),
    ("lake-allatoona-ga", "Lake Allatoona", ["Allatoona Lake"], "GA", ["GA"], "SAM", ["Allatoona.Elev.Inst.1Hour.0.Raw-USGS"], "Allatoona"),
    ("west-point-lake-ga", "West Point Lake", [], "GA", ["GA", "AL"], "SAM", ["WestPoint.Elev.Inst.15Minutes.0.Raw-USGS"], "WestPoint"),
    ("carters-lake-ga", "Carters Lake", [], "GA", ["GA"], "SAM", ["Carters.Elev.Inst.15Minutes.0.Raw-USGS"], "Carters"),
    ("walter-f-george-lake-ga", "Walter F. George Lake", ["Lake Eufaula, Alabama", "Lake Walter F. George"], "GA", ["GA", "AL"], "SAM", ["WalterFGeorge.Elev.Inst.15Minutes.0.Raw-USGS"], "WalterFGeorge"),
    ("lake-seminole-fl", "Lake Seminole", ["Jim Woodruff Lake"], "FL", ["FL", "GA"], "SAM", ["JimWoodruff.Elev.Inst.15Minutes.0.Raw-USGS"], "JimWoodruff", None, (["Top of Normal"], "first", "top of normal pool")),
    ("lake-martin-al", "Lake Martin", [], "AL", ["AL"], "SAM", ["Martin.Elev.Inst.1Hour.0.Raw-APCO"], "Martin", "alabama-power"),
    ("lewis-smith-lake-al", "Lewis Smith Lake", ["Smith Lake"], "AL", ["AL"], "SAM", ["Smith.Elev.Inst.1Hour.0.Raw-APCO"], "Smith", "alabama-power"),
    ("weiss-lake-al", "Weiss Lake", [], "AL", ["AL", "GA"], "SAM", ["Weiss.Elev.Inst.1Hour.0.Raw-APCO"], "Weiss", "alabama-power"),
    ("logan-martin-lake-al", "Logan Martin Lake", [], "AL", ["AL"], "SAM", ["LoganMartin.Elev.Inst.1Hour.0.Raw-APCO"], "LoganMartin", "alabama-power"),
    ("keystone-lake-ok", "Keystone Lake", [], "OK", ["OK"], "SWT", ["KEYS.Elev.Inst.1Hour.0.Ccp-Rev"], "KEYS"),
    ("lake-eufaula-ok", "Lake Eufaula", ["Eufaula Lake"], "OK", ["OK"], "SWT", ["EUFA.Elev.Inst.1Hour.0.Ccp-Rev"], "EUFA"),
    ("kaw-lake-ok", "Kaw Lake", [], "OK", ["OK"], "SWT", ["KAWL.Elev.Inst.1Hour.0.Ccp-Rev"], "KAWL"),
    ("oologah-lake-ok", "Oologah Lake", [], "OK", ["OK"], "SWT", ["OOLO.Elev.Inst.1Hour.0.Ccp-Rev"], "OOLO"),
    ("fort-gibson-lake-ok", "Fort Gibson Lake", [], "OK", ["OK"], "SWT", ["FGIB.Elev.Inst.1Hour.0.Ccp-Rev"], "FGIB"),
    ("lake-tenkiller-ok", "Lake Tenkiller", ["Tenkiller Ferry Lake", "Tenkiller Lake"], "OK", ["OK"], "SWT", ["TENK.Elev.Inst.1Hour.0.Ccp-Rev"], "TENK"),
    ("skiatook-lake-ok", "Skiatook Lake", [], "OK", ["OK"], "SWT", ["SKIA.Elev.Inst.1Hour.0.Ccp-Rev"], "SKIA"),
    ("copan-lake-ok", "Copan Lake", [], "OK", ["OK", "KS"], "SWT", ["COPA.Elev.Inst.1Hour.0.Ccp-Rev"], "COPA"),
    ("hulah-lake-ok", "Hulah Lake", [], "OK", ["OK"], "SWT", ["HULA.Elev.Inst.1Hour.0.Ccp-Rev"], "HULA"),
    ("canton-lake-ok", "Canton Lake", [], "OK", ["OK"], "SWT", ["CANT.Elev.Inst.1Hour.0.Ccp-Rev"], "CANT"),
    ("wister-lake-ok", "Wister Lake", [], "OK", ["OK"], "SWT", ["WIST.Elev.Inst.1Hour.0.Ccp-Rev"], "WIST"),
    ("broken-bow-lake-ok", "Broken Bow Lake", [], "OK", ["OK"], "SWT", ["BROK.Elev.Inst.1Hour.0.Ccp-Rev"], "BROK", None, (["Top of Power", "Top of Constant Conservation"], "first", "top of power pool")),
    ("hugo-lake-ok", "Hugo Lake", [], "OK", ["OK"], "SWT", ["HUGO.Elev.Inst.1Hour.0.Ccp-Rev"], "HUGO"),
    ("grand-lake-ok", "Grand Lake", ["Grand Lake O' the Cherokees", "Pensacola Dam"], "OK", ["OK"], "SWT", ["PENS.Elev.Inst.1Hour.0.Ccp-Rev"], "PENS", "grda"),
    ("john-redmond-reservoir-ks", "John Redmond Reservoir", [], "KS", ["KS"], "SWT", ["JOHN.Elev.Inst.1Hour.0.Ccp-Rev"], "JOHN"),
    ("fall-river-lake-ks", "Fall River Lake", [], "KS", ["KS"], "SWT", ["FALL.Elev.Inst.1Hour.0.Ccp-Rev"], "FALL"),
    ("elk-city-lake-ks", "Elk City Lake", [], "KS", ["KS"], "SWT", ["ELKC.Elev.Inst.1Hour.0.Ccp-Rev"], "ELKC"),
    ("el-dorado-lake-ks", "El Dorado Lake", [], "KS", ["KS"], "SWT", ["ELDR.Elev.Inst.1Hour.0.Ccp-Rev"], "ELDR"),
    ("rough-river-lake-ky", "Rough River Lake", [], "KY", ["KY"], "LRL", ["Rough.Elev.Inst.1Hour.0.lrldlb-comp"], "Rough"),
    ("nolin-river-lake-ky", "Nolin River Lake", ["Nolin Lake"], "KY", ["KY"], "LRL", ["Nolin.Elev.Inst.1Hour.0.lrldlb-comp"], "Nolin"),
    ("barren-river-lake-ky", "Barren River Lake", [], "KY", ["KY"], "LRL", ["Barren.Elev.Inst.1Hour.0.lrldlb-comp"], "Barren"),
    ("green-river-lake-ky", "Green River Lake", [], "KY", ["KY"], "LRL", ["Green.Elev.Inst.1Hour.0.lrldlb-comp"], "Green"),
    ("cave-run-lake-ky", "Cave Run Lake", [], "KY", ["KY"], "LRL", ["CaveRun.Elev.Inst.1Hour.0.lrldlb-comp"], "CaveRun"),
    ("taylorsville-lake-ky", "Taylorsville Lake", [], "KY", ["KY"], "LRL", ["Taylorsville.Elev.Inst.1Hour.0.lrldlb-comp"], "Taylorsville"),
    ("monroe-lake-in", "Monroe Lake", ["Lake Monroe"], "IN", ["IN"], "LRL", ["Monroe.Elev.Inst.1Hour.0.lrldlb-comp"], "Monroe"),
    ("patoka-lake-in", "Patoka Lake", [], "IN", ["IN"], "LRL", ["Patoka.Elev.Inst.1Hour.0.lrldlb-comp"], "Patoka"),
    ("brookville-lake-in", "Brookville Lake", [], "IN", ["IN"], "LRL", ["Brookville.Elev.Inst.1Hour.0.lrldlb-comp"], "Brookville"),
    ("lake-cumberland-ky", "Lake Cumberland", ["Wolf Creek Dam"], "KY", ["KY"], "LRN", ["WLCK2-WOLF_CREEK.Elev-Pool.Inst.1Hour.0.dcp-rev"], "WLCK2-WOLF_CREEK"),
    ("dale-hollow-lake-tn", "Dale Hollow Lake", [], "TN", ["TN", "KY"], "LRN", ["DLHT1-DALE_HOLLOW.Elev-Pool.Inst.1Hour.0.dcp-rev"], "DLHT1-DALE_HOLLOW"),
    ("center-hill-lake-tn", "Center Hill Lake", [], "TN", ["TN"], "LRN", ["CEHT1-CENTER_HILL.Elev-Pool.Inst.1Hour.0.dcp-rev"], "CEHT1-CENTER_HILL"),
    ("percy-priest-lake-tn", "J. Percy Priest Lake", ["Percy Priest Lake"], "TN", ["TN"], "LRN", ["JPHT1-J_PERCY_PRIEST.Elev-Pool.Inst.1Hour.0.dcp-rev"], "JPHT1-J_PERCY_PRIEST"),
    ("old-hickory-lake-tn", "Old Hickory Lake", [], "TN", ["TN"], "LRN", ["OHHT1-OLD_HICKORY.Elev-Pool.Inst.1Hour.0.dcp-rev"], "OHHT1-OLD_HICKORY"),
    ("lake-barkley-ky", "Lake Barkley", [], "KY", ["KY", "TN"], "LRN", ["BAHK2-BARKLEY.Elev-Pool.Inst.1Hour.0.dcp-rev"], "BAHK2-BARKLEY"),
    ("laurel-river-lake-ky", "Laurel River Lake", [], "KY", ["KY"], "LRN", ["LAPK2-LAUREL.Elev-Pool.Inst.1Hour.0.dcp-rev"], "LAPK2-LAUREL"),
    ("falls-lake-nc", "Falls Lake", [], "NC", ["NC"], "SAW", ["NEU FLP.Elev.Inst.15Minutes.0.USGS-raw"], "NEU FLP"),
    ("jordan-lake-nc", "Jordan Lake", ["B. Everett Jordan Lake"], "NC", ["NC"], "SAW", ["CFR BEJ.Elev.Inst.15Minutes.0.USGS-raw"], "CFR BEJ"),
    ("kerr-lake-va", "Kerr Lake", ["Buggs Island Lake", "John H. Kerr Reservoir"], "VA", ["VA", "NC"], "SAW", ["ROA JHK.Elev.Inst.5Minutes.0.USGS-raw", "ROA JHK.Elev.Inst.15Minutes.0.USGS-raw"], "ROA JHK"),
    ("grenada-lake-ms", "Grenada Lake", [], "MS", ["MS"], "MVK", ["Grenada Lake.Elev.Inst.1Hour.0.DCP-rev"], "Grenada Lake", None, (["Top of Conservation", "Top of Guide Curve"], "max", None)),
    ("sardis-lake-ms", "Sardis Lake", [], "MS", ["MS"], "MVK", ["Sardis Lake.Elev.Inst.1Hour.0.DCP-rev"], "Sardis Lake", None, (["Top of Conservation", "Top of Guide Curve"], "max", None)),
    ("enid-lake-ms", "Enid Lake", [], "MS", ["MS"], "MVK", ["Enid Lake.Elev.Inst.1Hour.0.DCP-rev"], "Enid Lake", None, (["Top of Conservation", "Top of Guide Curve"], "max", None)),
    ("arkabutla-lake-ms", "Arkabutla Lake", [], "MS", ["MS"], "MVK", ["Arkabutla Lake.Elev.Inst.1Hour.0.DCP-rev"], "Arkabutla Lake", None, (["Top of Conservation", "Top of Guide Curve"], "max", None)),
    ("lake-ouachita-ar", "Lake Ouachita", ["Blakely Mountain Dam"], "AR", ["AR"], "MVK", ["Blakely.Elev.Inst.1Hour.0.DCP-rev"], "Blakely"),
    ("degray-lake-ar", "DeGray Lake", [], "AR", ["AR"], "MVK", ["Degray Lake.Elev.Inst.1Hour.0.DCP-rev"], "Degray Lake"),
    ("lake-greeson-ar", "Lake Greeson", ["Narrows Dam"], "AR", ["AR"], "MVK", ["Narrows Dam.Elev.Inst.1Hour.0.DCP-rev"], "Narrows Dam"),
    ("mark-twain-lake-mo", "Mark Twain Lake", [], "MO", ["MO"], "MVS", ["Mark Twain Lk-Salt.Elev.Inst.30Minutes.0.lrgsShef-rev"], "Mark Twain Lk-Salt"),
    ("carlyle-lake-il", "Carlyle Lake", [], "IL", ["IL"], "MVS", ["Carlyle Lk-Kaskaskia.Elev.Inst.30Minutes.0.lrgsShef-rev"], "Carlyle Lk-Kaskaskia"),
    ("lake-shelbyville-il", "Lake Shelbyville", [], "IL", ["IL"], "MVS", ["Lk Shelbyville-Kaskaskia.Elev.Inst.30Minutes.0.lrgsShef-rev"], "Lk Shelbyville-Kaskaskia"),
    ("rend-lake-il", "Rend Lake", [], "IL", ["IL"], "MVS", ["Rend Lk-Big Muddy.Elev.Inst.30Minutes.0.lrgsShef-rev"], "Rend Lk-Big Muddy"),
    ("wappapello-lake-mo", "Wappapello Lake", [], "MO", ["MO"], "MVS", ["Wappapello Lk-St Francis.Elev.Inst.30Minutes.0.lrgsShef-rev"], "Wappapello Lk-St Francis"),
    ("summersville-lake-wv", "Summersville Lake", [], "WV", ["WV"], "LRH", ["Summersville-Lake.Elev.Inst.15Minutes.0.OBS"], "Summersville-Lake"),
    ("atwood-lake-oh", "Atwood Lake", [], "OH", ["OH"], "LRH", ["Atwood-Lake.Elev.Inst.15Minutes.0.OBS"], "Atwood-Lake"),
    ("youghiogheny-river-lake-pa", "Youghiogheny River Lake", ["Yough Lake"], "PA", ["PA", "MD"], "LRP", ["Youghiogheny-Lake.Elev.Inst.1Hour.0.OBS"], "Youghiogheny-Lake"),
    ("allegheny-reservoir-pa", "Allegheny Reservoir", ["Kinzua Lake", "Kinzua Dam"], "PA", ["PA", "NY"], "LRP", ["Kinzua-Lake.Elev.Inst.1Hour.0.OBS"], "Kinzua-Lake"),
]


def cwms_ts(office, name, days=3):
    end = NOW.replace(minute=0, second=0, microsecond=0)
    params = {"office": office, "name": name, "begin": (end - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "unit": "EN", "page-size": 5000}
    url = CWMS + "timeseries?" + urllib.parse.urlencode(params)
    d = http_json(url, accept="application/json;version=2")
    vals = [v for v in d.get("values") or [] if v[1] is not None]
    return url, d, vals


def level_locs(loc):
    """Pool levels sit on the series location or on its base location (before the first '-'):
    LRN 'WLCK2-WOLF_CREEK' -> 'WLCK2', MVS 'Mark Twain Lk-Salt' -> 'Mark Twain Lk', LRH 'Summersville-Lake' -> 'Summersville'."""
    return {loc, loc.split("-")[0]}


def cwms_levels(office, loc):
    base = loc.split("-")[0]
    url = CWMS + "levels?" + urllib.parse.urlencode({"office": office, "level-id-mask": f"{base}*", "unit": "EN", "page-size": 1000})
    d = http_json(url, accept="application/json;version=2")
    return url, d.get("levels", [])


def level_value(levels, loc, names):
    """Return (name, value, seasonal_list or None, level_id, level_date) for the first matching
    specified level on an elevation parameter, using the latest effective date."""
    out = []
    for nm in names:
        cands = [l for l in levels if l.get("specified-level-id") == nm
                 and l.get("location-level-id", "").split(".")[0] in level_locs(loc)
                 and l.get("parameter-id", "").startswith("Elev")
                 and not l.get("parameter-id", "").startswith("Elev-Tail")]
        cands = [l for l in cands if (l.get("level-date") or "") <= NOW.strftime("%Y-%m-%dT%H:%M:%SZ")]
        if not cands:
            continue
        # prefer constants among the latest effective date
        cands.sort(key=lambda l: (l.get("level-date") or "", l.get("constant-value") is not None))
        l = cands[-1]
        if l.get("constant-value") is not None:
            out.append((nm, round(l["constant-value"], 2), None, l["location-level-id"], l.get("level-date")))
        elif l.get("seasonal-values"):
            sv = [round(s["value"], 2) for s in l["seasonal-values"] if s.get("value") is not None]
            if sv:
                out.append((nm, max(sv), [[s.get("offset-months"), s.get("offset-minutes"), round(s["value"], 2)]
                                          for s in l["seasonal-values"]], l["location-level-id"], l.get("level-date")))
    return out


TZ_ALIASES = {"US/Central": "America/Chicago", "US/Eastern": "America/New_York", "US/Mountain": "America/Denver",
              "US/Pacific": "America/Los_Angeles", "CST6CDT": "America/Chicago", "EST5EDT": "America/New_York"}


def cwms_location(office, loc):
    for name in [loc.split("-")[0], loc]:
        url = CWMS + "locations/" + urllib.parse.quote(name) + "?" + urllib.parse.urlencode({"office": office, "unit": "EN"})
        try:
            d = http_json(url, accept="application/json;version=2")
            return url, d
        except RuntimeError:
            continue
    return None, {}


def cwms_lakes(evidence):
    lakes, rejected = [], []
    for row in CWMS_LAKES:
        lid, name, aliases, st, sts, office, series_list, loc = row[:8]
        op = row[8] if len(row) > 8 and row[8] else CWMS_OPERATOR[office]
        pools = row[9] if len(row) > 9 and row[9] else POOLS[office]
        ev = {"source": "cwms", "lake_id": lid, "office": office, "location": loc, "tried": []}
        chosen = None
        for s in series_list:
            try:
                url, d, vals = cwms_ts(office, s)
            except RuntimeError as e:
                ev["tried"].append({"series": s, "error": str(e)})
                continue
            if not vals:
                ev["tried"].append({"series": s, "url": url, "result": "no values"})
                continue
            t = dt.datetime.fromtimestamp(vals[-1][0] / 1000, dt.timezone.utc)
            ev["tried"].append({"series": s, "url": url, "last": r2(vals[-1][1]), "at": t.isoformat(),
                                "units": d.get("units"), "n": len(vals)})
            if d.get("units") != "ft":
                continue
            if hours_since(t) > 48:
                continue
            chosen = (s, url, d, vals, t)
            break
        if not chosen:
            rejected.append({"lake_id": lid, "office": office, "reason": "no ft series with a value in the last 48 h",
                             "tried": ev["tried"]})
            evidence["cwms"].append(ev)
            continue
        s, url, d, vals, t = chosen
        last = r2(vals[-1][1])
        diffs = [(vals[i][0] - vals[i - 1][0]) / 60000 for i in range(1, len(vals))]
        cadence = round(sorted(diffs)[len(diffs) // 2]) if diffs else None
        try:
            lurl, levels = cwms_levels(office, loc)
        except RuntimeError as e:
            lurl, levels = None, []
            ev["levels_error"] = str(e)
        names, mode, label = pools
        found = level_value(levels, loc, names)
        full = full_name = seasonal = full_level_id = None
        if found:
            if mode == "max":
                pick = max(found, key=lambda x: x[1])
            else:
                pick = found[0]
            full_name, full, seasonal, full_level_id, _ = pick
        lvl = {l.get("specified-level-id"): l for l in levels}
        allv = {}
        for nm in ["Top of Inactive", "Bottom of Conservation", "Bottom of Normal", "Bottom of Power", "Bottom of Operating",
                   "Streambed", "Top of Dam", "Top of Flood", "Top of Normal"]:
            r = level_value(levels, loc, [nm])
            if r:
                allv[nm] = r[0][1]
        bottoms = [allv[k] for k in ["Top of Inactive", "Bottom of Conservation", "Bottom of Normal",
                                     "Bottom of Power", "Bottom of Operating"] if k in allv]
        dead = allv.get("Top of Inactive")
        if bottoms:
            vmin = min(bottoms) - 5
            vmin_basis = "lowest USACE pool bottom level - 5 ft"
        elif "Streambed" in allv:
            vmin, vmin_basis = allv["Streambed"], "USACE streambed level"
        else:
            vmin, vmin_basis = last - 60, "reading - 60 ft (no USACE pool levels)"
        if "Top of Dam" in allv:
            vmax, vmax_basis = allv["Top of Dam"], "USACE top of dam level"
        elif "Top of Flood" in allv:
            vmax, vmax_basis = allv["Top of Flood"] + 10, "USACE top of flood level + 10 ft"
        elif full:
            vmax, vmax_basis = full + 60, "full pool + 60 ft"
        else:
            vmax, vmax_basis = last + 60, "reading + 60 ft (no USACE pool levels)"
        if vmin > last:   # the lake is below every bottom level: widen, never reject a real reading
            vmin, vmin_basis = last - 20, "reading - 20 ft (below the USACE bottom levels)"
        ev.update({"series": s, "ts_url": url, "last_ft": last, "last_at": t.isoformat(), "units": d.get("units"),
                   "time_zone": d.get("time-zone"), "cadence_min": cadence, "quality_codes": sorted({v[2] for v in vals}),
                   "levels_url": lurl, "levels_found": allv, "full_pool_level": full_level_id, "full_pool_ft": full,
                   "full_pool_seasonal": seasonal})
        if full is not None and abs(last - full) > 60:
            rejected.append({"lake_id": lid, "office": office, "reason": f"reading {last} ft is not plausible against full pool {full} ft"})
            evidence["cwms"].append(ev)
            continue
        if not (vmin <= last <= vmax):
            rejected.append({"lake_id": lid, "office": office, "reason": f"reading {last} outside {vmin}-{vmax}"})
            evidence["cwms"].append(ev)
            continue
        if label is None and full_name:
            label = full_name.lower() + " pool" if not full_name.lower().endswith("curve") else full_name.lower()
        if full is not None and full_name == "Summer Guide Curve":
            label = "summer guide curve (summer pool)"
        if full is not None and full_name == "Top of Guide Curve":
            label = "top of guide curve (summer pool)"
        lurl2, locd = cwms_location(office, loc)
        ev["location_url"] = lurl2
        ev["location"] = {k: locd.get(k) for k in ["latitude", "longitude", "vertical-datum", "public-name", "state-initial",
                                                   "county-name", "timezone-name"]}
        primary = {"source": "cwms", "series_id": f"{office}:{s}", "office": office, "name": s, "param": "elevation"}
        if seasonal:
            primary["full_pool_level"] = full_level_id
        zones = dict((c, tz) for c, _, _, tz in STATES)[st]
        cand = TZ_ALIASES.get(locd.get("timezone-name"), locd.get("timezone-name"))
        cand2 = TZ_ALIASES.get(d.get("time-zone"), d.get("time-zone"))
        loc_tz = cand if cand in zones else (cand2 if cand2 in zones else zones[0])
        vd = locd.get("vertical-datum")
        lake = {
            "id": lid, "name": name, "aliases": aliases, "kind": "reservoir",
            "state": st, "states": sts,
            "county": None if "Unknown" in (locd.get("county-name") or "Unknown") else locd.get("county-name"),
            "lat": locd.get("latitude"), "lon": locd.get("longitude"),
            "timezone": loc_tz, "operator": op, "system": None,
            "primary": primary, "fallback": None, "intraday": None,
            "extra": {"storage": None, "water_temp": None, "release": None},
            "datum": (f"{vd} (USACE CWMS location datum)" if vd else
                      "as published by USACE " + office + " (CWMS location datum not stated)"),
            "full_pool_ft": full,
            "full_pool_label": (label if full is not None else None),
            "full_pool_source": lurl if full is not None else None,
            "dead_pool_ft": dead, "capacity_af": None, "flood_control": True,
            "valid_min_ft": r2(vmin), "valid_max_ft": r2(vmax),
            "bands": None, "mapping_status": "verified", "verified_on": TODAY,
            "published": True, "page": "live", "demand": 0,
            "source_url": f"{CWMS}timeseries?office={urllib.parse.quote(office)}&name={urllib.parse.quote(s)}&unit=EN",
        }
        if seasonal:
            lake["full_pool_note"] = (f"USACE sets a seasonal {full_name.lower()}; full_pool_ft is its highest value "
                                      f"(summer). The pipeline reads the current value from {full_level_id}.")
        if office == "LRL" and "Top of Normal" in allv and full is not None and allv["Top of Normal"] < full:
            lake["full_pool_note"] = (f"Summer pool {full} ft; USACE lists a lower 'Top of Normal' of "
                                      f"{allv['Top of Normal']} ft (winter pool).")
        lake["valid_range_basis"] = f"min: {vmin_basis}; max: {vmax_basis}"
        ev["accepted"] = True
        evidence["cwms"].append(ev)
        lakes.append(lake)
    return lakes, rejected


# ----------------------------------------------------------------------------- RISE

RISE = "https://data.usbr.gov/rise/api/"
HOOVER_FAQ = "https://www.usbr.gov/lc/hooverdam/faqs/lakefaqs.html"
FEIS_APPB = "https://www.usbr.gov/lc/region/programs/strategies/FEIS/AppB.pdf"
FG_PAGE = "https://www.usbr.gov/uc/rm/crsp/fg/"
PROJ = "https://www.usbr.gov/projects/index.php?id="
RESALOC = "https://www.usbr.gov/gp/aop/resaloc/"

# id, name, aliases, dam state, states, tz, elevation item, storage item, release item,
# full-pool spec: (kind, url, regex or key, label), dead-pool spec, max-surface spec
RISE_LAKES = [
    ("lake-mead-nv", "Lake Mead", ["Hoover Dam"], "NV", ["NV", "AZ"], "America/Los_Angeles", 6123, 6124, 6125,
     ("text", HOOVER_FAQ, r"high-water line is at ([0-9,\.]+) feet", "high-water line"),
     ("proj", PROJ + "122", "Top of Dead Storage Pool (Elevation)"), ("proj", PROJ + "122", "Maximum Water Surface Elevation")),
    ("lake-powell-az", "Lake Powell", ["Glen Canyon Dam"], "AZ", ["AZ", "UT"], "America/Phoenix", 508, 509, None,
     ("pdf", FEIS_APPB, r'"full reservoir" water level is ([0-9,\.]+) feet', "full reservoir level (top of spillway gates)"),
     ("proj", PROJ + "144", "Top of Dead Storage Pool (Elevation)"), ("proj", PROJ + "144", "Maximum Water Surface Elevation")),
    ("flaming-gorge-reservoir-ut", "Flaming Gorge Reservoir", ["Flaming Gorge Lake"], "UT", ["UT", "WY"], "America/Denver", 341, 337, None,
     ("text", FG_PAGE, r"full at elevation ([0-9,\.]+) feet", "full pool"),
     ("proj", PROJ + "105", "Top of Dead Storage Pool (Elevation)"), ("proj", PROJ + "105", "Maximum Water Surface Elevation")),
    ("navajo-lake-nm", "Navajo Lake", ["Navajo Reservoir"], "NM", ["NM", "CO"], "America/Denver", 612, 613, None,
     ("proj", PROJ + "310", "Top of Joint Use Pool (Elevation)", "top of joint use pool"),
     ("proj", PROJ + "310", "Top of Dead Storage Pool (Elevation)"), ("proj", PROJ + "310", "Maximum Water Surface Elevation")),
    ("cheney-reservoir-ks", "Cheney Reservoir", ["Cheney Lake"], "KS", ["KS"], "America/Chicago", 133978, 133977, None,
     ("pdf", RESALOC + "cheney.pdf", r"Top of Active Conservation Elev\. ([0-9\.]+)", "top of active conservation pool"),
     ("pdf", RESALOC + "cheney.pdf", r"Top of Dead Elev\. ([0-9\.]+)"), ("pdf", RESALOC + "cheney.pdf", r"Top of Surcharge Elev\. ([0-9\.]+)")),
    ("lake-havasu-az", "Lake Havasu", ["Parker Dam"], "AZ", ["AZ", "CA"], "America/Phoenix", 6128, 6129, None,
     ("proj", PROJ + "207", "Maximum Water Surface Elevation", "maximum water surface"),
     None, ("proj", PROJ + "207", "Maximum Water Surface Elevation")),
    ("lake-mohave-az", "Lake Mohave", ["Davis Dam"], "AZ", ["AZ", "NV"], "America/Phoenix", 6133, 6134, None,
     ("proj", PROJ + "47", "Top of Active Conservation Pool (Elevation)", "top of active conservation pool"),
     ("proj", PROJ + "47", "Top of Dead Storage Pool (Elevation)"), ("proj", PROJ + "47", "Maximum Water Surface Elevation")),
    ("blue-mesa-reservoir-co", "Blue Mesa Reservoir", [], "CO", ["CO"], "America/Denver", 78, 76, None,
     ("proj", PROJ + "62", "Top of Joint Use Pool (Elevation)", "top of joint use pool"),
     ("proj", PROJ + "62", "Top of Dead Storage Pool (Elevation)"), ("proj", PROJ + "62", "Maximum Water Surface Elevation")),
    ("jordanelle-reservoir-ut", "Jordanelle Reservoir", [], "UT", ["UT"], "America/Denver", 471, 468, None,
     ("proj", PROJ + "176", "Top of Joint Use Pool (Elevation)", "top of joint use pool"),
     ("proj", PROJ + "176", "Top of Dead Storage Pool (Elevation)"), ("proj", PROJ + "176", "Maximum Water Surface Elevation")),
    ("caballo-lake-nm", "Caballo Reservoir", ["Caballo Lake"], "NM", ["NM"], "America/Denver", 212, 209, None,
     ("proj", PROJ + "42", "Top of Active Conservation Pool (Elevation)", "top of active conservation pool"),
     None, ("proj", PROJ + "42", "Maximum Water Surface Elevation")),
    ("willard-bay-ut", "Willard Bay Reservoir", ["Willard Bay"], "UT", ["UT"], "America/Denver", 869, 866, None,
     ("proj", PROJ + "48", "Top of Active Conservation Pool (Elevation)", "top of active conservation pool"),
     ("proj", PROJ + "48", "Top of Dead Storage Pool (Elevation)"), ("proj", PROJ + "48", "Maximum Water Surface Elevation")),
    ("horsetooth-reservoir-co", "Horsetooth Reservoir", [], "CO", ["CO"], "America/Denver", 87, 86, None,
     ("proj", PROJ + "119", "Top of Active Conservation Pool (Elevation)", "top of active conservation pool"),
     ("proj", PROJ + "119", "Top of Dead Storage Pool (Elevation)"), None),
    ("lake-granby-co", "Lake Granby", [], "CO", ["CO"], "America/Denver", 384, 383, None,
     ("proj", PROJ + "291", "Top of Joint Use Pool (Elevation)", "top of joint use pool"),
     ("proj", PROJ + "291", "Top of Dead Storage Pool (Elevation)"), None),
    ("pueblo-reservoir-co", "Pueblo Reservoir", ["Lake Pueblo"], "CO", ["CO"], "America/Denver", 682, 681, None,
     ("pdf", RESALOC + "pueblo.pdf", r"Top of Joint Use Elev\. ([0-9\.]+)", "top of joint use pool"),
     ("pdf", RESALOC + "pueblo.pdf", r"Top of Dead Elev\. ([0-9\.]+)"), ("pdf", RESALOC + "pueblo.pdf", r"Top of Surcharge Elev\. ([0-9\.]+)")),
    ("canyon-ferry-lake-mt", "Canyon Ferry Lake", [], "MT", ["MT"], "America/Denver", 14, 13, None,
     ("proj", PROJ + "50", "Top of Joint Use Pool (Elevation)", "top of joint use pool"),
     ("proj", PROJ + "50", "Top of Dead Storage Pool (Elevation)"), None),
    ("pathfinder-reservoir-wy", "Pathfinder Reservoir", [], "WY", ["WY"], "America/Denver", 645, 644, None,
     ("pdf", RESALOC + "pathfinder.pdf", r"Top of Active Conservation Elev\. ([0-9\.]+)", "top of active conservation pool"),
     ("pdf", RESALOC + "pathfinder.pdf", r"Top of Dead Elev\. ([0-9\.]+)"), ("proj", PROJ + "212", "Maximum Water Surface Elevation")),
    ("cachuma-lake-ca", "Cachuma Lake", ["Lake Cachuma", "Bradbury Dam"], "CA", ["CA"], "America/Los_Angeles", 10786, 10787, None,
     ("proj", PROJ + "254", "Top of Active Conservation Pool (Elevation)", "top of active conservation pool"),
     ("proj", PROJ + "254", "Top of Dead Storage Pool (Elevation)"), None),
]


def pdf_text(url):
    raw = http(url, spacing=1, binary=True)
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(raw)
        name = f.name
    try:
        out = subprocess.run(["pdftotext", "-layout", name, "-"], capture_output=True, text=True, timeout=60).stdout
    except Exception as e:
        raise RuntimeError(f"pdftotext not available: {e}")
    finally:
        os.unlink(name)
    return re.sub(r"\s+", " ", out)


def official_value(spec):
    """spec = (kind, url, pattern_or_field[, label]) -> (value, url, quote)."""
    if not spec:
        return None, None, None
    kind, url, pat = spec[0], spec[1], spec[2]
    if kind == "proj":
        t = page_text(http(url, spacing=1))
        m = re.search(re.escape(pat) + r" \| ([0-9][0-9,\.]*) ft", t)
        if not m:
            return None, url, None
        txt = m.group(1)
        if re.fullmatch(r"\d+,\d{2}", txt):   # e.g. "4104,00": malformed on the page, do not guess
            return None, url, f"{pat} | {txt} ft (malformed, not used)"
        return float(txt.replace(",", "")), url, f"{pat} | {txt} ft"
    t = page_text(http(url, spacing=1)) if kind == "text" else pdf_text(url)
    m = re.search(pat, t)
    if not m:
        return None, url, None
    return float(m.group(1).replace(",", "")), url, m.group(0)


def rise_result(item, days=12):
    after = (NOW - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    url = (RISE + "result?" + urllib.parse.urlencode({"itemId": item, "dateTime[after]": after, "itemsPerPage": 50,
                                                      "order[dateTime]": "DESC"}))
    d = http_json(url, accept="application/vnd.api+json", spacing=1, timeout=90)
    rows = sorted((x["attributes"] for x in d.get("data", [])), key=lambda a: a["dateTime"])
    return url, rows


def rise_lakes(evidence):
    lakes, rejected = [], []
    for (lid, name, aliases, st, sts, tz, item, stor, rel, fullspec, deadspec, maxspec) in RISE_LAKES:
        ev = {"source": "rise", "lake_id": lid, "item": item}
        try:
            cat = http_json(RISE + f"catalog-item/{item}", accept="application/vnd.api+json", spacing=1)
            ev["item_title"] = cat["data"]["attributes"].get("itemTitle")
            url, rows = rise_result(item)
        except (RuntimeError, KeyError) as e:
            rejected.append({"lake_id": lid, "reason": f"RISE request failed: {e}"})
            continue
        if not rows:
            rejected.append({"lake_id": lid, "reason": "no RISE rows in 12 days"})
            continue
        last = rows[-1]
        t = dt.datetime.fromisoformat(last["dateTime"])
        ev.update({"result_url": url, "last_ft": last["result"], "last_dateTime": last["dateTime"],
                   "result_fields": sorted(last.keys()), "n": len(rows)})
        if "Elevation-ft" not in (ev.get("item_title") or ""):
            rejected.append({"lake_id": lid, "reason": f"item {item} is not an elevation-ft item"})
            continue
        if hours_since(t) > 24 * 6:
            rejected.append({"lake_id": lid, "reason": f"latest RISE row {last['dateTime']} older than 6 days"})
            continue
        lat = lon = None
        try:
            locd = http_json(RISE + f"location/{last['locationId']}", accept="application/vnd.api+json", spacing=1)
            c = locd["data"]["attributes"]["locationCoordinates"]["coordinates"]
            lon, lat = c[0], c[1]
            ev["location"] = {"id": last["locationId"], "name": locd["data"]["attributes"]["locationName"],
                              "vertical_datum": (locd["data"]["attributes"].get("verticalDatum") or {}).get("_id")}
        except (RuntimeError, KeyError, TypeError) as e:
            ev["location_error"] = str(e)
        full, furl, fquote = official_value(fullspec)
        dead, durl, dquote = official_value(deadspec)
        mx, murl, mquote = official_value(maxspec)
        val = float(last["result"])
        ev.update({"full_pool_ft": full, "full_pool_url": furl, "full_pool_quote": fquote,
                   "dead_pool_ft": dead, "dead_pool_url": durl, "dead_quote": dquote,
                   "max_surface_ft": mx, "max_surface_url": murl, "max_quote": mquote})
        if full is not None and abs(val - full) > 250:
            rejected.append({"lake_id": lid, "reason": f"reading {val} implausible vs full {full}"})
            continue
        vmin = (dead - 5) if dead is not None else ((full - 120) if full else val - 120)
        vmax = (mx + 5) if mx is not None else ((full + 30) if full else val + 60)
        vmin = min(vmin, val - 5)
        lake = {
            "id": lid, "name": name, "aliases": aliases, "kind": "reservoir",
            "state": st, "states": sts, "county": None, "lat": lat, "lon": lon,
            "timezone": tz, "operator": "usbr", "system": "colorado-river-reservoirs" if lid in ("lake-mead-nv", "lake-powell-az") else None,
            "primary": {"source": "rise", "series_id": str(item), "param": "elevation"},
            "fallback": None, "intraday": None,
            "extra": {"storage": str(stor) if stor else None, "water_temp": None, "release": str(rel) if rel else None},
            "datum": "as published by USBR (project datum; not stated in the RISE item)",
            "full_pool_ft": full, "full_pool_label": fullspec[3] if full is not None else None,
            "full_pool_source": furl if full is not None else None,
            "dead_pool_ft": dead, "capacity_af": None, "flood_control": None,
            "valid_min_ft": r2(vmin), "valid_max_ft": r2(vmax),
            "bands": None, "mapping_status": "verified", "verified_on": TODAY,
            "published": True, "page": "live", "demand": 0,
            "source_url": f"{RISE}catalog-item/{item}",
        }
        lake["valid_range_basis"] = ("min: " + ("USBR dead pool - 5 ft" if dead is not None else "full pool - 120 ft") +
                                     "; max: " + ("USBR maximum water surface + 5 ft" if mx is not None else "full pool + 30 ft"))
        ev["accepted"] = True
        evidence["rise"].append(ev)
        lakes.append(lake)
    return lakes, rejected


# ----------------------------------------------------------------------------- CDEC

CDEC = "https://cdec.water.ca.gov/dynamicapp/"
# station, id, name, aliases, operator, full-pool spec (USBR page) or None, dead spec
CDEC_LAKES = [
    ("SHA", "shasta-lake-ca", "Shasta Lake", ["Lake Shasta"], "usbr", None, None),
    ("FOL", "folsom-lake-ca", "Folsom Lake", [], "usbr", None, None),
    ("ORO", "lake-oroville-ca", "Lake Oroville", [], None, None, None),
    ("CLE", "trinity-lake-ca", "Trinity Lake", ["Clair Engle Lake"], "usbr",
     ("proj", PROJ + "266", "Top of Active Conservation Pool (Elevation)", "top of active conservation pool"),
     ("proj", PROJ + "266", "Top of Dead Storage Pool (Elevation)")),
    ("NML", "new-melones-lake-ca", "New Melones Lake", ["New Melones Reservoir"], "usbr",
     ("proj", PROJ + "211", "Top of Joint Use Pool (Elevation)", "top of joint use pool"),
     ("proj", PROJ + "211", "Top of Dead Storage Pool (Elevation)")),
    ("DNP", "don-pedro-reservoir-ca", "Don Pedro Reservoir", ["New Don Pedro Reservoir"], None, None, None),
    ("SNL", "san-luis-reservoir-ca", "San Luis Reservoir", [], None, None, None),
    ("BER", "lake-berryessa-ca", "Lake Berryessa", ["Monticello Dam"], "usbr", None, None),
    ("MIL", "millerton-lake-ca", "Millerton Lake", ["Friant Dam"], "usbr", None, None),
    ("PNF", "pine-flat-lake-ca", "Pine Flat Lake", [], None, None, None),
    ("CSI", "lake-casitas-ca", "Lake Casitas", [], None, None, None),
    ("ISB", "lake-isabella-ca", "Lake Isabella", ["Isabella Lake"], None, None, None),
    ("WRS", "lake-sonoma-ca", "Lake Sonoma", ["Warm Springs Dam"], None, None, None),
    ("COY", "lake-mendocino-ca", "Lake Mendocino", ["Coyote Valley Dam"], None, None, None),
    ("TRM", "lake-kaweah-ca", "Lake Kaweah", ["Terminus Dam"], None, None, None),
    ("SCC", "lake-success-ca", "Lake Success", ["Success Lake"], None, None, None),
    ("NHG", "new-hogan-lake-ca", "New Hogan Lake", [], None, None, None),
    ("CMN", "camanche-reservoir-ca", "Camanche Reservoir", ["Lake Camanche"], None, None, None),
    ("BUL", "new-bullards-bar-reservoir-ca", "New Bullards Bar Reservoir", [], None, None, None),
    ("EXC", "lake-mcclure-ca", "Lake McClure", ["New Exchequer Dam"], None, None, None),
]


CDEC_OPERATORS = {"US Bureau of Reclamation": "usbr", "CA Dept of Water Resources": "ca-dwr",
                  "California Department of Water Resources": "ca-dwr"}   # prefix match


def cdec_lakes(evidence):
    lakes, rejected = [], []
    stations = ",".join(s[0] for s in CDEC_LAKES)
    start = (NOW - dt.timedelta(days=8)).strftime("%Y-%m-%d")
    url = (CDEC + "req/CSVDataServlet?" + urllib.parse.urlencode(
        {"Stations": stations, "SensorNums": "6,15", "dur_code": "D", "Start": start, "End": NOW.strftime("%Y-%m-%d")}))
    text = http(url, spacing=1)
    last = {}
    for r in csv.DictReader(io.StringIO(text)):
        if (r.get("VALUE") or "").strip() in ("", "---", "-9999"):
            continue
        last.setdefault(r["STATION_ID"], {})[r["SENSOR_NUMBER"]] = r
    for (sta, lid, name, aliases, op, fullspec, deadspec) in CDEC_LAKES:
        ev = {"source": "cdec", "lake_id": lid, "station": sta, "data_url": url}
        e6 = last.get(sta, {}).get("6")
        if not e6:
            rejected.append({"lake_id": lid, "station": sta, "reason": "no valid sensor-6 row in 8 days"})
            continue
        day = dt.datetime.strptime(e6["DATE TIME"], "%Y%m%d %H%M").date()
        ev.update({"last_ft": float(e6["VALUE"]), "date_time": e6["DATE TIME"], "obs_date": e6["OBS DATE"],
                   "units": e6["UNITS"], "storage_af": (last[sta].get("15") or {}).get("VALUE")})
        if e6["UNITS"] != "FEET" or (NOW.date() - day).days > 4:
            rejected.append({"lake_id": lid, "station": sta, "reason": f"stale or not feet ({e6['DATE TIME']}, {e6['UNITS']})"})
            continue
        purl = f"{CDEC}profile?s={sta}&type=res"
        cap = lake_name = None
        try:
            t = page_text(http(purl, spacing=1))
            m = re.search(r"\| Capacity \| ([0-9,]+) af \|", t)
            cap = int(m.group(1).replace(",", "")) if m else None
            m2 = re.search(r"\| Lake Name \| ([^|]+) \|", t)
            lake_name = m2.group(1).strip() if m2 else None
        except RuntimeError as e:
            ev["profile_error"] = str(e)
        # history for the validation range (sensor 6, daily, since 1990)
        hurl = (CDEC + "req/CSVDataServlet?" + urllib.parse.urlencode(
            {"Stations": sta, "SensorNums": "6", "dur_code": "D", "Start": "1990-01-01", "End": NOW.strftime("%Y-%m-%d")}))
        hist = []
        try:
            for r in csv.DictReader(io.StringIO(http(hurl, spacing=1, timeout=180))):
                try:
                    v = float(r["VALUE"])
                except (ValueError, TypeError):
                    continue
                if v > 0:
                    hist.append(v)
        except RuntimeError as e:
            ev["history_error"] = str(e)
        murl = f"{CDEC}staMeta?station_id={sta}"
        meta = {}
        try:
            t = page_text(http(murl, spacing=1))
            for k in ["Latitude", "Longitude", "County", "Operator"]:
                m = re.search(r"\| " + k + r" \| ([^|]+) \|", t)
                if m:
                    meta[k] = m.group(1).strip().rstrip("°")
        except RuntimeError as e:
            ev["meta_error"] = str(e)
        ev["station_meta_url"], ev["station_meta"] = murl, meta
        opname = meta.get("Operator") or ""
        cdec_op = next((v for k, v in CDEC_OPERATORS.items() if opname.startswith(k)), None)
        if op and cdec_op and op != cdec_op:
            ev["operator_conflict"] = [op, cdec_op]
        op_final = cdec_op or op
        full, furl, fq = official_value(fullspec) if fullspec else (None, None, None)
        dead, durl, dq = official_value(deadspec) if deadspec else (None, None, None)
        val = float(e6["VALUE"])
        hmax, hmin = (max(hist), min(hist)) if hist else (None, None)
        # trim obvious sensor spikes: use the 0.1th/99.9th percentile
        if hist:
            hs = sorted(hist)
            hmin, hmax = hs[int(len(hs) * 0.001)], hs[min(len(hs) - 1, int(len(hs) * 0.999))]
        vmin = (dead - 5) if dead is not None else (hmin - 10 if hmin is not None else val - 100)
        vmax = (hmax + 10) if hmax is not None else val + 100
        if full is not None:
            vmax = max(vmax, full + 10)
        ev.update({"profile_url": purl, "capacity_af": cap, "lake_name_on_profile": lake_name,
                   "history_url": hurl, "history_n": len(hist), "history_p001_ft": hmin, "history_p999_ft": hmax,
                   "full_pool_ft": full, "full_pool_url": furl, "full_pool_quote": fq, "dead_pool_ft": dead, "dead_quote": dq})
        lake = {
            "id": lid, "name": name, "aliases": aliases, "kind": "reservoir",
            "state": "CA", "states": ["CA"], "county": (meta.get("County") or "").title() or None,
            "lat": float(meta["Latitude"]) if meta.get("Latitude") else None,
            "lon": float(meta["Longitude"]) if meta.get("Longitude") else None,
            "timezone": "America/Los_Angeles", "operator": op_final, "operator_name": meta.get("Operator"), "system": None,
            "primary": {"source": "cdec", "series_id": sta, "param": "elevation", "dur": "D"},
            "fallback": None, "intraday": None,
            "extra": {"storage": f"{sta}:15", "water_temp": None, "release": None},
            "datum": "as published by CDEC (station datum not stated in the data service)",
            "full_pool_ft": full, "full_pool_label": fullspec[3] if full is not None else None,
            "full_pool_source": furl if full is not None else None,
            "dead_pool_ft": dead,
            "capacity_af": cap, "capacity_source": purl if cap else None,
            "flood_control": None,
            "valid_min_ft": r2(vmin), "valid_max_ft": r2(vmax),
            "bands": None, "mapping_status": "verified", "verified_on": TODAY,
            "published": True, "page": "live", "demand": 0,
            "source_url": f"{CDEC}QueryDaily?s={sta}",
        }
        lake["valid_range_basis"] = ("min: " + ("USBR dead pool - 5 ft" if dead is not None else "lowest daily reading since 1990 (0.1th percentile) - 10 ft")
                                     + "; max: highest daily reading since 1990 (99.9th percentile) + 10 ft")
        if full is None and cap:
            lake["full_pool_note"] = ("No official full-pool elevation found; percent full is computed from CDEC storage "
                                      "(sensor 15) and the CDEC capacity.")
        ev["accepted"] = True
        evidence["cdec"].append(ev)
        lakes.append(lake)
    return lakes, rejected


# ----------------------------------------------------------------------------- profiles

# High-demand lakes with no proven public feed (noindex profile pages). official_url only
# where a page was fetched with HTTP 200 by this script (see PROFILE_URLS check).
PROFILES = [
    ("smith-mountain-lake-va", "Smith Mountain Lake", ["SML"], "VA", ["VA"], "America/New_York"),
    ("lake-of-the-ozarks-mo", "Lake of the Ozarks", ["Lake Ozark", "Bagnell Dam"], "MO", ["MO"], "America/Chicago"),
    ("truman-lake-mo", "Truman Lake", ["Harry S. Truman Lake"], "MO", ["MO"], "America/Chicago"),
    ("lake-okeechobee-fl", "Lake Okeechobee", ["Lake O"], "FL", ["FL"], "America/New_York"),
    ("lake-norman-nc", "Lake Norman", [], "NC", ["NC"], "America/New_York"),
    ("lake-murray-sc", "Lake Murray", [], "SC", ["SC"], "America/New_York"),
    ("douglas-lake-tn", "Douglas Lake", [], "TN", ["TN"], "America/New_York"),
    ("norris-lake-tn", "Norris Lake", [], "TN", ["TN"], "America/New_York"),
    ("cherokee-lake-tn", "Cherokee Lake", [], "TN", ["TN"], "America/New_York"),
    ("stockton-lake-mo", "Stockton Lake", [], "MO", ["MO"], "America/Chicago"),
    ("pomme-de-terre-lake-mo", "Pomme de Terre Lake", [], "MO", ["MO"], "America/Chicago"),
    ("lake-nacimiento-ca", "Lake Nacimiento", [], "CA", ["CA"], "America/Los_Angeles"),
    ("lake-tahoe-ca", "Lake Tahoe", [], "CA", ["CA", "NV"], "America/Los_Angeles"),
    ("lake-mcconaughy-ne", "Lake McConaughy", [], "NE", ["NE"], "America/Chicago"),
    ("lake-marion-sc", "Lake Marion", [], "SC", ["SC"], "America/New_York"),
    ("roosevelt-lake-az", "Theodore Roosevelt Lake", ["Roosevelt Lake"], "AZ", ["AZ"], "America/Phoenix"),
    ("lake-pleasant-az", "Lake Pleasant", [], "AZ", ["AZ"], "America/Phoenix"),
]
# Candidate official pages; kept only if this run fetches them with HTTP 200.
PROFILE_URLS = {
    "roosevelt-lake-az": ("usbr", PROJ + "242", "Theodore Roosevelt Dam"),
    "lake-okeechobee-fl": (None, "https://www.saj.usace.army.mil/Missions/Civil-Works/Lake-Okeechobee/", "Okeechobee"),
    "truman-lake-mo": (None, "https://www.nwk.usace.army.mil/Locations/District-Lakes/Harry-S-Truman/", "Truman"),
    "stockton-lake-mo": (None, "https://www.nwk.usace.army.mil/Locations/District-Lakes/Stockton-Lake/", "Stockton"),
    "pomme-de-terre-lake-mo": (None, "https://www.nwk.usace.army.mil/Locations/District-Lakes/Pomme-de-Terre-Lake/", "Pomme"),
}
PROFILE_NOTES = {
    "smith-mountain-lake-va": "No AEP feed tested; no USGS lake gauge returned by the OGC latest-continuous call for Virginia.",
    "lake-of-the-ozarks-mo": "Ameren pages answered 403 (Nov 22); no USGS lake gauge returned for Missouri.",
    "truman-lake-mo": "CWMS NWK returned 0 elevation series (Nov 22-23).",
    "stockton-lake-mo": "CWMS NWK returned 0 elevation series.",
    "pomme-de-terre-lake-mo": "CWMS NWK returned 0 elevation series.",
    "lake-okeechobee-fl": "CWMS SAJ lists only computed navigation/zone series for Okeechobee, no lake-stage series accepted.",
    "douglas-lake-tn": "TVA pages answered 403; no USGS lake gauges returned for Tennessee.",
    "norris-lake-tn": "TVA pages answered 403; no USGS lake gauges returned for Tennessee.",
    "cherokee-lake-tn": "TVA pages answered 403; no USGS lake gauges returned for Tennessee.",
    "lake-norman-nc": "Duke Energy host timed out (Nov 22).",
    "lake-murray-sc": "Dominion feed not tested.",
    "lake-nacimiento-ca": "CDEC station NAC returned no sensor 6 or 15 rows for the last 8 days.",
    "lake-tahoe-ca": "No feed tested with a full-pool value in the same datum.",
    "lake-mcconaughy-ne": "No feed tested.",
    "lake-marion-sc": "No feed tested.",
    "roosevelt-lake-az": "RISE location 3713 has no daily elevation item; SRP not tested.",
    "lake-pleasant-az": "No feed tested.",
}


def profiles(evidence, taken):
    out = []
    for (lid, name, aliases, st, sts, tz) in PROFILES:
        if lid in taken:
            continue
        op, url = None, None
        if lid in PROFILE_URLS:
            o, u, must = PROFILE_URLS[lid]
            try:
                t = http(u, spacing=1, timeout=40)
                if must.lower() in t.lower():
                    op, url = o, u
                    evidence["profiles"].append({"lake_id": lid, "official_url": u, "status": 200})
                else:
                    evidence["profiles"].append({"lake_id": lid, "official_url": u, "status": "200 but lake name absent"})
            except RuntimeError as e:
                evidence["profiles"].append({"lake_id": lid, "official_url": u, "status": str(e)})
        out.append({
            "id": lid, "name": name, "aliases": aliases, "kind": "reservoir", "state": st, "states": sts,
            "county": None, "lat": None, "lon": None, "timezone": tz, "operator": op, "system": None,
            "primary": None, "fallback": None, "intraday": None,
            "extra": {"storage": None, "water_temp": None, "release": None},
            "datum": None, "full_pool_ft": None, "full_pool_label": None, "full_pool_source": None,
            "dead_pool_ft": None, "capacity_af": None, "flood_control": None,
            "valid_min_ft": None, "valid_max_ft": None, "bands": None,
            "mapping_status": "none", "verified_on": None, "published": False, "page": "profile",
            "demand": 0, "official_url": url, "source_url": url, "mapping_note": PROFILE_NOTES.get(lid),
        })
    return out


# ----------------------------------------------------------------------------- other ref files

SOURCES = {
    "twdb": {"id": "twdb", "agency": "Texas Water Development Board",
             "credit": "Texas Water Development Board, Water Data for Texas",
             "public_url": "https://www.waterdatafortexas.org/reservoirs/statewide",
             "fetch": {"kind": "bulk-json", "url": TWDB_JSON,
                       "history_url": TWDB_IND + "{twdb_slug}.csv", "recent_url": TWDB_IND + "{twdb_slug}-30day.csv"},
             "value_kind": "daily_average", "check_every_hours": 3, "soft_stale_hours": 48, "hard_stale_hours": 96,
             "expected_count": 123, "min_count": 118, "provisional": True,
             "disclaimer": "The reservoir data contained in this file are best estimates generated by the Texas Water Development Board (TWDB); TWDB makes no warranties.",
             "terms_checked": None, "last_verified": TODAY},
    "cwms": {"id": "cwms", "agency": "U.S. Army Corps of Engineers",
             "credit": "U.S. Army Corps of Engineers, CWMS Data API",
             "public_url": "https://cwms-data.usace.army.mil/cwms-data/",
             "fetch": {"kind": "per-lake", "url": CWMS + "timeseries?office={office}&name={name}&begin={begin}&end={end}&unit=EN",
                       "levels_url": CWMS + "levels?office={office}&level-id-mask={location}.*&unit=EN"},
             "value_kind": "hourly", "check_every_hours": 1, "soft_stale_hours": 6, "hard_stale_hours": 48,
             "expected_count": None, "min_count": None, "provisional": True,
             "disclaimer": "USACE data are provisional and subject to revision.",
             "terms_checked": None, "last_verified": TODAY},
    "rise": {"id": "rise", "agency": "U.S. Bureau of Reclamation",
             "credit": "U.S. Bureau of Reclamation, RISE",
             "public_url": "https://data.usbr.gov/",
             "fetch": {"kind": "per-item", "url": RISE + "result?itemId={series_id}&dateTime[after]={after}&itemsPerPage=50&order[dateTime]=DESC"},
             "value_kind": "daily_instant", "check_every_hours": 6, "soft_stale_hours": 60, "hard_stale_hours": 120,
             "expected_count": None, "min_count": None, "provisional": True,
             "disclaimer": "Data are provisional and subject to revision unless otherwise noted.",
             "terms_checked": None, "last_verified": TODAY},
    "cdec": {"id": "cdec", "agency": "California Department of Water Resources",
             "credit": "California Department of Water Resources, California Data Exchange Center (CDEC)",
             "public_url": "https://cdec.water.ca.gov/",
             "fetch": {"kind": "bulk-csv", "url": CDEC + "req/CSVDataServlet?Stations={stations}&SensorNums=6,15&dur_code={dur}&Start={start}&End={end}"},
             "value_kind": "daily_instant", "check_every_hours": 3, "soft_stale_hours": 48, "hard_stale_hours": 96,
             "hourly": {"value_kind": "hourly", "check_every_hours": 1, "soft_stale_hours": 6, "hard_stale_hours": 48},
             "expected_count": None, "min_count": None, "provisional": True,
             "disclaimer": "Data are provisional and subject to change.",
             "terms_checked": None, "last_verified": TODAY},
    "usgs": {"id": "usgs", "agency": "U.S. Geological Survey",
             "credit": "U.S. Geological Survey, Water Data API",
             "public_url": "https://waterdata.usgs.gov/",
             "fetch": {"kind": "bulk-geojson", "url": "https://api.waterdata.usgs.gov/ogcapi/v0/collections/latest-continuous/items?f=json&parameter_code={params}&state_name={state}&limit=500",
                       "paging": "follow the 'next' link exactly as returned"},
             "value_kind": "15min", "check_every_hours": 1, "soft_stale_hours": 6, "hard_stale_hours": 48,
             "expected_count": None, "min_count": None, "provisional": True,
             "disclaimer": "Provisional data are subject to revision.",
             "terms_checked": None, "last_verified": TODAY},
}

STATES = [  # code, name, neighbours, time zones
    ("AL", "Alabama", ["MS", "TN", "GA", "FL"], ["America/Chicago"]),
    ("AK", "Alaska", [], ["America/Anchorage", "America/Juneau", "America/Nome", "America/Sitka", "America/Yakutat", "America/Metlakatla", "America/Adak"]),
    ("AZ", "Arizona", ["CA", "NV", "UT", "NM", "CO"], ["America/Phoenix", "America/Denver"]),
    ("AR", "Arkansas", ["MO", "TN", "MS", "LA", "TX", "OK"], ["America/Chicago"]),
    ("CA", "California", ["OR", "NV", "AZ"], ["America/Los_Angeles"]),
    ("CO", "Colorado", ["WY", "NE", "KS", "OK", "NM", "AZ", "UT"], ["America/Denver"]),
    ("CT", "Connecticut", ["NY", "MA", "RI"], ["America/New_York"]),
    ("DE", "Delaware", ["MD", "PA", "NJ"], ["America/New_York"]),
    ("DC", "District of Columbia", ["MD", "VA"], ["America/New_York"]),
    ("FL", "Florida", ["AL", "GA"], ["America/New_York", "America/Chicago"]),
    ("GA", "Georgia", ["FL", "AL", "TN", "NC", "SC"], ["America/New_York"]),
    ("HI", "Hawaii", [], ["Pacific/Honolulu"]),
    ("ID", "Idaho", ["WA", "OR", "NV", "UT", "WY", "MT"], ["America/Boise", "America/Los_Angeles"]),
    ("IL", "Illinois", ["WI", "IA", "MO", "KY", "IN"], ["America/Chicago"]),
    ("IN", "Indiana", ["MI", "OH", "KY", "IL"], ["America/Indiana/Indianapolis", "America/Chicago"]),
    ("IA", "Iowa", ["MN", "WI", "IL", "MO", "NE", "SD"], ["America/Chicago"]),
    ("KS", "Kansas", ["NE", "MO", "OK", "CO"], ["America/Chicago", "America/Denver"]),
    ("KY", "Kentucky", ["IN", "OH", "WV", "VA", "TN", "MO", "IL"], ["America/New_York", "America/Chicago"]),
    ("LA", "Louisiana", ["TX", "AR", "MS"], ["America/Chicago"]),
    ("ME", "Maine", ["NH"], ["America/New_York"]),
    ("MD", "Maryland", ["PA", "DE", "VA", "WV", "DC"], ["America/New_York"]),
    ("MA", "Massachusetts", ["NY", "VT", "NH", "RI", "CT"], ["America/New_York"]),
    ("MI", "Michigan", ["WI", "IN", "OH"], ["America/Detroit", "America/Menominee"]),
    ("MN", "Minnesota", ["ND", "SD", "IA", "WI"], ["America/Chicago"]),
    ("MS", "Mississippi", ["LA", "AR", "TN", "AL"], ["America/Chicago"]),
    ("MO", "Missouri", ["IA", "IL", "KY", "TN", "AR", "OK", "KS", "NE"], ["America/Chicago"]),
    ("MT", "Montana", ["ID", "WY", "SD", "ND"], ["America/Denver"]),
    ("NE", "Nebraska", ["SD", "IA", "MO", "KS", "CO", "WY"], ["America/Chicago", "America/Denver"]),
    ("NV", "Nevada", ["OR", "ID", "UT", "AZ", "CA"], ["America/Los_Angeles"]),
    ("NH", "New Hampshire", ["VT", "ME", "MA"], ["America/New_York"]),
    ("NJ", "New Jersey", ["NY", "PA", "DE"], ["America/New_York"]),
    ("NM", "New Mexico", ["AZ", "UT", "CO", "OK", "TX"], ["America/Denver"]),
    ("NY", "New York", ["PA", "NJ", "CT", "MA", "VT"], ["America/New_York"]),
    ("NC", "North Carolina", ["VA", "TN", "GA", "SC"], ["America/New_York"]),
    ("ND", "North Dakota", ["MT", "SD", "MN"], ["America/Chicago", "America/Denver"]),
    ("OH", "Ohio", ["MI", "PA", "WV", "KY", "IN"], ["America/New_York"]),
    ("OK", "Oklahoma", ["KS", "MO", "AR", "TX", "NM", "CO"], ["America/Chicago"]),
    ("OR", "Oregon", ["WA", "ID", "NV", "CA"], ["America/Los_Angeles", "America/Boise"]),
    ("PA", "Pennsylvania", ["NY", "NJ", "DE", "MD", "WV", "OH"], ["America/New_York"]),
    ("RI", "Rhode Island", ["CT", "MA"], ["America/New_York"]),
    ("SC", "South Carolina", ["NC", "GA"], ["America/New_York"]),
    ("SD", "South Dakota", ["ND", "MN", "IA", "NE", "WY", "MT"], ["America/Chicago", "America/Denver"]),
    ("TN", "Tennessee", ["KY", "VA", "NC", "GA", "AL", "MS", "AR", "MO"], ["America/Chicago", "America/New_York"]),
    ("TX", "Texas", ["NM", "OK", "AR", "LA"], ["America/Chicago", "America/Denver"]),
    ("UT", "Utah", ["ID", "WY", "CO", "AZ", "NV", "NM"], ["America/Denver"]),
    ("VT", "Vermont", ["NY", "NH", "MA"], ["America/New_York"]),
    ("VA", "Virginia", ["MD", "DC", "WV", "KY", "TN", "NC"], ["America/New_York"]),
    ("WA", "Washington", ["ID", "OR"], ["America/Los_Angeles"]),
    ("WV", "West Virginia", ["OH", "PA", "MD", "VA", "KY"], ["America/New_York"]),
    ("WI", "Wisconsin", ["MI", "MN", "IA", "IL"], ["America/Chicago"]),
    ("WY", "Wyoming", ["MT", "SD", "NE", "CO", "UT", "ID"], ["America/Denver"]),
]

OPERATORS = {
    "lcra": {"name": "Lower Colorado River Authority", "short": "LCRA", "official_url": "https://www.lcra.org/",
             "has_public_feed": False,
             "note": "LCRA Hydromet hosts timed out from the build network (Nov 22); Highland Lakes levels come from TWDB. To be tested from a US runner."},
    "city-of-corpus-christi": {"name": "City of Corpus Christi", "short": "Corpus Christi", "official_url": "https://www.corpuschristitx.gov/",
                               "has_public_feed": False, "note": "Lake levels for the water-supply system come from TWDB."},
    "ca-dwr": {"name": "California Department of Water Resources", "short": "DWR", "official_url": "https://water.ca.gov/",
               "has_public_feed": True, "note": "Reservoir data through CDEC (cdec.water.ca.gov)."},
    "usbr": {"name": "U.S. Bureau of Reclamation", "short": "USBR", "official_url": "https://www.usbr.gov/",
             "has_public_feed": True, "note": "Daily reservoir data through RISE (data.usbr.gov)."},
    "grda": {"name": "Grand River Dam Authority", "short": "GRDA", "official_url": "https://www.grda.com/",
             "has_public_feed": False, "note": "Grand Lake's level is read from the USACE Tulsa District CWMS series PENS (Pensacola Dam)."},
    "alabama-power": {"name": "Alabama Power", "short": "Alabama Power", "official_url": "https://www.alabamapower.com/",
                      "has_public_feed": False, "note": "Its lake levels are read from USACE Mobile District CWMS series marked APCO. alabamapower.com answered 403 on Nov 22."},
}
USACE_DISTRICTS = {
    "usace-little-rock": ("USACE Little Rock District", "SWL", "https://www.swl.usace.army.mil/"),
    "usace-tulsa": ("USACE Tulsa District", "SWT", "https://www.swt.usace.army.mil/"),
    "usace-savannah": ("USACE Savannah District", "SAS", "https://www.sas.usace.army.mil/"),
    "usace-mobile": ("USACE Mobile District", "SAM", "https://www.sam.usace.army.mil/"),
    "usace-louisville": ("USACE Louisville District", "LRL", "https://www.lrl.usace.army.mil/"),
    "usace-nashville": ("USACE Nashville District", "LRN", "https://www.lrn.usace.army.mil/"),
    "usace-wilmington": ("USACE Wilmington District", "SAW", "https://www.saw.usace.army.mil/"),
    "usace-vicksburg": ("USACE Vicksburg District", "MVK", "https://www.mvk.usace.army.mil/"),
    "usace-st-louis": ("USACE St. Louis District", "MVS", "https://www.mvs.usace.army.mil/"),
    "usace-huntington": ("USACE Huntington District", "LRH", "https://www.lrh.usace.army.mil/"),
    "usace-pittsburgh": ("USACE Pittsburgh District", "LRP", "https://www.lrp.usace.army.mil/"),
}
for slug, (nm, off, url) in USACE_DISTRICTS.items():
    OPERATORS[slug] = {"name": nm, "short": f"USACE {off}", "official_url": url, "has_public_feed": True,
                       "note": f"Hourly or 15-minute pool elevations from the CWMS Data API, office {off}."}


def systems(lakes):
    ids = {l["id"]: l for l in lakes}

    def members(sysname):
        return [l["id"] for l in lakes if l.get("system") == sysname]
    out = {
        "highland-lakes": {"slug": "highland-lakes", "name": "Highland Lakes", "operator": "lcra",
                           "members": members("highland-lakes"),
                           "source_url": "https://www.waterdatafortexas.org/reservoirs/statewide",
                           "membership_basis": "TWDB tag 'highland' in recent-conditions.json",
                           "intro": "src/content/systems/highland-lakes.html"},
        "corpus-christi-water-supply": {"slug": "corpus-christi-water-supply", "name": "Corpus Christi water supply",
                                        "operator": "city-of-corpus-christi", "members": members("corpus-christi-water-supply"),
                                        "source_url": "https://www.waterdatafortexas.org/reservoirs/statewide",
                                        "membership_basis": "TWDB tag 'municipal_corpus_christi' (Choke Canyon, Lake Corpus Christi, Lake Texana)",
                                        "intro": "src/content/systems/corpus-christi-water-supply.html"},
        "colorado-river-reservoirs": {"slug": "colorado-river-reservoirs", "name": "Colorado River reservoirs (Lake Mead and Lake Powell)",
                                      "operator": "usbr", "members": members("colorado-river-reservoirs"),
                                      "source_url": "https://www.usbr.gov/lc/riverops.html",
                                      "membership_basis": "the two Colorado River storage reservoirs named in the plan",
                                      "intro": "src/content/systems/colorado-river-reservoirs.html"},
    }
    return out


# ----------------------------------------------------------------------------- docs

def write_docs(evidence, rejected, lakes):
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "evidence.json").write_text(json.dumps({"generated": NOW.isoformat(timespec="seconds"), "evidence": evidence,
                                                    "rejected": rejected}, indent=1, default=str) + "\n")
    by = {l["id"]: l for l in lakes}

    def fp(l):
        return "null (level only)" if l.get("full_pool_ft") is None else f"{l['full_pool_ft']} ft ({l['full_pool_label']})"
    hdr = f"Generated by `tools/map_lakes.py` on {NOW:%Y-%m-%d %H:%M} UTC. Machine-readable copy: `evidence.json`.\n\n"
    # TWDB
    rows = []
    for e in evidence["twdb"]:
        l = by.get(e.get("accepted_id"))
        if not l:
            continue
        r = e["reading"]
        rows.append(f"| `{l['id']}` | `{e['key']}` | `{e['slug']}` | {r['elevation_ft']} ft, {r['date']} | {l['datum']} | {fp(l)} | {l['valid_min_ft']}–{l['valid_max_ft']} |")
    txt = ("# TWDB mapping evidence\n\n" + hdr +
           f"All readings from `{TWDB_JSON}` (one request). Datum from each lake's `individual/<slug>-30day.csv` header "
           "(line 'Vertical datum(s) used'); the CSV slug is the lake's link on " + TWDB_STATEWIDE + ". "
           "Full pool = `conservation_pool_elevation` in the same JSON (same datum as `elevation`). "
           "Validation range: dead pool − 5 ft (or the lowest value of the last 20 years − 5 ft if lower) to the highest "
           "value in `individual/<slug>.csv` + 10 ft (the CSV header gives no flood-pool top), kept between full pool + 10 ft and "
           "full pool + 60 ft. Isolated spikes (> 15 ft from the median of the 3 values on each side) are dropped before "
           "the min/max; `evidence.json` lists them per lake.\n\n"
           "| Lake id | TWDB key | CSV slug | Reading | Datum | Full pool | Valid range ft |\n|---|---|---|---|---|---|---|\n"
           + "\n".join(rows) + "\n\n## Rejected\n\n"
           + ("\n".join(f"- `{r['key']}`: {r['reason']}" for r in rejected["twdb"]) or "None.") + "\n")
    (DOCS / "twdb.md").write_text(txt)
    # CWMS
    rows = []
    for e in evidence["cwms"]:
        if not e.get("accepted"):
            continue
        l = by[e["lake_id"]]
        rows.append(f"| `{l['id']}` | {e['office']} | `{e['series']}` | {e['last_ft']} {e['units']} at {e['last_at'][:16]}Z | "
                    f"{e['cadence_min']} min | {fp(l)}{' seasonal' if e.get('full_pool_seasonal') else ''} | {l['valid_min_ft']}–{l['valid_max_ft']} |")
    req = "\n".join(f"- `{e['lake_id']}`: series {e['ts_url']} ; levels {e['levels_url']}" for e in evidence["cwms"] if e.get("accepted"))
    txt = ("# CWMS mapping evidence\n\n" + hdr +
           "Rules: `unit=EN` on every call; a lake is accepted only when the last non-null value is within 48 h, the "
           "response `units` is `ft`, and the value sits within 60 ft of the full pool and inside the validation range. "
           "Full pool from `/levels` (specified level named in the table, latest effective date; for a seasonal level "
           "the highest seasonal value, with the level id stored as `full_pool_level` so the pipeline can read the "
           "current value). Catalog `latest-time` was never used for freshness.\n\n"
           "Hartwell: the earlier series `Hartwell.Elev-Head.Inst.1Hour.0.Raw-SHEF_SAS` (172 ft) is the head "
           "(pool minus tailwater), not the pool. The pool series is `Hartwell.Elev-Pool.Inst.1Hour.0.Raw-SHEF_SAS`.\n\n"
           "| Lake id | Office | Series | Last value | Cadence | Full pool | Valid range ft |\n|---|---|---|---|---|---|---|\n"
           + "\n".join(rows) + "\n\n## Request URLs\n\n" + req + "\n\n## Rejected\n\n"
           + ("\n".join(f"- `{r['lake_id']}` ({r['office']}): {r['reason']}" for r in rejected["cwms"]) or "None.") + "\n")
    (DOCS / "cwms.md").write_text(txt)
    # RISE
    rows = []
    for e in evidence["rise"]:
        l = by[e["lake_id"]]
        rows.append(f"| `{l['id']}` | {e['item']} | {e['last_ft']} ft, {e['last_dateTime'][:10]} | {fp(l)} | "
                    f"{e.get('full_pool_url') or '—'} | {e.get('full_pool_quote') or '—'} |")
    txt = ("# RISE mapping evidence\n\n" + hdr +
           "Readings: `result?itemId=<id>&dateTime[after]=<12 days ago>` (always date-filtered). Row fields: "
           "`dateTime`, `result`, `resultAttributes.timeStep`. Item ids were found by following "
           "location → catalogRecords → catalogItems (the `locationId` filter on `catalog-item` is ignored by the API). "
           "Full pool only from a usbr.gov page or PDF whose text is quoted below; otherwise null.\n\n"
           "| Lake id | Item | Last value | Full pool | Source URL | Quoted text |\n|---|---|---|---|---|---|\n"
           + "\n".join(rows) + "\n\nItems found: Mead 6123/6124/6125, Powell 508/509, Flaming Gorge 341/337, Navajo 612/613, "
           "Cheney 133978/133977, Havasu 6128/6129, Mohave 6133/6134, Blue Mesa 78/76, Jordanelle 471/468, Caballo 212/209, "
           "Willard Bay 869/866, Horsetooth 87/86, Granby 384/383, Pueblo 682/681, Canyon Ferry 14/13, Pathfinder 645/644, "
           "Cachuma 10786/10787, Millerton 10808/10807 (Millerton mapped to CDEC MIL). No daily elevation item found for "
           "Theodore Roosevelt (3713), Palisades (7193), Jackson Lake (7185), American Falls (7167).\n\n"
           "Lake Mead note: USBR gives 1,229.0 ft as the high-water line / top of the exclusive flood-control pool; "
           "the top of the joint-use pool is 1,219.61 ft (FEIS Appendix B). The stored full pool is 1,229.0 ft with the label "
           "'high-water line'.\n\n## Rejected\n\n"
           + ("\n".join(f"- `{r['lake_id']}`: {r['reason']}" for r in rejected["rise"]) or "None.") + "\n")
    (DOCS / "rise.md").write_text(txt)
    # CDEC
    rows = []
    for e in evidence["cdec"]:
        l = by[e["lake_id"]]
        rows.append(f"| `{l['id']}` | {e['station']} | {e['last_ft']} ft (DATE TIME {e['date_time']}, OBS DATE {e['obs_date']}) | "
                    f"{e.get('capacity_af') or '—'} af | {fp(l)} | {l['valid_min_ft']}–{l['valid_max_ft']} |")
    txt = ("# CDEC mapping evidence\n\n" + hdr +
           f"Readings from one call: {evidence['cdec'][0]['data_url'] if evidence['cdec'] else ''}\n\n"
           "Reading date = `DATE TIME` (the day the value belongs to); `OBS DATE` is often the next day. Capacity from "
           "the CDEC reservoir profile page (`profile?s=<ID>&type=res`); percent full = sensor 15 ÷ that capacity. "
           "Full-pool elevation only where a usbr.gov dam page states it; otherwise null. Validation range from the "
           "station's daily sensor-6 history since 1990 (0.1th/99.9th percentile ± 10 ft).\n\n"
           "| Lake id | Station | Last value | Capacity | Full pool | Valid range ft |\n|---|---|---|---|---|---|\n"
           + "\n".join(rows) + "\n\n## Rejected\n\n"
           + ("\n".join(f"- `{r['lake_id']}` ({r['station']}): {r['reason']}" for r in rejected["cdec"]) or "None.") + "\n")
    (DOCS / "cdec.md").write_text(txt)
    # USGS
    rows = [f"| `{e['lake_id']}` | {e['site']} | {e['param']} | {e['usgs_value_ft']} ft at {e['usgs_time']} | "
            f"{e['twdb_value_ft']} ft ({e['twdb_date']}) | {e['diff_ft']} | {e['distance_km']} |" for e in evidence["usgs"]]
    txt = ("# USGS mapping evidence\n\n" + hdr +
           "USGS OGC `latest-continuous` (parameters 62614, 62615, 00062; Texas) is used only as the **intraday** layer "
           "on TWDB lakes. A gauge is attached when it is a `USGS-` site within 5 km of TWDB's gauge location, its value "
           "is under 48 h old, and it reads within 0.10 ft of TWDB's latest daily value (which proves the same datum; "
           "the parameter code is stored because 62614/62615 differ by datum). LCRA-run sites (`TX071-…`) matched on "
           "parameter 00062 but are not attached (different operator; to be decided).\n\n"
           "No USGS gauge was used as a primary: no lake-elevation gauges were returned for Lake of the Ozarks, Smith "
           "Mountain Lake or Truman Lake, and no full-pool value in a gauge datum with a source URL was found for the "
           "others tried.\n\n"
           "| Lake id | Site | Param | USGS value | TWDB value | Diff ft | Distance km |\n|---|---|---|---|---|---|---|\n"
           + "\n".join(rows) + "\n")
    (DOCS / "usgs.md").write_text(txt)
    prof = [l for l in lakes if l["page"] == "profile"]
    txt = ("# Profile pages (no proven feed)\n\n" + hdr +
           "Profiles are `page: profile`, `mapping_status: none`, `published: false` (noindex). `official_url` and "
           "`operator` are set only where this run fetched the page with HTTP 200 and found the lake name on it.\n\n"
           "| Lake id | Why no feed | official_url |\n|---|---|---|\n"
           + "\n".join(f"| `{l['id']}` | {l.get('mapping_note') or ''} | {l.get('official_url') or 'null'} |" for l in prof) + "\n")
    (DOCS / "profiles.md").write_text(txt)


# ----------------------------------------------------------------------------- main

def main():
    global CACHE, OFFLINE, REFRESH
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(os.environ.get("TMPDIR", "/tmp"), "llf-map-cache"))
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--only", default="", help="comma list of sources to (re)map; others are kept from lakes.json")
    a = ap.parse_args()
    CACHE, OFFLINE, REFRESH = Path(a.cache), a.offline, a.refresh
    CACHE.mkdir(parents=True, exist_ok=True)
    evidence = {k: [] for k in ["twdb", "cwms", "rise", "cdec", "usgs", "profiles"]}
    rejected = {k: [] for k in ["twdb", "cwms", "rise", "cdec"]}
    print("TWDB ...", flush=True)
    tl, rejected["twdb"], tdata = twdb_lakes(evidence)
    print(f"  {len(tl)} accepted, {len(rejected['twdb'])} rejected", flush=True)
    print("USGS intraday ...", flush=True)
    try:
        usgs_intraday(tl, tdata, evidence)
    except RuntimeError as e:
        print("  USGS failed:", e)
    print(f"  {len(evidence['usgs'])} intraday gauges", flush=True)
    print("CWMS ...", flush=True)
    cl, rejected["cwms"] = cwms_lakes(evidence)
    print(f"  {len(cl)} accepted, {len(rejected['cwms'])} rejected", flush=True)
    print("RISE ...", flush=True)
    rl, rejected["rise"] = rise_lakes(evidence)
    print(f"  {len(rl)} accepted, {len(rejected['rise'])} rejected", flush=True)
    print("CDEC ...", flush=True)
    dl, rejected["cdec"] = cdec_lakes(evidence)
    print(f"  {len(dl)} accepted, {len(rejected['cdec'])} rejected", flush=True)
    live = tl + cl + rl + dl
    ids = [l["id"] for l in live]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise SystemExit(f"duplicate lake ids: {dup}")
    prof = profiles(evidence, set(ids))
    lakes = live + prof
    old = {}
    p = REF / "lakes.json"
    if p.exists():
        try:
            old = {l["id"]: l for l in json.loads(p.read_text())}
        except Exception:
            old = {}
    for l in lakes:   # keep demand from a previous tools/demand.py run
        if l["id"] in old:
            l["demand"] = old[l["id"]].get("demand", 0) or 0
    REF.mkdir(parents=True, exist_ok=True)
    lakes.sort(key=lambda l: (l["page"] != "live", l["id"]))
    p.write_text(json.dumps(lakes, indent=1, ensure_ascii=False) + "\n")
    (REF / "sources.json").write_text(json.dumps(SOURCES, indent=1) + "\n")
    used_ops = {l["operator"] for l in lakes if l.get("operator")} | {"lcra", "city-of-corpus-christi", "usbr"}
    ops = {k: dict(slug=k, **v) for k, v in OPERATORS.items() if k in used_ops}
    (REF / "operators.json").write_text(json.dumps(ops, indent=1, ensure_ascii=False) + "\n")
    st = {c: {"code": c, "name": n, "slug": slugify(n), "neighbours": nb, "timezones": tz} for c, n, nb, tz in STATES}
    (REF / "states.json").write_text(json.dumps(st, indent=1) + "\n")
    (REF / "systems.json").write_text(json.dumps(systems(lakes), indent=1) + "\n")
    write_docs(evidence, rejected, lakes)
    n_full = sum(1 for l in live if l["full_pool_ft"] is not None)
    n_pct = sum(1 for l in live if l["full_pool_ft"] is None and l.get("capacity_af"))
    print(f"live {len(live)} (full pool {n_full}, % from capacity only {n_pct}), profiles {len(prof)}")


if __name__ == "__main__":
    main()
