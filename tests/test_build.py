"""Front-end build tests: build.py renders a small data set and its --check rules hold (no network).

The data set is made here in a temporary folder: real readings from tests/fixtures/twdb/recent-conditions.json and the
real Travis 30-day CSV, plus clearly synthetic long series for the history-page rules (tests only, never published).
Needs pipeline/effective.py and pipeline/derive.py (agent B); if they are absent, set LLF_PIPELINE_STUB to a folder holding
a stand-in `pipeline` package, otherwise these tests are skipped.
"""
import csv
import datetime as dt
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "twdb"
HAVE_PIPELINE = (ROOT / "pipeline" / "effective.py").exists() or bool(os.environ.get("LLF_PIPELINE_STUB"))
NOW = "2026-11-23T07:30:00Z"

LAKES = [  # TWDB key, id, name, state(s), extra
    ("Travis", "lake-travis-tx", "Lake Travis", ["TX"], {"system": "highland-lakes", "operator": "lcra", "demand": 49500}),
    ("Buchanan", "lake-buchanan-tx", "Lake Buchanan", ["TX"], {"system": "highland-lakes", "operator": "lcra", "demand": 14800}),
    ("Canyon", "canyon-lake-tx", "Canyon Lake", ["TX"], {"operator": "usace-fort-worth", "demand": 33100}),
    ("Medina", "medina-lake-tx", "Medina Lake", ["TX"], {"operator": "bma", "demand": 27100, "long": True}),
    ("Texoma", "lake-texoma-tx", "Lake Texoma", ["TX", "OK"], {"operator": "usace-tulsa", "demand": 22200, "batch": 2}),
]


def make_dataset(base):
    rc = json.loads((FIX / "recent-conditions.json").read_text())
    data, content = base / "data", base / "content"
    for sub in ("ref", "series", "stats", "notes"):
        (data / sub).mkdir(parents=True)
    lakes, current = [], {}
    sys.path.insert(0, str(ROOT))
    for key, lid, name, states, x in LAKES:
        j = rc[key]
        lon, lat = j["gauge_location"]["coordinates"]
        pool = j["conservation_pool_elevation"]
        lakes.append({"id": lid, "name": name, "aliases": ["Lake Mathis"] if key == "Medina" else [], "kind": "reservoir",
                      "state": states[0], "states": states, "county": None, "lat": lat, "lon": lon, "timezone": "America/Chicago",
                      "operator": x["operator"], "system": x.get("system"),
                      "primary": {"source": "twdb", "series_id": key, "param": "elevation"}, "fallback": None, "intraday": None,
                      "extra": {}, "datum": "NGVD29", "full_pool_ft": pool, "full_pool_label": "conservation pool",
                      "full_pool_source": "twdb:conservation_pool_elevation", "dead_pool_ft": j.get("dead_pool_elevation"),
                      "capacity_af": int(j["conservation_capacity"]), "flood_control": j.get("flood_control_lake") == "Y",
                      "valid_min_ft": None, "valid_max_ft": None, "bands": None, "mapping_status": "verified",
                      "verified_on": "2026-11-22", "published": True, "page": "live", "demand": x["demand"],
                      "batch": x.get("batch", 1)})
        elev, end = j["elevation"], dt.date.fromisoformat(j["timestamp"])
        rows = []
        if key == "Travis":
            for r in csv.DictReader(ln for ln in (FIX / "travis-30day.csv").read_text().splitlines() if not ln.startswith("#")):
                rows.append((r["date"], float(r["water_level"]), float(r["percent_full"])))
        else:   # synthetic test series (400 days for Medina so a history page is built; 40 days otherwise), ending at the reading
            n = 400 if x.get("long") else 40
            for i in range(n, -1, -1):
                d = end - dt.timedelta(days=i)
                if x.get("long") and 100 <= i < 103:
                    continue        # a 3-day gap: charts must break the line here
                rows.append((d.isoformat(), round(elev - i * 0.01, 2), j["percent_full"]))
        rows = [r for r in rows if r[0] <= end.isoformat()]
        if rows[-1][0] != end.isoformat():
            rows.append((end.isoformat(), elev, j["percent_full"]))
        with open(data / "series" / f"{lid}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "elev_ft", "pct_full", "storage_af", "src", "flag"])
            for d, e, p in rows:
                w.writerow([d, f"{e:.2f}", p, "", "twdb", ""])
        by = {d: e for d, e, _ in rows}
        prev = (end - dt.timedelta(days=1)).isoformat()
        c24 = round(elev - by[prev], 2) if prev in by else None
        trend = None if c24 is None else ("rising" if c24 >= 0.05 else "falling" if c24 <= -0.05 else "steady")
        vs = round(elev - pool, 2)
        p = j["percent_full"]
        band = ("above_full" if vs >= 0.5 else "full" if p >= 97 else "near_full" if p >= 85 else "below_full" if p >= 60
                else "low" if p >= 30 else "very_low")
        current[lid] = {"lake_id": lid, "source": "twdb", "series_id": key, "elev_ft": elev, "reading_at": j["timestamp"],
                        "reading_kind": "daily_average", "pct_full": p, "pct_full_basis": "source",
                        "storage_af": int(j["volume"]), "conservation_storage_af": int(j["conservation_storage"]),
                        "area_acres": j["area"], "vs_full_pool_ft": vs, "band": band, "in_flood_pool": False,
                        "change_24h_ft": c24, "trend": trend, "change_7d_ft": None, "change_30d_ft": None, "provisional": True,
                        "quality": None, "fetched_at": "2026-11-23T06:10:04Z", "last_attempt_at": "2026-11-23T06:10:04Z",
                        "freshness": "current", "used_fallback": False, "intraday": None, "override": None,
                        "content_modified": "2026-11-23T06:10:04Z"}
    lakes.append({"id": "grand-lake-ok", "name": "Grand Lake", "aliases": [], "kind": "reservoir", "state": "OK", "states": ["OK"],
                  "lat": 36.48, "lon": -94.97, "timezone": "America/Chicago", "operator": "grda", "system": None, "primary": None,
                  "datum": None, "full_pool_ft": None, "mapping_status": "none", "published": True, "page": "profile",
                  "demand": 9900, "official_url": "https://www.grda.com/"})
    lakes.append({"id": "lake-candidate-tx", "name": "Candidate Lake", "state": "TX", "states": ["TX"], "lat": 30.0, "lon": -97.0,
                  "primary": {"source": "twdb", "series_id": "X"}, "mapping_status": "candidate", "page": "none", "published": True})
    (data / "ref" / "lakes.json").write_text(json.dumps(lakes))
    (data / "current.json").write_text(json.dumps(current))
    (data / "ref" / "sources.json").write_text(json.dumps({"twdb": {
        "id": "twdb", "agency": "Texas Water Development Board", "credit": "Texas Water Development Board, Water Data for Texas",
        "public_url": "https://www.waterdatafortexas.org/reservoirs/statewide", "value_kind": "daily_average",
        "check_every_hours": 3, "soft_stale_hours": 48, "hard_stale_hours": 96, "provisional": True,
        "disclaimer": "Reservoir data are best estimates generated by TWDB; no warranties."}}))
    (data / "ref" / "states.json").write_text(json.dumps({
        "TX": {"code": "TX", "name": "Texas", "slug": "texas", "neighbours": ["OK", "NM"], "timezones": ["America/Chicago"]},
        "OK": {"code": "OK", "name": "Oklahoma", "slug": "oklahoma", "neighbours": ["TX"], "timezones": ["America/Chicago"]}}))
    (data / "ref" / "systems.json").write_text(json.dumps({"highland-lakes": {
        "slug": "highland-lakes", "name": "Highland Lakes", "operator": "lcra", "members": ["lake-buchanan-tx", "lake-travis-tx"],
        "source_url": "https://www.waterdatafortexas.org/reservoirs/statewide", "intro": "src/content/systems/highland-lakes.html"}}))
    ops = {k: {"slug": k, "name": n, "short": s, "official_url": "https://example.org/", "has_public_feed": False, "note": None}
           for k, n, s in (("lcra", "Lower Colorado River Authority", "LCRA"), ("usace-fort-worth", "USACE Fort Worth District", "USACE Fort Worth"),
                           ("bma", "BMA", "BMA"), ("usace-tulsa", "USACE Tulsa District", "USACE Tulsa"), ("grda", "Grand River Dam Authority", "GRDA"))}
    (data / "ref" / "operators.json").write_text(json.dumps(ops))
    (data / "ref" / "demand.json").write_text(json.dumps({}))
    (data / "status.json").write_text(json.dumps({"generated_at": "2026-11-23T06:10:04Z", "sources": {}}))
    (data / "stats" / "medina-lake-tx.json").write_text(json.dumps({
        "period_start": "2025-10-19", "period_end": "2026-11-23", "n_days": 398,
        "record_high": {"elev_ft": current["medina-lake-tx"]["elev_ft"], "date": "2026-11-23"},
        "record_low": {"elev_ft": round(current["medina-lake-tx"]["elev_ft"] - 4.0, 2), "date": "2025-10-19"},
        "year_high": None, "year_low": None, "same_day_last_year": None, "median_this_date": None,
        "yearly": [{"year": 2025, "high": 1016.6, "high_date": "2025-12-31", "low": 1015.85, "low_date": "2025-10-19", "n": 74},
                   {"year": 2026, "high": 1019.85, "high_date": "2026-11-23", "low": 1016.6, "low_date": "2026-01-01", "n": 324}],
        "last_14": [], "spark_30": []}))
    for sub, names in (("pages", ("about", "sources", "disclaimer", "corrections", "privacy", "terms", "contact")),
                       ("guides", ("what-is-full-pool",))):
        (content / sub).mkdir(parents=True)
        for n in names:
            (content / sub / f"{n}.html").write_text(
                f"<!--\ntitle: {n.title()} | {{{{site.name}}}}\ndescription: Test page.\nh1: {n.title()}\nupdated: 2026-11-23\n-->\n"
                f"<p>Test copy for {{{{site.name}}}}: {{{{count.live_lakes}}}} lakes, mail {{{{site.contact_email}}}}.</p>\n")
    return data, content


@unittest.skipUnless(HAVE_PIPELINE, "pipeline/effective.py not present (set LLF_PIPELINE_STUB to test before merge)")
class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llf-build-"))
        self.data, self.content = make_dataset(self.tmp)
        self.dist = self.tmp / "dist"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, *args, now=NOW, check=True):
        env = dict(os.environ, LLF_NOW=now)
        env.pop("PUBLISH_BATCH", None)
        cmd = [sys.executable, str(ROOT / "build.py"), "--data", str(self.data), "--content", str(self.content),
               "--dist", str(self.dist)] + (["--check"] if check else []) + list(args)
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)

    def page(self, path):
        p = self.dist / path.lstrip("/") / "index.html" if path.endswith("/") else self.dist / path.lstrip("/")
        return p.read_text(encoding="utf-8")

    def test_full_build_passes_check(self):
        r = self.build("--batch", "9")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("check OK", r.stdout)
        cur = json.loads((self.data / "current.json").read_text())
        t = self.page("/lake/lake-travis-tx/")
        self.assertIn("<title>Lake Travis Water Level Today &amp; % Full | Lake Level Finder</title>", t)
        self.assertIn('<link rel="canonical" href="https://lakelevelfinder.com/lake/lake-travis-tx/">', t)
        self.assertIn(f'<data class="elev" value="{cur["lake-travis-tx"]["elev_ft"]:.2f}">', t)
        end = dt.date.fromisoformat(cur["lake-travis-tx"]["reading_at"]) + dt.timedelta(days=1)
        self.assertIn(f'data-reading-ts="{end.isoformat()}T06:00:00Z"', t)   # end of the reading's day in Austin (CST), as UTC
        self.assertIn('data-soft-h="48"', t)
        self.assertNotRegex(re.sub(r"<[^>]+>", " ", t.split("<title>")[1].split("</title>")[0]), r"\d{3}\.\d")  # no number in title
        self.assertIn("Daily average for <time", t)
        self.assertIn("Do not use this page for navigation, flood or safety decisions", t)
        for typ in ('"BreadcrumbList"', '"WebPage"', '"dateModified":"2026-11-23T06:10:04Z"', '"Reservoir"', '"FAQPage"',
                    '"name":"Lake Level Finder"'):
            self.assertIn(typ, t)
        # profile: noindex, not in the sitemap, but linked from the Oklahoma hub (no orphan)
        g = self.page("/lake/grand-lake-ok/")
        self.assertIn('content="noindex,follow"', g)
        sm = "".join(p.read_text() for p in self.dist.glob("sitemap-*.xml"))
        self.assertIn("/lake/lake-travis-tx/</loc><lastmod>2026-11-23T06:10:04Z", sm)
        self.assertNotIn("grand-lake-ok", sm)
        self.assertNotIn("lake-candidate-tx", sm)
        self.assertFalse((self.dist / "lake" / "lake-candidate-tx").exists())
        # hubs: Texas has >= 3 live lakes (indexed), Oklahoma only Texoma (noindex)
        self.assertNotIn("noindex", self.page("/texas/").split("</head>")[0])
        self.assertIn("noindex", self.page("/oklahoma/").split("</head>")[0])
        self.assertIn("/texas/</loc>", sm)
        self.assertNotIn("/oklahoma/</loc>", sm)
        # history only with >= 365 days of series; the gap breaks the line (two subpaths in the path)
        self.assertTrue((self.dist / "lake/medina-lake-tx/history/index.html").exists())
        self.assertFalse((self.dist / "lake/lake-travis-tx/history").exists())
        h = self.page("/lake/medina-lake-tx/history/")
        self.assertIn('"Dataset"', h)
        self.assertIn("/data/lake/medina-lake-tx.csv", h)
        self.assertGreaterEqual(len(re.findall(r'<path class="ln" d="M[^"]*M', h)), 1)
        self.assertIn("(partial)", h)
        self.assertIn("Period of record", h)                       # history hero and record tiles (from the series only)
        self.assertIn("Highest in record", h)
        self.assertIn('href="/lake/medina-lake-tx/"', h)            # link back to the lake page
        lw = self.page("/lowest-lakes/")
        self.assertIn('class="lst-tabs"', lw)                      # the four lists link to each other
        for p in ("/fullest-lakes/", "/rising-fastest/", "/falling-fastest/"):
            self.assertIn(f'href="{p}"', lw)
        self.assertIn('"ItemList"', lw)
        # data, headers, redirects, robots
        self.assertTrue((self.dist / "data/lake/lake-travis-tx.json").exists())
        csvt = (self.dist / "data/lake/lake-travis-tx.csv").read_text()
        self.assertTrue(csvt.startswith("# Lake Travis water level. Source: Texas Water Development Board"))
        self.assertIn("date,elevation_ft,percent_full,storage_af,source", csvt)
        self.assertIn("X-Robots-Tag: noindex", (self.dist / "_headers").read_text())
        red = (self.dist / "_redirects").read_text()
        self.assertIn("/lake/lake-travis-tx /lake/lake-travis-tx/ 301", red)
        self.assertTrue(red.strip().endswith("/*/index.html /:splat/ 301"))
        self.assertIn("Sitemap: https://lakelevelfinder.com/sitemap.xml", (self.dist / "robots.txt").read_text())
        self.assertIn("Test copy for Lake Level Finder: 5 lakes", self.page("/about/"))
        self.assertIn("<title>Lake Level Finder: US Lake &amp; Reservoir Water Levels Today</title>", self.page("/"))

    def test_batch_holds_pages_and_links_become_text(self):
        r = self.build("--batch", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.dist / "lake/lake-texoma-tx").exists())
        self.assertNotIn('href="/lake/lake-texoma-tx/"', self.page("/texas/"))
        self.assertFalse((self.dist / "oklahoma").exists() and "Texoma" in self.page("/oklahoma/"))

    def test_hubs_link_every_lake(self):
        r = self.build("--batch", "9")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lakes = json.loads((self.data / "ref/lakes.json").read_text())
        tx = self.page("/texas/")
        for lk in lakes:
            if lk.get("page") == "live" and lk.get("mapping_status") == "verified" and "TX" in (lk.get("states") or [lk.get("state")]):
                self.assertIn(f'href="/lake/{lk["id"]}/"', tx)
        for path in ("/texas/", "/system/highland-lakes/"):
            t = self.page(path)
            self.assertIn('class="pg-hub"', t)
            self.assertIn("/assets/hub.", t)
            self.assertIn('<symbol id="i-', t)
            self.assertEqual(len(re.findall(r"<h1[ >]", t)), 1)
            self.assertNotIn("data-filter", t)      # site.js filters tables only; hubs use lists
        self.assertIn('"FAQPage"', tx)

    def test_state_filter(self):
        r = self.build("--state", "TX", "--batch", "9")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.dist / "texas/index.html").exists())
        self.assertFalse((self.dist / "oklahoma").exists())
        self.assertFalse((self.dist / "lake/grand-lake-ok").exists())

    def test_hard_stale_shows_unknown_and_leaves_lists(self):
        r = self.build("--batch", "9", now="2026-12-10T12:00:00Z")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        t = self.page("/lake/lake-travis-tx/")
        self.assertIn('<p class="pn__unk">Current level unknown</p>', t)
        panel = t.split('class="lk-panel"', 1)[1].split("</section>", 1)[0]
        self.assertNotIn("badge badge--", panel)
        self.assertIn("Last reading:", panel)
        self.assertNotIn("lake-travis-tx", self.page("/lowest-lakes/"))

    def test_check_rejects_live_page_for_unverified_lake(self):
        lakes = json.loads((self.data / "ref/lakes.json").read_text())
        for lk in lakes:
            if lk["id"] == "lake-candidate-tx":
                lk["page"] = "live"
        (self.data / "ref/lakes.json").write_text(json.dumps(lakes))
        r = self.build("--batch", "9")
        self.assertEqual(r.returncode, 1)
        self.assertIn("lake-candidate-tx", r.stdout)

    def test_content_pages_layout(self):
        (self.content / "pages" / "contact.html").write_text(
            "<!--\ntitle: Contact | {{site.name}}\ndescription: How to reach us.\nh1: Contact\nupdated: 2026-11-23\n-->\n"
            '<p>Email: <a href="mailto:{{site.contact_email}}">{{site.contact_email}}</a></p>\n'
            "<h2>First part</h2><p>a</p><h2 id=\"kept\">Second part</h2><p>b</p><h2>Third part</h2><p>c</p>\n")
        r = self.build("--batch", "9")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        c = self.page("/contact/")
        self.assertRegex(c, r'<link rel="stylesheet" href="/assets/content\.[0-9a-f]+\.css">')
        self.assertIn("<h1>Contact</h1>", c)
        self.assertIn('Updated <time datetime="2026-11-23">Nov 23, 2026</time>', c)
        self.assertIn("1 min read", c)
        self.assertIn('class="cnt-act"', c)                               # mail action card, address from site.json
        self.assertIn('href="mailto:contact@lakelevelfinder.com"', c)
        self.assertIn('<h2 id="first-part">First part</h2>', c)           # ids added, existing ids kept, contents in both forms
        self.assertIn('<h2 id="kept">Second part</h2>', c)
        self.assertIn('<a href="#third-part">Third part</a>', c)
        self.assertIn('class="cnt-otp"', c)
        self.assertIn('class="toc cnt-toc"', c)
        self.assertIn('class="cnt-src"', self.page("/sources/"))         # source cards keep the freshness facts
        self.assertIn("“Current level unknown” after", self.page("/sources/"))
        gi = self.page("/guides/")
        self.assertIn('href="/guides/what-is-full-pool/"', gi)
        self.assertIn('"ItemList"', gi)
        g = self.page("/guides/what-is-full-pool/")
        self.assertIn('class="cnt-fig"', g)                              # the zones diagram, generic (no lake numbers)
        self.assertIn('"Article"', g)
        self.assertIn('class="cnt-cta"', g)
        nf = self.page("/404.html")
        self.assertIn('content="noindex,follow"', nf)
        self.assertIn('id="find"', nf)
        self.assertIn("<h1>Page not found</h1>", nf)

    def test_check_rejects_forbidden_words(self):
        (self.content / "pages" / "about.html").write_text(
            "<!--\ntitle: About\ndescription: x\nh1: About\nupdated: 2026-11-23\n-->\n<p>The lake is safe to swim today.</p>\n")
        r = self.build("--batch", "9")
        self.assertEqual(r.returncode, 1)
        self.assertIn("forbidden word", r.stdout)

    def test_check_rejects_headline_mismatch_and_bad_band(self):
        r = self.build("--batch", "9", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        p = self.dist / "lake/canyon-lake-tx/index.html"
        t = p.read_text()
        t = re.sub(r'<data class="elev" value="[0-9.]+">', '<data class="elev" value="1.23">', t, count=1)
        t = t.replace('badge--near_full', 'badge--good', 1)
        p.write_text(t)
        os.environ["LLF_NOW"] = NOW
        sys.path.insert(0, str(ROOT))
        build = importlib.import_module("build")
        site = build.Site(build.parse_ts(NOW), self.data, self.content, self.dist, 9, None)
        site.kind = {}  # only the per-lake checks matter here
        errs = build.check(site)
        self.assertTrue(any("headline number" in e and "canyon-lake-tx" in e for e in errs), errs)


class ChartTest(unittest.TestCase):
    def test_gaps_break_the_line_and_titles_exist(self):
        sys.path.insert(0, str(ROOT))
        charts = importlib.import_module("charts")
        d0 = dt.date(2026, 1, 1)
        pts = [(d0 + dt.timedelta(days=i), 100 + i * 0.1) for i in range(30) if not 10 <= i < 13]
        self.assertEqual(len(charts.segments(pts, 1)), 2)
        svg = charts.line_chart(pts, title="Test lake, last 30 days", desc="desc", full_pool=101.0)
        self.assertIn("<title", svg)
        self.assertIn('class="pool"', svg)
        self.assertEqual(len(re.findall(r'<path class="ln" d="M[^"]*M', svg)), 1)
        far = charts.line_chart(pts, title="t", desc="d", full_pool=200.0)
        self.assertIn("above this chart", far)
        self.assertIn("<svg", charts.sparkline(pts, full_pool=101.0))
        self.assertLess(len(charts.line_chart([(d0 + dt.timedelta(days=i), 600 + (i % 37) * 0.3) for i in range(366)],
                                              title="y", desc="d", full_pool=610.0)), 6000)


if __name__ == "__main__":
    unittest.main()
