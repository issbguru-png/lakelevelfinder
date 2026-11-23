"""HTTP GET with a 30 s timeout, 2 retries and an honest User-Agent (plan §3.3, §8.4 step 2).

Refuses an HTML body where JSON or CSV is expected (an error or challenge page is never data).
A 403/404 is not retried: it is recorded and the caller stops (no fake headers, no proxies).
"""
import json
import os
import time
import urllib.error
import urllib.request

USER_AGENT = os.environ.get("LLF_UA") or "LakeLevelFinder/0.1 (+https://lakelevelfinder.com)"
TIMEOUT = 30
RETRIES = 2
BACKOFF = (5, 20)
NO_RETRY = (400, 401, 403, 404, 410)


class FetchError(Exception):
    """The request failed (network, HTTP status, or a body of the wrong type)."""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def looks_like_html(body):
    head = body[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head[:200] \
        or head.startswith(b"<head") or head.startswith(b"<body")


def check_body(body, expect, content_type=""):
    """Raise FetchError if `body` is not the expected kind ('json', 'csv' or None = anything but HTML)."""
    ct = (content_type or "").lower()
    if looks_like_html(body) or "text/html" in ct:
        raise FetchError("HTML body where data was expected")
    if not body.strip():
        raise FetchError("empty body")
    if expect == "json":
        try:
            json.loads(body)
        except ValueError as e:
            raise FetchError(f"body is not JSON: {e}") from None
    elif expect == "csv":
        first = body.lstrip().split(b"\n", 1)[0]
        if b"," not in first and not first.startswith(b"#"):
            raise FetchError("body is not CSV")


def fetch(url, expect=None, timeout=TIMEOUT, retries=RETRIES, backoff=BACKOFF, headers=None):
    """Return (body_bytes, headers_dict_lowercase). Raises FetchError."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json, text/csv;q=0.9, */*;q=0.5"}
    if headers:
        hdrs.update(headers)
    fast = bool(os.environ.get("LLF_FAST_RETRY"))
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                h = {k.lower(): v for k, v in r.headers.items()}
            check_body(body, expect, h.get("content-type", ""))
            return body, h
        except urllib.error.HTTPError as e:
            last = FetchError(f"HTTP {e.code} for {url}", status=e.code)
            if e.code in NO_RETRY:
                raise last from None
        except FetchError as e:
            last = e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = FetchError(f"{type(e).__name__}: {e} ({url})")
        if attempt < retries:
            time.sleep(0 if fast else backoff[min(attempt, len(backoff) - 1)])
    raise last


def fetch_json(url, get=None, **kw):
    """GET and parse JSON. `get` replaces fetch() (tests inject a fixture reader)."""
    body, _ = (get or fetch)(url, expect="json", **kw)
    return json.loads(body)
