"""Adapters against saved real responses (2026-11-23) and broken variants. No network."""
import datetime as dt
import json
import unittest

from helpers import FixtureGet, broken, fixture  # (importing helpers sets the env first)

from pipeline.fetcher import FetchError, check_body
from pipeline.sources import SourceError, cdec, cwms, num, rise, twdb, usgs

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 23, 8, 0, tzinfo=UTC)


class MissingMarkers(unittest.TestCase):
    def test_num(self):
        for v in ("---", "-9999", -9999, -999, "-999", None, "", "null", float("nan"), " --- "):
            self.assertIsNone(num(v), v)
        self.assertEqual(num("1019.79"), 1019.79)
        self.assertEqual(num(972.6), 972.6)
        self.assertEqual(num("2,274,485"), 2274485.0)

    def test_html_refused(self):
        for name in ("html-error.html", "twdb-404.html"):
            with self.assertRaises(FetchError):
                check_body(broken(name), "json")
            with self.assertRaises(FetchError):
                check_body(broken(name), "csv")
        with self.assertRaises(FetchError):
            check_body(b"{not json", "json")
        check_body(fixture("cdec/daily.csv"), "csv")


class TWDB(unittest.TestCase):
    def setUp(self):
        self.s = twdb.parse(fixture("twdb/recent-conditions.json"))

    def test_parse(self):
        self.assertEqual(len(self.s), 123)
        tr = self.s["Travis"]
        self.assertEqual(tr["points"], [(dt.date(2026, 11, 22), 681.14)])
        self.assertEqual(tr["extras"]["conservation_pool_elevation"], 681.0)
        self.assertEqual(tr["extras"]["percent_full"], 100.0)
        self.assertEqual(tr["extras"]["flood_control_lake"], "Y")
        self.assertEqual(self.s["Medina"]["extras"]["percent_full"], 27.9)
        self.assertIsNone(self.s["Addicks"]["extras"]["percent_full"])  # flood-detention: null % full
        self.assertIn("Lake O' the Pines", self.s)
        twdb.check(self.s, {"min_count": 118}, NOW)

    def test_fetch_and_missing_key(self):
        series, errs = twdb.fetch([{"series_id": "Travis"}, {"series_id": "NoSuchLake"}], NOW, FixtureGet())
        self.assertIn("Travis", series)
        self.assertIn("NoSuchLake", errs)

    def test_wrong_count(self):
        with self.assertRaises(SourceError):
            twdb.fetch([], NOW, FixtureGet({"recent-conditions": broken("twdb-wrong-count.json")}),
                       {"min_count": 118})

    def test_not_object(self):
        with self.assertRaises(SourceError):
            twdb.parse(broken("twdb-not-object.json"))

    def test_html_body(self):
        with self.assertRaises(FetchError):
            twdb.fetch([], NOW, FixtureGet({"recent-conditions": broken("html-error.html")}))

    def test_too_many_stale(self):
        later = NOW + dt.timedelta(days=6)
        with self.assertRaises(SourceError):
            twdb.check(self.s, {"min_count": 118}, later)

    def test_null_percent(self):
        s = twdb.parse(broken("twdb-null-percent.json"))
        self.assertIsNone(s["Travis"]["extras"]["percent_full"])
        self.assertNotIn("pct_full_points", s["Travis"]["extras"])

    def test_csv(self):
        p = twdb.parse_csv(fixture("twdb/travis-30day.csv"))
        self.assertEqual(p["datum"], "NAVD88 +0.6")
        self.assertEqual(p["conservation_pool"], ["681.0 feet above NAVD88 +0.6"])
        self.assertEqual(p["dead_pool"], ["535.75 feet above NAVD88 +0.6"])
        self.assertEqual(p["rows"][0]["date"], dt.date(2026, 10, 23))
        self.assertEqual(p["rows"][0]["water_level"], 677.37)
        self.assertEqual(p["rows"][-1]["date"], dt.date(2026, 11, 22))
        self.assertEqual(twdb.missing_dates(p["rows"]), [])
        gap = [r for r in p["rows"] if r["date"] != dt.date(2026, 11, 1)]
        self.assertEqual(twdb.missing_dates(gap), [dt.date(2026, 11, 1)])
        s = twdb.csv_series(fixture("twdb/medina-30day.csv"))
        self.assertEqual(s["points"][-1], (dt.date(2026, 11, 23), 1019.85))

    def test_csv_html_rejected(self):
        with self.assertRaises(SourceError):
            twdb.parse_csv(broken("twdb-404.html"))


class CWMS(unittest.TestCase):
    def test_table_rock(self):
        pts, meta = cwms.parse_page(fixture("cwms/table-rock-ts.json"))
        self.assertEqual(meta["units"], "ft")
        self.assertEqual(meta["time-zone"], "America/Chicago")
        self.assertEqual(len(pts), 200)
        self.assertEqual(pts[-1], (dt.datetime(2026, 11, 23, 7, 0, tzinfo=UTC), 909.42))  # rounded from 909.4199..
        self.assertEqual(meta["quality_codes"], {0: 200})

    def test_lanier_two_pages(self):
        get = FixtureGet()
        s = cwms.fetch_series("SAM", "Buford.Elev.Inst.15Minutes.0.Raw-USGS", NOW, get)
        self.assertEqual(len(get.urls), 2)
        self.assertIn("page=", get.urls[1])
        self.assertTrue(all("unit=EN" in u for u in get.urls))
        self.assertEqual(len(s["points"]), 794)
        self.assertEqual(s["points"][-1], (dt.datetime(2026, 11, 23, 6, 15, tzinfo=UTC), 1061.84))

    def test_metric_rejected(self):
        with self.assertRaises(SourceError):
            cwms.parse_page(broken("cwms-table-rock-metric.json"))

    def test_trailing_nulls_dropped(self):
        pts, _ = cwms.parse_page(broken("cwms-trailing-nulls.json"))
        self.assertEqual(len(pts), 197)

    def test_fetch_isolates_failures(self):
        tg = [{"series_id": "Table_Rock_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev", "office": "SWL"},
              {"series_id": "SAM:Buford.Elev.Inst.15Minutes.0.Raw-USGS"},
              {"series_id": "NoOffice.Elev"}]
        series, errs = cwms.fetch(tg, NOW, FixtureGet({"Table_Rock": broken("html-error.html")}))
        self.assertIn("SAM:Buford.Elev.Inst.15Minutes.0.Raw-USGS", series)
        self.assertIn("Table_Rock_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev", errs)
        self.assertIn("NoOffice.Elev", errs)

    def test_levels(self):
        lv = cwms.parse_levels(fixture("cwms/table-rock-levels.json"), NOW)
        self.assertEqual(lv["Top of Conservation"], 917.0)  # 1906 record: 917 until 1 December
        self.assertEqual(lv["Bottom of Normal"], 881.0)
        self.assertEqual(lv["Top of Flood"], 931.0)
        for when, want in ((dt.datetime(2026, 7, 1, tzinfo=UTC), 917.0), (dt.datetime(2026, 1, 15, tzinfo=UTC), 915.0),
                           (dt.datetime(2026, 12, 15, tzinfo=UTC), 915.0), (dt.datetime(2026, 5, 15, 6, tzinfo=UTC), 916.48)):
            got = cwms.parse_levels(fixture("cwms/table-rock-levels.json"), when)["Top of Conservation"]
            self.assertEqual(got, want, when)

    def test_full_pool_level_on_fetch(self):
        tg = [{"series_id": "Table_Rock_Dam-Headwater.Elev.Inst.1Hour.0.Decodes-rev", "office": "SWL",
               "full_pool_level": "Table_Rock_Dam.Elev-Pool.Inst.0.Top of Conservation"}]
        series, errs = cwms.fetch(tg, NOW, FixtureGet())
        ex = series[tg[0]["series_id"]]["extras"]
        self.assertEqual(ex["full_pool_ft"], 917.0)
        self.assertEqual(ex["full_pool_label"], "Top of Conservation")

    def test_levels_error_body(self):
        with self.assertRaises(SourceError):
            cwms.parse_levels(broken("cwms-levels-400.json"), NOW)


class RISE(unittest.TestCase):
    def test_result(self):
        pts = rise.parse_result(fixture("rise/mead-result.json"), 6123)
        self.assertEqual(pts[-1], (dt.date(2026, 11, 21), 1041.41))
        self.assertIn((dt.date(2026, 11, 20), 1041.36), pts)

    def test_location_listing_rejected(self):
        with self.assertRaises(SourceError):
            rise.parse_result(broken("rise-location-listing.json"), 6123)

    def test_null_latest_dropped(self):
        pts = rise.parse_result(broken("rise-null-latest.json"), 6123)
        self.assertEqual(pts[-1][0], dt.date(2026, 11, 20))

    def test_fetch_always_date_filtered(self):
        get = FixtureGet()
        series, errs = rise.fetch([{"series_id": "6123"}, {"series_id": 6124}], NOW, get)
        self.assertEqual(errs, {})
        self.assertEqual(series["6123"]["units"], "ft")
        self.assertEqual(series["6124"]["units"], "af")
        self.assertEqual(series["6124"]["points"][-1], (dt.date(2026, 11, 21), 7075060.0))
        results = [u for u in get.urls if "/result?" in u]
        self.assertTrue(results and all("dateTime%5Bafter%5D=" in u for u in results))

    def test_download(self):
        meta, pts = rise.parse_download(fixture("rise/mead-download.json"))
        self.assertEqual(meta["Units"], "ft")
        self.assertEqual(pts[0], (dt.date(2026, 11, 1), 1038.93))


class CDEC(unittest.TestCase):
    def test_daily(self):
        s = cdec.parse_daily_csv(fixture("cdec/daily.csv"), ["SHA", "FOL", "ORO"])
        sha = s["SHA"]
        self.assertEqual(sha["kind"], "daily_average")
        self.assertEqual(sha["points"][-1], (dt.date(2026, 11, 21), 972.58))  # 11-22, 11-23 are '---'
        self.assertEqual(dict(sha["extras"]["storage_points"])[dt.date(2026, 11, 20)], 2274485.0)
        self.assertEqual(dict(s["FOL"]["points"])[dt.date(2026, 11, 20)], 398.61)  # DATE TIME, not OBS DATE

    def test_hourly(self):
        s = cdec.parse_hourly_json(fixture("cdec/hourly.json"), ["SHA"])
        last_t, last_v = s["SHA"]["points"][-1]
        self.assertEqual(last_t, dt.datetime(2026, 11, 23, 6, 0, tzinfo=UTC))  # 2026-11-22 22:00 PST
        self.assertEqual(last_v, 972.57)
        self.assertTrue(all(v > 0 for _, v in s["SHA"]["points"]))  # -9999 dropped

    def test_all_missing(self):
        series, errs = cdec.fetch([{"series_id": "SHA"}], NOW, FixtureGet({"CSVDataServlet": broken("cdec-daily-missing.csv")}))
        self.assertEqual(series["SHA"]["points"], [])
        self.assertIn("SHA", errs)

    def test_metric_units_rejected(self):
        with self.assertRaises(SourceError):
            cdec.parse_daily_csv(broken("cdec-daily-metric.csv"))

    def test_header_change_rejected(self):
        with self.assertRaises(SourceError):
            cdec.parse_daily_csv(b"STATION,VALUE\nSHA,972.6\n")


class USGS(unittest.TestCase):
    def test_latest_follows_next(self):
        get = FixtureGet()
        rows = usgs.get_all(usgs.latest_url("Texas", {"62614"}), get)
        self.assertEqual(len(rows), 107)  # 50 + 50 + 7: three real pages chained by their next links
        self.assertTrue(get.urls[1].startswith("https://api.waterdata.usgs.gov/ogcapi/v1/"))

    def test_parse_latest(self):
        rows, _ = usgs.parse_page(fixture("usgs/latest-continuous-tx.json"))
        series, errs = usgs.parse_latest(rows, {"USGS-08179500": "62614", "USGS-08179500X": "62614"})
        pts = series["USGS-08179500"]["points"]
        self.assertEqual(len(pts), 1)
        self.assertIsInstance(pts[0][1], float)
        self.assertIn("USGS-08179500X", errs)

    def test_parameter_mismatch(self):
        rows, _ = usgs.parse_page(fixture("usgs/latest-continuous-tx.json"))
        series, errs = usgs.parse_latest(rows, {"USGS-08179500": "62615"})
        self.assertIn("62615", errs["USGS-08179500"])
        with self.assertRaises(SourceError):
            usgs.to_series(rows, "62615", "USGS-08179500")

    def test_continuous(self):
        rows, nxt = usgs.parse_page(fixture("usgs/continuous-medina.json"))
        self.assertIsNone(nxt)
        s = usgs.to_series(rows, "62614", "USGS-08179500")
        self.assertEqual(s["points"][-1], (dt.datetime(2026, 11, 23, 7, 0, tzinfo=UTC), 1019.85))
        self.assertEqual(s["extras"]["quality"], "Provisional")

    def test_metric_and_missing(self):
        rows, _ = usgs.parse_page(broken("usgs-continuous-metric.json"))
        with self.assertRaises(SourceError):
            usgs.to_series(rows, "62614", "USGS-08179500")
        rows, _ = usgs.parse_page(broken("usgs-latest-missing.json"))
        series, _ = usgs.parse_latest(rows, {"USGS-08179500": "62614"})
        self.assertEqual(series["USGS-08179500"]["points"], [])

    def test_daily_mean_only(self):
        rows, _ = usgs.parse_page(fixture("usgs/daily-medina.json"))
        s = usgs.daily_series(rows, "62614", "USGS-08179500")
        self.assertEqual(s["points"][0], (dt.date(2026, 11, 10), 1018.80))
        self.assertEqual(len(s["points"]), 10)  # statistic 00003 only (min/max/others dropped)

    def test_fetch_roles(self):
        tg = [{"series_id": "USGS-08179500", "param": "62614", "role": "intraday", "state": "TX"},
              {"series_id": "USGS-X", "param": "62614", "role": "intraday", "state": "ZZ"}]
        series, errs = usgs.fetch(tg, NOW, FixtureGet())
        self.assertIn("USGS-08179500", series)
        self.assertIn("unknown state", errs["USGS-X"])

    def test_not_geojson(self):
        with self.assertRaises(SourceError):
            usgs.parse_page(json.dumps({"collections": []}))


if __name__ == "__main__":
    unittest.main()
