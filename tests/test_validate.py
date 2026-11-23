"""Validation rules (plan §8.4-8.6) and end-to-end runs of pipeline/run.py on saved responses."""
import datetime as dt
import json
import unittest

from helpers import NOW, TMP_DATA, broken, lakes_by_id, load, reset_data, run_pipeline

from pipeline import series
from pipeline.common import parse_ts
from pipeline.fetcher import FetchError
from pipeline.validate import decide, valid_range

LAKES = lakes_by_id()
T = parse_ts(NOW)
MEDINA = LAKES["medina-lake-tx"]
TABLE_ROCK = LAKES["table-rock-lake-mo"]


def cand(**kw):
    r = {"lake_id": "medina-lake-tx", "source": "twdb", "series_id": "Medina", "elev_ft": 1019.85,
         "reading_at": "2026-11-23", "reading_kind": "daily_average", "pct_full": 27.9, "units": "ft"}
    r.update(kw)
    return r


PREV = cand(elev_ft=1019.83, reading_at="2026-11-22")


class Rules(unittest.TestCase):
    def test_range_from_pools(self):
        self.assertEqual(valid_range(MEDINA), (912.2 - 5.0, 1064.2 + 60.0))
        self.assertEqual(valid_range(TABLE_ROCK), (876.0, 947.0))

    def test_accept_and_same(self):
        self.assertEqual(decide(cand(), PREV, None, MEDINA, T)[0], "accept")
        self.assertEqual(decide(cand(), cand(), None, MEDINA, T)[0], "same")
        # a revised value for the same day is accepted
        self.assertEqual(decide(cand(elev_ft=1019.9), cand(), None, MEDINA, T)[0], "accept")

    def test_rejections(self):
        cases = [cand(elev_ft=800.0), cand(elev_ft=1200.0), cand(pct_full=101.0), cand(pct_full=-1.0),
                 cand(units="m"), cand(elev_ft=None), cand(reading_at="2026-11-24T12:00:00Z"),
                 cand(reading_at="2026-11-21")]
        for c in cases:
            action, reason, label, _ = decide(c, PREV, None, MEDINA, T)
            self.assertEqual(action, "reject", c)
            self.assertEqual(label, "range-reject", c)

    def test_future_rule(self):
        self.assertEqual(decide(cand(reading_at="2026-11-23"), PREV, None, MEDINA, T)[0], "accept")  # today: partial day
        self.assertEqual(decide(cand(reading_at="2026-11-24"), PREV, None, MEDINA, T)[0], "reject")
        hourly = {"lake_id": "table-rock-lake-mo", "source": "cwms", "elev_ft": 909.4, "units": "ft"}
        ok = dict(hourly, reading_at="2026-11-23T09:30:00Z")
        bad = dict(hourly, reading_at="2026-11-23T10:30:00Z")
        self.assertEqual(decide(ok, None, None, TABLE_ROCK, T)[0], "accept")
        self.assertEqual(decide(bad, None, None, TABLE_ROCK, T)[0], "reject")

    def test_jump_hold_then_confirm(self):
        jump = cand(elev_ft=1040.0)
        action, reason, label, pend = decide(jump, PREV, None, MEDINA, T)
        self.assertEqual((action, label), ("hold", "jump-held"))
        self.assertEqual(pend["seen"], 1)
        self.assertEqual(decide(cand(elev_ft=1040.3), PREV, pend, MEDINA, T)[0], "accept")  # within 0.5 ft
        action, _, _, pend2 = decide(cand(elev_ft=1045.0), PREV, pend, MEDINA, T)
        self.assertEqual((action, pend2["seen"], pend2["elev_ft"]), ("hold", 2, 1045.0))
        self.assertEqual(decide(jump, PREV, None, MEDINA, T, release=True)[0], "accept")

    def test_jump_scales_with_gap(self):
        week_old = cand(elev_ft=1000.0, reading_at="2026-11-16")
        self.assertEqual(decide(cand(elev_ft=1019.85), week_old, None, MEDINA, T)[0], "accept")  # 19.85 ft in 7 days


class Pipeline(unittest.TestCase):
    """End to end on the saved responses with the 7 test lakes."""

    def setUp(self):
        reset_data()

    def test_first_run(self):
        run_pipeline()
        cur = load("current.json")
        self.assertEqual(len(cur), 7)
        tr = cur["lake-travis-tx"]
        self.assertEqual((tr["elev_ft"], tr["reading_at"], tr["band"], tr["pct_full"]), (681.14, "2026-11-22", "full", 100.0))
        self.assertEqual(tr["vs_full_pool_ft"], 0.14)
        self.assertEqual(tr["freshness"], "current")
        self.assertIsNone(tr["trend"])                           # no previous day stored yet
        me = cur["medina-lake-tx"]
        self.assertEqual(me["band"], "very_low")
        self.assertEqual(me["intraday"]["series_id"], "USGS-08179500")
        self.assertEqual(me["intraday"]["source"], "usgs")
        trk = cur["table-rock-lake-mo"]
        self.assertEqual((trk["elev_ft"], trk["full_pool_ft"], trk["vs_full_pool_ft"]), (909.42, 917.0, -7.58))
        self.assertEqual(trk["band"], "below_full")
        self.assertIn(trk["trend"], ("steady", "rising", "falling"))
        self.assertIsNotNone(trk["change_7d_ft"])
        self.assertEqual(cur["lake-lanier-ga"]["band"], "level_only")
        mead = cur["lake-mead-nv"]
        self.assertEqual((mead["elev_ft"], mead["reading_at"], mead["storage_af"]), (1041.41, "2026-11-21", 7075060))
        self.assertEqual(mead["trend"], "rising")                # 1041.41 - 1041.36 from the 10-day window
        sha = cur["shasta-lake-ca"]
        self.assertEqual((sha["source"], sha["reading_at"], sha["used_fallback"]), ("cdec", "2026-11-21", False))
        st = load("status.json")
        self.assertTrue(all(st["sources"][s]["ok"] for s in ("twdb", "cwms", "rise", "cdec", "usgs")))
        self.assertEqual(st["lakes_total"], 7)
        self.assertEqual(st["sources"]["twdb"]["lakes"], 3)
        self.assertEqual(st["alerts"], [])
        self.assertTrue((TMP_DATA / "series" / "lake-mead-nv.csv").exists())
        self.assertTrue((TMP_DATA / "raw" / "twdb" / "latest.json").exists())
        self.assertTrue((TMP_DATA / "raw" / "twdb" / "2026-11-23.json").exists())

    def test_daily_step_and_trend_from_series(self):
        series.write("lake-travis-tx", [{"date": dt.date(2026, 11, 21), "elev_ft": 681.26, "pct_full": 100.0,
                                          "storage_af": None, "src": "twdb", "flag": "bf"}])
        run_pipeline("2026-11-23T10:00:00Z")
        cur = load("current.json")
        self.assertEqual(cur["lake-travis-tx"]["change_24h_ft"], -0.12)
        self.assertEqual(cur["lake-travis-tx"]["trend"], "falling")
        st = load("status.json")
        self.assertEqual(st["last_daily"], "2026-11-23")
        stats = json.loads((TMP_DATA / "stats" / "table-rock-lake-mo.json").read_text())
        self.assertGreater(stats["n_days"], 5)                  # end-of-day rows from the 8-day window
        rows = series.read("table-rock-lake-mo")
        self.assertTrue(all(r["flag"] == "eod" for r in rows))
        self.assertLess(rows[-1]["date"], dt.date(2026, 11, 23))  # today is incomplete
        self.assertFalse(json.loads((TMP_DATA / "stats" / "lake-travis-tx.json").read_text())["has_history"])

    def test_not_due_sources_are_skipped(self):
        run_pipeline()
        get = run_pipeline("2026-11-23T09:00:00Z")
        urls = " ".join(get.urls)
        self.assertNotIn("recent-conditions", urls)              # TWDB every 3 h
        self.assertNotIn("data.usbr.gov", urls)                  # RISE every 6 h
        self.assertIn("cwms-data", urls)                         # CWMS every run

    def test_wrong_count_keeps_last_good(self):
        run_pipeline()
        before = load("current.json")["lake-travis-tx"]
        run_pipeline("2026-11-23T11:30:00Z", {"recent-conditions": broken("twdb-wrong-count.json")})
        after = load("current.json")["lake-travis-tx"]
        self.assertEqual(after["elev_ft"], before["elev_ft"])
        self.assertEqual(after["fetched_at"], before["fetched_at"])
        st = load("status.json")
        self.assertFalse(st["sources"]["twdb"]["ok"])
        self.assertTrue(any(a.startswith("count-mismatch: twdb") for a in st["alerts"]))
        self.assertTrue(st["sources"]["cwms"]["ok"])            # other sources unaffected

    def test_html_body_and_one_lake_failing(self):
        run_pipeline(swap={"Table_Rock_Dam-Headwater": broken("html-error.html")})
        cur = load("current.json")
        self.assertNotIn("table-rock-lake-mo", cur)
        self.assertIn("lake-lanier-ga", cur)                     # same source, other lake fine
        st = load("status.json")
        self.assertIn("table-rock-lake-mo", st["lake_notes"])
        self.assertTrue(st["sources"]["cwms"]["ok"])

    def test_network_failures_alert_after_three(self):
        run_pipeline()
        err = FetchError("timed out")
        for i, t in enumerate(("2026-11-23T11:30:00Z", "2026-11-23T14:30:00Z", "2026-11-23T17:30:00Z")):
            run_pipeline(t, {"recent-conditions": err})
            alerts = load("status.json")["alerts"]
            self.assertEqual(any(a.startswith("feed-down: twdb") for a in alerts), i == 2, i)
        self.assertEqual(load("current.json")["lake-travis-tx"]["elev_ft"], 681.14)

    def test_jump_held_then_confirmed(self):
        run_pipeline()
        swap = {"recent-conditions": broken("twdb-medina-jump.json")}
        run_pipeline("2026-11-23T11:30:00Z", swap)
        self.assertEqual(load("current.json")["medina-lake-tx"]["elev_ft"], 1019.85)
        st = load("status.json")
        self.assertTrue(any(a.startswith("jump-held: medina-lake-tx") for a in st["alerts"]))
        self.assertIn("medina-lake-tx", st["held"])
        run_pipeline("2026-11-23T14:30:00Z", swap)
        self.assertEqual(load("current.json")["medina-lake-tx"]["elev_ft"], 1039.85)
        self.assertNotIn("medina-lake-tx", load("status.json")["held"])

    def test_future_date_rejected(self):
        run_pipeline(swap={"recent-conditions": broken("twdb-future-date.json")})
        cur = load("current.json")
        self.assertNotIn("medina-lake-tx", cur)
        self.assertIn("lake-travis-tx", cur)
        self.assertTrue(any("medina-lake-tx" in a and "future" in a for a in load("status.json")["alerts"]))

    def test_null_percent_uses_elevation_rule(self):
        run_pipeline(swap={"recent-conditions": broken("twdb-null-percent.json")})
        tr = load("current.json")["lake-travis-tx"]
        self.assertIsNone(tr["pct_full"])
        self.assertIsNone(tr["pct_full_basis"])
        self.assertEqual(tr["band"], "full")                     # 0.14 ft above 681.0, elevation rule

    def test_datum_mismatch_hides_intraday(self):
        from helpers import fixture
        d = json.loads(fixture("usgs/latest-continuous-tx.json"))
        for f in d["features"]:
            if f["properties"]["monitoring_location_id"] == "USGS-08179500":
                f["properties"]["value"] = "1021.85"
        run_pipeline(swap={"latest-continuous/items?f=json": json.dumps(d).encode()})
        me = load("current.json")["medina-lake-tx"]
        self.assertIsNone(me["intraday"])
        self.assertTrue(any(a.startswith("datum-mismatch: medina-lake-tx") for a in load("status.json")["alerts"]))

    def test_hold_override_does_not_change_data(self):
        (TMP_DATA / "overrides.json").write_text(json.dumps([{
            "lake_id": "lake-travis-tx", "action": "hold", "reason": "check", "evidence_url": "https://www.lcra.org/",
            "set_by": "owner", "set_at": "2026-11-23T00:00:00Z", "expires_at": "2026-11-30T00:00:00Z"}]))
        run_pipeline()
        tr = load("current.json")["lake-travis-tx"]
        self.assertEqual(tr["elev_ft"], 681.14)
        self.assertEqual(tr["freshness"], "unknown")

    def test_dry_run_writes_nothing(self):
        run_pipeline(NOW, None, "--dry-run")
        self.assertFalse((TMP_DATA / "current.json").exists())
        self.assertFalse((TMP_DATA / "series").exists())

    def test_single_lake(self):
        get = run_pipeline(NOW, None, "--lake", "lake-mead-nv")
        self.assertEqual(list(load("current.json")), ["lake-mead-nv"])
        self.assertTrue(all("usbr.gov" in u for u in get.urls))

    def test_fallback_when_primary_hard_stale(self):
        run_pipeline()
        # CDEC daily rows stop (all '---'); 5 days later the daily reading is past 96 h, hourly is fresh.
        later = "2026-11-26T08:30:00Z"
        swap = {"CSVDataServlet": broken("cdec-daily-missing.csv")}
        from helpers import fixture
        rows = json.loads(fixture("cdec/hourly.json"))
        for r in rows:  # shift the hourly fixture by 3 days so it is fresh at `later`
            d, h = r["date"].split(" ")
            r["date"] = (dt.date.fromisoformat(d) + dt.timedelta(days=3)).isoformat() + " " + h
        swap["JSONDataServlet"] = json.dumps(rows).encode()
        run_pipeline(later, swap)
        sha = load("current.json")["shasta-lake-ca"]
        self.assertTrue(sha["used_fallback"])
        self.assertEqual(sha["reading_kind"], "hourly")
        self.assertEqual(sha["primary_last"]["reading_at"], "2026-11-21")


if __name__ == "__main__":
    unittest.main()
