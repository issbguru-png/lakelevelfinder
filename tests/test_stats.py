"""Daily series files and per-lake statistics (plan §7.3, §8.3, §8.8)."""
import datetime as dt
import unittest

from helpers import fixture

from pipeline import series, stats
from pipeline.sources import twdb

UTC = dt.timezone.utc


def rows_from_twdb(name):
    s = twdb.csv_series(fixture(name))
    return series.rows_from_points(s["points"], "America/Chicago", "twdb", "daily_average",
                                   s["extras"]["storage_points"], s["extras"]["pct_full_points"], flag="bf")


def synthetic(start, days, f=lambda i: 600.0 + (i % 365) / 10.0, skip=()):
    out = []
    for i in range(days):
        d = start + dt.timedelta(days=i)
        if d in skip:
            continue
        out.append({"date": d, "elev_ft": round(f(i), 2), "pct_full": None, "storage_af": None, "src": "twdb",
                    "flag": "bf"})
    return out


class Series(unittest.TestCase):
    def test_csv_round_trip(self):
        rows = rows_from_twdb("twdb/travis-30day.csv")
        self.assertEqual(rows[-1]["date"], dt.date(2026, 11, 22))
        text = series.to_csv(rows)
        self.assertTrue(text.startswith("date,elev_ft,pct_full,storage_af,src,flag\n"))
        self.assertIn("2026-11-22,681.14,100.0,", text)
        series.write("roundtrip-test", rows)
        back = series.read("roundtrip-test")
        self.assertEqual([r["elev_ft"] for r in back], [r["elev_ft"] for r in rows])
        self.assertEqual(back[-1]["flag"], "bf")

    def test_merge_rules(self):
        d = dt.date(2026, 11, 20)
        live = [{"date": d, "elev_ft": 681.0, "pct_full": 100.0, "storage_af": 1, "src": "twdb", "flag": ""}]
        bf = [{"date": d, "elev_ft": 680.0, "pct_full": None, "storage_af": None, "src": "twdb", "flag": "bf"}]
        rows, add, chg = series.merge(live, bf)
        self.assertEqual((add, chg, rows[0]["elev_ft"]), (0, 0, 681.0))  # bf never replaces live
        rows, add, chg = series.merge(bf, live)
        self.assertEqual((add, chg, rows[0]["flag"]), (0, 1, ""))        # live replaces bf
        revised = [dict(live[0], elev_ft=681.05)]
        rows, add, chg = series.merge(live, revised)
        self.assertEqual((chg, rows[0]["elev_ft"]), (1, 681.05))         # source revision replaces
        rows, add, chg = series.merge(live, live)
        self.assertEqual((add, chg), (0, 0))

    def test_end_of_day_from_hourly(self):
        pts = [(dt.datetime(2026, 11, 21, 5, 0, tzinfo=UTC), 909.5),   # 23:00 CST on 11-20
               (dt.datetime(2026, 11, 21, 6, 0, tzinfo=UTC), 909.6),   # 00:00 CST on 11-21
               (dt.datetime(2026, 11, 22, 5, 0, tzinfo=UTC), 909.7),   # 23:00 CST on 11-21
               (dt.datetime(2026, 11, 22, 7, 0, tzinfo=UTC), 909.8)]   # 01:00 CST on 11-22 (today: incomplete)
        rows = series.rows_from_points(pts, "America/Chicago", "cwms", "hourly", before=dt.date(2026, 11, 22))
        self.assertEqual([(r["date"], r["elev_ft"], r["flag"]) for r in rows],
                         [(dt.date(2026, 11, 20), 909.5, "eod"), (dt.date(2026, 11, 21), 909.7, "eod")])

    def test_gaps(self):
        start = dt.date(2026, 1, 1)
        rows = synthetic(start, 10, skip={dt.date(2026, 1, 4), dt.date(2026, 1, 5), dt.date(2026, 1, 8)})
        self.assertEqual(series.gaps(rows), [(dt.date(2026, 1, 4), dt.date(2026, 1, 5)),
                                             (dt.date(2026, 1, 8), dt.date(2026, 1, 8))])


class Stats(unittest.TestCase):
    def test_short_series_has_no_record(self):
        st = stats.compute(rows_from_twdb("twdb/travis-30day.csv"))
        self.assertEqual(st["n_days"], 31)
        self.assertFalse(st["has_history"])
        self.assertIsNone(st["record_high"])
        self.assertIsNone(st["record_low"])
        self.assertIsNone(st["median_this_date"])
        self.assertIsNone(st["same_day_last_year"])
        self.assertEqual(st["period_start"], "2026-10-23")
        self.assertEqual(st["period_end"], "2026-11-22")
        self.assertEqual(st["year_high"]["elev_ft"], max(r["elev_ft"] for r in rows_from_twdb("twdb/travis-30day.csv")))
        self.assertEqual(len(st["last_14"]), 14)
        self.assertEqual(st["last_14"][-1]["date"], "2026-11-22")
        self.assertEqual(len(st["spark_30"]), 30)
        self.assertEqual(st["backfilled_days"], 31)

    def test_long_series(self):
        start = dt.date(2020, 1, 1)
        rows = synthetic(start, 6 * 365 + 30, f=lambda i: 600.0 + (i % 400) / 10.0)
        end = rows[-1]["date"]
        st = stats.compute(rows)
        self.assertTrue(st["has_history"])
        self.assertEqual(st["record_high"]["elev_ft"], 639.9)
        self.assertEqual(st["record_high"]["date"], "2021-02-03")  # first time 639.9 is reached
        self.assertEqual(st["record_low"]["elev_ft"], 600.0)
        self.assertEqual(st["record_low"]["date"], "2020-01-01")
        self.assertGreaterEqual(st["median_this_date"]["years"], 5)  # count of years (plan §8.3)
        self.assertEqual(st["same_day_last_year"]["date"], end.replace(year=end.year - 1).isoformat())
        years = {y["year"]: y for y in st["yearly"]}
        self.assertFalse(years[2021]["partial"])
        self.assertTrue(years[end.year]["partial"])
        self.assertEqual(len(st["monthly"]), 60)

    def test_same_day_last_year_tolerance(self):
        start = dt.date(2025, 1, 1)
        end = dt.date(2026, 3, 1)
        days = (end - start).days + 1
        skip = {dt.date(2025, 3, 1) + dt.timedelta(days=k) for k in range(-2, 3)}
        st = stats.compute(synthetic(start, days, skip=skip))
        self.assertEqual(st["same_day_last_year"]["date"], "2025-02-26")  # 3 days away, earlier wins the tie
        skip |= {dt.date(2025, 2, 26), dt.date(2025, 3, 4)}
        st = stats.compute(synthetic(start, days, skip=skip))
        self.assertIsNone(st["same_day_last_year"])

    def test_gap_breaks_spark(self):
        start = dt.date(2026, 10, 1)
        rows = synthetic(start, 40, skip={dt.date(2026, 11, 5)})
        st = stats.compute(rows)
        spark = {e["date"]: e["elev_ft"] for e in st["spark_30"]}
        self.assertIsNone(spark["2026-11-05"])
        l14 = {e["date"]: e for e in st["last_14"]}
        self.assertIsNone(l14.get("2026-11-06", {"change_ft": None})["change_ft"])
        self.assertEqual(st["gaps"], [["2026-11-05", "2026-11-05"]])

    def test_change_from_series(self):
        rows = rows_from_twdb("twdb/travis-30day.csv")
        idx = {r["date"]: r["elev_ft"] for r in rows}
        want = round(idx[dt.date(2026, 11, 22)] - idx[dt.date(2026, 11, 21)], 2)
        self.assertEqual(stats.change_from_series(rows, dt.date(2026, 11, 22), 1), want)
        self.assertIsNone(stats.change_from_series(rows, dt.date(2026, 11, 22), 60))

    def test_empty(self):
        st = stats.compute([])
        self.assertEqual(st["n_days"], 0)
        self.assertIsNone(st["record_high"])


if __name__ == "__main__":
    unittest.main()
