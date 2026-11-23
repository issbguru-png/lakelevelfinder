"""Band, flood pool and trend: the exact §4.2 thresholds and the real-data checks."""
import unittest

import helpers  # noqa: F401  (sets env first)

from pipeline.derive import BAND_LABEL, BANDS, TREND_LABEL, TRENDS, band, in_flood_pool, trend, vs_full_pool


class RealDataChecks(unittest.TestCase):
    """Plan §4.2 'Checks against real data (Nov 21, 2026)'. Elevations not quoted in the plan are None
    (the % full rule does not need them)."""

    def test_travis_full_not_above(self):
        self.assertEqual(band(681.26, 681.0, 100.0, True), "full")  # 0.26 ft above < 0.50

    def test_medina_very_low(self):
        self.assertEqual(band(1019.76, 1064.2, 27.8, False), "very_low")

    def test_choke_canyon_very_low(self):
        self.assertEqual(band(190.41, 220.5, 21.0, False), "very_low")

    def test_corpus_christi_near_full(self):
        self.assertEqual(band(None, 94.0, 86.0, False), "near_full")

    def test_toledo_bend_below_full(self):
        self.assertEqual(band(None, 172.0, 83.5, False), "below_full")

    def test_canyon_full(self):
        self.assertEqual(band(None, 909.0, 97.2, True), "full")

    def test_elephant_butte_very_low(self):
        self.assertEqual(band(None, None, 13.0, False), "very_low")


class PercentRule(unittest.TestCase):
    def test_boundaries(self):
        cases = [(97.0, "full"), (96.9, "near_full"), (85.0, "near_full"), (84.9, "below_full"),
                 (60.0, "below_full"), (59.9, "low"), (30.0, "low"), (29.9, "very_low"), (0.0, "very_low"),
                 (100.0, "full")]
        for pct, want in cases:
            self.assertEqual(band(100.0, 110.0, pct, False), want, pct)

    def test_above_full_wins_over_percent(self):
        self.assertEqual(band(681.5, 681.0, 100.0, True), "above_full")   # exactly +0.50
        self.assertEqual(band(681.49, 681.0, 100.0, True), "full")

    def test_percent_without_full_pool(self):
        self.assertEqual(band(500.0, None, 72.0, False), "below_full")


class ElevationRule(unittest.TestCase):
    def test_boundaries(self):
        fp = 1000.0
        cases = [(1000.5, "above_full"), (1000.49, "full"), (1000.0, "full"), (999.0, "full"),
                 (998.99, "near_full"), (997.0, "near_full"), (996.99, "below_full"), (990.0, "below_full"),
                 (989.99, "low"), (975.0, "low"), (974.99, "very_low"), (900.0, "very_low")]
        for elev, want in cases:
            self.assertEqual(band(elev, fp, None, False), want, elev)

    def test_level_only(self):
        self.assertEqual(band(1041.41, None, None, False), "level_only")
        self.assertEqual(band(None, None, None, False), "level_only")

    def test_float_noise(self):
        # CWMS raw values look like 912.7999999999998; thresholds compare rounded figures
        self.assertEqual(band(912.7999999999998, 915.8, None, False), "near_full")  # exactly 3.00 below

    def test_override(self):
        deep = {"ft_below": [5.0, 15.0, 40.0, 80.0]}
        self.assertEqual(band(960.0, 1000.0, None, False, deep), "below_full")
        self.assertEqual(band(960.0, 1000.0, None, False), "very_low")
        self.assertEqual(band(100.0, 110.0, 80.0, False, {"pct": [95, 75, 50, 25]}), "near_full")


class FloodPool(unittest.TestCase):
    def test_flag(self):
        self.assertTrue(in_flood_pool(683.0, 681.0, True))
        self.assertFalse(in_flood_pool(682.99, 681.0, True))
        self.assertFalse(in_flood_pool(700.0, 681.0, False))
        self.assertFalse(in_flood_pool(700.0, None, True))

    def test_vs_full_pool(self):
        self.assertEqual(vs_full_pool(681.26, 681.0), 0.26)
        self.assertEqual(vs_full_pool(1019.76, 1064.2), -44.44)
        self.assertIsNone(vs_full_pool(1041.41, None))


class Trend(unittest.TestCase):
    def test_boundaries(self):
        cases = [(1.00, "rising_fast"), (0.99, "rising"), (0.05, "rising"), (0.04, "steady"), (0.0, "steady"),
                 (-0.04, "steady"), (-0.05, "falling"), (-0.99, "falling"), (-1.00, "falling_fast"),
                 (-3.2, "falling_fast"), (-0.11, "falling"), (0.0499999, "rising")]
        for c, want in cases:
            self.assertEqual(trend(c), want, c)
        self.assertIsNone(trend(None))

    def test_labels(self):
        self.assertEqual(set(BAND_LABEL), set(BANDS))
        self.assertEqual(BAND_LABEL["above_full"], "Above full pool")
        self.assertEqual(TREND_LABEL["falling_fast"], "Falling fast")
        self.assertEqual(len(TRENDS), 5)
        for word in ("safe", "open", "live", "official"):
            self.assertFalse(any(word in v.lower() for v in list(BAND_LABEL.values()) + list(TREND_LABEL.values())))


if __name__ == "__main__":
    unittest.main()
