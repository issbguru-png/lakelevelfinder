"""Manual overrides, data/overrides.json (plan §8.9).

[{"lake_id", "action", "reason", "evidence_url", "set_by", "set_at", "expires_at", ...}]
  hold           lake shows "Current level unknown" with the reason, whatever the feed says
  release_jump   accepts a held jump (a real flood) after the owner checked an official source
  set_full_pool  temporary full-pool value: needs full_pool_ft (and optional full_pool_label), with its source URL
  note           a dated, sourced notice line: needs text
Rules: reason, evidence_url, set_at and expires_at are required; at most 14 days; expired overrides are ignored
and reported. NO override can set a water level: any level-like field makes the override invalid.
"""
import datetime as dt

from pipeline import common
from pipeline.common import load_json, parse_ts

ACTIONS = ("hold", "release_jump", "set_full_pool", "note")
REQUIRED = ("lake_id", "action", "reason", "evidence_url", "set_at", "expires_at")
MAX_DAYS = 14
FORBIDDEN = ("elev_ft", "elevation", "level", "level_ft", "value", "water_level", "pct_full", "percent_full",
             "storage_af", "storage", "reading_at", "change_24h_ft", "band", "trend", "freshness")


def path():
    return common.DATA / "overrides.json"


def load():
    data = load_json(path(), []) or []
    return data if isinstance(data, list) else []


def validate(o):
    """Raise ValueError when the override breaks a rule."""
    if not isinstance(o, dict):
        raise ValueError("override is not an object")
    for k in REQUIRED:
        if not o.get(k):
            raise ValueError(f"override missing {k}")
    if o["action"] not in ACTIONS:
        raise ValueError(f"unknown action {o['action']!r}")
    bad = [k for k in FORBIDDEN if k in o]
    if bad:
        raise ValueError(f"an override cannot set a water level ({', '.join(bad)})")
    if not str(o["evidence_url"]).startswith(("https://", "http://")):
        raise ValueError("evidence_url must be a URL")
    set_at, exp = parse_ts(o["set_at"]), parse_ts(o["expires_at"])
    if exp <= set_at:
        raise ValueError("expires_at must be after set_at")
    if exp - set_at > dt.timedelta(days=MAX_DAYS, minutes=1):
        raise ValueError(f"override longer than {MAX_DAYS} days")
    if o["action"] == "set_full_pool":
        fp = o.get("full_pool_ft")
        if not isinstance(fp, (int, float)) or isinstance(fp, bool):
            raise ValueError("set_full_pool needs a numeric full_pool_ft")
    if o["action"] == "note" and not str(o.get("text") or "").strip():
        raise ValueError("note needs text")


def is_valid(o):
    try:
        validate(o)
        return True
    except (ValueError, TypeError):
        return False


def active(now, lake_id=None, action=None, data=None):
    """Valid overrides in force at `now`, optionally for one lake / one action."""
    out = []
    for o in (load() if data is None else data):
        if not is_valid(o):
            continue
        if lake_id and o["lake_id"] != lake_id:
            continue
        if action and o["action"] != action:
            continue
        if parse_ts(o["set_at"]) <= now < parse_ts(o["expires_at"]):
            out.append(o)
    return out


def for_lake(now, lake_id, data=None):
    """{action: override} for one lake (latest set_at wins per action)."""
    out = {}
    for o in sorted(active(now, lake_id, data=data), key=lambda o: o["set_at"]):
        out[o["action"]] = o
    return out


def expired(now, data=None):
    return [o for o in (load() if data is None else data)
            if isinstance(o, dict) and o.get("expires_at") and parse_ts(o["expires_at"]) <= now]


def invalid(data=None):
    out = []
    for o in (load() if data is None else data):
        try:
            validate(o)
        except (ValueError, TypeError) as e:
            out.append((o, str(e)))
    return out
