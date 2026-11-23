"""Freshness per source (plan §7.6) and overrides applied at build time (plan §8.9)."""
import datetime as dt
import unittest

from helpers import lakes_by_id, reset_data

from pipeline.common import SOURCE_DEFAULTS, parse_ts
from pipeline.effective import effective

LAKES = lakes_by_id()
TRAVIS = LAKES["lake-travis-tx"]
TABLE_ROCK = LAKES["table-rock-lake-mo"]
SHASTA = LAKES["shasta-lake-ca"]
TWDB = SOURCE_DEFAULTS["twdb"]
CWMS = SOURCE_DEFAULTS["cwms"]

TRAVIS_READING = {"lake_id": "lake-travis-tx", "source": "twdb", "series_id": "Travis", "elev_ft": 681.14,
                  "reading_at": "2026-11-22", "reading_kind": "daily_average", "pct_full": 100.0,
                  "full_pool_ft": 681.0, "band": "full", "trend": None, "vs_full_pool_ft": 0.14}
# 2026-11-22 ends at 2026-11-23T06:00Z in America/Chicago
END = parse_ts("2026-11-23T06:00:00Z")


def at(hours):
    return END + dt.timedelta(hours=hours)


def ov(action, **kw):
    o = {"lake_id": "lake-travis-tx", "action": action, "reason": "test", "evidence_url": "https://www.lcra.org/",
         "set_by": "owner", "set_at": "2026-11-23T00:00:00Z", "expires_at": "2026-11-30T00:00:00Z"}
    o.update(kw)
    return o


class Freshness(unittest.TestCase):
    def test_daily_end_of_day(self):
        e = effective(TRAVIS_READING, TRAVIS, TWDB, at(0))
        self.assertEqual(e["reading_ts"], "2026-11-23T06:00:00Z")
        self.assertEqual(e["age_hours"], 0.0)
        self.assertEqual(e["freshness"], "current")
        self.assertEqual(effective(TRAVIS_READING, TRAVIS, TWDB, at(-5))["age_hours"], 0.0)  # never negative

    def test_daily_thresholds(self):
        self.assertEqual(effective(TRAVIS_READING, TRAVIS, TWDB, at(48))["freshness"], "current")
        d = effective(TRAVIS_READING, TRAVIS, TWDB, at(48.5))
        self.assertEqual(d["freshness"], "delayed")
        self.assertTrue(d["show_badges"])
        u = effective(TRAVIS_READING, TRAVIS, TWDB, at(96.5))
        self.assertEqual(u["freshness"], "unknown")
        self.assertFalse(u["show_badges"])
        self.assertEqual(u["unknown_reason"], "stale")
        self.assertEqual((u["soft_stale_hours"], u["hard_stale_hours"]), (48.0, 96.0))
        self.assertEqual(u["elev_ft"], 681.14)  # the last reading stays visible

    def test_hourly_thresholds(self):
        r = {"lake_id": "table-rock-lake-mo", "source": "cwms", "elev_ft": 909.42, "reading_at": "2026-11-23T07:00:00Z",
             "reading_kind": "hourly"}
        t = parse_ts("2026-11-23T07:00:00Z")
        self.assertEqual(effective(r, TABLE_ROCK, CWMS, t + dt.timedelta(hours=5))["freshness"], "current")
        self.assertEqual(effective(r, TABLE_ROCK, CWMS, t + dt.timedelta(hours=7))["freshness"], "delayed")
        self.assertEqual(effective(r, TABLE_ROCK, CWMS, t + dt.timedelta(hours=49))["freshness"], "unknown")

    def test_cdec_hourly_fallback_uses_hourly_thresholds(self):
        r = {"lake_id": "shasta-lake-ca", "source": "cdec", "elev_ft": 972.57, "reading_at": "2026-11-23T06:00:00Z",
             "reading_kind": "hourly", "used_fallback": True}
        e = effective(r, SHASTA, SOURCE_DEFAULTS["cdec"], parse_ts("2026-11-23T13:00:00Z"))
        self.assertEqual((e["soft_stale_hours"], e["freshness"]), (6.0, "delayed"))

    def test_no_reading(self):
        e = effective(None, TRAVIS, TWDB, at(0))
        self.assertEqual(e["freshness"], "unknown")
        self.assertFalse(e["show_badges"])
        self.assertEqual(e["lake_id"], "lake-travis-tx")

    def test_pipeline_hold(self):
        e = effective(dict(TRAVIS_READING, hold_reason="datum review"), TRAVIS, TWDB, at(1))
        self.assertEqual((e["freshness"], e["unknown_reason"]), ("unknown", "datum review"))


class Overrides(unittest.TestCase):
    def setUp(self):
        reset_data()

    def test_hold(self):
        e = effective(TRAVIS_READING, TRAVIS, TWDB, at(1), overrides=[ov("hold", reason="wrong series")])
        self.assertEqual(e["freshness"], "unknown")
        self.assertFalse(e["show_badges"])
        self.assertEqual(e["unknown_reason"], "wrong series")
        self.assertEqual(e["override"]["action"], "hold")
        self.assertEqual(e["elev_ft"], 681.14)

    def test_note(self):
        e = effective(TRAVIS_READING, TRAVIS, TWDB, at(1), overrides=[ov("note", text="Boat ramp work announced")])
        self.assertEqual(e["freshness"], "current")
        self.assertEqual(e["notice"]["text"], "Boat ramp work announced")
        self.assertEqual(e["notice"]["evidence_url"], "https://www.lcra.org/")

    def test_set_full_pool(self):
        e = effective(TRAVIS_READING, TRAVIS, TWDB, at(1), overrides=[ov("set_full_pool", full_pool_ft=690.0)])
        self.assertEqual(e["full_pool_ft"], 690.0)
        self.assertEqual(e["vs_full_pool_ft"], -8.86)
        self.assertEqual(e["band"], "full")  # % full (100) still decides below full pool + 0.50
        e = effective(dict(TRAVIS_READING, pct_full=None), TRAVIS, TWDB, at(1),
                      overrides=[ov("set_full_pool", full_pool_ft=690.0)])
        self.assertEqual(e["band"], "below_full")

    def test_expired_and_invalid_ignored(self):
        old = ov("hold", set_at="2026-11-01T00:00:00Z", expires_at="2026-11-10T00:00:00Z")
        level = ov("hold", elev_ft=700.0)
        long_ = ov("hold", expires_at="2026-12-31T00:00:00Z")
        e = effective(TRAVIS_READING, TRAVIS, TWDB, at(1), overrides=[old, level, long_])
        self.assertEqual(e["freshness"], "current")
        self.assertIsNone(e["override"])

    def test_reads_overrides_file(self):
        import json

        from helpers import TMP_DATA
        (TMP_DATA / "overrides.json").write_text(json.dumps([ov("hold", reason="from file")]))
        e = effective(TRAVIS_READING, TRAVIS, TWDB, at(1))
        self.assertEqual(e["unknown_reason"], "from file")
        (TMP_DATA / "overrides.json").write_text("[]")
        self.assertEqual(effective(TRAVIS_READING, TRAVIS, TWDB, at(1))["freshness"], "current")


if __name__ == "__main__":
    unittest.main()
