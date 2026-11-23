"""Source adapters (plan §3.3, §8.4 step 3).

Every adapter module exposes:
  fetch(targets, now, get=None, source=None) -> (series, errors)
    targets: list of dicts {"lake_id", "role", "source", "series_id", "param", ...} from data/ref/lakes.json
    series:  {series_id: {"points": [(ts, value), ...], "units": "ft", "kind": value_kind, "extras": {...}}}
             ts is a datetime.date for daily values (the local day the value covers) or an aware UTC
             datetime for instantaneous values. Points are sorted oldest first; missing markers are dropped.
    errors:  {series_id: "reason"} for targets that failed on their own (other targets are unaffected)
  It raises SourceError when the whole source response is unusable (wrong type, wrong count, HTML).
  Pure parse functions are exposed separately so tests run on saved real responses without a network.
"""


class SourceError(Exception):
    """The whole source response is rejected (plan §8.4 step 6)."""


MISSING = ("---", "-9999", "-9999.0", "-999", "-999.0", "", "null", "None", "NaN", "nan")


def num(v):
    """Parse a source value; None for every missing marker (---, -9999, -999, null, blank)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        s = str(v).strip()
        if s in MISSING:
            return None
        try:
            f = float(s.replace(",", ""))
        except ValueError:
            return None
    if f != f or f in (-9999.0, -999.0, -99999.0):
        return None
    return f
