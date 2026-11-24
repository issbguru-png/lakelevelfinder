"""Static site builder for Lake Level Finder: data/ + src/ -> dist/ (Python 3 standard library only).

  python3 build.py                  build every published page
  python3 build.py --check          also fail on broken links, orphans, missing source/<time>/datum/disclaimer,
                                    bad band or trend labels, forbidden words, headline numbers that differ from
                                    data/current.json, and live pages for lakes that are not verified
  python3 build.py --batch 1        staged release: only lakes whose "batch" (default 1) is <= 1 (or PUBLISH_BATCH=1)
  python3 build.py --state TX       only lakes in one state (plus home, lists and trust pages)
  python3 build.py --data DIR --content DIR --dist DIR    alternative inputs/output (tests, sample data)
Env: LLF_NOW (fixed clock, ISO time), LLF_DATA, LLF_CONTENT, LLF_DIST, PUBLISH_BATCH,
     LLF_PIPELINE_STUB (directory holding a stand-in `pipeline` package; used only when pipeline/effective.py is absent).
Rules: plan §4.2 (vocabulary), §6 (URLs, linking, sitemaps, indexing), §7 (templates), §8.4 step 12 (checks).
"""
import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import shutil
import statistics
import sys
import zlib
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if not (ROOT / "pipeline" / "effective.py").exists() and os.environ.get("LLF_PIPELINE_STUB"):
    sys.path.insert(0, os.environ["LLF_PIPELINE_STUB"])
sys.path.insert(1, str(ROOT))

import charts  # noqa: E402
from pipeline import derive  # noqa: E402
from pipeline.effective import effective  # noqa: E402
try:
    from pipeline import stats as pstats  # noqa: E402
except ImportError:      # a stand-in pipeline without stats.py: pages fall back to what data/stats holds
    pstats = None

E = html.escape
MINUS = "−"
SITE = json.loads((SRC / "site.json").read_text(encoding="utf-8"))
BRAND = SITE["name"]
SITE_URL = "https://" + SITE["domain"]

BAND_LABEL = dict(derive.BAND_LABEL)
TREND_LABEL = dict(derive.TREND_LABEL)
BAND_FILL = {"above_full": 13, "full": 12, "near_full": 10, "below_full": 7.5, "low": 4.5, "very_low": 1.8, "level_only": 0}
TREND_ICON = {"rising_fast": "⇈", "rising": "↑", "steady": "→", "falling": "↓", "falling_fast": "⇊"}
STATE_RESERVOIR_WORD = {"CA", "UT", "ID", "CO"}  # plan §7.1: these hubs say "reservoir levels"
SHORT_SOURCE = {"twdb": "TWDB", "rise": "USBR", "cdec": "CDEC", "cwms": "USACE", "usgs": "USGS"}
DISCLAIMER_RE = re.compile(r"Do not use this page for navigation, flood or safety decisions")
LIST_TOP = 50
NEARBY_MAX_MI = 200   # 'nearby lakes' never lists a lake farther than this

# words never used (plan §4.2 D). Text inside <script>/<style> and tags is ignored.
FORBIDDEN_ALL = [r"\bsafe\b", r"\bsafe to (boat|swim)",
                 r"\b(lake level finder|this site|this page|we)\s+(is|are)\s+(an?\s+|the\s+)?official\b",
                 r"\ball[- ]time (high|low|record)"]
FORBIDDEN_GENERATED = [r"\bopen\b", r"\blive\b", r"real[- ]?time"]   # pages made from templates (not content fragments)
NEGATIONS = re.compile(r"\bnot (a |an )?(live|real[- ]?time)\b", re.I)


# ---------------------------------------------------------------- small helpers


def alias_text(aliases):
    """'Also called X, Y. Formed by Z Dam.' -- dam names are not lake names."""
    names = [a for a in aliases if not a.rstrip().endswith("Dam")]
    dams = [a for a in aliases if a.rstrip().endswith("Dam")]
    parts = []
    if names:
        parts.append(f"Also called {', '.join(names)}.")
    if dams:
        parts.append(f"Formed by {' and '.join(dams)}.")
    return " ".join(parts)

def _h(x):
    """Hours as a compact string (48.0 -> 48)."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return x
    return str(int(f)) if f.is_integer() else str(f)

def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def parse_ts(s):
    if not s:
        return None
    if isinstance(s, dt.datetime):
        return s
    s = str(s)
    if len(s) == 10:
        return dt.datetime.fromisoformat(s)
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso_z(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(v, d=2):
    return "–" if v is None else f"{v:,.{d}f}"


def pool_txt(v):
    if v is None:
        return "–"
    return f"{v:,.1f}" if abs(v * 10 - round(v * 10)) < 1e-6 else f"{v:,.2f}"


def signed(v, d=2):
    if v is None:
        return "–"
    if round(v, d) == 0:
        return f"{0:.{d}f}"
    return ("+" if v > 0 else MINUS) + f"{abs(v):,.{d}f}"


def pct_txt(p):
    if p is None:
        return "–"
    return f"{p:.0f}%" if abs(p - round(p)) < 0.05 else f"{p:.1f}%"


def acre_feet(v):
    return "–" if v is None else f"{int(round(v)):,}"


def d_short(d):
    return f"{d:%b} {d.day}"


def d_med(d):
    return f"{d:%b} {d.day}, {d.year}"


def d_wd(d):
    return f"{d:%a}, {d:%b} {d.day}, {d.year}"


def d_long(d):
    return f"{d:%A}, {d:%B} {d.day}, {d.year}"


def as_date(s):
    return dt.date.fromisoformat(str(s)[:10]) if s else None


def miles(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def possessive(name):
    return name + "’s"


def load_fragment(path):
    """Content fragment: a header comment of 'key: value' lines, then body HTML (contract: Content fragments)."""
    text = Path(path).read_text(encoding="utf-8")
    meta = {}
    m = re.match(r"\s*<!--(.*?)-->\s*", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
        text = text[m.end():]
    return meta, text


def read_series(path):
    rows = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    e = float(r["elev_ft"]) if r.get("elev_ft") not in (None, "") else None
                    p = float(r["pct_full"]) if r.get("pct_full") not in (None, "") else None
                    s = float(r["storage_af"]) if r.get("storage_af") not in (None, "") else None
                    rows.append({"date": dt.date.fromisoformat(r["date"][:10]), "elev": e, "pct": p, "storage": s,
                                 "src": r.get("src") or "", "flag": r.get("flag") or ""})
                except (ValueError, KeyError):
                    continue
    except OSError:
        return []
    rows.sort(key=lambda r: r["date"])
    return rows


def og_png(w=1200, h=630):
    """Fallback Open Graph image (deep blue, water band) written with zlib only; src/static/og-default.png wins."""
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            wave = 380 + 14 * math.sin(x / 70)
            row += bytes((12, 74, 110)) if y < wave else bytes((56, 140, 190)) if y < wave + 26 else bytes((14, 116, 168))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(t, d):
        return len(d).to_bytes(4, "big") + t + d + zlib.crc32(t + d).to_bytes(4, "big")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes((8, 2, 0, 0, 0)))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- the site
class Site:
    def __init__(self, now, data_dir, content_dir, dist, batch=None, state=None):
        self.now, self.data, self.content, self.dist = now, Path(data_dir), Path(content_dir), Path(dist)
        self.batch, self.state_filter = batch, (state or "").upper() or None
        ref = self.data / "ref"
        self.all_lakes = load_json(ref / "lakes.json", []) or []
        self.sources = load_json(ref / "sources.json", {}) or {}
        self.states = load_json(ref / "states.json", {}) or {}
        self.systems = load_json(ref / "systems.json", {}) or {}
        self.search_names = (load_json(ref / "search_names.json", {}) or {}).get("lakes", {})
        self.operators = load_json(ref / "operators.json", {}) or {}
        self.demand = load_json(ref / "demand.json", {}) or {}
        self.current = load_json(self.data / "current.json", {}) or {}
        self.status = load_json(self.data / "status.json", None)
        self.errors = []           # build-time rule violations, reported by --check
        self.pages = {}            # path -> {"lastmod", "group", "title", "index": bool, "kind"}
        self.kind = {}             # path -> page kind (lake, history, profile, hub, list, content, home, ...)
        self.lake_by_id = {lk["id"]: lk for lk in self.all_lakes}
        self.plan()

    # ------------------------------------------------------------ what gets built
    def in_scope(self, lk):
        if self.batch is not None and (lk.get("batch") or 1) > self.batch:
            return False
        if self.state_filter and self.state_filter not in (lk.get("states") or [lk.get("state")]):
            return False
        return lk.get("published", True) is not False

    def plan(self):
        self.live, self.profiles = [], []
        for lk in self.all_lakes:
            page = lk.get("page")
            if page == "live":
                if lk.get("mapping_status") != "verified":
                    self.errors.append(f"live page requested for {lk['id']} but mapping_status is "
                                       f"{lk.get('mapping_status')!r}: not built")
                    continue
                if self.in_scope(lk):
                    self.live.append(lk)
            elif page == "profile" and self.in_scope(dict(lk, published=True)):  # profiles are noindex; "published" governs indexing only
                self.profiles.append(lk)
        self.live_ids = {lk["id"] for lk in self.live}
        self.eff, self.series, self.stats, self.notes, self._tail = {}, {}, {}, {}, {}
        for lk in self.live:
            src = self.src_of(lk)
            r = self.current.get(lk["id"])
            self.eff[lk["id"]] = self.effective(r, lk, src)
            self.series[lk["id"]] = read_series(self.data / "series" / f"{lk['id']}.csv")
            self.stats[lk["id"]] = load_json(self.data / "stats" / f"{lk['id']}.json", None) or self.stats_from_series(lk["id"])
        for lk in self.live + self.profiles:
            self.notes[lk["id"]] = load_json(self.data / "notes" / f"{lk['id']}.json", []) or []
        # paths that will exist
        self.planned = {"/", "/lowest-lakes/", "/fullest-lakes/", "/rising-fastest/", "/falling-fastest/"}
        for lk in self.live + self.profiles:
            self.planned.add(self.lake_url(lk))
        self.history_ids = {lk["id"] for lk in self.live if self.history_ok(lk)}
        self.planned |= {self.lake_url(self.lake_by_id[i]) + "history/" for i in self.history_ids}
        self.hub_lakes = defaultdict(list)       # state code -> built lakes (live + profile)
        for lk in self.live + self.profiles:
            for s in lk.get("states") or [lk.get("state")]:
                if s in self.states and (not self.state_filter or s == self.state_filter):
                    self.hub_lakes[s].append(lk)
        for s in self.hub_lakes:
            self.planned.add(f"/{self.states[s]['slug']}/")
        self.sys_built = {k: v for k, v in self.systems.items() if any(m in self.live_ids for m in v.get("members", []))}
        self.planned |= {f"/system/{k}/" for k in self.sys_built}
        self.ops_built = sorted({lk.get("operator") for lk in self.live + self.profiles
                                 if lk.get("operator") in self.operators})
        self.planned |= {f"/operator/{k}/" for k in self.ops_built}
        self.content_pages = []
        for sub, prefix in (("pages", "/"), ("guides", "/guides/")):
            d = self.content / sub
            if d.is_dir():
                for p in sorted(d.glob("*.html")):
                    path = f"{prefix}{p.stem}/"
                    self.content_pages.append((path, p, sub))
                    self.planned.add(path)
        if any(sub == "guides" for _, _, sub in self.content_pages):
            self.planned.add("/guides/")
        self.live_states = sorted({s for lk in self.live for s in (lk.get("states") or [lk.get("state")])})

    def stats_from_series(self, lid):
        """Stand-in for a missing data/stats/{id}.json: the same pipeline.stats.compute(), run on the stored series."""
        rows = [{"date": r["date"], "elev_ft": r["elev"], "pct_full": r["pct"], "storage_af": r["storage"], "flag": r["flag"]}
                for r in self.series.get(lid) or [] if r["elev"] is not None]
        if not rows or pstats is None:
            return None
        return pstats.compute(rows)

    def history_ok(self, lk):
        ser = self.series.get(lk["id"]) or []
        vals = [r for r in ser if r["elev"] is not None]
        return len(vals) >= 365 and (vals[-1]["date"] - vals[0]["date"]).days >= 365

    def src_of(self, lk):
        sid = (lk.get("primary") or {}).get("source")
        return self.sources.get(sid) or {"id": sid or "unknown", "agency": sid or "the source", "soft_stale_hours": 48,
                                         "hard_stale_hours": 96, "value_kind": "daily_average"}

    def effective(self, r, lk, src):
        if not r:
            return {"freshness": "unknown", "show_badges": False, "elev_ft": None, "reading_at": None, "age_hours": None,
                    "soft_stale_hours": src.get("soft_stale_hours", 48), "hard_stale_hours": src.get("hard_stale_hours", 96)}
        e = effective(r, lk, src, self.now)
        e.setdefault("soft_stale_hours", src.get("soft_stale_hours", 48))
        e.setdefault("hard_stale_hours", src.get("hard_stale_hours", 96))
        if e.get("freshness") not in ("current", "delayed", "unknown"):
            self.errors.append(f"bad freshness {e.get('freshness')!r} for {lk['id']}")
            e["freshness"] = "unknown"
        e["show_badges"] = e.get("show_badges", e["freshness"] != "unknown") and e["freshness"] != "unknown"
        return e

    # ------------------------------------------------------------ URLs and links
    @staticmethod
    def lake_url(lk):
        return f"/lake/{lk['id']}/"

    def state_url(self, code):
        st = self.states.get(code)
        return f"/{st['slug']}/" if st else None

    def a(self, path, text, cls=None, raw=False):
        """Internal link, or plain text when the target is not built (held batch, other state)."""
        t = text if raw else E(text)
        if path and path.split("#")[0] in self.planned:
            return f'<a href="{path}"' + (f' class="{cls}"' if cls else "") + f">{t}</a>"
        return f"<span>{t}</span>"

    @staticmethod
    def ext(url, text, cls="ext"):
        return f'<a class="{cls}" href="{E(url)}" rel="noopener">{E(text)}<span class="ext__i" aria-hidden="true">↗</span></a>'

    # ------------------------------------------------------------ reading view (one source of every number)
    def view(self, lk):
        e = self.eff.get(lk["id"]) or {}
        src = self.src_of(lk)
        tz = ZoneInfo(lk.get("timezone") or "America/Chicago")
        pool = e.get("full_pool_ft") if e.get("full_pool_ft") is not None else lk.get("full_pool_ft")
        elev = e.get("elev_ft")
        vs = e.get("vs_full_pool_ft")
        if vs is None and elev is not None and pool is not None:
            vs = round(elev - pool, 2)
        pct = e.get("pct_full")
        band = e.get("band")
        if band is None and elev is not None:
            band = derive.band(elev, pool, pct, lk.get("flood_control"), lk.get("bands"))
        c24, c7, c30 = e.get("change_24h_ft"), e.get("change_7d_ft"), e.get("change_30d_ft")
        kind = e.get("reading_kind") or src.get("value_kind") or "daily_average"
        daily = str(kind).startswith("daily")
        ra = e.get("reading_at")
        filled = False
        if daily and ra and len(ra) == 10 and elev is not None and (c24 is None or c7 is None or c30 is None):
            # a daily reading that came without its changes: take them from the stored series (the same rows the
            # 14-day table prints), and only when the series row for the reading's day holds the reading itself
            day = as_date(ra)
            if c24 is None:
                c24 = self.series_change(lk["id"], day, elev, 1)
                filled = c24 is not None
            c7 = self.series_change(lk["id"], day, elev, 7) if c7 is None else c7
            c30 = self.series_change(lk["id"], day, elev, 30) if c30 is None else c30
        trend = e.get("trend") if "trend" in e else derive.trend(c24)
        if trend is None and c24 is not None and ("trend" not in e or filled):
            trend = derive.trend(c24)
        short = src.get("short") or SHORT_SOURCE.get(src.get("id"), src.get("agency", ""))
        v = {"lk": lk, "e": e, "src": src, "short": short, "elev": elev, "pool": pool, "vs": vs, "pct": pct,
             "band": band, "trend": trend, "c24": c24, "c7": c7, "c30": c30,
             "fresh": e.get("freshness", "unknown"), "badges": e.get("show_badges", False), "daily": daily,
             "kind": kind, "tz": tz, "provisional": e.get("provisional", src.get("provisional"))}
        if ra:
            if len(ra) == 10:
                d = as_date(ra)
                v["rdate"] = d
                end = dt.datetime(d.year, d.month, d.day, tzinfo=tz) + dt.timedelta(days=1)
                v["ts"] = iso_z(end)            # age is measured from the end of the day the value covers (§7.6)
                v["time_attr"] = ra
                v["when_short"] = d_short(d)
                v["when_wd"] = d_wd(d)
                v["when_long"] = d_long(d)
            else:
                t = parse_ts(ra).astimezone(tz)
                v["rdate"] = t.date()
                v["ts"] = iso_z(t)
                v["time_attr"] = iso_z(t)
                hm = t.strftime("%I:%M %p").lstrip("0")
                v["when_short"] = d_short(t)
                v["when_wd"] = f"{hm} {t.tzname()}, {d_wd(t)}"
                v["when_long"] = f"{hm} {t.tzname()} on {d_long(t)}"
        sk = src.get("value_kind") or kind
        if not ra:
            v["asof"] = ""
        elif sk == "daily_average":
            v["asof"] = f'Daily average for <time datetime="{v["time_attr"]}">{E(v["when_wd"])}</time>, published by {E(short)}'
        elif daily:
            v["asof"] = (f'Reading for <time datetime="{v["time_attr"]}">{E(v["when_wd"])}</time> ({E(short)}'
                         + (", provisional" if v["provisional"] else "") + ")")
        else:
            sid = (lk.get("primary") or {}).get("series_id") or ""
            site = f" {sid}" if src.get("id") == "usgs" and sid else ""
            v["asof"] = (f'<time datetime="{v["time_attr"]}">{E(v["when_wd"])}</time> ({E(short)}{E(site)}'
                         + (", provisional" if v["provisional"] else "") + ")")
        v["src_url"] = lk.get("source_url") or (lk.get("primary") or {}).get("url") or src.get("public_url") or "/sources/"
        credit = src.get("credit") or src.get("agency") or short
        v["src_text"] = credit.replace(src.get("agency", "\0"), short).replace(", ", " ") if src.get("agency") else credit
        if v["fresh"] == "unknown":
            v["band"] = v["band"] if v["band"] else None
        if v["band"] and v["band"] not in BAND_LABEL:
            self.errors.append(f"band {v['band']!r} outside the allowed set on {lk['id']}")
        if v["trend"] and v["trend"] not in TREND_LABEL:
            self.errors.append(f"trend {v['trend']!r} outside the allowed set on {lk['id']}")
        return v

    def series_change(self, lid, day, elev, days):
        """elev(day) - elev(day - days) from the stored daily series; None unless both days are stored and the row for
        `day` is the reading itself."""
        tail = self._tail.get(lid)
        if tail is None:
            tail = self._tail[lid] = {r["date"]: r["elev"] for r in (self.series.get(lid) or [])[-45:] if r["elev"] is not None}
        a, b = tail.get(day), tail.get(day - dt.timedelta(days=days))
        if a is None or b is None or abs(a - elev) > 0.005:
            return None
        return round(elev - b, 2)

    def vs_words(self, v, unit="ft"):
        if v["vs"] is None or v["pool"] is None:
            return ""
        if round(v["vs"], 2) == 0:
            return "at full pool"
        return f"{num(abs(v['vs']))} {unit} {'above' if v['vs'] > 0 else 'below'} full pool"

    def badge(self, band, small=False, use=False):
        """Band badge: text plus a fill-height shape. `use=True` draws the shape from the page sprite (pages with many badges)."""
        if not band:
            return ""
        if use:
            return (f'<span class="badge badge--{band}{" badge--s" if small else ""}"><svg aria-hidden="true"><use href="#b-{band}"/></svg>'
                    f'{E(BAND_LABEL[band])}</span>')
        h = BAND_FILL.get(band, 0)
        icon = (f'<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="1.5" width="11" height="13" rx="2.5" '
                f'fill="none" stroke="currentColor" stroke-width="1.4"/>'
                + (f'<rect x="4.2" y="{14.2 - h:.1f}" width="7.6" height="{h - 1.2 if h > 1.2 else h:.1f}" rx="1" fill="currentColor"/>'
                   if h else '<path d="M5 5h6M5 8h4M5 11h6" stroke="currentColor" stroke-width="1.3"/>')
                + "</svg>")
        return f'<span class="badge badge--{band}{" badge--s" if small else ""}">{icon}{E(BAND_LABEL[band])}</span>'

    def trend_badge(self, v, with_change=True):
        t = v["trend"]
        if not t:
            return ""
        ch = ""
        if with_change and v["c24"] is not None:
            ch = f' <span class="trend__ch">{signed(v["c24"])} ft {"since the day before" if v["daily"] else "in 24 h"}</span>'
        return (f'<span class="trend trend--{t}"><span class="trend__lab"><span aria-hidden="true">{TREND_ICON[t]}</span> '
                f'{E(TREND_LABEL[t])}</span>{ch}</span>')

    def sort_key_pct(self, v):
        return v["pct"] if v["pct"] is not None else 999

    # ------------------------------------------------------------ page writer
    def write(self, path, title, description, body, *, crumbs=None, lastmod=None, group="pages", noindex=False,
              graph=None, og_title=None, kind="page", body_class="", sitemap=True, css=None, about_id=None):
        canonical = SITE_URL + path
        g = [{"@type": "Organization", "@id": SITE_URL + "/#org", "name": BRAND, "url": SITE_URL + "/",
              "logo": {"@type": "ImageObject", "url": SITE_URL + "/logo.png", "width": 512, "height": 512},
              "email": SITE.get("contact_email")},
             {"@type": "WebSite", "@id": SITE_URL + "/#website", "name": BRAND,
              "alternateName": ["LakeLevelFinder", SITE["domain"]], "url": SITE_URL + "/",
              "publisher": {"@id": SITE_URL + "/#org"}}]
        wp = {"@type": "WebPage", "@id": canonical, "url": canonical,
              "name": re.sub(r"\s*\|\s*" + re.escape(BRAND) + r"$", "", og_title or title), "inLanguage": "en-US",
              "isPartOf": {"@id": SITE_URL + "/#website"}}
        if crumbs:
            g.append({"@type": "BreadcrumbList", "@id": canonical + "#breadcrumb", "itemListElement": [
                {"@type": "ListItem", "position": i + 1, "name": n, "item": SITE_URL + u} for i, (n, u) in enumerate(crumbs)]})
            wp["breadcrumb"] = {"@id": canonical + "#breadcrumb"}
        if about_id:
            wp["about"] = {"@id": about_id}
        if lastmod:
            wp["dateModified"] = lastmod
        g.append(wp)
        g += graph or []
        jsonld = ('<script type="application/ld+json">'
                  + json.dumps({"@context": "https://schema.org", "@graph": g}, separators=(",", ":"), ensure_ascii=False)
                  .replace("</", "<\\/") + "</script>\n")
        analytics = ""
        if SITE.get("ga4_id"):
            analytics = (SRC / "templates" / "analytics.html").read_text().replace("{{ga4_id}}", SITE["ga4_id"]).replace(
                "{{domain}}", SITE["domain"])
        repl = {"title": E(title), "description": E(description), "site_name": E(BRAND),
                "canonical": "" if noindex else f'<link rel="canonical" href="{canonical}">\n', "og_url": canonical,
                "og_title": E(og_title or title), "site_url": SITE_URL, "css_href": self.css, "js_href": self.js,
                "page_css": f'<link rel="stylesheet" href="{self.page_css[css]}">\n' if css else "",
                "jsonld": jsonld, "analytics": analytics, "body": body, "body_class": body_class,
                "robots": '<meta name="robots" content="noindex,follow">\n' if noindex else "",
                "footer": self.footer_html, "tagline": E(SITE.get("tagline", "")), "year": str(self.now.year)}
        out = self.base
        for k, val in repl.items():
            out = out.replace("{{" + k + "}}", val)
        left = re.findall(r"\{\{[a-z_.]+\}\}", out.split("<script", 1)[0])
        if left:
            raise SystemExit(f"unfilled placeholder in {path}: {left[:3]}")
        if path != "/":
            out = out.replace(f'<a href="{path}">', f'<a href="{path}" aria-current="page">', 1)
        dest = self.dist / path.lstrip("/") / "index.html" if path.endswith("/") else self.dist / path.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(out, encoding="utf-8")
        self.kind[path] = kind
        if sitemap and not noindex:
            self.pages[path] = {"lastmod": lastmod, "group": group, "title": title}

    def state_indexed(self, code):
        """A state hub is indexed (and linked site-wide) once it has 3 or more lakes with a data feed (state_page rule)."""
        return sum(1 for lk in self.hub_lakes.get(code) or [] if lk["id"] in self.live_ids) >= 3

    def state_crumb(self, code, st):
        """Breadcrumb for a lake's state: "Texas lake levels" when the state hub exists, else the plain name to the home page."""
        u = self.state_url(code)
        return (f"{st['name']} lake levels", u) if (u and self.state_indexed(code)) else None

    def crumbs_html(self, crumbs):
        parts = [self.a(u, n) for n, u in crumbs[:-1]] + [f'<span aria-current="page">{E(crumbs[-1][0])}</span>']
        return '<nav class="crumbs" aria-label="Breadcrumb">' + '<span aria-hidden="true">›</span>'.join(parts) + "</nav>"

    def asset(self, name, body):
        if name.endswith(".css"):   # light minify: comments and whitespace only
            body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
            body = re.sub(r"\s*\n\s*", "", body)
            body = re.sub(r"\s*([{};,>])\s*", r"\1", body)
            body = re.sub(r"rgba\((\d+),(\d+),(\d+),(\.\d+)\)", lambda m: "#%02x%02x%02x%02x" % (
                int(m[1]), int(m[2]), int(m[3]), round(float(m[4]) * 255)), body)      # same colour, shorter
            body = body.replace(";}", "}").replace("@media (", "@media(")
        h = hashlib.sha1(body.encode()).hexdigest()[:10]
        stem, ext = name.rsplit(".", 1)
        fn = f"{stem}.{h}.{ext}"
        (self.dist / "assets").mkdir(parents=True, exist_ok=True)
        (self.dist / "assets" / fn).write_text(body, encoding="utf-8")
        return f"/assets/{fn}"

    def fill(self, text):
        repl = {"{{site.name}}": BRAND, "{{site.domain}}": SITE["domain"],
                "{{site.contact_email}}": SITE.get("contact_email", ""),
                "{{site.corrections_email}}": SITE.get("corrections_email", SITE.get("contact_email", "")),
                "{{count.live_lakes}}": f"{len(self.live):,}", "{{count.states}}": str(len(self.live_states))}
        for k, val in repl.items():
            text = text.replace(k, val)
        return text

    def fragment(self, sub, name):
        p = self.content / sub / f"{name}.html"
        if not p.exists():
            return ""
        return self.fill(load_fragment(p)[1]).strip()

    def faq_html(self, qa, graph):
        if not qa:
            return ""
        items = "".join(f'<details class="qa"{" open" if i < 2 else ""}><summary><h3>{E(q)}</h3></summary><p>{a}</p></details>'
                        for i, (q, a) in enumerate(qa))
        graph.append({"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": re.sub(r"<[^>]+>", "", html.unescape(a))}}
            for q, a in qa]})
        return items

    # ------------------------------------------------------------ shared blocks
    def lake_row_cells(self, v, spark=False):
        lk = v["lk"]
        if v["fresh"] == "unknown" or v["elev"] is None:
            lvl = '<td data-v="-1" colspan="1">Unknown</td>'
        else:
            lvl = f'<td data-v="{v["elev"]}" class="num">{num(v["elev"])}</td>'
        vs = (f'<td data-v="{v["vs"]}" class="num">{signed(v["vs"])}</td>' if v["vs"] is not None else '<td data-v="-9999">–</td>')
        pct = (f'<td data-v="{v["pct"]}" class="num">{pct_txt(v["pct"])}</td>' if v["pct"] is not None else '<td data-v="-1">–</td>')
        band = f'<td data-v="{list(BAND_LABEL).index(v["band"]) if v["band"] in BAND_LABEL else 9}">{self.badge(v["band"], True) if v["badges"] else "Unknown"}</td>'
        c24 = (f'<td data-v="{v["c24"]}" class="num">{signed(v["c24"])}</td>' if v["badges"] and v["c24"] is not None
               else '<td data-v="-9999">–</td>')
        when = f'<td data-v="{v.get("ts", "")}"><time datetime="{v.get("time_attr", "")}">{E(v.get("when_short", "–"))}</time></td>' if v.get("ts") else "<td>–</td>"
        sp = ""
        if spark:
            pts = self.last_days(lk["id"], 30)
            sp = f'<td class="sp">{charts.sparkline(pts, full_pool=v["pool"], height=24)}</td>'
        return lvl + vs + pct + band + c24 + when + sp

    def lakes_table(self, lakes, tid, caption, spark=True, source_col=True, state_col=False):
        rows = []
        for lk in lakes:
            v = self.view(lk)
            name = self.a(self.lake_url(lk), lk["name"])
            extra = f'<td>{E(", ".join(lk.get("states") or [lk.get("state")]))}</td>' if state_col else ""
            src = f'<td>{E(v["short"])}</td>' if source_col else ""
            rows.append(f'<tr data-reading-ts="{v.get("ts", "")}" data-hard-h="{_h(v["e"].get("hard_stale_hours", 96))}">'
                        f'<th scope="row" data-v="{E(lk["name"])}">{name}</th>{extra}{self.lake_row_cells(v, spark)}{src}</tr>')
        head = ('<th scope="col"><button type="button">Lake</button></th>'
                + ('<th scope="col"><button type="button">State</button></th>' if state_col else "")
                + '<th scope="col" class="num"><button type="button">Level (ft)</button></th>'
                '<th scope="col" class="num"><button type="button">vs full pool</button></th>'
                '<th scope="col" class="num"><button type="button">% full</button></th>'
                '<th scope="col"><button type="button">Status</button></th>'
                '<th scope="col" class="num"><button type="button">24 h (ft)</button></th>'
                '<th scope="col"><button type="button">Reading</button></th>'
                + ('<th scope="col" class="sp">30 days</th>' if spark else "")
                + ('<th scope="col"><button type="button">Source</button></th>' if source_col else ""))
        return (f'<div class="tw"><table class="lt" id="{tid}" data-sort><caption class="vh">{E(caption)}</caption>'
                f'<thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')

    def last_days(self, lid, days):
        ser = self.series.get(lid) or []
        if not ser:
            return []
        end = ser[-1]["date"]
        start = end - dt.timedelta(days=days)
        return [(r["date"], r["elev"]) for r in ser if r["date"] >= start and r["elev"] is not None]

    def ranked(self, lakes, how):
        """Plan §4.4: live-data lakes with a Current reading only."""
        vs = [self.view(lk) for lk in lakes]
        vs = [v for v in vs if v["fresh"] == "current" and v["elev"] is not None]
        if how == "lowest":
            vs = sorted((v for v in vs if v["pct"] is not None), key=lambda v: (v["pct"], v["lk"]["name"]))
        elif how == "fullest":
            vs = sorted((v for v in vs if v["pct"] is not None), key=lambda v: (-v["pct"], -(v["vs"] or 0), v["lk"]["name"]))
        elif how == "rising":
            vs = sorted((v for v in vs if v["c24"] is not None and v["c24"] > 0), key=lambda v: (-v["c24"], v["lk"]["name"]))
        else:
            vs = sorted((v for v in vs if v["c24"] is not None and v["c24"] < 0), key=lambda v: (v["c24"], v["lk"]["name"]))
        return vs

    def mini_list(self, vs, how, n=5):
        if not vs:
            return '<p class="muted">No lake has a current reading for this list right now.</p>'
        out = []
        for v in vs[:n]:
            lk = v["lk"]
            val = pct_txt(v["pct"]) + " full" if how in ("lowest", "fullest") else f'{signed(v["c24"])} ft'
            out.append(f'<li>{self.a(self.lake_url(lk), lk["name"])} <span class="ml__v">{E(val)}</span>'
                       f'<span class="ml__d">{E(v.get("when_short", ""))}</span></li>')
        return '<ol class="ml">' + "".join(out) + "</ol>"

    def lastmod_of(self, lakes):
        ts = [self.eff[lk["id"]].get("content_modified") for lk in lakes if self.eff.get(lk["id"], {}).get("content_modified")]
        return max(ts) if ts else None

    # ------------------------------------------------------------ lake page
    def sec(self, sid, icon, title, sub, inner, cls=""):
        """A page section with the shared icon header (title is HTML, already escaped)."""
        return (f'<section{(" id=" + chr(34) + sid + chr(34)) if sid else ""} class="sec{(" " + cls) if cls else ""}">'
                f'{self.sec_head(icon, title, sub)}{inner}</section>')

    def with_sprite(self, body):
        """Fill {{sprite}} with the icons and the band shapes this page uses."""
        bands = "".join(self.badge(b).split("<svg", 1)[1].split("</svg>")[0].replace(' viewBox="0 0 16 16" aria-hidden="true">', f'<symbol id="b-{b}" viewBox="0 0 16 16">', 1)
                        + "</symbol>" for b in BAND_LABEL if f'href="#b-{b}"' in body)
        return body.replace("{{sprite}}", self.home_sprite(set(re.findall(r'href="#i-([a-z]+)"', body))).replace("</svg>", bands + "</svg>"))

    def lake_page(self, lk):
        v = self.view(lk)
        lid, name = lk["id"], lk["name"]
        url = self.lake_url(lk)
        st = self.states.get(lk.get("state"), {"name": lk.get("state"), "slug": None})
        crumbs = [c for c in [("Home", "/"), self.state_crumb(lk.get("state"), st), (name, url)] if c]
        e, src, stats = v["e"], v["src"], self.stats.get(lid) or {}
        unknown = v["fresh"] == "unknown" or v["elev"] is None
        has_storage = v["pct"] is not None or bool(lk.get("capacity_af"))
        tname = (self.search_names.get(lid) or {}).get("title_name") or name     # the name people search (title/meta only)
        title = f"{tname} Water Level Today & % Full | {BRAND}" if has_storage else f"{tname} Water Level Today | {BRAND}"
        if len(title) > 65:
            title = f"{tname} Water Level Today | {BRAND}"
        if len(title) > 65:
            title = f"{tname} Water Level Today"
        desc = self.lake_desc(lk, v, tname, has_storage)
        ago = (lk.get("aliases") or [])
        graph = []
        # --- lead sentence (known / unknown versions; the script swaps them if the page ages past the hard limit)
        lead_unknown = self.lead_unknown(v)
        lead_known = "" if unknown else self.lead_known(v)
        # --- headline panel
        pts30 = self.last_days(lid, 30)
        panel = self.panel(v, unknown, pts30)
        stale_txt = (f'This reading may be out of date. The last value {E(v["short"])} published was for '
                     f'{E(v.get("when_wd", "an earlier date"))}.')
        strip = (f'<p class="stale" role="status"{"" if v["fresh"] == "delayed" else " hidden"}>'
                 f'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5 15 14H1z" fill="none" stroke="currentColor" stroke-width="1.5"/>'
                 f'<path d="M8 6v4M8 11.5v1" stroke="currentColor" stroke-width="1.5"/></svg>{stale_txt}</p>')
        notice = ""
        n = e.get("notice")
        if n:
            if isinstance(n, dict):
                notice = (f'<p class="notice"><strong>Notice{" (" + E(n.get("date") or "") + ")" if n.get("date") else ""}:</strong> '
                          f'{E(n.get("text") or "")}' + (f' {self.ext(n["url"], "Source")}' if n.get("url") else "") + "</p>")
            else:
                notice = f'<p class="notice">{E(str(n))}</p>'
        intraday = ""
        it = e.get("intraday")
        if it and it.get("elev_ft") is not None and not unknown:
            t = parse_ts(it.get("at")).astimezone(v["tz"])
            isrc = self.sources.get(it.get("source"), {})
            ish = isrc.get("short") or SHORT_SOURCE.get(it.get("source"), it.get("source", ""))
            intraday = (f'<p class="intraday"><strong>Latest gauge reading:</strong> {num(it["elev_ft"])} ft at '
                        f'<time datetime="{iso_z(t)}">{E(t.strftime("%I:%M %p").lstrip("0"))} {E(t.tzname())}, {E(d_wd(t))}</time> '
                        f'({E(" ".join(x for x in ((ish if not str(it.get("series_id") or "").startswith(ish) else ""), str(it.get("series_id") or "")) if x))}, provisional). The headline above is the '
                        f'{"daily average" if v["kind"] == "daily_average" else "official daily value"} from {E(v["short"])}.</p>')
        alias = (f'<p class="aka">{E(alias_text(ago))}</p>' if ago else "")
        sections = []
        toc = []

        def sec(sid, icon, h, sub, inner, toc_label=None, cls=""):
            if inner:
                sections.append(self.sec(sid, icon, h, sub, inner, cls))
                toc.append((sid, toc_label or re.sub(r"<[^>]+>", "", h)))
        # changes (stat tiles)
        if not unknown:
            sec("changes", "up", "How the level has changed",
                f'Each tile compares an earlier level with the reading {"for" if v["daily"] else "at"} {E(v.get("when_wd", ""))}.',
                self.change_tiles(v, stats))
        # charts: 30 days, 12 months with the year before, 5 years; one visible at a time (all are in the HTML)
        hist_link = ""
        if lid in self.history_ids:
            hist_link = f'<p class="more">{self.a(url + "history/", f"See the full {name} water level history", cls="more__a")}</p>'
        ch, ch_sub = self.charts_block(lk, v, pts30)
        sec("charts", "chart", f"{E(name)} water level chart", ch_sub, (ch + hist_link) if ch else "", toc_label="Charts")
        # 14-day table
        t14 = self.table_14(lid, stats, v)
        sec("last-14-days", "cal", "Last 14 days", "Levels and changes in feet. Each change is from the previous day.",
            t14 + ("" if ch or not t14 else hist_link))
        # records
        sec("records", "flag", f"{E(name)} lake level records", "Highs and lows in the values we store for this lake.",
            self.records_block(lk, v, stats))
        # what this level means
        if not unknown:
            sec("what-it-means", "info", f"What {num(v['elev'])} feet means at {E(name)}", "", self.meaning_block(lk, v),
                toc_label="What this level means")
            sec("storage", "drop", "Storage", f'Figures for {E(v.get("when_wd", ""))} ({E(v["short"])}).' if v.get("when_wd") else "",
                self.storage_block(lk, v))
        sysb = self.system_block(lk)
        if sysb:
            sec("system", "link", E(sysb[0]), "", sysb[1])
        sec("nearby", "map", "Nearby lakes", "The nearest lakes with a current reading, with straight-line distance.", self.nearby_block(lk))
        sec("about", "shield", "About this reading", "Where the number comes from, and how to check it yourself.", self.about_block(lk, v))
        qa = self.lake_faq(lk, v, stats)
        faq = self.faq_html(qa, graph)
        sec("questions", "help", "Questions", f"Short answers about the {E(name)} water level, made from the same data as the numbers above.",
            f'<div class="qs">{faq}</div>' if faq else "")
        # related links (plan §4.3 item 18)
        foot = [self.a(self.state_url(lk.get("state")) or "/", f"All {st['name']} lake levels")]
        for s2 in (lk.get("states") or [])[1:]:
            if s2 in self.states and s2 != lk.get("state"):
                foot.append(self.a(self.state_url(s2), f"{self.states[s2]['name']} lake levels"))
        op = self.operators.get(lk.get("operator"))
        if op:
            foot.append(self.a(f"/operator/{op['slug']}/", f"{op.get('short') or op['name']} lakes"))
        foot += [self.a("/lowest-lakes/", "Lowest lakes"), self.a("/fullest-lakes/", "Fullest lakes"),
                 self.a("/rising-fastest/", "Rising fastest"), self.a("/falling-fastest/", "Falling fastest"),
                 self.a("/sources/", "Data sources"), self.a("/corrections/", "Report a problem")]
        toc_html = "".join(f'<li><a href="#{i}">{E(t)}</a></li>' for i, t in toc)
        aside = (f'<aside class="rail"><nav class="toc" aria-label="On this page"><h2>On this page</h2><ol>{toc_html}</ol></nav>'
                 +
                 f'<p class="rail__h">{self.a(url + "history/", "Full history and CSV", cls="more__a")}</p></aside>' if lid in self.history_ids else "</aside>")
        jump = (f'<nav class="jump" aria-label="Jump to a section"><ul>'
                + "".join(f'<li><a href="#{i}">{E(self.JUMP.get(i, t))}</a></li>' for i, t in toc) + "</ul></nav>")
        top = (f'<div class="lk-top"><div class="lk-panel">{panel}{strip}</div>'
               f'<div class="lk-lead">{lead_known}{lead_unknown}{intraday}{notice}</div></div>')
        body = ("{{sprite}}" + self.crumbs_html(crumbs) + f'<header class="lk-head"><h1>{E(name)} water level</h1>{alias}</header>'
                + top + jump
                + f'<div class="layout"><div class="main-col">{"".join(sections)}'
                f'<nav class="xl" aria-label="Related"><h2>More lake levels</h2><p class="chips-l">{"".join(foot)}</p></nav></div>{aside}</div>')
        place = {"@type": "Reservoir" if lk.get("kind", "reservoir") == "reservoir" else "LakeBodyOfWater", "name": name}
        if ago:
            place["alternateName"] = ago
        if lk.get("lat") is not None and lk.get("lon") is not None:
            place["geo"] = {"@type": "GeoCoordinates", "latitude": lk["lat"], "longitude": lk["lon"]}
        place["containedInPlace"] = [dict({"@type": "State", "name": self.states.get(s, {}).get("name", s)},
                                          **({"url": SITE_URL + self.state_url(s)} if self.state_url(s) else {}))
                                     for s in (lk.get("states") or [lk.get("state")])]
        place["@id"] = SITE_URL + url + "#lake"
        place["url"] = SITE_URL + url
        graph.append(place)
        lm = e.get("content_modified")
        self.write(url, title, desc, self.with_sprite(body), crumbs=crumbs, lastmod=lm, group="lakes", graph=graph, kind="lake",
                   body_class='class="pg-lake"', css="lake", about_id=SITE_URL + url + "#lake")

    def lake_desc(self, lk, v, tname, has_storage):
        """Meta description without live numbers (snippets are cached): what the page shows, the agency, how often.
        The longest form that fits in 160 characters wins."""
        cadence = "updated daily" if v["daily"] else "updated hourly"
        short = v["src"].get("short") or v["short"]
        pool = f" ({pool_txt(v['pool'])} ft)" if v["pool"] is not None else ""
        what = []
        if has_storage:
            what.append("% full")
        if v["pool"] is not None:
            what.append(f"distance from full pool{pool}")
        lead = f"{tname} water level today in feet" + (", " + " and ".join(what) if what else "")
        for tail in (", with daily change and 30-day and 1-year charts. Data: {s}, {c}.",
                     ", with daily change and charts. Data: {s}, {c}.",
                     ". Daily change and charts. Data: {s}, {c}.",
                     ". Data: {s}, {c}."):
            d = lead + tail.format(s=short, c=cadence)
            if len(d) <= 160:
                return d
        return d

    JUMP = {"changes": "Changes", "last-14-days": "14 days", "records": "Records", "what-it-means": "What it means",
            "system": "System", "nearby": "Nearby", "about": "Source", "questions": "Questions"}

    def panel(self, v, unknown, pts30=None):
        """The headline panel. The number, band, trend, date and source are text in the HTML; the tank is filled to the
        real percent full (lakes without one get the 30-day line instead). site.js swaps it to "unknown" past the hard limit."""
        lk = v["lk"]
        e = v["e"]
        kick = ("Daily average level" if v["kind"] == "daily_average" else "Daily reading" if v["daily"] else "Latest gauge reading")
        elev_html = (f'<data class="elev" value="{v["elev"]:.2f}">{num(v["elev"])}</data>' if v["elev"] is not None else "")
        pool_line = ""
        if v["pool"] is not None and v["vs"] is not None:
            pool_line = f'<p class="pn__vs">{E(self.vs_words(v))} <span>({pool_txt(v["pool"])} ft)</span></p>'
        elif v["pool"] is None:
            pool_line = '<p class="pn__vs pn__vs--none">No full-pool value on record for this lake</p>'
        flood = ' <span class="badge badge--flood">In flood pool</span>' if e.get("in_flood_pool") and v["badges"] else ""
        badges = f'<div class="pn__b">{self.badge(v["band"]) if v["badges"] else ""}{flood}</div>'
        trend = f'<p class="pn__trend">{self.trend_badge(v)}</p>' if v["badges"] and v["trend"] else ""
        viz = ""
        if v["pct"] is not None:
            fill = max(2.0, min(118.0, v["pct"]))          # 100 = the full-pool line; the tank leaves room above it
            cap = lk.get("capacity_af")
            ref = (f"Full pool {pool_txt(v['pool'])} ft" if v["pool"] is not None
                   else f"Capacity {acre_feet(cap)} acre-feet" if cap else "100% of capacity")
            viz = (f'<div class="pn__viz"><div class="hg__tank{" hg--lo" if fill < 34 else " hg--hi" if fill >= 90 else ""}" style="--p:{fill:.1f}">'
                   '<span class="hg__ticks" aria-hidden="true"></span>'
                   f'<span class="hg__pool"><i>{E(ref)}</i></span>'
                   '<span class="hg__lvl"><span class="hg__rise"><span class="hg__w hg__w--b"></span><span class="hg__w"></span>'
                   f'<span class="hg__mk"><strong>{pct_txt(v["pct"])}</strong> full</span></span></span></div></div>')
        elif pts30 and len(pts30) > 1:
            span = "Last 30 days" if (pts30[-1][0] - pts30[0][0]).days >= 28 else f"{d_short(pts30[0][0])} to {d_short(pts30[-1][0])}"
            sp = charts.sparkline(pts30, full_pool=v["pool"], title=f"{lk['name']} level, {span[0].lower() + span[1:]}")
            if sp:
                viz = f'<div class="pn__viz"><div class="spark30">{sp}<span class="spark30__l">{E(span)}</span></div></div>'
        src = f'<p class="pn__src">Source: {self.ext(v["src_url"], v["src_text"], "ext src-link")}</p>'
        asof = f'<p class="pn__asof">{self.ico("clock")}<span>{v["asof"]}</span></p>' if v["asof"] else ""
        last = ""
        if v["elev"] is not None:
            last = (f'<p class="pn__last"{"" if unknown else " hidden"}>Last reading: {num(v["elev"])} ft '
                    f'{"for" if v["daily"] else "at"} {E(v.get("when_wd", ""))} ({E(v["short"])})</p>')
        known = (f'<div class="pn__known"{" hidden" if unknown else ""}><div class="pn__main">'
                 f'<div class="pn__top"><p class="pn__k">{self.ico("gauge")}{kick}</p>{badges}</div>'
                 f'<p class="pn__elev">{elev_html}<span class="pn__u">ft</span></p>{pool_line}{trend}</div>{viz}</div>')
        unk = ('<p class="pn__unk">Current level unknown</p>' if unknown
               else '<template class="tpl-unk"><p class="pn__unk">Current level unknown</p></template>')
        cls = "pn pn--" + v["fresh"]
        return (f'<section class="{cls}" aria-label="Current level" data-reading-ts="{v.get("ts", "")}" '
                f'data-soft-h="{_h(e.get("soft_stale_hours", 48))}" data-hard-h="{_h(e.get("hard_stale_hours", 96))}">'
                f'{unk}{known}{last}<div class="pn__foot">{asof}{src}</div></section>')

    def tile(self, label, value, unit="", sub="", icon=""):
        return (f'<li class="ti"><span class="ti__k">{label}</span><span class="ti__v">{self.ico(icon) if icon else ""}{value}'
                + (f" <small>{unit}</small>" if unit else "") + "</span>" + (f'<span class="ti__s">{sub}</span>' if sub else "") + "</li>")

    def change_tiles(self, v, stats):
        """Plan §4.3 item 6 as stat tiles: 24 h, 7 d, 30 d, same day last year, the median for this date (each only with data)."""
        tiles = []

        def add(label, then, ch, sub):
            icon = "up" if ch >= 0.045 else "down" if ch <= -0.045 else "flat"       # the trend rule's steady range
            tiles.append(self.tile(label, signed(ch), "ft", f"{sub} {num(then)} ft", icon))
        rd = v.get("rdate")
        for lab, c, days in (("24 hours" if not v["daily"] else "1 day", v["c24"], 1), ("7 days", v["c7"], 7), ("30 days", v["c30"], 30)):
            if c is not None:
                add(lab, v["elev"] - c, c, f"{d_short(rd - dt.timedelta(days=days))}:" if rd else "then:")
        sd = stats.get("same_day_last_year")
        if sd and sd.get("elev_ft") is not None:
            add("1 year", sd["elev_ft"], v["elev"] - sd["elev_ft"], f'{d_med(as_date(sd["date"]))}:')
        md = stats.get("median_this_date")
        ny = (len(md["years"]) if isinstance(md.get("years"), list) else md.get("years") or 0) if md else 0
        if md and md.get("elev_ft") is not None and ny >= 5:
            yrs = f"{ny} years" + (f' ({md["first_year"]}–{md["last_year"]})' if md.get("first_year") else "")
            dm = v["elev"] - md["elev_ft"]
            icon = "up" if dm >= 0.045 else "down" if dm <= -0.045 else "flat"
            tiles.append(self.tile(f"vs {d_short(rd) if rd else 'date'} median", signed(v["elev"] - md["elev_ft"]), "ft",
                                   f'Median {num(md["elev_ft"])} ft, {yrs}', icon))
        return f'<ul class="tiles">{"".join(tiles)}</ul>' if tiles else ""

    def tabs(self, gid, items):
        """A range switcher without script: radio buttons choose which panel shows. Every panel is in the HTML."""
        if not items:
            return ""
        if len(items) == 1:
            return items[0][1]
        ins = "".join(f'<input class="rg__i" type="radio" name="{gid}" id="{gid}{i}"{" checked" if i == 0 else ""}>'
                      f'<label for="{gid}{i}">{E(lab)}</label>' for i, (lab, _) in enumerate(items))
        return (f'<div class="rg" role="group" aria-label="Chart range">{ins}'
                + "".join(f'<div class="rg__p">{h}</div>' for _, h in items) + "</div>")

    def charts_block(self, lk, v, pts30):
        name, items = lk["name"], []
        word = "daily average" if v["kind"] == "daily_average" else "daily level"
        if pts30 and len(pts30) > 1:
            hi = max(pts30, key=lambda p: p[1])
            lo = min(pts30, key=lambda p: p[1])
            n = (pts30[-1][0] - pts30[0][0]).days
            period = "In the last 30 days" if n >= 28 else f"From {d_short(pts30[0][0])} to {d_short(pts30[-1][0])}"
            summ = (f'<p>{period} the highest {word} was {num(hi[1])} ft on {d_short(hi[0])}, and the lowest was {num(lo[1])} ft on {d_short(lo[0])}.</p>')
            gaps = len(charts.segments(pts30, 1)) > 1
            ch = charts.line_chart(pts30, title=f"{name} water level, last 30 days",
                                   desc=f"Daily levels from {d_med(pts30[0][0])} to {d_med(pts30[-1][0])}; high {num(hi[1])} ft, low {num(lo[1])} ft"
                                   + (f"; full pool {pool_txt(v['pool'])} ft." if v["pool"] is not None else "."),
                                   full_pool=v["pool"], max_gap_days=1, height=210, line_label=f"{name}, {word}",
                                   caption="Gaps in the line are days without a value." if gaps else "")
            items.append(("30 days", f'<h3 class="vh">Last 30 days</h3>{summ}{ch}'))
        yr = self.year_chart(lk, v)
        if yr:
            items.append(("12 months", f'<h3 class="vh">Last 12 months</h3>{yr}'))
        sub = f"{word.capitalize()}s in feet from {E(v['short'])}."
        if v["pool"] is not None and items:
            sub += (f" The dashed line is full pool, {pool_txt(v['pool'])} ft." if 'class="pool"' in "".join(h for _, h in items)
                    else f" Full pool is {pool_txt(v['pool'])} ft.")
        return self.tabs("rg", items), sub

    def five_chart(self, lk, v, days, height):
        ser = [(r["date"], r["elev"]) for r in (self.series.get(lk["id"]) or []) if r["elev"] is not None]
        if not ser:
            return ""
        five = [p for p in ser if p[0] >= ser[-1][0] - dt.timedelta(days=round(365.25 * 5))]
        if (five[-1][0] - five[0][0]).days <= 400:
            return ""
        pts = charts.downsample(five, days) if len(five) > 700 else five
        avg = {3: "Three-day averages", 7: "Weekly averages"}.get(days, f"{days}-day averages") if len(five) > 700 else "Daily values"
        hi = max(five, key=lambda p: p[1])
        lo = min(five, key=lambda p: p[1])
        return charts.line_chart(pts, title=f"{lk['name']}, last 5 years", desc=f"Levels from {d_med(five[0][0])} to {d_med(five[-1][0])}, {avg.lower()}.",
                                 full_pool=v["pool"], max_gap_days=max(10, days * 2), height=height, marks=False, line_label=f"{lk['name']}, {avg.lower()}",
                                 caption=f"{avg}. Highest daily value {num(hi[1])} ft ({d_med(hi[0])}); lowest {num(lo[1])} ft ({d_med(lo[0])})."
                                 " Gaps in the line are periods without values.")

    def table_14(self, lid, stats, v):
        rows = stats.get("last_14") if isinstance(stats.get("last_14"), list) else None
        recs = []
        if rows and isinstance(rows[0], dict):
            for r in rows:
                recs.append((as_date(r.get("date")), r.get("elev_ft"), r.get("change_ft"), r.get("pct_full")))
        else:
            ser = [r for r in (self.series.get(lid) or []) if r["elev"] is not None][-15:]
            for i in range(len(ser) - 1, max(0, len(ser) - 15), -1):
                r, p = ser[i], ser[i - 1]
                ch = round(r["elev"] - p["elev"], 2) if (r["date"] - p["date"]).days == 1 else None
                recs.append((r["date"], r["elev"], ch, r["pct"]))
        recs = [r for r in recs if r[0] and r[1] is not None][:14]
        if not recs:
            return ""
        recs.sort(key=lambda r: r[0], reverse=True)
        # the headline's date shows the headline value (a provisional daily value may since have been revised in the series)
        rd = v.get("rdate") if v.get("daily") and v.get("elev") is not None and v["fresh"] != "unknown" else None
        for i, (d, e, c, p) in enumerate(recs):
            if rd and d == rd and round(e, 2) != round(v["elev"], 2):
                prev = recs[i + 1] if i + 1 < len(recs) and (d - recs[i + 1][0]).days == 1 else None
                recs[i] = (d, v["elev"], round(v["elev"] - prev[1], 2) if prev else None, v["pct"] if v["pct"] is not None else p)
                if i > 0 and (recs[i - 1][0] - d).days == 1:
                    n = recs[i - 1]
                    recs[i - 1] = (n[0], n[1], round(n[1] - v["elev"], 2), n[3])
        has_pct = any(r[3] is not None for r in recs)
        big = max([abs(c) for _, _, c, _ in recs if c is not None] + [0.01])

        def chg(c):     # the change, with a small bar scaled to the largest change in the table (direction is in the sign)
            if c is None:
                return '<td class="num">–</td>'
            w = round(abs(c) / big * 50)
            return (f'<td class="num"><span class="cb{" cb--d" if c < 0 else ""}" aria-hidden="true"><i style="width:{w}%"></i></span>{signed(c)}</td>')
        body = "".join(f'<tr><th scope="row"><time datetime="{d.isoformat()}">{d:%a} {d_short(d)}</time></th>'
                       f'<td class="num">{num(e)}</td>{chg(c)}'
                       + (f'<td class="num">{num(p, 1) if p is not None else "–"}</td>' if has_pct else "") + "</tr>"
                       for d, e, c, p in recs)
        return ('<div class="tw"><table class="t14"><caption class="vh">Daily levels in feet for the last 14 days, newest first</caption>'
                '<thead><tr><th scope="col">Date</th><th scope="col" class="num">Level</th><th scope="col" class="num">Change</th>'
                + ('<th scope="col" class="num">% full</th>' if has_pct else "") + f"</tr></thead><tbody>{body}</tbody></table></div>")

    def year_chart(self, lk, v):
        ser = [r for r in (self.series.get(lk["id"]) or []) if r["elev"] is not None]
        if len(ser) < 60:
            return ""
        end = ser[-1]["date"]
        start = end - dt.timedelta(days=365)
        cur = [(r["date"], r["elev"]) for r in ser if r["date"] >= start]
        if (cur[-1][0] - cur[0][0]).days < 60:
            return ""
        prev = []
        for r in ser:
            if start - dt.timedelta(days=366) <= r["date"] < start + dt.timedelta(days=1):
                try:
                    prev.append((r["date"].replace(year=r["date"].year + 1), r["elev"]))
                except ValueError:
                    continue
        if len(prev) < 60:
            prev = None
        hi = max(cur, key=lambda p: p[1])
        lo = min(cur, key=lambda p: p[1])
        cap = (f'12-month high {num(hi[1])} ft ({d_med(hi[0])}) · low {num(lo[1])} ft ({d_med(lo[0])})'
               + (". Grey line: the same dates a year earlier." if prev else "."))
        keep = lambda pts, n: sorted(set(charts.thin(pts, n)) | {max(pts, key=lambda p: p[1]), min(pts, key=lambda p: p[1])})
        return charts.line_chart(keep(cur, 3), title=f"{lk['name']}, last 12 months",
                                 desc=f"Daily levels from {d_med(cur[0][0])} to {d_med(cur[-1][0])}"
                                 + ("; the year before shown as a grey line." if prev else ".") + " Every third day is drawn.",
                                 full_pool=v["pool"], overlay=charts.thin(prev, 4) if prev else None,
                                 overlay_label="Year before" if prev else None, max_gap_days=3, height=210, caption=cap,
                                 line_label=f"{lk['name']}, last 12 months")

    def records_block(self, lk, v, stats):
        if not stats or not stats.get("record_high"):
            return ""
        ps, pe = as_date(stats.get("period_start")), as_date(stats.get("period_end"))
        if not ps or not pe:
            return ""
        enough = (stats.get("n_days") or 0) >= 365 and (pe - ps).days >= 365
        rh, rl = stats["record_high"], stats["record_low"]
        rf = as_date(stats.get("records_from"))

        def t(label, rec):
            return self.tile(label, num(rec["elev_ft"]), "ft", f'<time datetime="{E(str(rec["date"]))}">{d_med(as_date(rec["date"]))}</time>')
        if enough and rf:
            tiles = t(f"Highest since {rf.year}", rh) + t(f"Lowest since {rf.year}", rl)
        elif enough:
            tiles = t("Highest in record", rh) + t("Lowest in record", rl)
        else:
            tiles = t(f"Highest since {d_med(ps)}", rh) + t(f"Lowest since {d_med(ps)}", rl)
        yh, yl = stats.get("year_high"), stats.get("year_low")
        if enough and yh and yl:
            tiles += t("12-month high", yh) + t("12-month low", yl)
        # where the latest reading sits between the two records (only with a full year of data and a dated reading)
        rng = ""
        hi, lo, el = rh["elev_ft"], rl["elev_ft"], v["elev"]
        if enough and el is not None and v.get("rdate") and hi > lo and lo <= el <= hi:
            pos = lambda x: (x - lo) / (hi - lo) * 100
            pool = v["pool"] if v["pool"] is not None and lo <= v["pool"] <= hi and abs(pos(v["pool"]) - pos(el)) > 2.5 else None
            rng = ('<div class="rng"><div class="rng__t" aria-hidden="true">'
                   + (f'<i class="rng__p" style="left:{pos(pool):.1f}%"></i>' if pool is not None else "")
                   + f'<i class="rng__n" style="left:{pos(el):.1f}%"></i></div>'
                   f'<p class="rng__e" aria-hidden="true"><span>{num(lo)} ft</span><span>{num(hi)} ft</span></p>'
                   f'<p class="rng__c">The {d_short(v["rdate"])} level, {num(el)} ft, is <strong>{num(el - lo)} ft above</strong> the lowest and '
                   f'<strong>{num(hi - el)} ft below</strong> the highest level '
                   + (f"since {d_med(rf)}." if rf else "in our record.")
                   + (f" The dashed mark is full pool ({pool_txt(pool)} ft)." if pool is not None else "") + "</p></div>")
        note = ('<p class="muted small">'
                + (f'Period of record: {E(v["short"])} data, <time datetime="{ps.isoformat()}">{d_med(ps)}</time> to '
                   f'<time datetime="{pe.isoformat()}">{d_med(pe)}</time>. ' if enough else "")
                + 'Records come from the values we store for this lake, so they cover only the period shown. '
                + (self.first_fill_note(rf) if enough and rf else "")
                + (f'{self.a(self.lake_url(lk) + "history/", "Year-by-year highs and lows")}.' if lk["id"] in self.history_ids else "")
                + "</p>")
        return f'<ul class="tiles tiles--4">{tiles}</ul>{rng}{note}'

    def meaning_block(self, lk, v):
        items = []
        name = lk["name"]
        if v["pool"] is not None:
            lab = lk.get("full_pool_label")
            items.append(f'<li><strong>Full pool is {pool_txt(v["pool"])} ft</strong>' + (f' ({E(lab)})' if lab else "")
                         + (f', in feet above {E(lk["datum"])} as {E(v["short"])} states it' if lk.get("datum") else "") + ".</li>")
        if lk.get("flood_control") and v["pool"] is not None:
            items.append(f"<li>{E(name)} is a flood-control lake: water above full pool is flood storage, held for a time and then released.</li>")
        if v["pct"] is not None and v["pct"] >= 99.95 and (v["vs"] or 0) > 0:
            items.append("<li><strong>“100% full” cannot go higher.</strong> Percent full counts conservation storage only. "
                         "To see how far into the flood pool the lake is, use the feet above full pool.</li>")
        e = v["e"]
        if lk.get("dead_pool_ft") is not None and e.get("storage_af") is None and not lk.get("capacity_af") and e.get("area_acres") is None:
            items.append(f'<li>Dead pool elevation is {pool_txt(lk["dead_pool_ft"])} ft'
                         + (f', {num(v["elev"] - lk["dead_pool_ft"])} ft below the {E(v.get("when_short", "latest"))} level' if v["elev"] is not None and v["elev"] >= lk["dead_pool_ft"] else "") + ".</li>")
        if v["band"] and v["badges"]:
            items.append(f'<li>Status <strong>{E(BAND_LABEL[v["band"]])}</strong> describes how full the lake is, using the bands on our '
                         f'{self.a("/sources/", "sources page")}' + (" (bands set for this lake)" if lk.get("bands") else "")
                         + ". It says nothing about conditions on the water.</li>")
        for n in self.notes.get(lk["id"]) or []:
            if n.get("text") and n.get("source_url"):
                items.append(f'<li>{E(n["text"])} {self.ext(n["source_url"], n.get("source_name") or "Source")}'
                             + (f' <span class="muted small">(checked {E(d_med(as_date(n["checked"])))})</span>' if n.get("checked") else "")
                             + "</li>")
        return '<ul class="facts facts--c">' + "".join(items) + "</ul>" if items else ""

    def cap_bar(self, used, cap, pct, what):
        """A capacity bar: stored water against capacity, with the figures as text."""
        w = max(0.0, min(100.0, 100 * used / cap))
        return (f'<div class="stg"><p class="stg__t"><strong>{acre_feet(used)}</strong> <span>of {acre_feet(cap)} acre-feet {what}</span> '
                f'<b>{pct_txt(pct) if pct is not None else pct_txt(100 * used / cap)}</b></p>'
                f'<div class="stg__bar" aria-hidden="true"><i style="width:{w:.1f}%"></i></div></div>')

    def storage_block(self, lk, v):
        e = v["e"]
        st, cs, cap = e.get("storage_af"), e.get("conservation_storage_af"), lk.get("capacity_af")
        bar, tiles = "", []
        if cs is not None and cap:
            bar = self.cap_bar(cs, cap, v["pct"], "of conservation storage")
        elif st is not None and cap and e.get("pct_full_basis") == "computed_storage":
            bar = self.cap_bar(st, cap, v["pct"], "of capacity")
        if st is not None:
            tiles.append(self.tile("Water in storage", acre_feet(st), "acre-feet"))
        if cap:
            tiles.append(self.tile("Conservation capacity" if cs is not None or v["pool"] is not None else "Capacity", acre_feet(cap), "acre-feet"))
        if e.get("area_acres") is not None:
            tiles.append(self.tile("Surface area", acre_feet(e["area_acres"]), "acres"))
        if not bar and not tiles:
            return ""           # a dead-pool elevation alone goes in "what this level means"
        if lk.get("dead_pool_ft") is not None:
            tiles.append(self.tile("Dead pool elevation", pool_txt(lk["dead_pool_ft"]), "ft"))
        return bar + (f'<ul class="tiles tiles--4">{"".join(tiles)}</ul>' if tiles else "")

    def system_block(self, lk):
        sk = lk.get("system")
        sysrec = self.sys_built.get(sk)
        if not sysrec:
            return None
        others = [self.lake_by_id[m] for m in sysrec.get("members", []) if m != lk["id"] and m in self.lake_by_id]
        lis = []
        for o in others:
            if o["id"] in self.live_ids:
                ov = self.view(o)
                val = (f'<b>{num(ov["elev"])} ft</b>' + (f', {pct_txt(ov["pct"])} full' if ov["pct"] is not None else "")
                       + f' <small>{E(ov.get("when_short", ""))}</small>') if ov["fresh"] != "unknown" and ov["elev"] is not None else "current level unknown"
                lis.append(f'<li>{self.a(self.lake_url(o), o["name"])}<span>{val}</span></li>')
        nums = self.system_numbers(sysrec)
        comb = self.cap_bar(nums["storage"], nums["capacity"], nums["pct"], "of combined conservation storage") if nums else ""
        label = f"Part of the {sysrec['name']}" if not sysrec["name"].lower().startswith("the ") else f"Part of {sysrec['name']}"
        inner = (f'<p>{E(lk["name"])} is one of the lakes we track in the {E(sysrec["name"])}.</p>' + comb
                 + (f'<ul class="sysl">{"".join(lis)}</ul>' if lis else "")
                 + f'<p class="more">{self.a(f"/system/{sk}/", "Combined " + sysrec["name"] + " storage", cls="more__a")}</p>')
        return label, inner

    def nearby_block(self, lk):
        """The nearest lakes with a current reading, as cards with level, band and reading date (plan §6.4: 8 nearest)."""
        if lk.get("lat") is None:
            return ""
        cands = []
        for o in self.live:
            if o["id"] == lk["id"] or o.get("lat") is None:
                continue
            ov = self.eff.get(o["id"]) or {}
            if ov.get("freshness") == "unknown" or ov.get("elev_ft") is None:
                continue
            cands.append((miles((lk["lat"], lk["lon"]), (o["lat"], o["lon"])), o))
        cands.sort(key=lambda x: x[0])
        cands = [c for c in cands if c[0] <= NEARBY_MAX_MI][:8]
        if not cands:
            return ""
        return '<ul class="lkcs lkcs--nb">' + "".join(self.home_card(o, f"{dist:,.0f} mi", True) for dist, o in cands) + "</ul>"

    def about_block(self, lk, v):
        src, e = v["src"], v["e"]
        sid = (lk.get("primary") or {}).get("series_id")
        what = {"daily_average": "An estimated daily average, published once a day. It is one value per day, not a gauge reading taken minutes ago.",
                "daily_instant": "One reading per day.", "hourly": "An hourly gauge reading.", "15min": "A gauge reading taken every 15 minutes."}
        rows = [("Source", self.ext(v["src_url"], src.get("credit") or src.get("agency", v["short"]), "ext src-link") + "."
                 + (f' <span class="muted">{E(src["disclaimer"])}</span>' if src.get("disclaimer") else ""), "")]
        rows.append(("What it is", E(what.get(src.get("value_kind") or v["kind"], "A reading from the source.")), ""))
        if sid:
            rows.append(("Series", f"<code>{E(str(sid))}</code>", ""))
        rows.append(("Datum", (f'Feet above {E(lk["datum"])}, as stated by {E(v["short"])} for this lake.' if lk.get("datum")
                               else "The source does not state a vertical datum for this lake."), "datum"))
        if v["pool"] is not None:
            rows.append(("Full pool", f'{pool_txt(v["pool"])} ft'
                         + (f', {E(lk["full_pool_label"])}' if lk.get("full_pool_label") else "")
                         + (f' (from {E(self.pool_source_text(lk, v))})' if lk.get("full_pool_source") else "") + ".", ""))
        if v["pct"] is not None:
            if e.get("pct_full_basis") == "computed_storage":
                rows.append(("Percent full", f'Storage ÷ capacity ({acre_feet(lk.get("capacity_af"))} acre-feet), worked out by us.', ""))
            else:
                rows.append(("Percent full", f'Published by {E(v["short"])}: conservation storage ÷ conservation capacity'
                             + (", capped at 100%" if src.get("id") == "twdb" else "") + ".", ""))
        if v["provisional"]:
            rows.append(("Provisional", "The source may revise recent values.", ""))
        ch = src.get("check_every_hours")
        fa = e.get("fetched_at")
        if ch or fa:
            last = ""
            if fa:
                t = parse_ts(fa).astimezone(v["tz"])
                last = (f' Last checked <time datetime="{iso_z(t)}">{E(t.strftime("%I:%M %p").lstrip("0"))} {E(t.tzname())}, '
                        f'{E(d_wd(t))}</time>.')
            rows.append(("Checked", (f"Every {ch} hour{'s' if ch != 1 else ''}." if ch else "") + last, ""))
        dl = "".join(f'<div{(" class=" + chr(34) + c + chr(34)) if c else ""}><dt>{k}</dt><dd>{val}</dd></div>' for k, val, c in rows)
        return (f'<div class="abr"><dl>{dl}</dl><p class="abr__w">{self.ico("info")}<span><strong>{E(BRAND)} is independent and is not a government website.</strong> '
                f'Do not use this page for navigation, flood or safety decisions. In high water or severe weather, follow the '
                f'lake’s operator, local officials and the National Weather Service.</span></p></div>')

    def lead_known(self, v):
        lk = v["lk"]
        name = lk["name"]
        bits = [f"{E(name)} is at <strong>{num(v['elev'])} feet</strong>"]
        if v["vs"] is not None and v["pool"] is not None:
            bits.append(E(self.vs_words(v, "feet")))
        s = ", ".join(bits)
        if v["pct"] is not None:
            s += f", and {pct_txt(v['pct'])} full."
        else:
            s += "."
        if v["kind"] == "daily_average":
            s2 = (f' That is the daily average for <time datetime="{v["time_attr"]}">{E(v["when_long"])}</time>, '
                  f'published by the {E(v["src"].get("agency", v["short"]))}.')
        elif v["daily"]:
            s2 = f' That is the reading for <time datetime="{v["time_attr"]}">{E(v["when_long"])}</time> from {E(v["src"].get("agency", v["short"]))}.'
        else:
            s2 = f' That reading is from <time datetime="{v["time_attr"]}">{E(v["when_long"])}</time> ({E(v["src"].get("agency", v["short"]))}).'
        s3 = ""
        st = self.stats.get(lk["id"]) or {}
        md = st.get("median_this_date")
        rd = v.get("rdate")
        if (md and md.get("elev_ft") is not None and v["badges"] and rd
                and as_date(st.get("period_end")) == rd and (md.get("years") or 0) >= 5):
            dm = v["elev"] - md["elev_ft"]
            yrs = f'{md["years"]} years' + (f' ({md["first_year"]}–{md["last_year"]})' if md.get("first_year") else "")
            s3 += (f" The level is {num(abs(dm))} feet {'above' if dm > 0 else 'below'} the median for {E(d_short(rd))} over {yrs}."
                   if round(dm, 2) else f" The level equals the median for {E(d_short(rd))} over {yrs}.")
        if v["c24"] is not None and v["badges"]:
            c = v["c24"]
            if round(c, 2) == 0:
                s3 += " The level did not change from the day before." if v["daily"] else " The level did not change in the last 24 hours."
            else:
                s3 += (f" The lake {'rose' if c > 0 else 'fell'} {num(abs(c))} feet "
                      + ("from the day before." if v["daily"] else "in the last 24 hours."))
        names = [a for a in (lk.get("aliases") or []) if not a.rstrip().endswith("Dam")]
        s4 = f" {E(name)} is also called {E(' or '.join(names) if len(names) < 3 else ', '.join(names[:-1]) + ' or ' + names[-1])}." if names else ""
        return f'<p class="lead lead--known">{s}{s2}{s3}{s4}</p>'


    def lead_unknown(self, v):
        lk = v["lk"]
        last = ""
        if v["elev"] is not None and v.get("when_long"):
            last = (f' The last reading we have is {num(v["elev"])} ft for <time datetime="{v["time_attr"]}">{E(v["when_long"])}</time> '
                    f'({E(v["short"])}).')
        reason = v["e"].get("reason") or ""
        why = f" {E(reason)}." if reason and not reason.endswith(".") else (f" {E(reason)}" if reason else "")
        p = (f'<p class="lead lead--unknown">The current level of {E(lk["name"])} is unknown: '
             f'we have no recent reading from {E(v["short"])}.{why}{last}</p>')
        # while the reading is current the text sits in a <template>, so search snippets never pick it up; site.js
        # clones it in if the page ages past the hard limit
        return p if (v["fresh"] == "unknown" or v["elev"] is None) else f'<template class="tpl-unk">{p}</template>'


    @staticmethod
    def combined_ids(sysrec):
        """The reservoirs the operator adds up (systems.json `combined`), else every member."""
        return sysrec.get("combined") or sysrec.get("members", [])

    def combined_label(self, sysrec):
        ids = self.combined_ids(sysrec)
        if ids == sysrec.get("members", []):
            return f"{len(ids)} reservoirs"
        names = [self.lake_by_id[i]["name"] for i in ids if i in self.lake_by_id]
        return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else (names[0] if names else "")

    def system_numbers(self, sysrec):
        """Combined storage for a system, only when every member has a current reading with storage and capacity."""
        cs = cap = 0
        dates = []
        for mid in self.combined_ids(sysrec):
            lk = self.lake_by_id.get(mid)
            if not lk or mid not in self.live_ids:
                return None
            v = self.view(lk)
            c = v["e"].get("conservation_storage_af")
            if c is None and v["pct"] is not None and lk.get("capacity_af"):
                c = v["pct"] / 100 * lk["capacity_af"]
            if v["fresh"] == "unknown" or c is None or not lk.get("capacity_af"):
                return None
            cs += c
            cap += lk["capacity_af"]
            dates.append(v.get("rdate"))
        return {"storage": cs, "capacity": cap, "pct": 100 * cs / cap, "dates": dates}


    def pool_source_text(self, lk, v):
        s = lk.get("full_pool_source") or ""
        if s.startswith("http"):
            return "the operator’s published value"
        return f"{v['short']} data" if ":" in s else s


    def lake_faq(self, lk, v, stats):
        name = lk["name"]
        qa = []
        unknown = v["fresh"] == "unknown" or v["elev"] is None
        if not unknown:
            a = f"{num(v['elev'])} ft"
            a += f" ({'daily average for ' if v['kind'] == 'daily_average' else ''}{E(v['when_long'])})"
            if v["vs"] is not None and v["pool"] is not None:
                a += f", which is {E(self.vs_words(v))} ({pool_txt(v['pool'])} ft)"
            a += f", according to the {E(v['src'].get('agency', v['short']))}."
            qa.append((f"What is the water level at {name} today?", a))
        else:
            qa.append((f"What is the water level at {name} today?",
                       f"We do not know the current level: we have no recent reading from {E(v['short'])}."
                       + (f" The last reading was {num(v['elev'])} ft for {E(v['when_long'])}." if v["elev"] is not None else "")))
        if not unknown and v["pct"] is not None:
            cs, cap = v["e"].get("conservation_storage_af"), lk.get("capacity_af")
            qa.append((f"How full is {name}?", f"{pct_txt(v['pct'])} of conservation capacity"
                       + (f": {acre_feet(cs)} of {acre_feet(cap)} acre-feet." if cs is not None and cap else ".")))
        if not unknown and v["trend"] and v["badges"]:
            a = f"{E(TREND_LABEL[v['trend']])}: {signed(v['c24'])} ft {'since the day before' if v['daily'] else 'in the last 24 hours'}"
            if v["c7"] is not None:
                a += f" and {signed(v['c7'])} ft in 7 days"
            a += "."
            if v["c30"] is not None:
                a += f" It is {num(abs(v['c30']))} ft {'higher' if v['c30'] > 0 else 'lower'} than 30 days ago." if round(v["c30"], 2) else " It is at the same level as 30 days ago."
            qa.append((f"Is {name} rising or falling?", a))
        if not unknown and v["badges"] and v["band"] and v["band"] != "level_only" and v["pool"] is not None and v["vs"] is not None:
            a = (f"{E(BAND_LABEL[v['band']])}. On {E(v['when_long'])} it was {E(self.vs_words(v))} ({pool_txt(v['pool'])} ft)"
                 + (f" and {pct_txt(v['pct'])} full" if v["pct"] is not None else "") + f", according to the {E(v['src'].get('agency', v['short']))}.")
            qa.append((f"Is {name} full right now?", a))
        if v["pool"] is not None:
            a = f"{pool_txt(v['pool'])} ft" + (f" ({E(lk['full_pool_label'])}" + (f", {E(lk['datum'])}" if lk.get("datum") else "") + ")" if lk.get("full_pool_label") else "") + "."
            if lk.get("flood_control"):
                a += " Levels above this are flood storage."
            qa.append((f"What is full pool for {name}?", a))
        sd = stats.get("same_day_last_year") if stats else None
        if not unknown and sd and sd.get("elev_ft") is not None:
            diff = v["elev"] - sd["elev_ft"]
            qa.append((f"What was {possessive(name)} level a year ago?",
                       f"{num(sd['elev_ft'])} ft on {d_med(as_date(sd['date']))}, "
                       + (f"{num(abs(diff))} ft {'lower' if diff > 0 else 'higher'} than today." if round(diff, 2) else "the same as today.")))
        if stats and stats.get("record_high") and (stats.get("n_days") or 0) >= 365:
            rh, rl = stats["record_high"], stats["record_low"]
            rf = as_date(stats.get("records_from"))
            frm = rf or as_date(stats["period_start"])
            qa.append((f"What are the highest and lowest levels on record at {name}?",
                       f"In our record ({E(v['short'])} data, {d_med(frm)} to {d_med(as_date(stats['period_end']))}) "
                       f"the highest level was {num(rh['elev_ft'])} ft on {d_med(as_date(rh['date']))} and the lowest was "
                       f"{num(rl['elev_ft'])} ft on {d_med(as_date(rl['date']))}."
                       + (f" Lower values from {d_med(as_date(stats['period_start']))} to {d_med(rf)}, while the reservoir was "
                          "filling for the first time, are not counted as records." if rf else "")))
        src = v["src"]
        cad = {"daily_average": "one daily average per day", "daily_instant": "one reading per day",
               "hourly": "readings every hour", "15min": "readings every 15 minutes"}.get(src.get("value_kind"), "new readings regularly")
        ch = src.get("check_every_hours")
        qa.append(("How often is this page updated?",
                   f"The {E(src.get('agency', v['short']))} publishes {cad}." + (f" We check for new data every {ch} hour{'s' if ch != 1 else ''}." if ch else "")
                   + " If the source stops updating, this page says “Current level unknown” and shows the last reading with its date."))
        sid = (lk.get("primary") or {}).get("series_id")
        qa.append(("Where does the data come from?",
                   f"{E(src.get('credit') or src.get('agency', v['short']))}" + (f" (series {E(str(sid))})" if sid else "") + "."
                   + (" The values are provisional and may be revised." if v["provisional"] else "")))
        names = [a for a in (lk.get("aliases") or []) if not a.rstrip().endswith("Dam")]
        dams = [a for a in (lk.get("aliases") or []) if a.rstrip().endswith("Dam")]
        if names:
            qa.append((f"What else is {name} called?", f"{E(name)} is also known as {E(', '.join(names))}."))
        for dam in dams:
            a = f"{E(dam)} forms {E(name)}, so the water level behind the dam is the lake level"
            if not unknown:
                a += (f": {num(v['elev'])} ft {'(daily average) ' if v['kind'] == 'daily_average' else ''}for {E(v['when_long'])}, "
                      f"according to the {E(v['src'].get('agency', v['short']))}.")
            else:
                a += ". We have no current reading."
            qa.append((f"What is the water level at {dam}?", a))
        return qa


    # ------------------------------------------------------------ history page
    def history_page(self, lk):
        v = self.view(lk)
        name, lid = lk["name"], lk["id"]
        url = self.lake_url(lk) + "history/"
        st = self.states.get(lk.get("state"), {"name": lk.get("state")})
        crumbs = [c for c in [("Home", "/"), self.state_crumb(lk.get("state"), st), (name, self.lake_url(lk)), ("History", url)] if c]
        ser = [r for r in self.series[lid] if r["elev"] is not None]
        first, last = ser[0]["date"], ser[-1]["date"]
        stats = self.stats.get(lid) or {}
        title = f"{name} Water Level History: Charts, Highs & Lows"
        for alt in (f"{name} Water Level History & Records", f"{name} Water Level History"):
            if len(title) > 65:
                title = alt
        desc = (f"{name} levels since {first.year}: charts, the highest and lowest level of each year, record levels, "
                f"and a CSV download.")
        pts = [(r["date"], r["elev"]) for r in ser]
        hi_all = max(pts, key=lambda p: p[1])
        lo_all = min(pts, key=lambda p: p[1])
        # records count from records_from when the series starts with the reservoir's first filling (stats.py)
        rf = as_date(stats.get("records_from"))
        rec = [p for p in pts if p[0] >= rf] if rf else []
        if not rec:
            rf, rec = None, pts
        hi = max(rec, key=lambda p: (p[1], -p[0].toordinal()))
        lo = min(rec, key=lambda p: (p[1], p[0].toordinal()))
        years = sorted({d.year for d, _ in pts})
        summary = (f'<p class="lead">Our {E(name)} record holds {len(pts):,} values from {E(v["short"])}, '
                   f'from <time datetime="{first.isoformat()}">{d_med(first)}</time> to <time datetime="{last.isoformat()}">{d_med(last)}</time>. '
                   + (f'Since it first filled ({d_med(rf)}), the highest was <strong>{num(hi[1])} ft</strong> on {d_med(hi[0])} '
                      f'and the lowest <strong>{num(lo[1])} ft</strong> on {d_med(lo[0])}.' if rf else
                      f'The highest was <strong>{num(hi[1])} ft</strong> on {d_med(hi[0])}; the lowest was <strong>{num(lo[1])} ft</strong> on {d_med(lo[0])}.')
                   + (f' Full pool is {pool_txt(v["pool"])} ft.' if v["pool"] is not None else "") + "</p>")
        today = self.a(self.lake_url(lk), f"{name} water level today", cls="more__a")
        blocks = [self.lst_hist_hero(lk, v, pts, hi, lo, years, rf), summary]
        # charts: the long-term view first; every range is in the HTML, radio buttons choose which one shows
        items = []
        span = (last - first).days
        if span > 365 * 6:
            days = max(7, math.ceil(span / 1500))       # about 1,500 points at most: more than the chart has pixels
            ptsf = charts.downsample(pts, days)
            avg = "Weekly averages" if days == 7 else f"{days}-day averages"
            items.append(("Full record", '<h3 class="vh">Full record</h3>' + charts.line_chart(
                ptsf, title=f"{name}, full record", desc=f"{avg} from {d_med(first)} to {d_med(last)}.",
                full_pool=v["pool"], max_gap_days=max(45, days * 3), height=290, xticks="years", marks=False,
                line_label=f"{name}, {avg.lower()}",
                caption=f"{avg} (older years may hold only monthly values), {first.year} to {last.year}. "
                        f"Highest value {num(hi_all[1])} ft ({d_med(hi_all[0])}); lowest {num(lo_all[1])} ft ({d_med(lo_all[0])})"
                        + (f", during the first filling" if rf and lo_all[0] < rf else "") + ". "
                        "Gaps in the line are periods without values.")))
        five = self.five_chart(lk, v, 3, 290 if not items else 250)
        if five:
            items.append(("5 years", f'<h3 class="vh">Last 5 years</h3>{five}'))
        yr = self.year_chart(lk, v)
        if yr:
            items.append(("12 months", f'<h3 class="vh">Last 12 months</h3>{yr}'))
        if items:
            sub = f"Levels in feet from {E(v['short'])}."
            if v["pool"] is not None:
                sub += (f" The dashed line is full pool, {pool_txt(v['pool'])} ft." if 'class="pool"' in "".join(h for _, h in items)
                        else f" Full pool is {pool_txt(v['pool'])} ft.")
            blocks.append(self.sec("chart", "chart", f"{E(name)} level over time", sub,
                                   f'<div class="lst-ch">{self.tabs("hr", items)}</div>'))
        blocks.append(self.sec("records", "flag", f"Highest and lowest levels at {E(name)}" + (f" since {rf.year}" if rf else ""), "Highs and lows in the values we store for this lake.",
                               self.lst_hist_tiles(lk, v, stats, pts, hi, lo, years, rf)))
        yt = self.lst_yearly(stats, hi[1], lo[1], v["pool"], rf)
        if yt:
            blocks.append(self.sec("yearly", "cal", "Highest and lowest level each year",
                                   "Years with fewer than 300 daily values are marked partial. The bar shows each year’s range "
                                   "against the record low and high."
                                   + (f" Newest first; scroll the table for all {yt.count('<tr') - 1} years." if yt.count("<tr") > 11 else ""), yt))
        blocks.append(self.sec("monthly", "bars", "Average level by month",
                               "Monthly means of the stored values, in feet. ▲ marks the highest and ▼ the lowest month in each column.",
                               self.monthly_table(lk, ser)))
        csv_url = f"/data/lake/{lid}.csv"
        cite = (f"{name} water level, {v['src'].get('credit') or v['src'].get('agency')}, via {BRAND}, "
                f"{SITE_URL}{url} (retrieved {d_med(self.now.date())}).")
        blocks.append(self.sec("download", "dl", "Download the data", "One row per day: date, elevation_ft, percent_full, storage_af, source.",
                               f'<div class="dlc"><a class="btn" href="{csv_url}" download rel="nofollow">{self.ico("dl")}Download CSV</a>'
                               f'<p class="small">Cite as: {E(cite)}</p></div>'))
        blocks.append(f'<p class="more lst-today">{today}</p>')
        graph = [{"@type": "Dataset", "name": f"{name} daily water level", "description": desc,
                  "url": SITE_URL + url, "temporalCoverage": f"{first.isoformat()}/{last.isoformat()}",
                  "creator": dict({"@type": "Organization", "name": v["src"].get("agency", v["short"])},
                                  **({"url": v["src"]["public_url"]} if v["src"].get("public_url") else {})),
                  "publisher": {"@id": SITE_URL + "/#org"}, "isAccessibleForFree": True,
                  "variableMeasured": ["Water surface elevation (ft)"] + (["Percent full"] if any(r.get("pct") is not None for r in ser) else []),
                  "isBasedOn": v["src_url"],
                  "distribution": [{"@type": "DataDownload", "name": f"{name} daily levels (CSV)", "encodingFormat": "text/csv",
                                    "contentUrl": SITE_URL + csv_url}]}]
        if v["e"].get("content_modified"):
            graph[0]["dateModified"] = v["e"]["content_modified"]
        if lk.get("lat") is not None:
            graph[0]["spatialCoverage"] = {"@type": "Place", "geo": {"@type": "GeoCoordinates", "latitude": lk["lat"], "longitude": lk["lon"]}}
        alias = f'<p class="aka">{E(alias_text(lk["aliases"]))}</p>' if lk.get("aliases") else ""
        body = ("{{sprite}}" + self.crumbs_html(crumbs) + f'<header class="lk-head lst-hd"><h1>{E(name)} water level history</h1>{alias}'
                f'<p class="lst-back">{today}</p></header>'
                + f'<div class="lst-hw">{"".join(blocks)}</div>')
        self.write(url, title, desc, self.with_sprite(body), crumbs=crumbs, lastmod=v["e"].get("content_modified"), group="sub", graph=graph,
                   kind="history", body_class='class="pg-lst pg-hist"', css="list")

    def monthly_table(self, lk, ser):
        """Month x (last three years + all years) means. The highest and lowest month of each column carry a marker and a
        hidden word, so colour is never the only signal."""
        by = defaultdict(list)
        for r in ser:
            by[(r["date"].year, r["date"].month)].append(r["elev"])
        years = sorted({y for y, _ in by})
        recent = years[-3:]
        cols = [{m: statistics.mean(by[(y, m)]) for m in range(1, 13) if (y, m) in by} for y in recent]
        allm = {}
        for m in range(1, 13):
            vals = [statistics.mean(by[(y, m)]) for y in years if (y, m) in by]
            if vals:
                allm[m] = statistics.mean(vals)
        cols.append(allm)

        def cell(col, m):
            if m not in col:
                return '<td class="num">–</td>'
            val = col[m]
            mark = ""
            if len(col) >= 3:
                if val == max(col.values()):
                    mark = "mx"
                elif val == min(col.values()):
                    mark = "mn"
            word = {"mx": "highest", "mn": "lowest"}.get(mark)
            return (f'<td class="num{" " + mark if mark else ""}">{num(val)}'
                    + (f'<span class="vh"> ({word})</span>' if word else "") + "</td>")
        rows = "".join(f'<tr><th scope="row"><abbr title="{dt.date(2000, m, 1):%B}">{dt.date(2000, m, 1):%b}</abbr></th>'
                       + "".join(cell(c, m) for c in cols) + "</tr>" for m in range(1, 13))
        head = "".join(f'<th scope="col" class="num">{y}</th>' for y in recent)
        return ('<div class="tw lst-mt"><table class="lt mt"><caption class="vh">Average level by month in feet, '
                f'{", ".join(map(str, recent))} and all years {years[0]}–{years[-1]}</caption><thead><tr><th scope="col">Month</th>'
                f'{head}<th scope="col" class="num">All years<small>{years[0]}–{years[-1]}</small></th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div>')

    def first_fill_note(self, rf):
        """One sentence for lakes whose stored series starts with the reservoir's first filling (tools/first_fill.py)."""
        return (f'Records count from <time datetime="{rf.isoformat()}">{d_med(rf)}</time>, the first day the level came '
                "within 10% of full pool; the lower values from the reservoir’s first filling stay in the charts and "
                "yearly table but are not counted as records. ")

    # history helpers (prefix lst_)
    def lst_hist_hero(self, lk, v, pts, hi, lo, years, rf=None):
        """Dark hero card: the period of record, and where the latest stored value sits between the record low and high."""
        (first, _), (last, el) = pts[0], pts[-1]
        rng = ""
        if hi[1] > lo[1]:
            pos = lambda x: (x - lo[1]) / (hi[1] - lo[1]) * 100
            p = max(0.0, min(100.0, pos(el)))
            pool = v["pool"] if v["pool"] is not None and lo[1] <= v["pool"] <= hi[1] and abs(pos(v["pool"]) - p) > 2.5 else None
            lean = "lst-rg__l--a" if p < 18 else "lst-rg__l--z" if p > 82 else ""
            rng = ('<div class="lst-rg">'
                   f'<p class="lst-rg__l {lean}" style="left:{p:.1f}%" aria-hidden="true"><b>{num(el)} ft</b> {d_short(last)}</p>'
                   '<div class="lst-rg__t" aria-hidden="true">'
                   + (f'<i class="lst-rg__p" style="left:{pos(pool):.1f}%"></i>' if pool is not None else "")
                   + f'<i class="lst-rg__n" style="left:{p:.1f}%"></i></div>'
                   '<p class="lst-rg__e" aria-hidden="true">'
                   f'<span>Lowest <b>{num(lo[1])} ft</b> {d_med(lo[0])}</span><span>Highest <b>{num(hi[1])} ft</b> {d_med(hi[0])}</span></p>'
                   f'<p class="lst-rg__c">The <time datetime="{last.isoformat()}">{d_med(last)}</time> value, {num(el)} ft, is '
                   f'<strong>{num(el - lo[1])} ft above</strong> the lowest and <strong>{num(hi[1] - el)} ft below</strong> the highest value '
                   + (f"since the reservoir first filled ({d_med(rf)})." if rf else "in our record.")
                   + (f" The dashed mark is full pool ({pool_txt(pool)} ft)." if pool is not None else "") + "</p></div>")
        n = len(years)
        return (f'<section class="pn lst-hh" aria-label="Period of record"><div class="lst-hh__in"><div>'
                f'<p class="pn__k">{self.ico("clock")}Period of record</p>'
                f'<p class="lst-hh__y">{first.year}<span>–</span>{last.year}</p>'
                f'<p class="lst-hh__s"><b>{len(pts):,}</b> values · <b>{n}</b> year{"s" if n != 1 else ""} with data</p></div>{rng}</div>'
                f'<div class="pn__foot"><p class="pn__asof">{self.ico("cal")}<span>Values from <time datetime="{first.isoformat()}">{d_med(first)}</time> '
                f'to <time datetime="{last.isoformat()}">{d_med(last)}</time>, published by {E(v["short"])}</span></p>'
                f'<p class="pn__src">Source: {self.ext(v["src_url"], v["src_text"], "ext src-link")}</p></div></section>')

    def lst_hist_tiles(self, lk, v, stats, pts, hi, lo, years, rf=None):
        """Record tiles, each with its own date: only values from the stored series and the stats file."""
        def dated(d):
            return f'<time datetime="{d.isoformat()}">{d_med(d)}</time>'
        word = f"since {rf.year}" if rf else "in record"
        tiles = [self.tile(f"Highest {word}", num(hi[1]), "ft", dated(hi[0])),
                 self.tile(f"Lowest {word}", num(lo[1]), "ft", dated(lo[0]))]
        yh, yl = stats.get("year_high"), stats.get("year_low")
        if yh and yl and yh.get("elev_ft") is not None and yl.get("elev_ft") is not None:
            tiles += [self.tile("12-month high", num(yh["elev_ft"]), "ft", dated(as_date(yh["date"]))),
                      self.tile("12-month low", num(yl["elev_ft"]), "ft", dated(as_date(yl["date"])))]
        md = stats.get("median_this_date")
        ny = (len(md["years"]) if isinstance(md.get("years"), list) else md.get("years") or 0) if md else 0
        pe = as_date(stats.get("period_end"))
        if md and md.get("elev_ft") is not None and ny >= 5 and pe:
            yrs = f"{ny} years" + (f' ({md["first_year"]}–{md["last_year"]})' if md.get("first_year") else "")
            tiles.append(self.tile(f"Median for {d_short(pe)}", num(md["elev_ft"]), "ft", yrs))
        tiles.append(self.tile("Years with data", f"{len(years)}", "", f"{years[0]}–{years[-1]}"))
        tiles.append(self.tile("First value", num(pts[0][1]), "ft", dated(pts[0][0])))
        tiles.append(self.tile("Latest value", num(pts[-1][1]), "ft", dated(pts[-1][0])))
        note = (f'<p class="muted small">Period of record: {E(v["short"])} data, {dated(pts[0][0])} to {dated(pts[-1][0])}. '
                "Records come from the values we store for this lake, so they cover only the period shown. "
                + (self.first_fill_note(rf) if rf else "") + "</p>")
        return f'<ul class="tiles lst-ti">{"".join(tiles)}</ul>{note}'

    def lst_yearly(self, stats, rhi, rlo, pool, rf=None):
        """Each year's high and low (with dates), a range bar on the record scale, and the record years marked in words."""
        yearly = stats.get("yearly") or []
        if not yearly:
            return ""
        span = (rhi - rlo) or 1
        pos = lambda x: max(0.0, min(100.0, (x - rlo) / span * 100))
        counted = [y for y in yearly if not rf or y["year"] > rf.year] or yearly
        top = rhi if rf else max(y["high"] for y in counted)
        bot = rlo if rf else min(y["low"] for y in counted)
        rows = []
        for y in sorted(yearly, key=lambda y: -y["year"]):
            rh = ' class="num rec"' if y["high"] == top else ' class="num"'
            rl = ' class="num rec"' if y["low"] == bot else ' class="num"'
            a, b = pos(y["low"]), pos(y["high"])
            rows.append(
                f'<tr><th scope="row">{y["year"]}{" <span class=muted>(partial)</span>" if (y.get("n") or 0) < 300 else ""}'
                f'{" <span class=muted>(first filling)</span>" if rf and y["year"] < rf.year else ""}</th>'
                f'<td{rh} data-v="{y["high"]}">{num(y["high"])}{"<span class=vh> (record high)</span>" if y["high"] == top else ""}'
                f'<small>{d_short(as_date(y["high_date"]))}</small></td>'
                f'<td{rl} data-v="{y["low"]}">{num(y["low"])}{"<span class=vh> (record low)</span>" if y["low"] == bot else ""}'
                f'<small>{d_short(as_date(y["low_date"]))}</small></td>'
                f'<td class="yb" aria-hidden="true"><i style="--a:{a:.1f};--w:{b - a:.1f}"></i></td>'
                f'<td class="num hm">{y.get("n", "")}</td></tr>')
        pp = f' style="--pp:{pos(pool):.1f}"' if pool is not None and rlo <= pool <= rhi else ""
        return ('<div class="tw tw--tall lst-yt"><table class="lt yt" data-sort' + pp + '><caption class="vh">Highest and lowest level '
                'each year in feet, newest first</caption><thead><tr>'
                '<th scope="col"><button type="button">Year</button></th><th scope="col" class="num"><button type="button">High (ft)</button></th>'
                '<th scope="col" class="num"><button type="button">Low (ft)</button></th><th scope="col" class="yb">Range'
                + ('<span class="vh">, with full pool dashed</span>' if pp else "") + '</th>'
                f'<th scope="col" class="num hm">Values</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')

    # ------------------------------------------------------------ no-feed profile (noindex)
    def state_cards(self, lk, n=6):
        """Most searched lakes with a current reading in the same state(s): the fallback when a lake has no coordinates."""
        sts = set(lk.get("states") or [lk.get("state")])
        out = []
        for o in self.top_lakes(len(self.live)):
            if o["id"] != lk["id"] and sts & set(o.get("states") or [o.get("state")]):
                ov = self.eff.get(o["id"]) or {}
                if ov.get("freshness") != "unknown" and ov.get("elev_ft") is not None:
                    out.append(o)
        return '<ul class="lkcs lkcs--nb">' + "".join(self.home_card(o, None, True) for o in out[:n]) + "</ul>" if out else ""

    def profile_page(self, lk):
        name = lk["name"]
        url = self.lake_url(lk)
        code = lk.get("state")
        st = self.states.get(code, {"name": code})
        crumbs = [c for c in [("Home", "/"), self.state_crumb(code, st), (name, url)] if c]
        op = self.operators.get(lk.get("operator"))
        off = lk.get("official_url") or (op or {}).get("official_url")
        who = (op or {}).get("name") or "The lake’s operator"
        card = (f'<section class="pn pn--none lst-nf" aria-label="Level data"><p class="pn__k">{self.ico("info")}No level shown</p>'
                f'<p class="pn__unk">We do not show a level for {E(name)}</p>'
                f'<p class="pn__why">We have not found a public data feed for it that we can check automatically and date, '
                f'and we do not print a number we cannot check.' + (f' {E(who)} publishes the lake level on its own site.' if off else '') + '</p>'
                + '<p class="pn__go">' + (self.ext(off, who + ": lake level", "btn ext") if off else "")
                + self.a("/corrections/", "Tell us about a data feed", cls="btn btn--ghost") + "</p></section>")
        # what we know: only fields that exist in the reference data, links only where a page or a URL exists
        rows = []
        codes = [c for c in (lk.get("states") or [code]) if c]
        rows.append(("State" if len(codes) == 1 else "States",
                     ", ".join(self.a(self.state_url(c), self.states[c]["name"]) if c in self.states else E(c) for c in codes)))
        if op:
            rows.append(("Operator", self.a("/operator/" + op["slug"] + "/", op["name"])))
        sysrec = self.systems.get(lk.get("system"))
        if sysrec:
            rows.append(("Water system", self.a(f"/system/{lk['system']}/", sysrec["name"])))
        al = lk.get("aliases") or []
        names = [x for x in al if not x.rstrip().endswith("Dam")]
        dams = [x for x in al if x.rstrip().endswith("Dam")]
        if names:
            rows.append(("Also called", E(", ".join(names))))
        if dams:
            rows.append(("Formed by", E(" and ".join(dams))))
        if lk.get("full_pool_ft") is not None:
            rows.append(("Full pool", f'{pool_txt(lk["full_pool_ft"])} ft' + (f' ({E(lk["full_pool_label"])})' if lk.get("full_pool_label") else "")
                         + (f', {self.ext(lk["full_pool_source"], "source")}' if str(lk.get("full_pool_source") or "").startswith("http") else "")))
        for n in self.notes.get(lk["id"]) or []:
            if n.get("text") and n.get("source_url"):
                rows.append(("Note", f'{E(n["text"])} {self.ext(n["source_url"], n.get("source_name") or "Source")}'))
        if off:
            rows.append(("Level published by", self.ext(off, who)))
        rows.append(("Level on this site", "Not shown: no checkable public data feed found"))
        known = self.sec("known", "info", f"What we know about {E(name)}", "From our lake records. We add a level only when we find a feed we can check.",
                         '<table class="kv"><caption class="vh">What we know about ' + E(name) + '</caption><tbody>'
                         + "".join(f'<tr><th scope="row">{k}</th><td>{val}</td></tr>' for k, val in rows) + "</tbody></table>")
        lines = [f'<div class="lst-pf">{card}{self.lst_hub_card(lk)}</div>', known]
        nb = self.nearby_block(lk)
        if nb:
            lines.append(self.sec("nearby", "map", "Nearby lakes with a current reading", "The nearest lakes we track, with straight-line distance.", nb))
        else:
            sc = self.state_cards(lk)
            if sc:
                lines.append(self.sec("nearby", "map", f"{E(st['name'])} lakes with a current reading", "The most searched lakes we track in the state.", sc))
            else:
                nc = self.lst_neighbour_cards(lk)
                if nc:
                    lines.append(self.sec("nearby", "map", "Lakes with a current reading in neighboring states",
                                          f"We track no lake with a data feed in {E(st['name'])} yet; these are the most searched ones next door.", nc))
        foot = [self.a(self.state_url(code) or "/", f"All {st['name']} lake levels"), self.a("/lowest-lakes/", "Lowest lakes"),
                self.a("/fullest-lakes/", "Fullest lakes"), self.a("/sources/", "How we choose data sources"), self.a("/corrections/", "Tell us about a data feed")]
        lines.append(f'<nav class="xl" aria-label="Related"><h2>More lake levels</h2><p class="chips-l">{"".join(foot)}</p></nav>')
        lines.append(f'<p class="small muted">{E(BRAND)} is independent and is not a government website.</p>')
        alias = f'<p class="aka">{E(alias_text(al))}</p>' if al else ""
        body = ("{{sprite}}" + self.crumbs_html(crumbs) + f'<header class="lk-head lst-hd"><h1>{E(name)} water level</h1>{alias}</header>'
                + f'<div class="lst-hw">{"".join(lines)}</div>')
        self.write(url, f"{name} Water Level | {BRAND}", f"Where to find the {name} water level, its full pool, and nearby lakes with current readings.",
                   self.with_sprite(body), crumbs=crumbs, noindex=True, sitemap=False, kind="profile", body_class='class="pg-lst pg-prof"', css="list")

    # profile helpers (prefix lst_)
    def lst_hub_card(self, lk):
        """The state hub as a card: how many lakes we track there (counts from the built pages)."""
        code = lk.get("state")
        hub = self.state_url(code)
        if not hub or hub not in self.planned:
            return ""
        st = self.states[code]
        lakes = self.hub_lakes.get(code) or []
        n_live = sum(1 for o in lakes if o["id"] in self.live_ids)
        cnt = f"{len(lakes):,} lake{'s' if len(lakes) != 1 else ''} tracked" + (f", {n_live:,} with a data feed" if n_live else "")
        return (f'<a class="lst-hub" href="{hub}">{self.ico("map", "ic--b")}<span><span class="lst-hub__t">All {E(st["name"])} lake levels</span>'
                f'<span class="lst-hub__s">{cnt}</span></span></a>')

    def lst_neighbour_cards(self, lk, n=6):
        """Fallback when the state has no lake with a current reading: the most searched ones in neighboring states."""
        near = set()
        for c in lk.get("states") or [lk.get("state")]:
            near |= set((self.states.get(c) or {}).get("neighbours") or [])
        out = []
        for o in self.top_lakes(len(self.live)):
            if near & set(o.get("states") or [o.get("state")]):
                ov = self.eff.get(o["id"]) or {}
                if ov.get("freshness") != "unknown" and ov.get("elev_ft") is not None:
                    out.append(o)
        return '<ul class="lkcs lkcs--nb">' + "".join(self.home_card(o, None, True) for o in out[:n]) + "</ul>" if out else ""

    # ------------------------------------------------------------ hubs (state, system and operator pages)
    def state_page(self, code):
        st = self.states[code]
        lakes = sorted(self.hub_lakes[code], key=lambda lk: self.hub_sort(lk["name"]))
        live = [lk for lk in lakes if lk["id"] in self.live_ids]
        prof = [lk for lk in lakes if lk["id"] not in self.live_ids]
        published = len(live) >= 3
        word = "reservoir" if code in STATE_RESERVOIR_WORD else "lake"
        url = f"/{st['slug']}/"
        crumbs = [("Home", "/"), (f"{st['name']} lake levels", url)]
        Word = word.capitalize()
        title = f"{st['name']} {Word} Levels Today: All {len(live)} Lakes & Reservoirs"
        if len(title) > 65:
            title = f"{st['name']} {Word} Levels Today: {len(live)} Lakes"
        if len(live) < 2:      # unpublished (noindex) hubs: no "All 0 Lakes" or "All 1 Lakes" in the tab
            title = f"{st['name']} {Word} Levels Today | {BRAND}"
        srcs = sorted({self.src_of(lk).get("agency", "") for lk in live})
        shorts = sorted({SHORT_SOURCE.get(self.src_of(lk).get("id"), "") for lk in live} - {""})
        desc = (f"{st['name']} lake levels today: {len(live)} lakes and reservoirs with feet from full pool, percent full and "
                f"daily change. Data: {', '.join(shorts) or 'agency gauges'}.")
        if len(desc) > 160:
            desc = (f"{st['name']} lake levels today: {len(live)} lakes and reservoirs with feet from full pool, percent full "
                    f"and daily change, each with its reading date.")
        if len(live) == 1:
            desc = (f"Current level for the {st['name']} lake we track with a data feed: feet above or below full pool, percent full "
                    f"and daily change, from {', '.join(s for s in srcs if s) or 'agency gauges'}.")
        elif not live:
            desc = f"{st['name']} lakes we list, who runs them, and lake levels in neighboring states."
        nm = E(st["name"])
        views = [self.view(lk) for lk in live]
        cur = [v for v in views if v["fresh"] != "unknown" and v["elev"] is not None]
        # combined storage, only when every reservoir counted reports through the same source
        aside = [self.hub_bands(views, f"Where {len(cur)} {nm} {'lake stands' if len(cur) == 1 else 'lakes stand'} today", 3)]
        same = [v for v in cur if v["e"].get("conservation_storage_af") is not None and v["lk"].get("capacity_af")]
        if len(same) >= 3 and len({v["src"].get("id") for v in same}) == 1:
            cs = sum(v["e"]["conservation_storage_af"] for v in same)
            cap = sum(v["lk"]["capacity_af"] for v in same)
            aside.append(self.hub_storage(cs, cap, f'Combined conservation storage of the {len(same)} {nm} reservoirs on this page with storage data: '
                                                   f'{acre_feet(cs)} of {acre_feet(cap)} acre-feet ({E(same[0]["short"])} data, '
                                                   f'{self.hub_span([v.get("rdate") for v in same], "readings for ")}).'))
        sys_here = [s for k, s in self.sys_built.items() if any(self.lake_by_id.get(m, {}).get("state") == code for m in s.get("members", []))]
        ops = defaultdict(int)
        for lk in lakes:
            if lk.get("operator") in self.operators:
                ops[lk["operator"]] += 1
        ops_built = [o for o in sorted(ops, key=lambda o: (-ops[o], self.operators[o]["name"])) if f"/operator/{o}/" in self.planned]
        nolive = []
        for lk in prof:
            op = self.operators.get(lk.get("operator"))
            nolive.append(f'<li>{self.a(self.lake_url(lk), lk["name"])}' + (f'<small>{E(op["name"])}</small>' if op else "") + "</li>")
        for lk in self.all_lakes:
            if code in (lk.get("states") or [lk.get("state")]) and lk.get("page") in (None, "none") and lk.get("official_url"):
                nolive.append(f'<li>{E(lk["name"])}<small>{self.ext(lk["official_url"], "operator’s level page")}</small></li>')
        intro = self.fragment("states", code.lower())
        blocks, jump = self.hub_lake_blocks(live, st["name"], f"t-{code.lower()}", show_state=False)
        low = self.ranked(live, "lowest")
        jump += [("now", "Right now")] + ([("systems", "Systems")] if sys_here else []) + ([("nofeed", "No data feed")] if nolive else []) \
            + ([("about", "About the data")] if intro else [])
        if live:
            kick, summary = f'{len(live)} {"lake" if len(live) == 1 else "lakes"} with a data feed', self.hub_summary(views, cur, "lakes and reservoirs")
        else:
            kick = f'{len(prof)} {"lake" if len(prof) == 1 else "lakes"} listed'
            summary = (f'{"The" if len(prof) != 1 else "The one"} {nm} {"lake" if len(prof) == 1 else "lakes"} on this site '
                       f'{"has" if len(prof) == 1 else "have"} no public data feed we can check automatically. Each lake page links to the '
                       'operator’s own level page where we know it.')
        show_now = len(live) >= 3 and any(self.ranked(live, h) for h in ("lowest", "fullest", "rising", "falling"))
        if not show_now:
            jump = [j for j in jump if j[0] != "now"]
        head = self.hub_head(f"{nm} {word} levels", f'{self.ico("map")}{nm} · {kick}', summary, aside, jump)
        parts = [head] + blocks
        lists = "".join(self.hub_rank(r, how) for how, r in ((h, self.ranked(live, h)) for h in ("lowest", "fullest", "rising", "falling")) if r)
        if show_now and lists:
            parts.append(self.sec("now", "bars", f"{nm} lakes right now",
                                  "Ranked from current readings only. Each lake shows its reading date.", f'<div class="hb-rks">{lists}</div>'))
        if sys_here:
            parts.append(self.sec("systems", "layers", "Reservoir systems", "Reservoirs that are run together, with combined storage where every one reports it.",
                                  self.hub_systems(sys_here)))
        if ops_built:
            chips = "".join(self.a(f"/operator/{o}/", f'{E(self.operators[o]["name"])} <small>{ops[o]}</small>', raw=True) for o in ops_built)
            parts.append(self.sec("operators", "org", f"Who runs {nm} lakes", "Operators of the lakes on this page, with the number of lakes each runs here.",
                                  f'<p class="chips-l hb-ch">{chips}</p>'))
        if nolive:
            parts.append(self.sec("nofeed", "info", "Lakes without a data feed",
                                  "We have not found a public feed we can check automatically for these lakes. Where we know the operator’s level page, "
                                  "the lake page links to it.", f'<ul class="hb-nf">{"".join(nolive)}</ul>'))
        if intro:
            parts.append(self.sec("about", "book", f"About {nm} lake data", "", f'<div class="intro hb-ab">{intro}</div>'))
        nbs = []
        for n in st.get("neighbours", []):
            if n in self.states:
                k = sum(1 for lk in self.hub_lakes.get(n, []) if lk["id"] in self.live_ids)
                nbs.append(self.a(self.state_url(n), f'{E(self.states[n]["name"])}' + (f" <small>{k}</small>" if k else ""), raw=True))
        if nbs:
            parts.append(self.sec("nearby", "map", "Neighboring states", "Lake levels across the state line; the number is lakes with a data feed.",
                                  f'<p class="chips-l hb-ch">{"".join(nbs)}</p>'))
        graph = []
        qa = [(f"How many {st['name']} lakes are on this page?",
               (f"{len(live)} lakes and reservoirs with a data feed" + (f", plus {len(prof)} without one" if prof else "") + ".") if live
               else f"{len(prof)} {'lake' if len(prof) == 1 else 'lakes'} without a data feed.")]
        if low:
            v0 = low[0]
            qa.append((f"Which {st['name']} lake is the lowest right now?",
                       f"Of the lakes with a current reading, {E(v0['lk']['name'])} is the lowest at {pct_txt(v0['pct'])} full "
                       f"({num(v0['elev'])} ft, reading for {E(v0.get('when_long', ''))})."))
        if live:
            qa.append(("Where do these levels come from?",
                       f"From {E(', '.join(s for s in srcs if s) or 'agency gauges')}. Each row shows its own reading date; "
                       f"see {self.a('/sources/', 'data sources and methods')}."))
        parts.append(self.sec("faq", "help", "Questions", "", f'<div class="hb-fq">{self.faq_html(qa, graph)}</div>'))
        body = "{{sprite}}" + self.crumbs_html(crumbs) + "".join(parts)
        self.write(url, title, desc, self.hub_with_sprite(body), crumbs=crumbs, lastmod=self.lastmod_of(live), group="hubs", noindex=not published,
                   sitemap=published, graph=graph, kind="hub", body_class='class="pg-hub"', css="hub")

    def operator_page(self, slug):
        # the FAQ here is visible only (no FAQPage JSON-LD): one trivial question earns no rich result
        op = self.operators[slug]
        lakes = sorted([lk for lk in self.live + self.profiles if lk.get("operator") == slug], key=lambda lk: self.hub_sort(lk["name"]))
        live = [lk for lk in lakes if lk["id"] in self.live_ids]
        prof = [lk for lk in lakes if lk["id"] not in self.live_ids]
        url = f"/operator/{slug}/"
        short = op.get("short") or op["name"]
        # "USACE SWT" is an office code nobody searches; the district name is what people type
        nm = op["name"] if short.startswith("USACE ") else short
        crumbs = [("Home", "/"), (f"{nm} lake levels", url)]
        views = [self.view(lk) for lk in live]
        cur = [v for v in views if v["fresh"] != "unknown" and v["elev"] is not None]
        lead = (f'The {E(op["name"])} manages {len(lakes)} of the lakes on {E(BRAND)}. '
                + ("It publishes lake levels that we read and date automatically."
                   if op.get("has_public_feed") else "We read these lakes’ levels from the public data sources named on each lake page."))
        states = sorted({s for lk in lakes for s in (lk.get("states") or [lk.get("state")]) if s in self.states},
                        key=lambda s: self.states[s]["name"])
        facts = [("With a data feed", str(len(live)))] + ([("Without a feed", str(len(prof)))] if prof else [])
        if states:
            facts.append(("State" if len(states) == 1 else "States",
                          ", ".join(self.a(self.state_url(s), self.states[s]["name"]) for s in states)))
        aside = [self.hub_facts(facts), self.hub_bands(views, f"Where {len(cur)} {E(nm)} {'lake stands' if len(cur) == 1 else 'lakes stand'} today", 4)]
        sysops = [s for s in self.sys_built.values() if s.get("operator") == slug]
        intro = self.fragment("operators", slug)
        blocks, jump = self.hub_lake_blocks(live, nm, "t-op", show_state=len(states) > 1, word="lakes we track")
        if intro or op.get("note") or op.get("official_url"):
            jump.append(("about", "About"))
        if sysops:
            jump.append(("systems", "Systems"))
        if prof:
            jump.append(("nofeed", "No data feed"))
        kicker = f'{self.ico("org")}Lake operator' + (f" · {E(short)}" if short != nm else "")
        head = self.hub_head(f"{E(nm)} lake levels", kicker, lead, aside, jump)
        parts = [head] + blocks
        about = []
        if intro:
            about.append(f'<div class="intro">{intro}</div>')
        if op.get("note"):
            about.append(f'<p class="hb-note">{self.ico("info")}<span>{E(op["note"])}</span></p>')
        if op.get("official_url"):
            about.append(f'<p class="hb-go">{self.ext(op["official_url"], op["name"] + " website", "btn ext")}</p>')
        if about:
            parts.append(self.sec("about", "org", f"About the {E(op['name'])}", "", f'<div class="hb-ab">{"".join(about)}</div>'))
        if sysops:
            parts.append(self.sec("systems", "layers", f"{E(nm)} reservoir systems", "Reservoirs it runs together, with combined storage where every one reports it.",
                                  self.hub_systems(sysops)))
        if prof:
            parts.append(self.sec("nofeed", "info", "Without a data feed", "We have not found a public feed we can check automatically for these lakes.",
                                  '<ul class="hb-nf">' + "".join(f'<li>{self.a(self.lake_url(lk), lk["name"])}</li>' for lk in prof) + "</ul>"))
        graph = []
        qa = [(f"Is {E(BRAND)} part of the {E(op['name'])}?",
               f"No. {E(BRAND)} is independent and is not a government website. For decisions, use the operator’s own notices.")]
        parts.append(self.sec("faq", "help", "Questions", "", f'<div class="hb-fq">{self.faq_html(qa, [])}</div>'))
        body = "{{sprite}}" + self.crumbs_html(crumbs) + "".join(parts)
        thin = not live or (len(live) < 3 and slug not in self.SEARCHED_OPERATORS)   # same rule as state hubs
        self.write(url, f"{nm} Lake Levels Today | {BRAND}" if len(nm) < 30 else f"{nm} Lake Levels Today",
                   f"Current levels for the {op['name']} lakes we track, with feet above or below full pool and the reading date.",
                   self.hub_with_sprite(body), crumbs=crumbs, lastmod=self.lastmod_of(live), group="hubs", graph=graph, kind="hub",
                   noindex=thin, sitemap=not thin, body_class='class="pg-hub"', css="hub")

    # operators people search by name keep an indexable page even with one or two lakes
    SEARCHED_OPERATORS = {"lcra", "grda", "tva", "duke-energy"}

    def system_page(self, slug):
        s = self.sys_built[slug]
        members = [self.lake_by_id[m] for m in s.get("members", []) if m in self.live_ids]
        url = f"/system/{slug}/"
        crumbs = [("Home", "/"), (s["name"], url)]
        nums = self.system_numbers(s)
        views = [self.view(lk) for lk in members]
        cur = [v for v in views if v["fresh"] != "unknown" and v["elev"] is not None]
        op = self.operators.get(s.get("operator"))
        aside = []
        if nums:
            ds = sorted({d for d in nums["dates"] if d})
            when = d_med(ds[-1]) if len(ds) == 1 else f"{d_med(ds[0])} to {d_med(ds[-1])}"
            aside.append(self.hub_storage(nums["storage"], nums["capacity"],
                                          f'Combined conservation storage of {E(self.combined_label(s))}: {acre_feet(nums["storage"])} of '
                                          f'{acre_feet(nums["capacity"])} acre-feet (readings for {E(when)}).', pct=nums["pct"]))
            if self.combined_ids(s) == s.get("members", []):
                lead = (f'{len(members)} reservoirs we track, run as one system. Together they hold <b>{nums["pct"]:.1f}%</b> of their combined '
                        f'conservation capacity, from readings for {E(when)}.')
            else:
                lead = (f'{len(members)} reservoirs we track, run as one system. {E(s.get("combined_note", ""))} Together '
                        f'{E(self.combined_label(s))} hold <b>{nums["pct"]:.1f}%</b> of their combined conservation capacity, '
                        f'from readings for {E(when)}.')
        else:
            lead = ('A combined figure is shown only when every reservoir in the system has a current reading with storage data. '
                    'One or more is missing right now; the cards below show each lake.')
        aside.append(self.hub_bands(views, f"Where the {len(cur)} reservoirs stand today", 4))
        kicker = f'{self.ico("layers")}Reservoir system' + (" · " + self.a(f'/operator/{op["slug"]}/', op.get("short") or op["name"]) if op else "")
        jump = [("members", "Reservoirs")]
        ch = self.system_chart(s, members)
        if ch:
            jump.append(("trend", "12-month storage"))
        intro = self.fragment("systems", slug)
        if intro:
            jump.append(("about", "About the system"))
        head = self.hub_head(f"{E(s['name'])} water levels", kicker, lead, aside, jump)
        parts = [head]
        cards = "".join(self.hub_card(lk) for lk in members)
        parts.append(self.sec("members", "drop", "Reservoirs in this system", "Each reservoir’s latest level with its reading date and last 30 days.",
                              f'<ul class="lkcs hb-cs" aria-label="{E(s["name"])}">{cards}</ul>'))
        if ch:
            parts.append(self.sec("trend", "chart", "Combined storage, last 12 months", "", ch))
        if intro:
            parts.append(self.sec("about", "book", f"About the {E(s['name'])}" if not s["name"].lower().startswith("the ") else f"About {E(s['name'])}",
                                  "", f'<div class="intro hb-ab">{intro}</div>'))
        foot = []
        if s.get("source_url"):
            foot.append(f'Source: {self.ext(s["source_url"], "reservoir data")}')
        if op:
            foot.append("Operator: " + self.a(f'/operator/{op["slug"]}/', op["name"]))
        sts = sorted({st for lk in members for st in (lk.get("states") or [lk.get("state")]) if st in self.states}, key=lambda c: self.states[c]["name"])
        if sts:
            foot.append("More lake levels: " + ", ".join(self.a(self.state_url(c), self.states[c]["name"]) for c in sts))
        if foot:
            parts.append(f'<p class="hb-src small">{" · ".join(foot)}</p>')
        body = "{{sprite}}" + self.crumbs_html(crumbs) + "".join(parts)
        if nums:
            title = f"{s['name']}: Combined Storage & Lake Levels Today"
            title = title if len(title) <= 65 else f"{s.get('title_name') or s['name']}: Combined Storage Today"
        else:   # promise combined storage only when the page shows it
            title = f"{s.get('title_name') or s['name']} Water Levels Today | {BRAND}"
            title = title if len(title) <= 65 else f"{s.get('title_name') or s['name']} Water Levels Today"
        self.write(url, title,
                   f"Combined storage and percent full for the {s['name']}, with each reservoir's level and reading date.",
                   self.hub_with_sprite(body), crumbs=crumbs, lastmod=self.lastmod_of(members), group="hubs", kind="hub",
                   body_class='class="pg-hub"', css="hub")

    def system_chart(self, s, members):
        ids = self.combined_ids(s)
        members = [lk for lk in members if lk["id"] in ids]
        if len(members) != len(ids) or not all(lk.get("capacity_af") for lk in members):
            return ""
        by = defaultdict(dict)
        for lk in members:
            for r in self.series.get(lk["id"]) or []:
                if r["pct"] is not None:
                    by[r["date"]][lk["id"]] = r["pct"]
        cap = sum(lk["capacity_af"] for lk in members)
        pts = sorted((d, sum(vals[lk["id"]] * lk["capacity_af"] for lk in members) / cap)
                     for d, vals in by.items() if len(vals) == len(members))
        if not pts:
            return ""
        end = pts[-1][0]
        pts = [p for p in pts if p[0] >= end - dt.timedelta(days=365)]
        if len(pts) < 30:
            return ""
        return charts.line_chart(pts, title=f"{s['name']} combined percent full, last 12 months" if ids == s.get("members", []) else f"{self.combined_label(s)}: combined percent full, last 12 months",
                                 desc=f"Combined percent of conservation capacity, {d_med(pts[0][0])} to {d_med(pts[-1][0])}.",
                                 full_pool=100.0, pool_label="Full", max_gap_days=3, unit="%", height=210,
                                 caption="Percent of combined conservation capacity, on days when every reservoir has a value.")

    # hub helpers (prefix hub_)
    HUB_ICONS = {
        "list": '<path d="M7.5 5h9.5M7.5 10h9.5M7.5 15h9.5"/><path d="M3.2 5h.01M3.2 10h.01M3.2 15h.01" stroke-width="2.6"/>',
        "layers": '<path d="m10 2.5 7.5 4-7.5 4-7.5-4z"/><path d="m2.5 10 7.5 4 7.5-4"/><path d="m2.5 13.5 7.5 4 7.5-4"/>',
        "org": '<path d="M2.5 17.5h15M4.5 17.5V9M8.2 17.5V9M11.8 17.5V9M15.5 17.5V9M2.5 8 10 3l7.5 5z"/>',
    }

    @staticmethod
    def hub_sort(name):
        """A–Z key that files "Lake Travis" under T, the way people look for it."""
        return re.sub(r"^(lake|the)\s+", "", name.lower())

    def hub_with_sprite(self, body):
        """with_sprite(), plus the hub icons this page uses (skipped when the shared sprite already has them)."""
        out = self.with_sprite(body)
        add = "".join(f'<symbol id="i-{k}" viewBox="0 0 20 20">{v}</symbol>' for k, v in self.HUB_ICONS.items()
                      if f'href="#i-{k}"' in out and f'id="i-{k}"' not in out)
        i = out.find("</svg>", out.find('<svg class="sprite"'))
        return out[:i] + add + out[i:] if add and i > 0 else out

    def card_date(self, v):
        """The short reading date on a lake card, as <time> with the full ISO date when the reading has one."""
        d = v.get("rdate")
        txt = E(v.get("when_short", ""))
        return (f'<time class="lkc__d" datetime="{d.isoformat()}">{txt}</time>' if d and txt
                else f'<span class="lkc__d">{txt}</span>')

    def hub_span(self, dates, prefix="dated "):
        ds = sorted({d for d in dates if d})
        if not ds:
            return ""
        return prefix + (d_short(ds[0]) if ds[0] == ds[-1] else f"{d_short(ds[0])} to {d_short(ds[-1])}")

    def hub_head(self, h1, kicker, summary, aside, jump):
        """Hub header: kicker, H1, a one-line summary from the data and jump links; the band picture and storage beside it."""
        side = "".join(x for x in aside if x)
        links = "".join(f'<a href="#{i}">{E(t)}</a>' for i, t in jump)
        return (f'<header class="hb-hd{"" if side else " hb-hd--solo"}"><div class="hb-hd__t"><p class="hb-k">{kicker}</p><h1>{h1}</h1>'
                f'<p class="hb-sum">{summary}</p>' + (f'<nav class="hb-j" aria-label="On this page">{links}</nav>' if len(jump) > 1 else "")
                + "</div>" + (f'<div class="hb-hd__a">{side}</div>' if side else "") + "</header>")

    def hub_summary(self, views, cur, what):
        """One line from the readings on the page: how many are full, near full and low today, and their dates."""
        if not views:
            return "No lake here has a data feed."
        if not cur:
            return f"None of the {len(views)} {what} here has a current reading right now. Each lake page shows its last reading and date."
        if len(cur) == 1:
            v = cur[0]
            band = f': <b>{E(BAND_LABEL[v["band"]].lower())}</b>' if v["badges"] and v["band"] and v["band"] != "level_only" else ""
            out = f'{E(v["lk"]["name"])} is the one lake here with a current reading{band}, {self.hub_span([v.get("rdate")])}.'
            k = len(views) - 1
            return out + (f" {k} more {'has' if k == 1 else 'have'} no current reading." if k else "")
        c = lambda *bs: sum(1 for v in cur if v["badges"] and v["band"] in bs)
        cl = [(n, t) for n, t in ((c("full", "above_full"), "full or above"), (c("near_full"), "near full"), (c("below_full"), "below full"),
                                  (c("low", "very_low"), "low or very low")) if n]
        words = [f"<b>{n}</b> {t}" for n, t in cl]
        if cl:
            words[0] = f'<b>{cl[0][0]}</b> {"is" if cl[0][0] == 1 else "are"} {cl[0][1]}'
            band_txt = (", ".join(words[:-1]) + " and " + words[-1]) if len(words) > 1 else words[0]
            out = f'Of {len(cur)} {what} with a current reading, {band_txt}.'
        else:
            out = f'{len(cur)} {what} have a current reading, with no full-pool mark to compare against.'
        out += f' Readings {self.hub_span([v.get("rdate") for v in cur])}.'
        k = len(views) - len(cur)
        return out + (f" {k} more {'has' if k == 1 else 'have'} no current reading." if k else "")

    def hub_bands(self, views, head, min_n):
        """Band picture for this hub: a stacked bar (decorative) and a counted key (the text), from readings shown with a band."""
        vs = [v for v in views if v["fresh"] != "unknown" and v["elev"] is not None and v["badges"] and v["band"]]
        cnt = {b: sum(1 for v in vs if v["band"] == b) for b in self.BAND_ORDER}
        n = sum(cnt.values())
        if n < min_n:
            return ""
        only = sum(1 for v in vs if v["band"] == "level_only")
        bar = "".join(f'<i class="s-{b}" style="flex-grow:{c}"></i>' for b, c in cnt.items() if c)
        key = "".join(f'<li><i class="s-{b}"></i><b>{c}</b>{E(BAND_LABEL[b])}</li>' for b, c in cnt.items() if c)
        note = (f"From each lake’s latest reading, {self.hub_span([v.get('rdate') for v in vs if v['band'] in cnt])}."
                + (f" {only} more report a level with no full-pool mark." if only else ""))
        return (f'<div class="hb-bd"><p class="hb-bd__h">{head}</p><div class="hb-bar" aria-hidden="true">{bar}</div>'
                f'<ul class="hb-bd__k">{key}</ul><p class="hb-bd__n">{note}</p></div>')

    def hub_storage(self, used, cap, text, pct=None):
        p = pct if pct is not None else 100 * used / cap
        return (f'<div class="hb-st"><p class="hb-st__n"><b>{p:.1f}%</b>combined conservation storage</p>'
                f'<div class="hb-st__bar" aria-hidden="true"><i style="width:{max(0.0, min(100.0, p)):.1f}%"></i></div><p class="hb-st__t">{text}</p></div>')

    def hub_facts(self, facts):
        return '<dl class="hb-f">' + "".join(f'<div><dt>{E(k)}</dt><dd{"" if v.isdigit() else " class=t"}>{v}</dd></div>' for k, v in facts) + "</dl>"

    def hub_card(self, lk):
        """A highlight card (home card look) with the last 30 days drawn under the level."""
        v = self.view(lk)
        ok = v["fresh"] != "unknown" and v["elev"] is not None
        sub = [x for x in ((self.vs_words(v), pct_txt(v["pct"]) + " full" if v["pct"] is not None else "") if ok else ()) if x]
        sp = ""
        if ok:
            s = charts.sparkline(self.last_days(lk["id"], 30), full_pool=v["pool"])
            sp = f'<span class="hb-sp">{s}<small>30 days</small></span>' if s else ""
        return (
            f'<li><a class="lkc" href="{self.lake_url(lk)}"{self.home_stale(v) if ok else ""}>'
            f'<span class="lkc__n">{E(lk["name"])} <span class="lkc__st">{E(self.st_txt(lk))}</span></span>'
            f'<span class="lkc__v">{("<b>" + num(v["elev"]) + "</b> ft") if ok else "Level unknown"}</span>'
            f'<span class="lkc__s">{E(" · ".join(sub)) if sub else ("" if ok else "The source has not updated")}</span>'
            f'{sp}<span class="lkc__f">{self.badge(v["band"], True, True) if ok and v["badges"] else ""}'
            f'{self.home_trend(v) if ok else ""}{self.card_date(v)}</span></a></li>')

    def hub_lake_blocks(self, live, place, lid, show_state=False, word="lakes"):
        """Most searched lakes as cards, then every lake as a compact A–Z list. Six lakes or fewer: cards only.
        Returns (sections, jump links)."""
        nm = E(place)
        if not live:
            return [], []
        if len(live) <= 6:
            cards = "".join(self.hub_card(lk) for lk in live)
            return [self.sec("all", "drop", f"{nm} {word}", "Each lake’s latest level with its reading date and last 30 days.",
                             f'<ul class="lkcs hb-cs">{cards}</ul>')], [("all", f"All {len(live)} {'lake' if len(live) == 1 else 'lakes'}")]
        ids = {lk["id"] for lk in live}
        top = [lk for lk in self.top_lakes(len(self.live)) if lk["id"] in ids
               and (self.eff.get(lk["id"]) or {}).get("freshness") != "unknown" and (self.eff.get(lk["id"]) or {}).get("elev_ft") is not None]
        top = top[:6 if len(live) >= 12 else 3]
        out, jump = [], []
        if top:
            out.append(self.sec("top", "gauge", f"Most searched {nm} lakes", "The lakes people look up most. Each shows its own reading date.",
                                f'<ul class="lkcs hb-cs hb-cs--x">{"".join(self.hub_card(lk) for lk in top)}</ul>'))
            jump.append(("top", "Most searched"))
        out.append(self.sec("all", "list", f"All {nm} {word}", f"{len(live)} lakes with a data feed, A to Z. Choose a lake for its chart, records and source.",
                            self.hub_list(live, f"{place} lakes", lid, show_state)))
        jump.append(("all", f"All {len(live)} lakes"))
        return out, jump

    def hub_list(self, lakes, label, lid, show_state=False):
        """Every lake as a light row list (the homepage long-tail look): the whole row is the lake link; level with its date,
        the 5-step band scale with feet from full pool and percent full, and the 1-day change. A–Z groups from 25 lakes, two
        columns on wide screens. No per-row charts: the page stays small."""
        groups = []
        for lk in lakes:
            v = self.view(lk)
            ok = v["fresh"] != "unknown" and v["elev"] is not None
            sts = lk.get("states") or [lk.get("state")]
            st = f' <small>{E(", ".join(sts))}</small>' if show_state or len(sts) > 1 else ""
            cells = (f'<span>{self.a(self.lake_url(lk), lk["name"])}{st}</span>'
                     f'<span class="lkr__v">{("<b>" + num(v["elev"]) + "</b> ft") if ok else "Level unknown"}</span>'
                     f'<small>{E(v.get("when_short", ""))}</small>')
            if ok:
                band = v["band"] if v["badges"] else None
                sub = [x for x in (f'{signed(v["vs"])} ft' if v["vs"] is not None and v["pool"] is not None else "",
                                   pct_txt(v["pct"]) + " full" if v["pct"] is not None else "") if x]
                cells += (f'<span class="q-{self.HL_BAND.get(band, "lv")}">{E(BAND_LABEL[band]) if band else ""}'
                          + (f'<small>{E(" · ".join(sub))}</small>' if sub else "") + "</span>")
                t = v["trend"] if v["c24"] is not None else None
                cells += (f'<span class="{"u" if t.startswith("rising") else "d" if t.startswith("falling") else "s"}">'
                          f'<span class="vh">{E(TREND_LABEL[t])}, </span>{signed(v["c24"])} ft</span>' if t else "<span></span>")
            hts = ""
            if ok and v.get("ts"):
                hh = _h(v["e"].get("hard_stale_hours", 96))
                hts = f' data-hts="{v["ts"]}"' + (f' data-hh="{hh}"' if str(hh) != "96" else "")     # site.js reads 96 when absent
            letter = self.hub_sort(lk["name"])[:1].upper()
            letter = letter if letter.isalpha() else "#"
            if not groups or groups[-1][0] != letter:
                groups.append((letter, []))
            groups[-1][1].append(f"<li{hts}>{cells}</li>")
        az = len(lakes) >= 25
        if not az:
            groups = [(None, [r for _, rs in groups for r in rs])]
        # two columns on wide screens: split at the group boundary nearest the middle (or mid-list when there are no groups)
        cols = [groups]
        n = len(lakes)
        if n >= 16:
            if az:
                acc, best, cut = 0, None, 1
                for i, (_, rs) in enumerate(groups[:-1]):
                    acc += len(rs)
                    if best is None or abs(n / 2 - acc) < best:
                        best, cut = abs(n / 2 - acc), i + 1
                cols = [groups[:cut], groups[cut:]]
            else:
                rs = groups[0][1]
                cols = [[(None, rs[:(n + 1) // 2])], [(None, rs[(n + 1) // 2:])]]
        head = '<div class="hb-lh" aria-hidden="true"><span>Lake</span><span>Level</span><span>vs full pool</span><span>1 day</span></div>'
        html_cols = []
        for col in cols:
            inner = "".join((f'<h3 id="{lid}-{g.lower() if g != "#" else "n"}">{g}</h3>' if g else "") + f'<ul>{"".join(rs)}</ul>' for g, rs in col)
            html_cols.append(f'<div class="hb-l">{head}{inner}</div>')
        nav = ""
        if az:
            nav = ('<nav class="hb-az" aria-label="Lakes by first letter">'
                   + "".join(f'<a href="#{lid}-{g.lower() if g != "#" else "n"}">{g}</a>' for g, _ in groups) + "</nav>")
        return (f'{nav}<p class="vh">{E(label)}: each row gives the level and reading date, the band with feet from full pool and percent full, '
                f'and the change in one day.</p><div class="hb-cols">{"".join(html_cols)}</div>')

    def hub_rank(self, vs, how):
        icon, title, path, more = {"lowest": ("low", "Lowest", "/lowest-lakes/", "Lowest in the US"),
                                   "fullest": ("full", "Fullest", "/fullest-lakes/", "Fullest in the US"),
                                   "rising": ("up", "Rising fastest", "/rising-fastest/", "Rising fastest in the US"),
                                   "falling": ("down", "Falling fastest", "/falling-fastest/", "Falling fastest in the US")}[how]
        if vs:
            rows = "".join(
                f'<li>{self.a(self.lake_url(v["lk"]), v["lk"]["name"])}'
                f'<b>{pct_txt(v["pct"]) + " full" if how in ("lowest", "fullest") else signed(v["c24"]) + " ft"}</b>'
                f'<small>{E(v.get("when_short", ""))}</small></li>' for v in vs[:5])
            lst = f"<ol>{rows}</ol>"
        else:
            lst = '<p class="muted hb-none">No lake here has a current reading for this list right now.</p>'
        return (f'<div class="hb-rk hb-rk--{how}"><h3>{self.ico(icon, "ic--b")}{title}</h3>{lst}'
                f'<p class="hb-more">{self.a(path, more, "more__a")}</p></div>')

    def hub_systems(self, systems):
        out = []
        for s in systems:
            n = sum(1 for m in s.get("members", []) if m in self.live_ids)
            op = self.operators.get(s.get("operator"))
            nums = self.system_numbers(s)
            fig = ""
            if nums:
                fig = (f'<p class="hb-sy__v"><b>{nums["pct"]:.1f}%</b> combined storage <small>{self.hub_span(nums["dates"], "readings for ")}</small></p>'
                       f'<div class="hb-st__bar" aria-hidden="true"><i style="width:{max(0.0, min(100.0, nums["pct"])):.1f}%"></i></div>')
            out.append(f'<li>{self.a("/system/" + s["slug"] + "/", s["name"])}<p>{n} reservoir{"s" if n != 1 else ""} we track'
                       + (f' · {E(op.get("short") or op["name"])}' if op else "") + f"</p>{fig}</li>")
        return f'<ul class="hb-sy">{"".join(out)}</ul>'

    def list_page(self, how):
        path = {"lowest": "/lowest-lakes/", "fullest": "/fullest-lakes/", "rising": "/rising-fastest/", "falling": "/falling-fastest/"}[how]
        title = {"lowest": "Lowest Lakes in the US Right Now (Percent Full)", "fullest": "Fullest Lakes in the US Right Now (Percent Full)",
                 "rising": "Lakes Rising Fastest in the US Right Now", "falling": "Lakes Falling Fastest in the US Right Now"}[how]
        rule = {"lowest": "ranked by percent full, lowest first", "fullest": "ranked by percent full, fullest first (ties by feet above full pool)",
                "rising": "ranked by the change since the previous day’s reading, largest rise first",
                "falling": "ranked by the change since the previous day’s reading, largest fall first"}[how]
        allv = self.ranked(self.live, how)
        vs = allv[:LIST_TOP]
        crumbs = [("Home", "/"), (title.split(" in the US")[0], path)]
        made = self.now.astimezone(ZoneInfo("America/Chicago"))
        dates = sorted(v["rdate"] for v in vs if v.get("rdate"))
        span = (f" (readings dated {d_short(dates[0])}" + (f" to {d_short(dates[-1])}" if dates[-1] != dates[0] else "") + ")") if dates else ""
        count = (f"Showing the top {len(vs)} of {len(allv):,} lakes that qualify." if len(allv) > len(vs)
                 else f"{len(vs)} lake{'s' if len(vs) != 1 else ''} qualif{'y' if len(vs) != 1 else 'ies'} right now." if vs else "")
        head = (f'<header class="lst-h"><p class="lst-k">{self.ico(self.LST_LISTS[how][1])}Ranked list</p><h1>{E(title)}</h1>'
                f'<p class="lead">Top {LIST_TOP} lakes with a data feed and a current reading, {rule}.</p>'
                f'<p class="lst-m">{self.ico("cal")}<span>List made <time datetime="{made.date().isoformat()}">{E(d_long(made))}</time>; '
                f'each lake shows its own reading date{span}. {count}</span></p></header>')
        tabs = ('<nav class="lst-tabs" aria-label="Lake lists"><ul>'
                + "".join(f'<li><a href="{p}"' + (' aria-current="page"' if p == path else "") + f">{self.ico(ic)}{E(lab)}</a></li>"
                          for p, ic, lab in self.LST_LISTS.values()) + "</ul></nav>")
        if vs:
            pod = '<ol class="lst-pod">' + "".join(self.lst_card(v, i, how) for i, v in enumerate(vs[:3], 1)) + "</ol>"
            rest = self.lst_rows(vs[3:], how, 4) if len(vs) > 3 else ""
            main = pod + rest
        else:
            main = '<p class="lst-none">No lake has a current reading for this list right now.</p>'
        states = "".join(self.a(self.state_url(c), self.states[c]["name"]) for c in sorted(self.hub_lakes, key=lambda c: self.states[c]["name"]))
        body = ("{{sprite}}" + self.crumbs_html(crumbs) + f'<div class="lst-w lst--{self.LST_LISTS[how][1]}">{head}{tabs}{main}'
                + self.sec("by-state", "map", "By state", "Every lake we track, state by state.", f'<p class="chips-l">{states}</p>')
                + "</div>")
        graph = [{"@type": "ItemList", "name": title, "numberOfItems": len(vs),
                  "itemListElement": [{"@type": "ListItem", "position": i, "url": SITE_URL + self.lake_url(v["lk"]), "name": v["lk"]["name"]}
                                      for i, v in enumerate(vs, 1)]}] if vs else []
        self.write(path, title, f"Lakes and reservoirs {rule}, from public agency gauge data. Each lake shows its reading date.",
                   self.with_sprite(body), crumbs=crumbs, lastmod=self.lastmod_of([v["lk"] for v in vs]), group="hubs", kind="list",
                   graph=graph, body_class='class="pg-lst pg-rank"', css="list")

    # ranked-list helpers (prefix lst_)
    LST_LISTS = {"lowest": ("/lowest-lakes/", "low", "Lowest"), "fullest": ("/fullest-lakes/", "full", "Fullest"),
                 "rising": ("/rising-fastest/", "up", "Rising fastest"), "falling": ("/falling-fastest/", "down", "Falling fastest")}

    def lst_key(self, v, how):
        """(ranked value, its unit, what it means) for one lake on one list."""
        if how in ("lowest", "fullest"):
            return pct_txt(v["pct"]), "full", self.vs_words(v)
        return signed(v["c24"]), "ft", "since the day before" if v["daily"] else "in 24 hours"

    def lst_card(self, v, i, how):
        """Top-3 card: rank, lake, the ranked value large, band, level and the reading's own date. The card is the link."""
        lk = v["lk"]
        key, unit, sub = self.lst_key(v, how)
        if how in ("lowest", "fullest"):
            viz = (f'<span class="meter lst-x" aria-hidden="true"><i style="width:{max(0, min(100, v["pct"])):.1f}%"></i></span>'
                   if v["pct"] is not None else "")
        else:
            pts = self.last_days(lk["id"], 14)
            sp = charts.sparkline(pts) if len(pts) > 2 else ""
            viz = f'<span class="lst-c__sp lst-x">{sp}<small>14 days</small></span>' if sp else ""
            if v["pct"] is not None:
                sub += f" · {pct_txt(v['pct'])} full"
        return (f'<li><a class="lst-c{" lst-1" if i == 1 else ""}" href="{self.lake_url(lk)}"{self.home_stale(v)}>'
                f'<span class="lst-c__r" aria-hidden="true">{i}</span>'
                f'<span class="lst-c__n">{E(lk["name"])} <small>{E(self.st_txt(lk))}</small></span>'
                f'<span class="lst-c__v lst-x"><b>{key}</b> {unit}</span>'
                f'<span class="lst-c__s lst-x">{E(sub)}</span>{viz}'
                f'<span class="lst-c__f">{self.badge(v["band"], True, True) if v["badges"] else ""}'
                f'<span class="lkr__v">{num(v["elev"])} ft</span>'
                f'<time datetime="{v.get("time_attr", "")}">{E(v.get("when_short", ""))}</time></span></a></li>')

    def lst_rows(self, vs, how, start):
        """Ranks 4 and down in the homepage popular-lakes style: an ordered list, the whole row is the lake link."""
        pctl = how in ("lowest", "fullest")
        rows = []
        for i, v in enumerate(vs, start):
            lk = v["lk"]
            key, unit, _ = self.lst_key(v, how)
            band = v["band"] if v["badges"] else None
            sub = [x for x in (f'{signed(v["vs"])} ft' if v["vs"] is not None and v["pool"] is not None else "",
                               "" if pctl or v["pct"] is None else pct_txt(v["pct"]) + " full") if x]
            if pctl:
                k = f'<span class="lst-v lst-x">{key}<small> full</small></span>'
            else:
                t = v["trend"] or ("rising" if v["c24"] > 0 else "falling")      # the arrow follows the sign of the change
                k = (f'<span class="lst-v lst-x {"u" if v["c24"] > 0 else "d"}"><span class="vh">{E(TREND_LABEL.get(t, ""))}, </span>'
                     f'{key}<small> ft</small></span>')
            rows.append(f'<li{self.home_stale(v)}><span class="lst-n" aria-hidden="true">{i}</span>'
                        f'<span class="lst-nm"><a href="{self.lake_url(lk)}">{E(lk["name"])}</a> <small>{E(self.st_txt(lk)).replace(", ", ",&nbsp;")}</small></span>'
                        f'{k}<span class="lst-lv"><span class="lkr__v"><b>{num(v["elev"])}</b> ft</span>'
                        f'<time datetime="{v.get("time_attr", "")}">{E(v.get("when_short", ""))}</time></span>'
                        f'<span class="q-{self.HL_BAND.get(band, "lv")}">{E(BAND_LABEL[band]) if band else ""}'
                        + (f'<small>{E(" · ".join(sub))}</small>' if sub else "") + "</span></li>")
        keyh = "% full" if pctl else "1 day"
        return (f'<div class="tw lst-l"><div class="lst-l__h" aria-hidden="true"><span>#</span><span>Lake</span><span>{keyh}</span>'
                '<span>Level</span><span>vs full pool</span></div>'
                f'<ol start="{start}" aria-label="Ranks {start} to {start + len(vs) - 1}: lake, {"percent full" if pctl else "change in feet"}, '
                f'level, reading date, band">{"".join(rows)}</ol></div>')

    # ------------------------------------------------------------ home
    # One inline sprite; every icon is 20x20, stroke 1.8, round caps, currentColor.
    HOME_ICONS = {
        "search": '<circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4.5 4.5"/>',
        "loc": '<circle cx="10" cy="10" r="4.5"/><path d="M10 2v3M10 15v3M2 10h3M15 10h3"/>',
        "gauge": '<path d="M5 2.5v15M5 5h3M5 8.5h2M5 12h3M5 15.5h2"/><path d="M10.5 12.5c1.2-1 2.3-1 3.5 0s2.3 1 3.5 0"/><path d="M10.5 16c1.2-1 2.3-1 3.5 0s2.3 1 3.5 0"/>',
        "up": '<path d="m2.5 14 5-5 3.5 3.5 6.5-6.5"/><path d="M12.5 6h5v5"/>',
        "down": '<path d="m2.5 6 5 5L11 7.5l6.5 6.5"/><path d="M12.5 14h5V9"/>',
        "flat": '<path d="M3 10h13.5"/><path d="m13 6 4 4-4 4"/>',
        "map": '<path d="m2.5 5 5-2 5 2 5-2v12l-5 2-5-2-5 2z"/><path d="M7.5 3v12M12.5 5v12"/>',
        "bars": '<path d="M3 17V9.500M8 17V3.500M13 17v-6M18 17V7"/>',
        "info": '<circle cx="10" cy="10" r="7.5"/><path d="M10 9v5"/><path d="M10 6.200h.01" stroke-width="2.6"/>',
        "shield": '<path d="M10 2.5 4 5v4.500c0 3.7 2.5 6.7 6 8 3.5-1.3 6-4.3 6-8V5z"/><path d="m7.3 10 2 2 3.5-3.8"/>',
        "low": '<path d="M4 3.500h12v13H4z"/><path d="M4 13.500c2-1 4-1 6 0s4 1 6 0"/>',
        "full": '<path d="M4 3.500h12v13H4z"/><path d="M4 7c2-1 4-1 6 0s4 1 6 0"/><path d="M7 11h6M7 14h6" stroke-width="1.2"/>',
        "book": '<path d="M10 5.500C8.5 4.3 6 3.8 3 4v11.500c3-.2 5.5.3 7 1.5 1.5-1.2 4-1.7 7-1.500V4c-3-.2-5.5.3-7 1.500zM10 5.500V17"/>',
        "drop": '<path d="M10 2.500c3 3.6 5 6.2 5 9a5 5 0 0 1-10 0c0-2.8 2-5.4 5-9z"/>',
        "chart": '<path d="M3 3v14h14"/><path d="m6 13 3.5-4 3 2.5L17 6"/>',
        "cal": '<rect x="3" y="4.5" width="14" height="12.5" rx="2"/><path d="M3 8.5h14M7 2.5v4M13 2.5v4"/>',
        "flag": '<path d="M5 17.5V3M5 4h9.5L13 7.2l1.5 3.3H5"/>',
        "link": '<path d="M8.5 11.5a3.5 3.5 0 0 0 5 0L16 9a3.5 3.5 0 0 0-5-5l-1 1"/><path d="M11.5 8.5a3.5 3.5 0 0 0-5 0L4 11a3.5 3.5 0 0 0 5 5l1-1"/>',
        "clock": '<circle cx="10" cy="10" r="7.5"/><path d="M10 5.5V10l3 2"/>',
        "dl": '<path d="M10 3v10M6 9.5l4 4 4-4M4 17h12"/>',
        "help": '<circle cx="10" cy="10" r="7.5"/><path d="M7.8 7.800a2.300 2.300 0 1 1 3.400 2c-.8.5-1.200 1-1.200 1.900"/><path d="M10 14.300h.01" stroke-width="2.6"/>',
    }
    SOURCE_WORD = {"usgs": "USGS", "cwms": "Army Corps", "rise": "Reclamation", "twdb": "TWDB", "cdec": "CDEC"}
    # US tile map: state code -> (column, row) on a 12 x 8 grid
    TILES = {"AK": (1, 1), "ME": (12, 1), "WI": (7, 2), "VT": (11, 2), "NH": (12, 2),
             "WA": (2, 3), "ID": (3, 3), "MT": (4, 3), "ND": (5, 3), "MN": (6, 3), "IL": (7, 3), "MI": (8, 3), "NY": (10, 3), "MA": (11, 3),
             "OR": (2, 4), "NV": (3, 4), "WY": (4, 4), "SD": (5, 4), "IA": (6, 4), "IN": (7, 4), "OH": (8, 4), "PA": (9, 4), "NJ": (10, 4),
             "CT": (11, 4), "RI": (12, 4),
             "CA": (2, 5), "UT": (3, 5), "CO": (4, 5), "NE": (5, 5), "MO": (6, 5), "KY": (7, 5), "WV": (8, 5), "VA": (9, 5), "MD": (10, 5),
             "DE": (11, 5),
             "AZ": (3, 6), "NM": (4, 6), "KS": (5, 6), "AR": (6, 6), "TN": (7, 6), "NC": (8, 6), "SC": (9, 6),
             "OK": (5, 7), "LA": (6, 7), "MS": (7, 7), "AL": (8, 7), "GA": (9, 7), "HI": (1, 8), "TX": (5, 8), "FL": (10, 8)}
    BAND_ORDER = ("above_full", "full", "near_full", "below_full", "low", "very_low")

    @staticmethod
    def ico(name, cls=""):
        return f'<svg class="ic{(" " + cls) if cls else ""}" aria-hidden="true"><use href="#i-{name}"/></svg>'

    def home_sprite(self, used):
        syms = "".join(f'<symbol id="i-{k}" viewBox="0 0 20 20">{v}</symbol>' for k, v in self.HOME_ICONS.items() if k in used)
        return ('<svg class="sprite" aria-hidden="true" width="0" height="0" fill="none" stroke="currentColor" stroke-width="1.8" '
                f'stroke-linecap="round" stroke-linejoin="round">{syms}</svg>')

    def top_lakes(self, n):
        """Lakes with a data feed, most searched first."""
        return sorted(self.live, key=lambda lk: -((self.demand.get(lk["id"]) or {}).get("volume") or lk.get("demand") or 0))[:n]

    def source_facts(self):
        """(check interval text, source ids, agency names) for the sources that lakes with a data feed actually use."""
        used = {}
        for lk in self.live:
            s = self.src_of(lk)
            if s.get("id"):
                used[s["id"]] = used.get(s["id"], 0) + 1
        ids = sorted(used, key=lambda k: -used[k])
        checks = sorted({self.sources[k].get("check_every_hours") for k in ids if self.sources[k].get("check_every_hours")})
        txt = (f"{checks[0]}–{checks[-1]} hours" if len(checks) > 1 else
               (f"{checks[0]} hour{'s' if checks[0] != 1 else ''}" if checks else ""))
        return txt, ids, [self.sources[k].get("agency", k) for k in ids]

    def sec_head(self, icon, title, sub=""):
        return (f'<header class="sh">{self.ico(icon, "ic--h")}<div><h2>{title}</h2>' + (f"<p>{sub}</p>" if sub else "") + "</div></header>")

    def home_stale(self, v):
        """Attributes the browser uses to stop showing a reading as current once it passes the hard limit (site.js)."""
        if not v.get("ts"):
            return ""
        return f' data-hts="{v["ts"]}" data-hh="{_h(v["e"].get("hard_stale_hours", 96))}"'

    def home_trend(self, v):
        t = v["trend"]
        if not t or v["c24"] is None:
            return ""
        icon = "up" if t.startswith("rising") else ("down" if t.startswith("falling") else "flat")
        return (f'<span class="tr">{self.ico(icon)}<span class="vh">{E(TREND_LABEL[t])}: </span>{signed(v["c24"])} ft'
                f'<span class="vh"> {"since the day before" if v["daily"] else "in 24 hours"}</span></span>')

    @staticmethod
    def st_txt(lk):
        return ", ".join(lk.get("states") or [lk.get("state")])

    def home_feature(self, top):
        """Hero gauge: the most searched lake that has a current reading and a percent-full value. No lake, no gauge."""
        for lk in top:
            v = self.view(lk)
            if v["fresh"] == "current" and v["elev"] is not None and v["pct"] is not None and v["badges"] and v["pool"] is not None:
                break
        else:
            return "", None
        fill = max(2.0, min(118.0, v["pct"]))          # 100 = the full-pool line; the tank leaves room above it
        return (
            f'<a class="hg{" hg--lo" if fill < 34 else " hg--hi" if fill >= 90 else ""}" href="{self.lake_url(lk)}"{self.home_stale(v)} style="--p:{fill:.1f}">'
            f'<span class="hg__k">{self.ico("gauge")}Most searched lake</span>'
            f'<span class="hg__top"><span class="hg__n">{E(lk["name"])} <span class="hg__st">{E(self.st_txt(lk))}</span></span>'
            f'{self.badge(v["band"], True)}</span>'
            f'<span class="hg__v"><b>{num(v["elev"])}</b> ft</span>'
            f'<span class="hg__s">{E(self.vs_words(v))}{self.home_trend(v)}</span>'
            '<span class="hg__tank" aria-hidden="true"><span class="hg__ticks"></span>'
            f'<span class="hg__pool"><i>Full pool {pool_txt(v["pool"])} ft</i></span>'
            '<span class="hg__lvl"><span class="hg__rise"><span class="hg__w hg__w--b"></span><span class="hg__w"></span>'
            f'<span class="hg__mk">{pct_txt(v["pct"])} full</span></span></span></span>'
            f'<span class="hg__d">{v["asof"]}</span></a>'), lk["id"]

    def home_card(self, lk, tag=None, use=False):
        v = self.view(lk)
        ok = v["fresh"] != "unknown" and v["elev"] is not None
        sub = [x for x in ((self.vs_words(v), pct_txt(v["pct"]) + " full" if v["pct"] is not None else "") if ok else ()) if x]
        meter = ""
        if ok and v["pct"] is not None:
            meter = f'<span class="meter meter--s" aria-hidden="true"><i style="width:{max(0, min(100, v["pct"])):.1f}%"></i></span>'
        return (
            f'<li><a class="lkc" href="{self.lake_url(lk)}"{self.home_stale(v) if ok else ""}>'
            f'<span class="lkc__n">{E(lk["name"])} <span class="lkc__st">{E(tag or self.st_txt(lk))}</span></span>'
            f'<span class="lkc__v">{("<b>" + num(v["elev"]) + "</b> ft") if ok else "Level unknown"}</span>'
            f'<span class="lkc__s">{E(" · ".join(sub)) if sub else ("" if ok else "The source has not updated")}</span>'
            f'{meter}<span class="lkc__f">{self.badge(v["band"], True, use) if ok and v["badges"] else ""}'
            f'{self.home_trend(v) if ok else ""}{self.card_date(v)}</span></a></li>')

    HL_BAND = {"above_full": "af", "full": "f", "near_full": "nf", "below_full": "bf", "low": "lo", "very_low": "vl", "level_only": "lv"}

    def home_table(self, lakes):
        """The long tail of popular lakes as one ranked, full-width list (numbered 1-15, after the six cards above).
        Each row: name and state, level with its reading date, band (a 5-step band scale, never a percent bar) with feet from
        full pool and percent full where published, and the one-day change. The whole row is the lake link (CSS stretched link)."""
        rows = []
        for lk in lakes:
            v = self.view(lk)
            ok = v["fresh"] != "unknown" and v["elev"] is not None
            cells = (f'<span><a href="{self.lake_url(lk)}">{E(lk["name"])}</a> <small>{E(self.st_txt(lk)).replace(", ", ",\u00a0")}</small></span>'
                     f'<span class="lkr__v">{("<b>" + num(v["elev"]) + "</b> ft") if ok else "Level unknown"}</span>'
                     f'<small>{E(v.get("when_short", ""))}</small>')
            if ok:
                band = v["band"] if v["badges"] else None
                sub = [x for x in (f'{signed(v["vs"])} ft' if v["vs"] is not None and v["pool"] is not None else "",
                                   pct_txt(v["pct"]) + " full" if v["pct"] is not None else "") if x]
                cells += (f'<span class="q-{self.HL_BAND.get(band, "lv")}">{E(BAND_LABEL[band]) if band else ""}'
                          + (f'<small>{E(" · ".join(sub))}</small>' if sub else "") + "</span>")
                t = v["trend"] if v["c24"] is not None else None
                cells += (f'<span class="{"u" if t.startswith("rising") else "d" if t.startswith("falling") else "s"}">'
                          f'<span class="vh">{E(TREND_LABEL[t])}, </span>{signed(v["c24"])} ft</span>' if t else "<span></span>")
            rows.append(f'<li{self.home_stale(v) if ok else ""}>{cells}</li>')
        return ('<div class="tw hl"><div class="hl__h" aria-hidden="true"><span>Lake</span><span>Level</span><span>Band</span><span>vs full pool</span><span>1 day</span></div>'
                '<ol aria-label="More popular lakes, most searched first: level, reading date, band, feet from full pool, percent full, 1-day change">'
                f'{"".join(rows)}</ol></div>')

    def home_movers(self, how):
        vs = self.ranked(self.live, how)[:5]
        head = {"rising": ("up", "Rising fastest", "/rising-fastest/", "All rising lakes"),
                "falling": ("down", "Falling fastest", "/falling-fastest/", "All falling lakes")}[how]
        if vs:
            rows = "".join(
                f'<li><a href="{self.lake_url(v["lk"])}"><span class="mv__n">{E(v["lk"]["name"])} '
                f'<small>{E(self.st_txt(v["lk"]))}</small></span><span class="mv__sp">{charts.sparkline(self.last_days(v["lk"]["id"], 7))}</span>'
                f'<span class="mv__v">{signed(v["c24"])} ft<small>{E(v.get("when_short", ""))}</small></span></a></li>' for v in vs)
            lst = f'<ol class="mv__l">{rows}</ol>'
        else:
            lst = '<p class="muted mv__none">No lake has a current reading for this list right now.</p>'
        return (f'<div class="mv mv--{how}"><h3>{self.ico(head[0], "ic--b")}{head[1]}</h3>{lst}'
                f'<p class="mv__more">{self.a(head[2], head[3], "more__a")}</p></div>')

    def home_bands(self, today):
        """How many lakes sit in each band right now: a stacked bar with a counted legend, from current readings only."""
        vs = [v for v in (self.view(lk) for lk in self.live) if v["fresh"] == "current" and v["elev"] is not None and v["badges"] and v.get("rdate")]
        cnt = {b: sum(1 for v in vs if v["band"] == b) for b in self.BAND_ORDER}
        n = sum(cnt.values())
        if not n:
            return ""
        dates = sorted(v["rdate"] for v in vs if v["band"] in cnt)
        span = d_short(dates[0]) if dates[0] == dates[-1] else f"{d_short(dates[0])} to {d_short(dates[-1])}"
        only = sum(1 for v in vs if v["band"] == "level_only")
        bar = "".join(f'<i class="s-{b}" style="flex-grow:{c}"></i>' for b, c in cnt.items() if c)
        key = "".join(f'<li><i class="s-{b}"></i><b>{c}</b> {E(BAND_LABEL[b])}</li>' for b, c in cnt.items() if c)
        return (f'<div class="bd"><h3>Where {n:,} lakes stand against full pool</h3><div class="bd__bar" aria-hidden="true">{bar}</div><ul class="bd__k">{key}</ul>'
                f'<p class="bd__n">Counted {E(today)} from each lake’s latest reading (dated {E(span)}).'
                + (f" {only} more lake{'s' if only != 1 else ''} report a level with no full-pool mark." if only else "") + "</p></div>")

    def home(self):
        top = self.top_lakes(30)
        cards = "".join(self.home_card(lk) for lk in top[:6])
        rest = top[6:21]     # the next 15 lakes after the six cards, numbered 1-15
        tables = self.home_table(rest) if rest else ""
        n_live, n_states = len(self.live), len(self.live_states)
        check_txt, src_ids, src_full = self.source_facts()
        src_words = [self.SOURCE_WORD.get(k) or self.sources[k].get("short") or self.sources[k].get("agency", k) for k in src_ids]
        today = d_long(self.now.astimezone(ZoneInfo("America/Chicago")))
        # national lists, each with its current leader and that reading's date
        lists = []
        for how, icon, path, name in (("lowest", "low", "/lowest-lakes/", "Lowest lakes"), ("fullest", "full", "/fullest-lakes/", "Fullest lakes"),
                                      ("rising", "up", "/rising-fastest/", "Rising fastest"), ("falling", "down", "/falling-fastest/", "Falling fastest")):
            r = self.ranked(self.live, how)
            lead = ""
            if r:
                v = r[0]
                val = pct_txt(v["pct"]) + " full" if how in ("lowest", "fullest") else f'{signed(v["c24"])} ft'
                lead = f'<span class="nl__l">First now: {E(v["lk"]["name"])}, <b>{E(val)}</b> <small>{E(v.get("when_short", ""))}</small></span>'
            lists.append(f'<li><a class="nl" href="{path}">{self.ico(icon, "ic--b")}<span class="nl__t">{name}</span>{lead}</a></li>')
        # states: a list on phones, a tile map of the US on wide screens
        sts = []
        for c in sorted(self.hub_lakes, key=lambda c: self.states[c]["name"]):
            n = sum(1 for lk in self.hub_lakes[c] if lk["id"] in self.live_ids)
            cnt = f'{n} lake{"s" if n != 1 else ""}' if n else "Profiles"
            tone = 0 if not n else 1 if n < 3 else 2 if n < 6 else 3 if n < 16 else 4
            col, row = self.TILES.get(c, (0, 0))
            sts.append(f'<li style="--c:{col};--r:{row}"><a class="t{tone}" href="{self.state_url(c)}"><i aria-hidden="true">{E(c)}</i>'
                       f'<b>{E(self.states[c]["name"])}</b> <small>{cnt}</small></a></li>')
        sts.append('<li class="sg__k" aria-hidden="true">Fewer lakes<u class="t1"></u><u class="t2"></u><u class="t3"></u><u class="t4"></u>more lakes</li>')
        sts += [f'<li class="x" aria-hidden="true" style="--c:{col};--r:{row}">{c}</li>' for c, (col, row) in self.TILES.items() if c not in self.hub_lakes]
        guides = [(p, load_fragment(f)[0]) for p, f, sub in self.content_pages if sub == "guides"]
        guide_html = ""
        if guides:
            guide_html = ('<aside class="gd" id="guides"><h2>' + self.ico("book", "ic--b") + 'Guides</h2><ul>' + "".join(
                f'<li>{self.a(p, m.get("h1") or m.get("title", p).split("|")[0].strip())}</li>' for p, m in guides) + "</ul></aside>")
        feature, fid = self.home_feature(top)
        movers = self.home_movers("rising") + self.home_movers("falling")
        trust = ", ".join(f"<strong>{E(w)}</strong>" for w in src_words[:-1]) + (" and " if len(src_words) > 1 else "") + f"<strong>{E(src_words[-1])}</strong>" if src_words else ""
        hero = (
            f'<section class="hero{"" if feature else " hero--solo"}"><div class="hero__in"><div class="hero__c">'
            '<h1>Lake and reservoir water levels, <span class="hero__u">today</span></h1>'
            '<p class="hero__t">Feet from full pool, percent full and daily change, read from public agency gauges. Every number shows its date.</p>'
            '<form class="find" id="find" action="/" role="search" data-index="/data/search.json">'
            '<label for="q" class="vh">Search for a lake</label>'
            f'<div class="find__row">{self.ico("search")}'
            '<input id="q" name="q" type="search" placeholder="Search a lake, e.g. Lake Travis" autocomplete="off" spellcheck="false" enterkeyhint="search">'
            '<button type="submit" class="find__go">Find</button></div>'
            f'<button type="button" class="btn btn--ghost near" hidden>{self.ico("loc")}Lakes near me</button>'
            '<p class="find__msg" role="status"></p></form>'
            f'<ul class="hero__n"><li><strong>{n_live:,}</strong> lakes</li><li><strong>{n_states}</strong> state{"s" if n_states != 1 else ""}</li>'
            + (f'<li>checked every <strong>{check_txt}</strong></li>' if check_txt else "") + "</ul>"
            + (f'<p class="hero__src">{self.ico("shield")}<span>Data from {trust}</span></p>' if trust else "")
            + f'</div>{feature}</div>'
            '<div class="hero__sea" aria-hidden="true"><i></i><i></i></div></section>')
        how = (
            '<section class="sec how" id="how">' + self.sec_head("shield", "How it works, and why you can check it",
                                                                 f'Sources in use: {E(", ".join(src_full))}.') + '<ol class="tru">'
            '<li><h3>Agency sources</h3><p>Every level comes from a public agency gauge. The agency is named and linked on each lake page, so you can check the number yourself.</p></li>'
            f'<li><h3>Checked every {E(check_txt)}</h3><p>Each source is read on its own schedule. The level shown changes only when the agency publishes a new value.</p></li>'
            '<li><h3>Never an old reading as current</h3><p>Each number shows the day or time it covers. If a source stops updating, the page says “Current level unknown”.</p></li>'
            f'<li><h3>Independent</h3><p>{E(BRAND)} is not a government website and does not run any gauge. Charts, records and answers come from one stored series.</p></li></ol>'
            f'<p class="tru__s">{self.a("/sources/", "Data sources and methods", "more__a")} {self.a("/corrections/", "Report a wrong level", "more__a")}</p></section>')
        about = (
            '<section class="sec ag" id="about"><div class="abt">' + self.sec_head("info", "About US lake levels") +
            f'<p>{E(BRAND)} tracks the water level of {n_live:,} lakes and reservoirs in {n_states} US states. A lake level is the height of the water surface, '
            'in feet above a fixed reference (a vertical datum), measured by a gauge at the dam or on the shore. Most reservoirs also have a '
            f'{self.a("/guides/what-is-full-pool/", "full pool")}: the level the lake is managed to hold. Each lake page shows how many feet the water is above or below that mark, '
            'the percent full where the operator publishes storage, the change over 24 hours, 7 days and 30 days, and charts of the past month and year.</p>'
            '<p>Anglers and boaters use the level to judge ramps and shallow water, lake-home owners watch docks and shorelines, and water managers and reporters follow drought and flood storage. '
            f'Start with a lake search, pick a {self.a("/#states", "state")}, or compare lakes on the {self.a("/lowest-lakes/", "lowest")} and {self.a("/fullest-lakes/", "fullest")} lists. '
            f'Readings are provisional and can be revised by the agency; do not use them for navigation or flood decisions.</p></div>{guide_html}</section>')
        graph = []   # Organization and WebSite come from write() on every page
        qa = [
            ("How often are the lake levels updated?",
             f"Each source is checked every {E(check_txt)}, depending on the agency. Some agencies publish a new value several times a day and others publish one "
             "daily value, so every reading on this site shows the day or time it covers."),
            ("Where do the water levels come from?",
             f"From public gauge data published by {E(', '.join(src_full[:-1]) + ' and ' + src_full[-1] if len(src_full) > 1 else ''.join(src_full))}. "
             f"The source is named and linked on every lake page. {self.a('/sources/', 'Data sources and methods')} lists each feed."),
            ("What does “full pool” mean?",
             "Full pool is the water level a reservoir is managed to hold, usually the top of its conservation or normal pool. "
             f"Water above it is in flood storage. The guide {self.a('/guides/what-is-full-pool/', 'What is full pool?')} explains the terms."),
            ("Why does a lake say “Current level unknown”?",
             "The agency’s feed has stopped updating, or its latest value is a large jump being held until the next check confirms it. Instead of showing an old number as today’s level, "
             "the page shows the last reading with its date and marks the current level as unknown until new data arrives."),
            (f"Is {BRAND} a government website?",
             f"No. {E(BRAND)} is an independent site that republishes public agency data. For navigation, flood or emergency decisions, "
             "follow the lake’s operator, local officials and the National Weather Service."),
        ]
        qa = [(q, a) for q, a in qa if "<span>" not in a]      # drop an answer whose link target is not built
        faq = ('<section class="sec fq" id="faq">' + self.sec_head("help", "Common questions", "Short answers about the data and this site.")
               + "<div>" + self.faq_html(qa, graph) + "</div></section>")
        body = (
            "{{sprite}}" + hero
            + '<section class="sec" id="top">' + self.sec_head("gauge", "Most searched lakes", f"List made {E(today)}. Each lake shows its own reading date.")
            + f'<ul class="lkcs">{cards}</ul>'
            + (f'<h3 class="sh3">More popular lakes</h3>{tables}' if tables else "") + "</section>"
            '<section class="sec" id="lists">' + self.sec_head("bars", "The national picture", "Every lake with a current reading, sorted into bands and ranked lists.")
            + f'<div class="np">{self.home_bands(today)}<ul class="nls">{"".join(lists)}</ul></div></section>'
            '<section class="sec" id="movers">' + self.sec_head("up", "Biggest moves today", "Change since the previous day’s reading."
                            + (" Lines trace the last 7 days where the series has them, each lake on its own scale." if '<svg class="spark"' in movers else ""))
            + f'<div class="mvs">{movers}</div></section>'
            '<section class="sec" id="states">' + self.sec_head("map", "Lake levels by state", f"{n_live:,} lakes with a data feed in {n_states} states. A stronger shade means more lakes.")
            + f'<ul class="sg">{"".join(sts)}</ul></section>'
            + how + about + faq
        )
        used = set(re.findall(r'href="#i-([a-z]+)"', body))
        body = body.replace("{{sprite}}", self.home_sprite(used))
        self.write("/", f"{BRAND}: US Lake & Reservoir Water Levels Today",
                   f"Lake levels today for {n_live:,} US lakes and reservoirs: feet from full pool, percent full and daily change "
                   f"from public agency gauges, each with its reading date.",
                   body, lastmod=self.lastmod_of(self.live), group="pages", graph=graph, kind="home", body_class='class="pg-home"',
                   css="home")

    # ------------------------------------------------------------ content pages
    def content_page(self, path, p, sub):
        meta, frag = load_fragment(p)
        h1 = meta.get("h1") or meta.get("title", p.stem).split("|")[0].strip()
        guide = sub == "guides"
        crumbs = [("Home", "/")] + ([("Guides", "/guides/")] if guide else []) + [(h1, path)]
        body_html = self.fill(frag).strip()
        upd = meta.get("updated") if re.match(r"\d{4}-\d\d-\d\d", meta.get("updated") or "") else None
        desc = self.fill(meta.get("description") or h1)
        if p.stem == "sources" and sub == "pages":        # the source cards follow the intro paragraph(s), before "The sources"
            cards = self.sources_table()
            m = re.search(r"<h2[\s>]", body_html)
            body_html = (body_html[:m.start()] + cards + body_html[m.start():]) if m else body_html + cards
        if guide:
            body_html = self.cnt_figure(p.stem, body_html)
        elif p.stem not in ("sources", "disclaimer"):
            body_html = self.cnt_action(body_html)
        body_html, toc = self.cnt_toc(body_html)
        mins = self.cnt_minutes(frag)
        kicker, icon = (("Guide", "book") if guide else self.CNT_KICKER.get(p.stem, ("About this site", "info")))
        head = self.cnt_head(crumbs, kicker, icon, h1, desc, upd, mins)
        tail = self.cnt_related(path) if guide else self.cnt_more(path)
        body = self.cnt_layout(head, body_html, toc, tail)
        graph = []
        if guide:
            art = {"@type": "Article", "headline": h1, "description": desc, "inLanguage": "en-US",
                   "mainEntityOfPage": SITE_URL + path, "image": SITE_URL + "/og-default.png",
                   "author": {"@id": SITE_URL + "/#org"}, "publisher": {"@id": SITE_URL + "/#org"}}
            if upd:
                art["dateModified"] = upd
            graph.append(art)
        title = meta.get("title") or f"{h1} | {BRAND}"
        self.write(path, self.fill(title), desc, self.cnt_with_sprite(body), crumbs=crumbs, lastmod=upd, group="pages",
                   kind="content", graph=graph, body_class='class="pg-cnt"', css="content")

    def sources_table(self):
        """Source cards: every source the lakes with a data feed use, with its check interval and freshness limits (from
        data/ref/sources.json). The bar draws the same limits to one scale so sources can be compared."""
        used = defaultdict(int)
        for lk in self.live:
            used[(lk.get("primary") or {}).get("source")] += 1
        srcs = [(sid, n, self.sources[sid]) for sid, n in sorted(used.items(), key=lambda x: -x[1]) if self.sources.get(sid)]
        if not srcs:
            return ""
        top = max((s.get("hard_stale_hours") or 0) for _, _, s in srcs) or 1
        span = top * 1.18
        cards = []
        for sid, n, s in srcs:
            kind = {"daily_average": "Daily average", "daily_instant": "Daily reading", "hourly": "Hourly", "15min": "15-minute"}.get(
                s.get("value_kind"), s.get("value_kind", ""))
            chk, soft, hard = s.get("check_every_hours"), s.get("soft_stale_hours"), s.get("hard_stale_hours")
            short = s.get("short") or SHORT_SOURCE.get(sid, sid.upper())
            hrs = lambda h: f"{h} h" if h is not None else "–"
            bar = ""
            if soft and hard:
                w1, w2 = soft / span * 100, (hard - soft) / span * 100
                bar = (f'<div class="cnt-fr" aria-hidden="true"><i class="cnt-fr__c" style="width:{w1:.1f}%"></i>'
                       f'<i class="cnt-fr__d" style="width:{w2:.1f}%"></i><i class="cnt-fr__u"></i></div>'
                       f'<p class="cnt-fr__k"><span><i class="cnt-fr__c"></i>Current to {soft} h</span>'
                       f'<span><i class="cnt-fr__d"></i>May be out of date to {hard} h</span>'
                       f'<span><i class="cnt-fr__u"></i>Unknown after</span></p>')
            cards.append(
                f'<li class="cnt-src"><div class="cnt-src__h"><span class="cnt-src__m" aria-hidden="true">{E(short)}</span>'
                f'<h3>{self.ext(s.get("public_url", "/"), s.get("agency", sid))}</h3>'
                f'<span class="cnt-src__n"><b>{n:,}</b> lake{"s" if n != 1 else ""}</span></div>'
                f'<dl class="cnt-src__d"><div><dt>Values</dt><dd>{E(kind)}' + (" (provisional)" if s.get("provisional") else "") + "</dd></div>"
                f'<div><dt>Checked every</dt><dd>{hrs(chk)}</dd></div>'
                f'<div><dt>“May be out of date” after</dt><dd>{hrs(soft)}</dd></div>'
                f'<div><dt>“Current level unknown” after</dt><dd>{hrs(hard)}</dd></div></dl>{bar}</li>')
        return ('<h2 id="sources-in-use">Sources in use</h2>'
                f'<ul class="cnt-srcs">{"".join(cards)}</ul>'
                '<p class="cnt-note">Built from our source settings each time the site is built. Hours count from the time of the '
                'reading itself, not from when we fetched it.</p>')

    def guides_index(self):
        guides = self.cnt_guides()
        cards = []
        for g in guides:
            cards.append(f'<li class="cnt-gc"><div class="cnt-gc__art" aria-hidden="true">{self.cnt_guide_art(g["slug"])}</div>'
                         f'<div class="cnt-gc__b"><h2>{self.a(g["path"], g["h1"], cls="cnt-gc__a")}</h2>'
                         + (f'<p>{E(g["desc"])}</p>' if g["desc"] else "")
                         + f'<p class="cnt-gc__m">{self.ico("clock")}<span>{g["mins"]} min read'
                         + (f' · Updated <time datetime="{g["upd"]}">{E(d_med(as_date(g["upd"])))}</time>' if g["upd"] else "")
                         + "</span></p></div></li>")
        crumbs = [("Home", "/"), ("Guides", "/guides/")]
        desc = "Plain-language guides to full pool, how lake levels are measured and how to read a lake level chart."
        upds = [g["upd"] for g in guides if g["upd"]]
        head = self.cnt_head(crumbs, "Guides", "book", "Guides", desc, max(upds) if upds else None, None)
        body = (f'<div class="cnt-top">{head}</div><ul class="cnt-gcs">{"".join(cards)}</ul>' + self.cnt_cta())
        graph = [{"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "url": SITE_URL + g["path"], "name": g["h1"]} for i, g in enumerate(guides)]}]
        self.write("/guides/", f"Lake Level Guides | {BRAND}", desc, self.cnt_with_sprite(body), crumbs=crumbs,
                   lastmod=max(upds) if upds else None, group="pages", kind="page", graph=graph, body_class='class="pg-cnt"',
                   css="content")

    def not_found(self):
        n_live = len(self.live)
        pop = self.cnt_popular(6)
        lists = [("/lowest-lakes/", "Lowest lakes"), ("/fullest-lakes/", "Fullest lakes"), ("/rising-fastest/", "Rising fastest"),
                 ("/falling-fastest/", "Falling fastest")]
        guides = self.cnt_guides()
        states = "".join(f'<a href="{self.state_url(c)}">{E(self.states[c]["name"])}</a>'
                         for c in sorted(self.hub_lakes, key=lambda c: self.states[c]["name"]))
        col = lambda icon, title, inner: (f'<section class="cnt-nf__c">{self.sec_head(icon, title)}{inner}</section>')
        lk_list = ('<ul class="cnt-pl">' + "".join(
            f'<li><a href="{self.lake_url(v["lk"])}">{E(v["lk"]["name"])}</a>'
            f'<span>{num(v["elev"])} ft <small><time datetime="{E(v.get("time_attr", ""))}">{E(v.get("when_short", ""))}</time></small></span></li>' for v in pop) + "</ul>") if pop else ""
        link_list = lambda items: '<ul class="cnt-ll">' + "".join(f"<li>{self.a(p, t)}</li>" for p, t in items) + "</ul>"
        body = (
            '<div class="cnt-nf"><div class="cnt-nf__art" aria-hidden="true">' + self.CNT_ART_404 + '</div>'
            '<div class="cnt-nf__t"><p class="cnt-kick">' + self.ico("help") + 'Error 404</p><h1>Page not found</h1>'
            '<p class="cnt-dek">We could not find that page. The address may have a typo, or the page may have moved. '
            'Search for a lake below, try the <a href="/">home page</a>, or browse <a href="/#states">lake levels by state</a>.</p>'
            '<form class="find cnt-find" id="find" action="/#find" role="search" data-index="/data/search.json">'
            '<label for="q" class="vh">Search for a lake</label>'
            f'<div class="cnt-find__row">{self.ico("search")}'
            '<input id="q" name="q" type="search" placeholder="Search a lake by name" autocomplete="off" spellcheck="false" enterkeyhint="search">'
            '<button class="cnt-find__go" type="submit">Search</button></div>'
            f'<p class="find__msg" aria-live="polite"></p></form>'
            + (f'<p class="cnt-nf__n">{n_live:,} lakes and reservoirs in {len(self.live_states)} states.</p>' if n_live else "")
            + '</div></div><div class="cnt-nf__g">'
            + (col("drop", "Most searched lakes", lk_list) if lk_list else "")
            + col("bars", "Lake lists", link_list(lists))
            + (col("book", "Guides", link_list([(g["path"], g["short"]) for g in guides])) if guides else "")
            + "</div>"
            + (f'<section class="cnt-nf__st">{self.sec_head("map", "Lake levels by state")}<p class="chips-l">{states}</p></section>' if states else ""))
        self.write("/404.html", f"Page not found | {BRAND}", "Page not found.", self.cnt_with_sprite(body), noindex=True, sitemap=False,
                   kind="404", body_class='class="pg-cnt"', css="content")

    # --- content-page helpers (prefix cnt_)
    CNT_ICONS = {
        "mail": '<rect x="2.5" y="4.5" width="15" height="11" rx="2"/><path d="m3 5.5 7 5.5 7-5.5"/>',
        "lock": '<rect x="4" y="9" width="12" height="8.5" rx="2"/><path d="M7 9V6.500a3 3 0 0 1 6 0V9M10 12.500v2"/>',
        "doc": '<path d="M5 2.500h6.500L15 6v11.500H5z"/><path d="M11.500 2.500V6H15M7.500 10h5M7.500 13h5"/>',
        "alert": '<path d="M10 3 18 16.500H2z"/><path d="M10 8v4"/><path d="M10 14.300h.01" stroke-width="2.600"/>',
        "list": '<path d="M7.500 5.500H17M7.500 10H17M7.500 14.500H17"/><path d="M3.500 5.500h.01M3.500 10h.01M3.500 14.500h.01" stroke-width="2.600"/>',
        "arrow": '<path d="M3.500 10h13M12 5.500l4.500 4.500-4.500 4.500"/>',
    }
    CNT_KICKER = {"about": ("About this site", "info"), "sources": ("Methods", "shield"), "disclaimer": ("Legal", "alert"),
                  "corrections": ("Corrections", "flag"), "privacy": ("Legal", "lock"), "terms": ("Legal", "doc"),
                  "contact": ("Contact", "mail")}
    CNT_TRUST = [("/about/", "About"), ("/sources/", "Data sources and methods"), ("/corrections/", "Report a wrong reading"),
                 ("/disclaimer/", "Disclaimer"), ("/contact/", "Contact"), ("/privacy/", "Privacy"), ("/terms/", "Terms")]

    def cnt_with_sprite(self, body):
        """{{sprite}} → the home icons plus CNT_ICONS that this page uses (a page-local sprite, same format as home_sprite)."""
        css = "".join(self.cnt_min(v) for k, v in self.CNT_PAGE_CSS.items() if f'class="{k}' in body)
        if css:
            body = f"<style>{css}</style>" + body
        icons = {**self.HOME_ICONS, **self.CNT_ICONS}
        used = set(re.findall(r'href="#i-([a-z]+)"', body))
        syms = "".join(f'<symbol id="i-{k}" viewBox="0 0 20 20">{v}</symbol>' for k, v in icons.items() if k in used)
        return ("{{sprite}}" + body if "{{sprite}}" not in body else body).replace("{{sprite}}", (
            '<svg class="sprite" aria-hidden="true" width="0" height="0" fill="none" stroke="currentColor" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round">{syms}</svg>'), 1)

    @staticmethod
    def cnt_minutes(frag):
        words = len(visible_text(frag).split())
        return max(1, math.ceil(words / 220))

    @staticmethod
    def cnt_toc(body_html):
        """Give every <h2> an id (kept when present) and return (html, [(id, text)])."""
        seen, toc = set(), []

        def fix(m):
            attrs, inner = m.group(1) or "", m.group(2)
            text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", inner)).split())
            im = re.search(r'\bid="([^"]+)"', attrs)
            if im:
                hid = im.group(1)
            else:
                base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48].strip("-") or "section"
                hid, k = base, 2
                while hid in seen:
                    hid, k = f"{base}-{k}", k + 1
                attrs = f' id="{hid}"' + attrs
            seen.add(hid)
            toc.append((hid, text))
            return f"<h2{attrs}>{inner}</h2>"
        out = re.sub(r"<h2(\s[^>]*)?>(.*?)</h2>", fix, body_html, flags=re.S)
        return out, toc

    def cnt_head(self, crumbs, kicker, icon, h1, dek, upd, mins):
        meta = []
        if upd:
            meta.append(f'{self.ico("cal")}<span>Updated <time datetime="{E(upd)}">{E(d_med(as_date(upd)))}</time></span>')
        if mins:
            meta.append(f'{self.ico("clock")}<span>{mins} min read</span>')
        return (self.crumbs_html(crumbs) + f'<header class="cnt-hd"><p class="cnt-kick">{self.ico(icon)}{E(kicker)}</p>'
                f'<h1>{E(h1)}</h1>' + (f'<p class="cnt-dek">{E(dek)}</p>' if dek else "")
                + ('<p class="cnt-meta">' + "".join(f"<span>{x}</span>" for x in meta) + "</p>" if meta else "") + "</header>")

    def cnt_layout(self, head, body_html, toc, tail):
        """Header, then the article sheet with a sticky contents rail (wide screens) or a folded "On this page" block (phones)."""
        rail = otp = ""
        if len(toc) >= 3:
            items = "".join(f'<li><a href="#{i}">{E(t)}</a></li>' for i, t in toc)
            otp = (f'<details class="cnt-otp"><summary>{self.ico("list")}On this page <small>{len(toc)} sections</small></summary>'
                   f'<ol>{items}</ol></details>')
            rail = (f'<aside class="cnt-rail"><nav class="toc cnt-toc" aria-label="On this page"><h2>On this page</h2>'
                    f'<ol>{items}</ol></nav><p class="cnt-rail__top"><a href="#main">Back to top</a></p></aside>')
        return (f'<div class="cnt-top">{head}</div><div class="cnt-grid{" cnt-grid--toc" if rail else ""}">'
                f'<article class="cnt-sheet">{otp}<div class="cnt-prose">{body_html}</div></article>{rail}</div>{tail}')

    def cnt_action(self, body_html):
        """The first top-level paragraph with a mailto link (from the copy) becomes an action card with a button."""
        m = re.search(r'<p>((?:(?!</p>).)*?<a href="(mailto:[^"]+)">(?:(?!</p>).)*?)</p>', body_html, re.S)
        if not m:
            return body_html
        href = m.group(2)
        label = "Email a correction" if "subject=correction" in href.lower() else "Send an email"
        card = (f'<div class="cnt-act">{self.ico("mail", "ic--h")}<div class="cnt-act__t"><p>{m.group(1)}</p>'
                f'<a class="btn" href="{href}">{label}{self.ico("arrow")}</a></div></div>')
        return body_html[:m.start()] + card + body_html[m.end():]

    def cnt_guides(self):
        out = []
        for path, f, sub in self.content_pages:
            if sub != "guides":
                continue
            meta, frag = load_fragment(f)
            h1 = meta.get("h1") or meta.get("title", path).split("|")[0].strip()
            upd = meta.get("updated") if re.match(r"\d{4}-\d\d-\d\d", meta.get("updated") or "") else None
            out.append({"path": path, "slug": f.stem, "h1": h1, "short": h1.split("?")[0].split(":")[0].strip(),
                        "desc": self.fill(meta.get("description", "")), "upd": upd, "mins": self.cnt_minutes(frag)})
        order = {s: i for i, s in enumerate(("what-is-full-pool", "how-lake-levels-are-measured", "reading-a-lake-level-chart"))}
        return sorted(out, key=lambda g: (order.get(g["slug"], 99), g["slug"]))

    def cnt_popular(self, n):
        """Most searched lakes whose reading is not past the hard limit, as views (every level shown with its own date)."""
        out = []
        for lk in self.top_lakes(40):
            v = self.view(lk)
            if v["fresh"] != "unknown" and v["elev"] is not None and v.get("when_short"):
                out.append(v)
            if len(out) >= n:
                break
        return out

    def cnt_cta(self):
        """Back to the data: search, states, and the most searched lakes."""
        pop = self.cnt_popular(5)
        chips = "".join(f'<a href="{self.lake_url(v["lk"])}">{E(v["lk"]["name"])}</a>' for v in pop)
        return ('<aside class="cnt-cta"><div><h2>Check a lake level</h2>'
                f'<p>{len(self.live):,} lakes and reservoirs, each with its reading date and source.</p></div>'
                f'<p class="cnt-cta__b"><a class="btn" href="/#find">{self.ico("search")}Search lakes</a>'
                f'<a class="btn btn--ghost" href="/#states">{self.ico("map")}Browse by state</a></p>'
                + (f'<p class="cnt-cta__l"><span>Most searched:</span>{chips}</p>' if chips else "") + "</aside>")

    def cnt_related(self, path):
        others = [g for g in self.cnt_guides() if g["path"] != path]
        rel = ""
        if others:
            rel = ('<section class="cnt-rel"><h2>Related guides</h2><ul class="cnt-gcs cnt-gcs--s">' + "".join(
                f'<li class="cnt-gc"><div class="cnt-gc__art" aria-hidden="true">{self.cnt_guide_art(g["slug"])}</div>'
                f'<div class="cnt-gc__b"><h3>{self.a(g["path"], g["h1"], cls="cnt-gc__a")}</h3>'
                f'<p class="cnt-gc__m">{self.ico("clock")}{g["mins"]} min read</p></div></li>' for g in others)
                + f'</ul><p class="more">{self.a("/guides/", "All guides", cls="more__a")}</p></section>')
        return rel + self.cnt_cta()

    def cnt_more(self, path):
        links = "".join(self.a(p, t) for p, t in self.CNT_TRUST if p != path and p in self.planned)
        return (f'<nav class="cnt-more" aria-label="About this site"><h2>More about {E(BRAND)}</h2><p class="chips-l">{links}'
                f'{self.a("/guides/", "Guides")}</p></nav>') if links else ""

    # figures: generic illustrations (no real lake numbers), placed after the paragraph that introduces them
    CNT_FIG_AT = {"what-is-full-pool": "The three main zones", "how-lake-levels-are-measured": None,
                  "reading-a-lake-level-chart": "The parts of the chart"}

    def cnt_figure(self, slug, body_html):
        fig = {"what-is-full-pool": self.cnt_fig_zones, "how-lake-levels-are-measured": self.cnt_fig_flow,
               "reading-a-lake-level-chart": self.cnt_fig_chart}.get(slug)
        if not fig:
            return body_html
        at = self.CNT_FIG_AT.get(slug)
        pos = None
        if at:
            m = re.search(r"<h2[^>]*>\s*" + re.escape(at), body_html)
            if m:
                h_end = body_html.index("</h2>", m.end()) + 5
                nxt = re.search(r"<h[23][\s>]", body_html[h_end:])
                pe = body_html.find("</p>", h_end)
                pos = pe + 4 if pe >= 0 and (not nxt or pe < h_end + nxt.start()) else h_end
        if pos is None:        # after the intro (before the first h2)
            m = re.search(r"<h2[\s>]", body_html)
            pos = m.start() if m else len(body_html)
        return body_html[:pos] + fig() + body_html[pos:]

    @staticmethod
    def cnt_num(x, y, n):
        return (f'<g class="f-n"><circle cx="{x}" cy="{y}" r="9"/><text x="{x}" y="{y + 4}" text-anchor="middle">{n}</text></g>')

    def cnt_fig_wrap(self, title, art, keys, note="Illustration, not to scale. It shows no real lake’s data."):
        key = "".join(f'<li><b>{E(k)}</b> {E(t)}</li>' for k, t in keys)
        return (f'<figure class="cnt-fig"><div class="cnt-fig__art">{art}</div><figcaption><p class="cnt-fig__t">{E(title)}</p>'
                f'<ol class="cnt-key">{key}</ol><p class="cnt-fig__n">{E(note)}</p></figcaption></figure>')

    def cnt_fig_zones(self):
        basin = "M0 46V58L44 78L112 170L170 204H262L279 46Z"
        art = (
            '<svg viewBox="0 0 360 252" role="img" aria-labelledby="fz-t"><title id="fz-t">Cross-section of a reservoir: dead pool at the '
            'bottom, conservation pool up to full pool, flood pool above it, and the water level measured as an elevation above the datum'
            '</title><defs><clipPath id="fz-c"><path d="' + basin + '"/></clipPath>'
            '<pattern id="fz-h" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><path d="M0 0v7" class="f-hl"/></pattern></defs>'
            '<g clip-path="url(#fz-c)"><rect y="46" width="300" height="40" class="f-fl"/><rect y="46" width="300" height="40" fill="url(#fz-h)"/>'
            '<rect y="86" width="300" height="90" class="f-cp"/><rect y="176" width="300" height="30" class="f-dp"/>'
            '<path d="M0 112q12-4 24 0t24 0 24 0 24 0 24 0 24 0 24 0 24 0 24 0 24 0 24 0 24 0V206H0z" class="f-w"/></g>'
            '<path d="M0 58L44 78L112 170L170 204H262" class="f-bank"/>'
            '<path d="M0 58L44 78L112 170L170 204H262L279 46H292L314 214H0V58z" class="f-g"/>'
            '<path d="M314 214L292 46H279L262 214z" class="f-dam"/><path d="M314 214H360V222H0V214" class="f-g"/>'
            '<path d="M0 86H300M0 176H300" class="f-ln"/><path d="M0 112H340" class="f-sl"/>'
            '<path d="M8 236H352" class="f-dt"/><path d="M340 233V116M336 121l4-6 4 6M336 228l4 6 4-6" class="f-ar"/>'
            '<text x="150" y="70" class="f-t">Flood pool</text><text x="150" y="148" class="f-t f-t--w">Conservation pool</text>'
            '<text x="196" y="195" class="f-t f-t--w">Dead pool</text><text x="57" y="82" class="f-t f-t--s">Full pool</text>'
            '<text x="12" y="249" class="f-t f-t--s">Datum (near sea level)</text>'
            '<text x="300" y="38" class="f-t f-t--s" text-anchor="middle">Dam</text>'
            + self.cnt_num(132, 66, 1) + self.cnt_num(43, 78, 2) + self.cnt_num(132, 144, 3) + self.cnt_num(178, 191, 4)
            + self.cnt_num(326, 168, 5) + '</svg>')
        return self.cnt_fig_wrap("The zones of a reservoir", art, [
            ("Flood pool.", "Space above full pool on flood-control lakes. Water here is held for a while and released later."),
            ("Full pool.", "The top of the conservation pool, also called normal pool, full pond or summer pool."),
            ("Conservation pool.", "Water held for supply, hydropower and recreation. Percent full is based on this storage."),
            ("Dead pool.", "Water below the outlet works. It is not counted as usable storage."),
            ("The level.", "The height of the water surface above the datum, not the depth of the lake.")])

    def cnt_fig_flow(self):
        steps = [
            ("gauge", "Gauge", "Measures the height of the water surface above a datum, often every 15 minutes or every hour."),
            ("bars", "Agency", "Publishes instant readings or a daily average. Most new values are provisional."),
            ("shield", "Our checks", "Unit, plausible range, timestamp and sudden jumps are checked before a reading is used."),
            ("chart", "Lake page", "Shows the reading with its own date, its kind and a link to the source."),
        ]
        ys = [24 - 11 * math.sin(i / 1.9) for i in range(12)]
        dots = "".join(f'<circle cx="{8 + i * 9}" cy="{y:.1f}" r="2.4" class="f-dot"/>' for i, y in enumerate(ys))
        my = sum(ys) / len(ys)
        avg = ('<svg viewBox="0 0 120 44" class="cnt-flow__mini" role="img" aria-labelledby="ff-t"><title id="ff-t">Many instant readings '
               'in a day become one daily average</title>' + dots + f'<path d="M4 {my:.1f}H112" class="f-avg"/></svg>')
        lis = "".join(f'<li><span class="cnt-flow__i">{self.ico(ic)}</span><b>{E(t)}</b><span>{E(d)}</span>'
                      + (avg if ic == "bars" else "") + "</li>" for ic, t, d in steps)
        return ('<figure class="cnt-fig cnt-fig--flow"><ol class="cnt-flow">' + lis + '</ol><figcaption><p class="cnt-fig__t">'
                'From the gauge to a lake page</p><p class="cnt-fig__n">Once a week the last 60 days are downloaded again, so values the '
                'agency revises replace the provisional ones.</p></figcaption></figure>')

    def cnt_fig_chart(self):
        # a made-up series in plot coordinates (x 40..340, y 20..170); no real lake numbers
        cur = [(40, 104), (62, 100), (84, 92), (106, 70), (128, 58), (150, 62), (172, 76), (194, 88)]
        cur2 = [(228, 102), (250, 112), (272, 118), (294, 132), (316, 128), (338, 122)]
        last = [(40, 120), (70, 116), (100, 112), (130, 108), (160, 114), (190, 124), (220, 134), (250, 140), (280, 146), (310, 142), (338, 138)]
        pl = lambda pts: "M" + " L".join(f"{x} {y}" for x, y in pts)
        ticks = "".join(f'<path d="M36 {y}H340" class="f-gr"/>' for y in (40, 80, 120, 160))
        art = (
            '<svg viewBox="0 0 360 214" role="img" aria-labelledby="fc-t"><title id="fc-t">An example lake level chart with the level '
            'line, the full-pool line, last year’s line, the highest and lowest points, a gap and the latest reading</title>'
            + ticks + '<path d="M36 16V176H340" class="f-ax"/><path d="M31 168l5-4 5 4M31 174l5-4 5 4" class="f-brk"/>'
            '<path d="M36 50H340" class="f-pool"/><text x="44" y="44" class="f-t f-t--s">Full pool</text>'
            f'<path d="{pl(last)}" class="f-last"/><path d="{pl(cur)}" class="f-lv"/><path d="{pl(cur2)}" class="f-lv"/>'
            '<path d="M194 88L228 102" class="f-gap"/>'
            '<circle cx="128" cy="58" r="4.5" class="f-hi"/><circle cx="294" cy="132" r="4.5" class="f-lo"/>'
            '<circle cx="338" cy="122" r="9" class="f-halo"/><circle cx="338" cy="122" r="5" class="f-hi"/>'
            '<text x="18" y="96" class="f-t f-t--s" transform="rotate(-90 18 96)" text-anchor="middle">Feet above datum</text>'
            '<text x="40" y="194" class="f-t f-t--s">Earlier</text><text x="340" y="194" class="f-t f-t--s" text-anchor="end">Latest</text>'
            + self.cnt_num(84, 76, 1) + self.cnt_num(250, 36, 2) + self.cnt_num(250, 158, 3) + self.cnt_num(128, 38, 4)
            + self.cnt_num(294, 152, 4) + self.cnt_num(211, 80, 5) + self.cnt_num(322, 104, 6) + self.cnt_num(50, 164, 7) + '</svg>')
        return self.cnt_fig_wrap("The parts of a lake level chart", art, [
            ("Level line.", "The water surface elevation, one point per day."),
            ("Full-pool line.", "The gap between it and the level line is the feet above or below full pool."),
            ("Last year’s line.", "Where the level line is above it, the lake is higher than a year ago."),
            ("Highest and lowest points.", "They describe only the period the chart covers."),
            ("Gap.", "Days with no valid value. A gap is not a reading of zero."),
            ("Latest point.", "Check its date and whether it is a daily average or an instant reading."),
            ("Broken axis.", "The scale does not start at zero, so small changes look steep.")],
            note="Illustration with made-up values. It shows no real lake’s data.")

    def cnt_guide_art(self, slug):
        """Small card illustration per guide (200 x 72, centred so a narrow crop still reads); a book icon for any other guide."""
        art = {
            "what-is-full-pool": (
                '<path d="M0 10H142V72H0z" class="f-fl"/><path d="M0 24H142V72H0z" class="f-cp"/><path d="M0 56H142V72H0z" class="f-dp"/>'
                '<path d="M0 34q10-3 20 0t20 0 20 0 20 0 20 0 20 0 20 0V72H0z" class="f-w"/><path d="M0 24H142" class="f-ln"/>'
                '<path d="M0 72V20L44 26L74 58L96 66H130L137 12H147L158 72z" class="f-g"/><path d="M130 66L137 12H147L158 72H126z" class="f-dam"/>'
                '<path d="M158 72V64H200V72z" class="f-g"/>'),
            "how-lake-levels-are-measured": (
                '<path d="M0 46q10-4 20 0t20 0 20 0 20 0 20 0 20 0 20 0 20 0 20 0 20 0V72H0z" class="f-w"/>'
                '<rect x="92" y="6" width="16" height="62" rx="2" class="f-staff"/>'
                + "".join(f'<path d="M92 {12 + i * 8}h{7 if i % 2 else 11}" class="f-tick"/>' for i in range(7))
                + '<path d="M92 46h16" class="f-sl"/><path d="M116 13a8 8 0 0 1 0 12M121 8a15 15 0 0 1 0 22" class="f-tick"/>'),
            "reading-a-lake-level-chart": (
                '<path d="M44 8V64H160" class="f-ax"/><path d="M44 22H160" class="f-pool"/>'
                '<path d="M46 46L62 42L78 32L94 30L110 38L126 46L142 50L156 48" class="f-last"/>'
                '<path d="M46 42L62 36L78 26L94 28L110 34L126 42L142 46L156 44" class="f-lv"/><circle cx="156" cy="44" r="3.5" class="f-hi"/>'),
        }.get(slug)
        if not art:
            art = f'<g transform="translate(86 22) scale(1.4)" class="f-ico">{self.HOME_ICONS["book"]}</g>'
        return f'<svg viewBox="0 0 200 72" class="cnt-art" preserveAspectRatio="xMidYMid slice">{art}</svg>'

    # Styles that only one kind of page uses, inlined as <style> on the pages whose HTML has the marker class, so content.css
    # (loaded by every trust page) stays small. Keys: marker class prefix; minified by cnt_min().
    CNT_PAGE_CSS = {
        "f-": """
.f-t{font:600 12px system-ui,sans-serif;fill:var(--ink);paint-order:stroke;stroke:var(--bg);stroke-width:3px;stroke-linejoin:round}
.f-t--w{fill:#fff;stroke:none}
.f-t--s{font-size:11px;font-weight:500;fill:var(--ink2)}
.f-t--b{font-size:12px;font-weight:800;fill:var(--ink2);stroke:none}
.f-t--q{font-size:20px;font-weight:800;fill:#fff;stroke:none}
.f-n circle{fill:var(--brand)}
.f-n text{font:700 11px system-ui,sans-serif;fill:#fff}
.f-fl{fill:var(--b-af)}
.f-hl{stroke:var(--t-af);stroke-opacity:.25;stroke-width:1.2}
.f-cp{fill:var(--b-nf)}
.f-dp{fill:var(--b-bf)}
.f-w{fill:#2b86c2;fill-opacity:.85}
.f-w2{fill:#0a5a94;fill-opacity:.85}
.f-g{fill:var(--b-lo)}
.f-dam{fill:var(--co)}
.f-bank{fill:none;stroke:var(--co);stroke-width:1.5}
.f-ln,.f-pool,.f-dt{fill:none;stroke:var(--cp);stroke-width:1.4;stroke-dasharray:5 4}
.f-sl{fill:none;stroke:#fff;stroke-width:1.6}
.f-ar,.f-brk{fill:none;stroke:var(--ink);stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.f-gr{stroke:var(--line);stroke-width:1}
.f-ax{fill:none;stroke:var(--co);stroke-width:1.4}
.f-lv{fill:none;stroke:var(--cl);stroke-width:2.6;stroke-linejoin:round;stroke-linecap:round}
.f-last{fill:none;stroke:var(--co);stroke-width:2;stroke-linejoin:round}
.f-gap{fill:none;stroke:var(--cl);stroke-width:1.5;stroke-dasharray:2 4;stroke-opacity:.6}
.f-hi{fill:var(--cl);stroke:var(--card);stroke-width:2}
.f-lo{fill:var(--card);stroke:var(--cl);stroke-width:2.5}
.f-halo{fill:var(--ca)}
.f-staff{fill:var(--card);stroke:var(--co);stroke-width:1.5}
.f-tick{fill:none;stroke:var(--ink2);stroke-width:1.6;stroke-linecap:round}
.f-sig{fill:var(--accbg);stroke:var(--acc);stroke-width:1.5}
.f-q{fill:var(--acc)}
.f-dot{fill:var(--cl);fill-opacity:.55}
.f-avg{stroke:var(--brand);stroke-width:2.4;stroke-linecap:round}
.f-ico{fill:none;stroke:var(--acc);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
@media(prefers-color-scheme:dark){.f-avg{stroke:#e6edf3}.f-n circle{fill:#2b86c2}}
""",
        "cnt-flow": """
.cnt-prose .cnt-flow{list-style:none;margin:0;padding:0;display:grid;gap:22px;counter-reset:s}
.cnt-prose .cnt-flow li{position:relative;display:grid;grid-template-columns:44px minmax(0,1fr);gap:2px 12px;margin:0;padding:0;font-size:14.5px;line-height:1.45}
.cnt-flow li:not(:last-child)::after{content:"";position:absolute;left:21px;top:48px;bottom:-18px;border-left:2px dashed var(--co)}
.cnt-flow__i{grid-row:span 3;display:grid;place-items:center;width:44px;height:44px;border-radius:12px;background:var(--brand);color:#bfe3f7}
.cnt-flow b{font-size:15.5px;color:var(--ink)}
.cnt-flow li>span:not(.cnt-flow__i){color:var(--ink2)}
.cnt-flow__mini{display:block;width:120px;max-width:100%;height:44px;margin-top:4px}
@media (min-width:720px){.cnt-prose .cnt-flow{grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}
.cnt-prose .cnt-flow li{grid-template-columns:1fr;align-content:start}
.cnt-flow__i{grid-row:auto;margin-bottom:8px}
.cnt-flow li:not(:last-child)::after{left:54px;right:-12px;top:21px;bottom:auto;border-left:0;border-top:2px dashed var(--co)}
.cnt-fig--flow figcaption{margin-top:16px}}
""",
        "cnt-srcs": """
.cnt-prose .cnt-srcs{list-style:none;margin:0 0 12px;padding:0;display:grid;gap:12px}
.cnt-prose .cnt-src{margin:0;padding:14px 16px;border:1px solid var(--line);border-radius:14px;background:var(--card);font-size:15px;line-height:1.45}
.cnt-src__h{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.cnt-src__m{flex:none;display:grid;place-items:center;min-width:44px;height:30px;padding:0 6px;border-radius:8px;background:var(--brand);color:#bfe3f7;font-size:12px;font-weight:750;letter-spacing:.04em}
.cnt-prose .cnt-src h3{margin:0;font-size:16px;line-height:1.3;flex:1;min-width:0}
.cnt-src__n{flex:none;font-size:13px;color:var(--ink2);white-space:nowrap}.cnt-src__n b{font-size:17px;color:var(--ink)}
.cnt-src__d{display:grid;grid-template-columns:1fr 1fr;gap:1px;margin:0;background:var(--line2);border-radius:10px;overflow:hidden}
.cnt-src__d div{background:var(--card);padding:8px 10px}
.cnt-src__d dt{font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--ink2);line-height:1.3}
.cnt-src__d dd{margin:2px 0 0;padding:0;border:0;font-weight:680}
.cnt-fr{display:flex;height:8px;margin:12px 0 6px;border-radius:99px;overflow:hidden;background:var(--line2)}
.cnt-fr i{display:block;height:100%}
.cnt-fr__c{background:var(--cl)}.cnt-fr__d{background:var(--warnb)}.cnt-fr__u{flex:1;background:repeating-linear-gradient(135deg,var(--co) 0 3px,transparent 3px 6px)}
.cnt-fr__k{display:flex;flex-wrap:wrap;gap:2px 14px;margin:0;font-size:12.5px;color:var(--ink2)}
.cnt-fr__k span{display:inline-flex;align-items:center;gap:5px}
.cnt-fr__k i{display:inline-block;width:12px;height:8px;border-radius:2px}
@media (min-width:600px){.cnt-src__d{grid-template-columns:repeat(4,1fr)}}
""",
        "cnt-nf": """
.cnt-nf{display:grid;gap:18px;align-items:center;margin-top:18px;padding:22px 20px;background:var(--card);border:1px solid var(--line);border-radius:20px}
.cnt-nf__art{max-width:180px}
.cnt-nf__art svg{height:auto}
.cnt-nf .cnt-dek{margin-bottom:16px}
.cnt-nf__n{margin:4px 0 0;font-size:14px;color:var(--ink2)}
.cnt-find{position:relative;max-width:560px}
.cnt-find__row{display:flex;align-items:center;gap:4px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:5px;box-shadow:0 10px 26px -18px rgba(11,61,92,.6)}
.cnt-find__row:focus-within{border-color:var(--acc);box-shadow:0 0 0 3px rgba(58,160,220,.35)}
.cnt-find__row>.ic{margin-left:9px;color:var(--ink2)}
.cnt-find input{flex:1;min-width:0;border:0;background:transparent;font:17px/1.2 inherit;padding:11px 8px;color:var(--ink);appearance:none}
.cnt-find input:focus{outline:none}
.cnt-find__go{border:0;border-radius:10px;background:var(--brand);color:#fff;font:650 15px inherit;font-family:inherit;padding:0 18px;height:44px;cursor:pointer}
.cnt-find__go:hover{background:var(--acc)}
.cnt-find .find__msg{margin:8px 0 0;font-size:14px;color:var(--ink2);min-height:1px}
.cnt-find .suggest{position:absolute;left:0;right:0;top:60px;z-index:5;list-style:none;margin:0;padding:6px;background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:0 14px 40px -10px rgba(0,0,0,.35);max-height:352px;overflow:auto}
.cnt-find .suggest a{display:flex;justify-content:space-between;gap:10px;padding:11px 12px;border-radius:8px;text-decoration:none;color:var(--ink)}
.cnt-find .suggest [aria-selected=true] a,.cnt-find .suggest a:hover{background:var(--accbg)}
.cnt-find .s2{color:var(--ink2);font-size:14px;white-space:nowrap}
.cnt-nf__g{display:grid;gap:14px;margin-top:22px}
.cnt-nf__c{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px}
.cnt-nf__c .sh,.cnt-nf__st .sh{margin-bottom:8px;align-items:center}
.cnt-nf__c .sh h2,.cnt-nf__st .sh h2{font-size:18px}
.cnt-nf__c .ic--h,.cnt-nf__st .ic--h{width:34px;height:34px;padding:7px;border-radius:10px}
.cnt-pl,.cnt-ll{list-style:none;margin:0;padding:0}
.cnt-pl li,.cnt-ll li{border-bottom:1px solid var(--line2)}
.cnt-pl li:last-child,.cnt-ll li:last-child{border:0}
.cnt-pl li{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.cnt-pl a,.cnt-ll a{display:block;padding:10px 0;font-weight:620}
.cnt-pl span{font-weight:700;white-space:nowrap}
.cnt-pl small{font-weight:400;color:var(--ink2);margin-left:4px}
.cnt-nf__st{margin-top:30px}
@media(min-width:760px){.cnt-nf{grid-template-columns:200px minmax(0,1fr);gap:36px;padding:32px 40px}.cnt-nf__art{max-width:none}.cnt-nf__g{grid-template-columns:repeat(3,1fr)}}
""",
    }

    @staticmethod
    def cnt_min(css):
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        css = re.sub(r"\s*\n\s*", "", css)
        css = re.sub(r"\s*([{};,>])\s*", r"\1", css)
        return css.replace(";}", "}").replace("@media (", "@media(")

    CNT_ART_404 = (
        '<svg viewBox="0 0 220 180" class="cnt-art">'
        '<path d="M0 132q18-7 36 0t36 0 36 0 36 0 36 0 36 0 36 0V180H0z" class="f-w"/>'
        '<path d="M0 146q18-6 36 0t36 0 36 0 36 0 36 0 36 0 36 0V180H0z" class="f-w2"/>'
        '<rect x="96" y="14" width="28" height="150" rx="3" class="f-staff"/>'
        + "".join(f'<path d="M96 {28 + i * 13}h{12 if i % 2 else 18}" class="f-tick"/>' for i in range(10))
        + '<path d="M60 132H160" class="f-sl"/><text x="110" y="10" text-anchor="middle" class="f-t f-t--b">404</text>'
        '<circle cx="168" cy="54" r="18" class="f-q"/><text x="168" y="61" text-anchor="middle" class="f-t f-t--q">?</text></svg>')

    # ------------------------------------------------------------ data files, static files, sitemaps
    def data_files(self):
        dd = self.dist / "data" / "lake"
        dd.mkdir(parents=True, exist_ok=True)
        idx = []
        for lk in self.live:
            v = self.view(lk)
            e = v["e"]
            ok = v["fresh"] != "unknown" and v["elev"] is not None
            rec = {"lake": {k: lk.get(k) for k in ("id", "name", "aliases", "state", "states", "lat", "lon", "datum", "full_pool_ft",
                                                   "full_pool_label", "capacity_af")},
                   "page": SITE_URL + self.lake_url(lk),
                   "reading": {k: e.get(k) for k in ("elev_ft", "reading_at", "reading_kind", "pct_full", "storage_af", "vs_full_pool_ft",
                                                     "change_24h_ft", "change_7d_ft", "change_30d_ft", "provisional", "fetched_at",
                                                     "content_modified")},
                   "band": v["band"] if ok and v["badges"] else None, "trend": v["trend"] if ok and v["badges"] else None,
                   "freshness_at_build": v["fresh"], "soft_stale_hours": e.get("soft_stale_hours"), "hard_stale_hours": e.get("hard_stale_hours"),
                   "source": {"name": v["src"].get("credit") or v["src"].get("agency"), "url": v["src_url"]},
                   "disclaimer": f"{BRAND} is independent and is not a government website. Not for navigation, flood or safety decisions."}
            (dd / f"{lk['id']}.json").write_text(json.dumps(rec, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
            ser = self.series.get(lk["id"]) or []
            with open(dd / f"{lk['id']}.csv", "w", newline="", encoding="utf-8") as f:
                f.write(f"# {lk['name']} water level. Source: {v['src'].get('credit') or v['src'].get('agency')} ({v['src_url']}).\n")
                f.write(f"# Elevation in feet above {lk.get('datum') or 'the source datum'}. Provisional data; may be revised.\n")
                f.write(f"# {BRAND} ({SITE_URL}) is independent and is not a government website. Not for navigation, flood or safety decisions.\n")
                w = csv.writer(f)
                w.writerow(["date", "elevation_ft", "percent_full", "storage_af", "source"])
                for r in ser:
                    w.writerow([r["date"].isoformat(), "" if r["elev"] is None else f"{r['elev']:.2f}", "" if r["pct"] is None else r["pct"],
                                "" if r["storage"] is None else int(r["storage"]), r["src"]])
            idx.append([lk["id"], lk["name"], " ".join(lk.get("aliases") or []), " ".join(lk.get("states") or [lk.get("state")]),
                        lk.get("lat"), lk.get("lon"), (num(v["elev"]) + " ft" + (", " + v["when_short"] if v.get("when_short") else "")) if ok else "",
                        BAND_LABEL.get(v["band"], "") if ok and v["badges"] else ""])
        for lk in self.profiles:
            idx.append([lk["id"], lk["name"], " ".join(lk.get("aliases") or []), " ".join(lk.get("states") or [lk.get("state")]),
                        lk.get("lat"), lk.get("lon"), "", ""])
        idx.sort(key=lambda r: -((self.demand.get(r[0]) or {}).get("volume") or self.lake_by_id[r[0]].get("demand") or 0))
        (self.dist / "data" / "search.json").write_text(json.dumps(idx, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        status = dict(self.status or {})
        if not status:
            status = {"generated_at": None}
        status["pages_built"] = len(self.kind)
        status["built_at"] = iso_z(self.now)
        (self.dist / "data" / "status.json").write_text(json.dumps(status, separators=(",", ":")), encoding="utf-8")

    def static_files(self):
        sd = SRC / "static"
        if sd.is_dir():
            for p in sd.iterdir():
                if p.is_file() and not p.name.startswith("."):
                    shutil.copy(p, self.dist / p.name)
        if not (self.dist / "og-default.png").exists():
            (self.dist / "og-default.png").write_bytes(og_png())
        groups = defaultdict(list)
        for path, m in sorted(self.pages.items()):
            groups[m["group"]].append((path, m["lastmod"]))
        idx = ['<?xml version="1.0" encoding="UTF-8"?>', '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for g in ("lakes", "hubs", "sub", "pages"):
            entries = groups.get(g)
            if not entries:
                continue
            name = f"sitemap-{g}.xml"
            xs = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
            for path, lm in entries:
                xs.append(f"<url><loc>{SITE_URL}{path}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm else "") + "</url>")
            xs.append("</urlset>")
            (self.dist / name).write_text("\n".join(xs) + "\n", encoding="utf-8")
            lms = [lm for _, lm in entries if lm]
            idx.append(f"<sitemap><loc>{SITE_URL}/{name}</loc>" + (f"<lastmod>{max(lms)}</lastmod>" if lms else "") + "</sitemap>")
        idx.append("</sitemapindex>")
        (self.dist / "sitemap.xml").write_text("\n".join(idx) + "\n", encoding="utf-8")
        (self.dist / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8")
        # 301s: /index.html -> /, no-slash -> slash per built page, then the /x/index.html splat last (dynamic rules go last).
        dirs = sorted(p for p in self.kind if p != "/" and p.endswith("/"))
        red = ["/index.html / 301"]
        lake_dirs = [p for p in dirs if p.startswith("/lake/")]
        if len(dirs) > 1900:      # Cloudflare allows 2,000 static rules: fold lake pages into two placeholder rules
            red += [f"{p[:-1]} {p} 301" for p in dirs if not p.startswith("/lake/")]
            red += ["/lake/:id /lake/:id/ 301", "/lake/:id/history /lake/:id/history/ 301"]
        else:
            red += [f"{p[:-1]} {p} 301" for p in dirs]
        red.append("/*/index.html /:splat/ 301")
        (self.dist / "_redirects").write_text("\n".join(red) + "\n", encoding="utf-8")
        _ = lake_dirs

    def footer(self):
        """Site footer, the same on every page: brand and disclaimer, link columns, a compact state grid, data credits."""
        col = lambda title, links: (f'<div class="ft__c"><h2>{title}</h2><ul>' + "".join(f"<li>{self.a(p, t)}</li>" for p, t in links) + "</ul></div>")
        # most searched lakes whose reading is not past the hard limit (a lake with no current level is not promoted site-wide)
        popular = [(self.lake_url(lk), lk["name"]) for lk in self.top_lakes(40) if self.view(lk)["fresh"] != "unknown"][:6]
        lists = [("/lowest-lakes/", "Lowest lakes"), ("/fullest-lakes/", "Fullest lakes"), ("/rising-fastest/", "Rising fastest"),
                 ("/falling-fastest/", "Falling fastest")]
        guides = [(p, (m.get("h1") or m.get("title", p).split("|")[0]).split("?")[0].split(":")[0].strip())
                  for p, m in ((p, load_fragment(f)[0]) for p, f, sub in self.content_pages if sub == "guides")]
        pages = [("/about/", "About"), ("/sources/", "Data sources and methods"), ("/corrections/", "Report a wrong level"),
                 ("/disclaimer/", "Disclaimer"), ("/contact/", "Contact"), ("/privacy/", "Privacy"), ("/terms/", "Terms")]
        states = "".join(f'<li><a href="{self.state_url(c)}" aria-label="{E(self.states[c]["name"])}">{E(c)}</a></li>'
                         for c in sorted(self.hub_lakes, key=lambda c: self.states[c]["name"]) if self.state_indexed(c))
        check_txt, _, agencies = self.source_facts()
        credits = ""
        if agencies:
            credits = ('<p class="ft__src"><b>Water data:</b> ' + E(" · ".join(agencies)) + "."
                       + (f" Each source is checked every {E(check_txt)}. " if check_txt else " ")
                       + self.a("/sources/", "How we use each source") + "</p>")
        sister = " · ".join(self.ext(s["url"], s["name"]) for s in SITE.get("sister_sites") or [])
        return (
            '<div class="ft__top"><div class="ft__b">'
            f'<a class="ft__logo" href="/"><svg aria-hidden="true" width="34" height="34" viewBox="0 0 32 32"><use href="#lg"/></svg>{E(BRAND)}</a>'
            f'<p>{E(SITE.get("tagline", ""))}</p>'
            '<p class="ft__warn">Readings are provisional. Do not use this site for navigation, flood or safety decisions; in high water or severe weather '
            'follow the lake’s operator, local officials and the National Weather Service.</p></div>'
            + (col("Popular lakes", popular) if popular else "") + col("Lake lists", lists) + (col("Guides", guides) if guides else "") + col("About", pages) + "</div>"
            f'<div class="ft__st"><h2>Lake levels by state</h2><ul>{states}</ul></div>'
            + credits
            + f'<p class="ft__end"><span>© {self.now.year} {E(BRAND)}. Independent. Not a government website.</span>'
            + (f"<span>Also by us: {sister}</span>" if sister else "") + "</p>")

    # ------------------------------------------------------------ build
    def build_all(self):
        if self.dist.exists():
            shutil.rmtree(self.dist)
        self.dist.mkdir(parents=True)
        self.css = self.asset("site.css", (SRC / "assets" / "site.css").read_text(encoding="utf-8"))
        # page-type styles, loaded after site.css only by the templates that need them (each page stays within the CSS budget)
        self.page_css = {k: self.asset(f"{k}.css", (SRC / "assets" / f"{k}.css").read_text(encoding="utf-8")) for k in ("home", "lake", "hub", "content", "list")}
        self.js = self.asset("site.js", (SRC / "assets" / "site.js").read_text(encoding="utf-8"))
        self.base = (SRC / "templates" / "base.html").read_text(encoding="utf-8")
        self.footer_html = self.footer()
        for lk in self.live:
            self.lake_page(lk)
            if lk["id"] in self.history_ids:
                self.history_page(lk)
        for lk in self.profiles:
            self.profile_page(lk)
        for code in self.hub_lakes:
            self.state_page(code)
        for slug in self.sys_built:
            self.system_page(slug)
        for slug in self.ops_built:
            self.operator_page(slug)
        for how in ("lowest", "fullest", "rising", "falling"):
            self.list_page(how)
        for path, p, sub in self.content_pages:
            self.content_page(path, p, sub)
        if "/guides/" in self.planned:
            self.guides_index()
        self.home()
        self.not_found()
        self.data_files()
        self.static_files()


# ---------------------------------------------------------------- checks (plan §8.4 step 12)
def visible_text(page):
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return " ".join(html.unescape(t).split())


def check(site):
    errors, warns = list(site.errors), []
    dist = site.dist
    files = {}
    for p in dist.rglob("*.html"):
        rel = "/" + str(p.relative_to(dist)).replace("\\", "/")
        rel = rel[:-len("index.html")] if rel.endswith("/index.html") or rel == "/index.html" else rel
        files[rel] = p.read_text(encoding="utf-8")
    exist = set(files) | {"/" + str(p.relative_to(dist)).replace("\\", "/") for p in dist.rglob("*") if p.is_file()}
    inbound = defaultdict(int)
    href_re = re.compile(r'href="(/[^"#?]*)')
    allowed_band_labels = set(BAND_LABEL.values()) | {"In flood pool"}
    for path, text in files.items():
        for h in href_re.findall(text):
            if h not in exist and (h.rstrip("/") + "/index.html") not in exist and h + "index.html" not in exist:
                errors.append(f"broken link {h} on {path}")
            if h != path:
                inbound[h] += 1
        kind = site.kind.get(path, "page")
        low = NEGATIONS.sub(" ", visible_text(text)).lower()
        pats = FORBIDDEN_ALL + (FORBIDDEN_GENERATED if kind not in ("content",) else [])
        for pat in pats:
            m = re.search(pat, low)
            if m:
                errors.append(f"forbidden word /{pat}/ on {path}: “…{low[max(0, m.start() - 40):m.end() + 40]}…”")
        for key, lab in re.findall(r'class="badge badge--([a-z_]+)[^"]*">(?:<svg.*?</svg>)?([^<]*)</span>', text):
            if key != "flood" and (key not in BAND_LABEL or BAND_LABEL[key] != lab):
                errors.append(f"band badge {key}/{lab!r} outside the allowed set on {path}")
            if lab not in allowed_band_labels:
                errors.append(f"band label {lab!r} not allowed on {path}")
        for key, lab in re.findall(r'class="trend trend--([a-z_]+)"><span class="trend__lab"><span aria-hidden="true">.</span> ([^<]*)</span>', text):
            if TREND_LABEL.get(key) != lab:
                errors.append(f"trend {key}/{lab!r} outside the allowed set on {path}")
        m = re.search(r"<title>(.*?)</title>", text)
        if m and len(html.unescape(m.group(1))) > 65:
            warns.append(f"long title ({len(html.unescape(m.group(1)))}) on {path}")
    for path in site.kind:
        if path in ("/", "/404.html"):
            continue
        if inbound.get(path, 0) == 0:
            errors.append(f"orphan page {path}")
    n_lake = 0
    for lk in site.live:
        path = site.lake_url(lk)
        text = files.get(path)
        if text is None:
            errors.append(f"missing lake page {path}")
            continue
        n_lake += 1
        v = site.view(lk)
        if E(v["src_url"]) not in text:
            errors.append(f"no source link on {path}")
        if "<time " not in text:
            errors.append(f"no <time> on {path}")
        if 'class="datum"' not in text:
            errors.append(f"no datum line on {path}")
        if not DISCLAIMER_RE.search(text):
            errors.append(f"no disclaimer on {path}")
        cur = site.current.get(lk["id"]) or {}
        shown = re.findall(r'<data class="elev" value="([0-9.\-]+)">', text)
        if cur.get("elev_ft") is not None:
            if shown != [f"{cur['elev_ft']:.2f}"]:
                errors.append(f"headline number {shown} differs from current.json {cur['elev_ft']} on {path}")
        elif shown:
            errors.append(f"headline number {shown} shown but current.json has none on {path}")
        if v["fresh"] == "unknown":
            if 'class="badge badge--' in text.split('class="lk-panel"', 1)[-1].split("</section>", 1)[0]:
                errors.append(f"band badge shown on an unknown reading on {path}")
        if site.src_of(lk).get("value_kind", "").startswith("daily"):
            low = NEGATIONS.sub(" ", visible_text(text)).lower()
            if re.search(r"\blive\b|real[- ]?time", low):
                errors.append(f"'live'/'real-time' on a daily-average lake page {path}")
    print(f"pages: {len(files)} html ({n_lake} lake pages, {len(site.history_ids)} history), sitemap entries: {len(site.pages)}")
    print(f"broken internal links: {sum(1 for e in errors if e.startswith('broken'))}")
    for w in warns[:10]:
        print("warn:", w)
    if len(warns) > 10:
        print(f"warn: ... {len(warns) - 10} more")
    for e in errors[:40]:
        print("ERROR:", e)
    return errors


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--batch", type=int)
    ap.add_argument("--state")
    ap.add_argument("--data", default=os.environ.get("LLF_DATA") or str(ROOT / "data"))
    ap.add_argument("--content", default=os.environ.get("LLF_CONTENT") or str(SRC / "content"))
    ap.add_argument("--dist", default=os.environ.get("LLF_DIST") or str(ROOT / "dist"))
    a = ap.parse_args(argv)
    batch = a.batch or (int(os.environ["PUBLISH_BATCH"]) if os.environ.get("PUBLISH_BATCH") else None)
    now = parse_ts(os.environ["LLF_NOW"]) if os.environ.get("LLF_NOW") else dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    site = Site(now, a.data, a.content, a.dist, batch, a.state)
    site.build_all()
    print(f"built {len(site.kind)} pages ({len(site.pages)} indexable; {len(site.live)} lake pages, {len(site.profiles)} profiles) into {site.dist}")
    if a.check:
        errs = check(site)
        if errs:
            print(f"CHECK FAILED: {len(errs)} error(s)")
            return 1
        print("check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
