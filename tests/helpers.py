"""Test helpers: a temp data dir, a fixture-backed HTTP getter, broken variants. No network.

Fixtures are real responses saved on 2026-11-23 (~07:10 UTC) by small live requests; tests/fixtures/broken/
holds real error bodies (twdb-404.html, cwms-table-rock-metric.json with unit=SI, cwms-levels-400.json,
rise-location-listing.json = a location listing that was once mislabeled as a result) and variants made by
editing one field of a real response (wrong count, null percent_full, future date, missing markers, metric units,
a +20 ft jump).
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
BROKEN = FIX / "broken"
sys.path.insert(0, str(ROOT))

# Point the pipeline at a temp data dir and the test lakes BEFORE importing it.
TMP_DATA = Path(tempfile.mkdtemp(prefix="llf-test-"))
os.environ["LLF_DATA"] = str(TMP_DATA)
os.environ["LLF_LAKES"] = str(FIX / "lakes-test.json")
os.environ["LLF_FAST_RETRY"] = "1"
os.environ.pop("GITHUB_ACTIONS", None)
os.environ.pop("GITHUB_TOKEN", None)
NOW = "2026-11-23T08:00:00Z"
os.environ["LLF_NOW"] = NOW

from pipeline.fetcher import FetchError, check_body  # noqa: E402

# URL substring -> fixture file (first match wins; order matters).
ROUTES = [
    ("recent-conditions.json", "twdb/recent-conditions.json"),
    ("Buford.Elev.Inst.15Minutes.0.Raw-USGS&begin", None),  # resolved below (page 1 / page 2)
    ("Table_Rock_Dam-Headwater", "cwms/table-rock-ts.json"),
    ("levels?office=SWL&level-id-mask=Table_Rock_Dam.Elev-Pool", "cwms/table-rock-level-toc.json"),
    ("levels?office=SWL", "cwms/table-rock-levels.json"),
    ("catalog-item/6123", "rise/mead-item.json"),
    ("catalog-item/6124", "rise/mead-storage-item.json"),
    ("result?itemId=6123", "rise/mead-result.json"),
    ("result?itemId=6124", "rise/mead-storage-result.json"),
    ("result/download", "rise/mead-download.json"),
    ("CSVDataServlet", "cdec/daily.csv"),
    ("JSONDataServlet", "cdec/hourly.json"),
    ("latest-continuous/items?cursor=NzJhNTYx", "usgs/latest-continuous-tx-page2.json"),
    ("latest-continuous/items?cursor=ZWRiZDE0", "usgs/latest-continuous-tx-page3.json"),
    ("latest-continuous/items", "usgs/latest-continuous-tx.json"),
    ("continuous/items", "usgs/continuous-medina.json"),
    ("daily/items", "usgs/daily-medina.json"),
]


def fixture(rel):
    return (FIX / rel).read_bytes()


def broken(name):
    return (BROKEN / name).read_bytes()


class FixtureGet:
    """Stands in for pipeline.fetcher.fetch. `swap` maps a URL substring to bytes, a fixture path, or an
    Exception to raise. Records every URL requested."""

    def __init__(self, swap=None):
        self.swap = swap or {}
        self.urls = []

    def __call__(self, url, expect=None, **kw):
        self.urls.append(url)
        for key, val in self.swap.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                body = val if isinstance(val, bytes) else (FIX / val).read_bytes()
                check_body(body, expect)
                return body, {}
        for key, rel in ROUTES:
            if key in url:
                if rel is None:
                    rel = "cwms/lanier-ts-page2.json" if "page=" in url else "cwms/lanier-ts.json"
                body = (FIX / rel).read_bytes()
                check_body(body, expect)
                return body, {}
        raise FetchError(f"no fixture for {url}", status=404)


def reset_data():
    for sub in ("series", "stats", "raw"):
        shutil.rmtree(TMP_DATA / sub, ignore_errors=True)
    for f in ("current.json", "status.json", "alerts.json", "pipeline-state.json", "overrides.json"):
        (TMP_DATA / f).unlink(missing_ok=True)


def run_pipeline(now=NOW, swap=None, *extra):
    os.environ["LLF_NOW"] = now
    from pipeline import run
    import contextlib
    import io
    get = FixtureGet(swap)
    get.output = io.StringIO()
    try:
        with contextlib.redirect_stdout(get.output):
            run.main(["--no-commit", *extra], get=get)
    finally:
        os.environ["LLF_NOW"] = NOW
    return get


def load(name):
    import json
    p = TMP_DATA / name
    return json.loads(p.read_text()) if p.exists() else None


def lakes_by_id():
    from pipeline.common import load_lakes
    return {lk["id"]: lk for lk in load_lakes()}
