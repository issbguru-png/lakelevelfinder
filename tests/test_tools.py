"""tools/backfill.py and tools/accuracy_check.py on saved responses (no network)."""
import contextlib
import io
import json
import sys
import unittest

from helpers import ROOT, TMP_DATA, FixtureGet, fixture, load, reset_data, run_pipeline

sys.path.insert(0, str(ROOT / "tools"))
import accuracy_check  # noqa: E402
import backfill  # noqa: E402

from pipeline import series  # noqa: E402

EMPTY_PAGE = json.dumps({"type": "FeatureCollection", "features": [], "links": []}).encode()


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = fn(*a, **kw)
    return code, out.getvalue()


class Backfill(unittest.TestCase):
    def setUp(self):
        reset_data()
        (TMP_DATA / "backfill-state.json").unlink(missing_ok=True)

    def test_twdb(self):
        get = FixtureGet({"individual/travis.csv": "twdb/travis-30day.csv"})
        code, out = quiet(backfill.main, ["--lake", "lake-travis-tx", "--sleep", "0"], get=get)
        self.assertEqual(code, 0, out)
        rows = series.read("lake-travis-tx")
        self.assertEqual(len(rows), 31)
        self.assertTrue(all(r["flag"] == "bf" for r in rows))
        self.assertEqual(rows[-1]["pct_full"], 100.0)
        st = json.loads((TMP_DATA / "backfill-state.json").read_text())
        self.assertTrue(st["lake-travis-tx"]["complete"])
        code, out = quiet(backfill.main, ["--lake", "lake-travis-tx", "--sleep", "0"], get=get)
        self.assertIn("already backfilled", out)                     # resumable: not fetched again

    def test_bf_never_replaces_live(self):
        run_pipeline()                                                # live TWDB row for 2026-11-22
        get = FixtureGet({"individual/travis.csv": "twdb/travis-30day.csv"})
        quiet(backfill.main, ["--lake", "lake-travis-tx", "--sleep", "0"], get=get)
        last = series.read("lake-travis-tx")[-1]
        self.assertEqual((str(last["date"]), last["flag"]), ("2026-11-22", ""))

    def test_rise_yearly_chunks_resume(self):
        item = json.loads(fixture("rise/mead-item.json"))
        item["data"]["attributes"]["temporalStartDate"] = "2025-02-02T07:00:00+00:00"
        get = FixtureGet({"catalog-item/6123": json.dumps(item).encode()})
        code, out = quiet(backfill.main, ["--lake", "lake-mead-nv", "--sleep", "0"], get=get)
        self.assertEqual(code, 0, out)
        downloads = [u for u in get.urls if "result/download" in u]
        self.assertEqual(len(downloads), 4)                           # 2025 + 2026, elevation + storage item
        rows = series.read("lake-mead-nv")
        self.assertEqual(rows[0]["elev_ft"], 1038.93)
        st = json.loads((TMP_DATA / "backfill-state.json").read_text())["lake-mead-nv"]
        self.assertEqual(st["done"], ["rise:6123:2025"])              # the current year is never marked done
        get2 = FixtureGet({"catalog-item/6123": json.dumps(item).encode()})
        quiet(backfill.main, ["--lake", "lake-mead-nv", "--sleep", "0"], get=get2)
        self.assertEqual(len([u for u in get2.urls if "result/download" in u]), 2)

    def test_cwms_cdec_usgs(self):
        get = FixtureGet({"daily/items?cursor=": EMPTY_PAGE})
        code, out = quiet(backfill.main, ["--lake", "table-rock-lake-mo", "--lake", "shasta-lake-ca", "--years", "1",
                                          "--sleep", "0"], get=get)
        self.assertEqual(code, 0, out)
        tr = series.read("table-rock-lake-mo")
        self.assertTrue(tr and all(r["flag"] == "bf" and r["src"] == "cwms" for r in tr))
        sha = series.read("shasta-lake-ca")
        self.assertEqual(str(sha[-1]["date"]), "2026-11-21")
        rows = backfill.rows_usgs({"id": "x"}, {"series_id": "USGS-08179500", "param": "62614"}, get,
                                  backfill.utcnow(), set(), lambda *_: None, 1)[0]
        self.assertEqual(len(rows), 10)

    def test_failure_is_reported_and_resumable(self):
        get = FixtureGet({"individual/travis.csv": "broken/twdb-404.html"})
        code, out = quiet(backfill.main, ["--lake", "lake-travis-tx", "--sleep", "0"], get=get)
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)

    def test_multiword_twdb_key_needs_slug(self):
        self.assertIsNone(backfill.twdb_slug({"series_id": "Lake O' the Pines"}))
        self.assertEqual(backfill.twdb_slug({"series_id": "ChokeCanyon", "twdb_slug": "choke-canyon"}), "choke-canyon")
        self.assertEqual(backfill.twdb_slug({"series_id": "Travis"}), "travis")

    def test_minimum_sleep(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                backfill.main(["--sleep", "0.2"])


class Accuracy(unittest.TestCase):
    def setUp(self):
        reset_data()
        run_pipeline()

    def test_all_match_on_the_same_responses(self):
        code, out = quiet(accuracy_check.main, ["--n", "7"], get=FixtureGet())
        self.assertEqual(code, 0, out)
        self.assertIn("7 / 7 match", out)

    def test_mismatch_detected(self):
        cur = load("current.json")
        cur["lake-travis-tx"]["elev_ft"] = 690.0
        (TMP_DATA / "current.json").write_text(json.dumps(cur))
        code, out = quiet(accuracy_check.main, ["--n", "7"], get=FixtureGet())
        self.assertEqual(code, 1)
        self.assertIn("lake-travis-tx [twdb] MISMATCH", out)

    def test_lag_and_source_down(self):
        cur = load("current.json")
        cur["lake-mead-nv"]["reading_at"] = "2026-11-20"
        cur["lake-mead-nv"]["elev_ft"] = 1041.36
        (TMP_DATA / "current.json").write_text(json.dumps(cur))
        from pipeline.fetcher import FetchError
        code, out = quiet(accuracy_check.main, ["--n", "7"],
                          get=FixtureGet({"cwms-data": FetchError("timed out")}))
        self.assertIn("lake-mead-nv [rise] lag", out)
        self.assertIn("5 / 5 match", out)                             # 2 CWMS lakes unchecked, not mismatched
        self.assertIn("2 unchecked", out)
        self.assertEqual(code, 0)

    def test_log_written(self):
        saved = accuracy_check.LOG
        accuracy_check.LOG = TMP_DATA / "accuracy-log.md"   # never touch docs/accuracy-log.md in tests
        try:
            quiet(accuracy_check.main, ["--n", "3", "--log", "--date", "2026-11-23"], get=FixtureGet())
            text = accuracy_check.LOG.read_text(encoding="utf-8")
            self.assertIn("| 2026-11-23 | accuracy_check.py |", text)
            self.assertIn("3 / 3 match", text)
        finally:
            accuracy_check.LOG = saved

if __name__ == "__main__":
    unittest.main()


class AccuracyClassifyTest(unittest.TestCase):
    def test_revision_vs_mismatch(self):
        acc = accuracy_check
        eff = {"freshness": "current"}
        pub = {"reading_at": "2026-11-22", "elev_ft": 487.49, "fetched_at": "2026-11-23T09:30:24Z"}
        self.assertEqual(acc.classify(("2026-11-22", 487.49), pub, eff, False)[0], "ok")
        self.assertEqual(acc.classify(("2026-11-22", 488.49), pub, eff, False)[0], "revised")
        self.assertEqual(acc.classify(("2026-11-22", 495.0), pub, eff, False)[0], "MISMATCH")
        old = dict(pub, fetched_at="2026-11-30T09:00:00Z")
        self.assertEqual(acc.classify(("2026-11-22", 488.49), old, eff, False)[0], "MISMATCH")
