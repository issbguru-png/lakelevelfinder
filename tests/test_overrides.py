"""Override rules and the override CLI (plan §8.9). No override can set a water level."""
import contextlib
import io
import json
import unittest

from helpers import TMP_DATA, reset_data

from pipeline import override as cli
from pipeline import overrides as ov
from pipeline.common import parse_ts

NOW = parse_ts("2026-11-23T08:00:00Z")


def o(action="hold", **kw):
    d = {"lake_id": "lake-travis-tx", "action": action, "reason": "r", "evidence_url": "https://www.lcra.org/x",
         "set_by": "owner", "set_at": "2026-11-23T00:00:00Z", "expires_at": "2026-12-01T00:00:00Z"}
    d.update(kw)
    return d


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(args))
        except SystemExit as e:
            code = e.code
    return code, out.getvalue() + err.getvalue()


class Rules(unittest.TestCase):
    def test_valid_actions(self):
        ov.validate(o("hold"))
        ov.validate(o("release_jump"))
        ov.validate(o("set_full_pool", full_pool_ft=917.0))
        ov.validate(o("note", text="Drawdown announced"))

    def test_required_fields(self):
        for k in ("lake_id", "action", "reason", "evidence_url", "set_at", "expires_at"):
            bad = o()
            bad.pop(k)
            with self.assertRaises(ValueError, msg=k):
                ov.validate(bad)

    def test_limits(self):
        with self.assertRaises(ValueError):
            ov.validate(o(expires_at="2026-12-08T00:00:00Z"))  # 15 days
        ov.validate(o(expires_at="2026-12-07T00:00:00Z"))      # 14 days
        with self.assertRaises(ValueError):
            ov.validate(o(expires_at="2026-11-22T00:00:00Z"))  # before set_at
        with self.assertRaises(ValueError):
            ov.validate(o(action="set_level"))
        with self.assertRaises(ValueError):
            ov.validate(o(evidence_url="lcra page"))

    def test_no_level_can_be_set(self):
        for k in ("elev_ft", "level", "value", "water_level", "pct_full", "storage_af", "reading_at"):
            with self.assertRaises(ValueError, msg=k):
                ov.validate(o("note", text="x", **{k: 680.0}))
        with self.assertRaises(ValueError):
            ov.validate(o("set_full_pool"))                     # needs a numeric full_pool_ft
        with self.assertRaises(ValueError):
            ov.validate(o("set_full_pool", full_pool_ft="917"))
        with self.assertRaises(ValueError):
            ov.validate(o("note"))                              # needs text

    def test_active_and_expired(self):
        data = [o(), o("note", text="x", set_at="2026-11-01T00:00:00Z", expires_at="2026-11-10T00:00:00Z"),
                o("note", text="future", set_at="2026-11-25T00:00:00Z", expires_at="2026-11-30T00:00:00Z")]
        self.assertEqual([x["action"] for x in ov.active(NOW, data=data)], ["hold"])
        self.assertEqual(len(ov.expired(NOW, data)), 1)
        self.assertEqual(set(ov.for_lake(NOW, "lake-travis-tx", data)), {"hold"})
        self.assertEqual(ov.for_lake(NOW, "canyon-lake-tx", data), {})


class CLI(unittest.TestCase):
    def setUp(self):
        reset_data()

    def stored(self):
        return json.loads((TMP_DATA / "overrides.json").read_text())

    def test_add_list_clear(self):
        code, out = run_cli("add", "--lake", "lake-travis-tx", "--action", "hold", "--reason", "series check",
                            "--evidence", "https://www.waterdatafortexas.org/reservoirs/individual/travis", "--days", "3")
        self.assertEqual(code, 0, out)
        rows = self.stored()
        self.assertEqual(rows[0]["action"], "hold")
        self.assertEqual(parse_ts(rows[0]["expires_at"]) - parse_ts(rows[0]["set_at"]),
                         parse_ts("2026-11-26T00:00:00Z") - parse_ts("2026-11-23T00:00:00Z"))
        code, out = run_cli("list")
        self.assertIn("lake-travis-tx hold", out)
        self.assertIn("[active]", out)
        code, out = run_cli("clear", "--lake", "lake-travis-tx")
        self.assertEqual(self.stored(), [])

    def test_set_full_pool_and_note(self):
        self.assertEqual(run_cli("add", "--lake", "table-rock-lake-mo", "--action", "set_full_pool", "--full-pool",
                                 "915", "--reason", "winter pool", "--evidence", "https://www.swl.usace.army.mil/")[0], 0)
        self.assertEqual(run_cli("add", "--lake", "table-rock-lake-mo", "--action", "note", "--text", "Drawdown",
                                 "--reason", "operator notice", "--evidence", "https://www.swl.usace.army.mil/")[0], 0)
        acts = {r["action"]: r for r in self.stored()}
        self.assertEqual(acts["set_full_pool"]["full_pool_ft"], 915.0)
        self.assertEqual(acts["note"]["text"], "Drawdown")

    def test_refusals(self):
        base = ["add", "--lake", "lake-travis-tx", "--reason", "r", "--evidence", "https://x.gov/"]
        self.assertNotEqual(run_cli(*base, "--action", "hold", "--days", "15")[0], 0)
        self.assertNotEqual(run_cli(*base, "--action", "set_full_pool")[0], 0)
        self.assertNotEqual(run_cli(*base, "--action", "note")[0], 0)
        self.assertNotEqual(run_cli("add", "--lake", "no-such-lake", "--action", "hold", "--reason", "r",
                                    "--evidence", "https://x.gov/")[0], 0)
        self.assertNotEqual(run_cli("add", "--lake", "lake-travis-tx", "--action", "hold", "--evidence", "https://x.gov/")[0], 0)
        self.assertNotEqual(run_cli(*base, "--action", "hold", "--elev", "680")[0], 0)  # no such option
        self.assertFalse((TMP_DATA / "overrides.json").exists())

    def test_prune(self):
        (TMP_DATA / "overrides.json").write_text(json.dumps(
            [o(), o("note", text="x", set_at="2026-11-01T00:00:00Z", expires_at="2026-11-10T00:00:00Z")]))
        run_cli("prune")
        self.assertEqual([r["action"] for r in self.stored()], ["hold"])


if __name__ == "__main__":
    unittest.main()
