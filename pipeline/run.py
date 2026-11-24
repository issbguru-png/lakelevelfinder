"""Pipeline run: fetch -> parse -> normalize -> validate -> derive -> write data/ (plan §8.4).

  python3 pipeline/run.py                     fetch every source that is due, write data/
  python3 pipeline/run.py --dry-run           fetch and print what would change; write nothing
  python3 pipeline/run.py --source cwms       one source only (always fetched)
  python3 pipeline/run.py --lake lake-travis-tx   one lake only (its sources are always fetched)
  python3 pipeline/run.py --no-commit         never commit (default: commit only inside GitHub Actions)
  python3 pipeline/run.py --daily             run the once-a-day step now (series for hourly lakes, stats)
  python3 pipeline/run.py --force             fetch every source even if not due
  python3 pipeline/run.py --lakes FILE        lakes file (default data/ref/lakes.json, or LLF_LAKES)

Writes data/current.json (Reading per lake), data/status.json (health), data/alerts.json (for notify.py),
data/pipeline-state.json (last accepted reading per lake and role, held jumps), data/series/*.csv,
data/stats/*.json, data/raw/{source}/ and .commitmsg. Each source is fetched only when its check_every_hours
has passed since its last success. One failing lake or source never stops the others; a lake that fails
validation keeps its last good reading. Exit code 0 even when sources fail (the site shows the data's age).
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import common, series, stats  # noqa: E402
from pipeline import overrides as ov  # noqa: E402
from pipeline.common import iso, load_json, local_date, median, parse_ts, r2, save_json, utcnow  # noqa: E402
from pipeline.effective import effective  # noqa: E402
from pipeline.fetcher import FetchError, fetch as http_fetch  # noqa: E402
from pipeline.normalize import derive_fields, intraday_layer, to_reading  # noqa: E402
from pipeline.sources import SourceError, cdec, cwms, rise, twdb, usgs  # noqa: E402
from pipeline.validate import CONFIRM_TOL_FT, decide, in_range, ts_bounds  # noqa: E402

ADAPTERS = {"twdb": twdb.fetch, "cwms": cwms.fetch, "rise": rise.fetch, "cdec": cdec.fetch,
            "cdec_hourly": cdec.fetch_hourly, "usgs": usgs.fetch}
RAW_SAVED = ("twdb", "cdec", "cdec_hourly", "rise")
FAILURE_ALERT_AFTER = 3
DUE_SLACK_H = 0.25
DAILY_HOUR_UTC = 9
STALE_ALERT_EVERY_H = 24
INTRADAY_MAX_AGE_H = 48


# ---------------------------------------------------------------- targets

def slot_source(slot):
    """Source id of a lake slot. CDEC hourly is written {"source": "cdec", "dur": "H"} or "cdec_hourly"."""
    s = slot.get("source")
    if s == "cdec" and str(slot.get("dur") or slot.get("param") or "").upper() in ("H", "HOURLY"):
        return "cdec_hourly"
    return s


def series_key(source_id, series_id):
    return str(series_id).upper() if source_id in ("cdec", "cdec_hourly") else str(series_id)


def targets_for(lakes):
    by = {}
    for lake in lakes:
        for role in ("primary", "fallback", "intraday"):
            slot = lake.get(role)
            if slot and slot.get("source") and slot.get("series_id") not in (None, ""):
                by.setdefault(slot_source(slot), []).append(
                    {**slot, "lake_id": lake["id"], "role": role, "state": lake.get("state")})
        sto = (lake.get("extra") or {}).get("storage")
        if isinstance(sto, dict) and sto.get("source") == "rise" and sto.get("series_id"):
            by.setdefault("rise", []).append({**sto, "lake_id": lake["id"], "role": "storage",
                                              "state": lake.get("state")})
    return by


def is_due(src_id, source, health, now):
    last = parse_ts((health.get(src_id) or {}).get("last_success"))
    if last is None:
        return True
    every = float(source.get("check_every_hours", 1))
    return (now - last).total_seconds() / 3600 >= every - DUE_SLACK_H


# ---------------------------------------------------------------- raw responses

class Recorder:
    """Wraps the HTTP getter and keeps every body of this run (for data/raw/ snapshots)."""

    def __init__(self, get):
        self.get = get
        self.bodies = []

    def __call__(self, url, **kw):
        body, h = self.get(url, **kw)
        self.bodies.append((url, body))
        return body, h


def save_raw(src_id, bodies, now, dry):
    if dry or src_id not in RAW_SAVED or not bodies:
        return
    d = common.DATA / "raw" / src_id
    d.mkdir(parents=True, exist_ok=True)
    if len(bodies) == 1:
        body = bodies[0][1]
        ext = "csv" if body.lstrip()[:1] not in (b"{", b"[") else "json"
    else:
        doc = {}
        for url, b in bodies:
            try:
                doc[url] = json.loads(b)
            except ValueError:
                doc[url] = b.decode("utf-8", errors="replace")
        body, ext = json.dumps(doc).encode(), "json"
    (d / f"latest.{ext}").write_bytes(body)
    daily = d / f"{now:%Y-%m-%d}.{ext}"
    if not daily.exists():
        daily.write_bytes(body)


# ---------------------------------------------------------------- per lake

def accept(cand, prev, lake, now, same=False):
    r = derive_fields(dict(cand), lake)
    r.pop("units", None)
    t = iso(now)
    r.update(fetched_at=t, last_attempt_at=t, used_fallback=False, override=None, hold_reason=None)
    changed = not prev or prev.get("elev_ft") != r["elev_ft"] or prev.get("reading_at") != r["reading_at"]
    r["content_modified"] = t if (changed and not same) else (prev or {}).get("content_modified", t)
    return r


def process_slot(lake, role, results, state, now, log, alerts, notes, sources, rows, release):
    """Candidate -> decision for one lake slot. Returns (reading or None, points used, action)."""
    slot = lake[role]
    sid = slot_source(slot)
    if sid not in results:
        return None, None, "not-fetched"
    smap, errs = results[sid]
    key = series_key(sid, slot["series_id"])
    acc = state["accepted"].setdefault(lake["id"], {})
    pend = state["pending"].setdefault(lake["id"], {})
    if key in errs or key not in smap:
        notes[lake["id"]] = f"{role} {sid} {key}: {errs.get(key, 'not in the response')}"
        return None, None, "error"
    s = smap[key]
    sto = None
    st_slot = (lake.get("extra") or {}).get("storage")
    if isinstance(st_slot, dict) and st_slot.get("source") == "rise" and "rise" in results:
        sto = results["rise"][0].get(str(st_slot.get("series_id")))
    cand = to_reading(lake, slot, s, sources.get(sid, {}), now, rows, sto)
    if cand is None:
        notes[lake["id"]] = f"{role} {sid} {key}: no valid point"
        return None, None, "error"
    prev = acc.get(role)
    action, reason, label, new_pend = decide(cand, prev, pend.get(role), lake, now, release)
    if action in ("accept", "same"):
        r = accept(cand, prev, lake, now, same=action == "same")
        acc[role] = r
        pend.pop(role, None)
        if reason:
            log.append(f"  {lake['id']}: {reason}")
        return r, s, action
    if action == "hold":
        pend[role] = new_pend
        alerts.append(f"{label}: {lake['id']} {reason}")
    else:
        alerts.append(f"{label}: {lake['id']} {role} {sid} {key}: {reason}")
    notes[lake["id"]] = f"{role} {sid}: {action}: {reason}"
    return None, s, action


def attach_intraday(lake, reading, results, state, now, alerts, notes):
    slot = lake.get("intraday")
    acc = state["accepted"].setdefault(lake["id"], {})
    if slot and slot_source(slot) in results:
        smap, errs = results[slot_source(slot)]
        key = series_key(slot_source(slot), slot["series_id"])
        layer = intraday_layer(lake, slot, smap.get(key)) if key not in errs else None
        if key in errs:
            notes.setdefault(lake["id"], f"intraday {key}: {errs[key]}")
        if layer:
            at = parse_ts(layer["at"])
            if not in_range(layer["elev_ft"], lake) or at > now + dt.timedelta(hours=2):
                alerts.append(f"range-reject: {lake['id']} intraday {key} {layer['elev_ft']} ft at {layer['at']}")
                layer = None
        if layer and reading and common.is_date_str(reading.get("reading_at")):
            start, end = ts_bounds(reading, lake)
            if start <= at < end and abs(layer["elev_ft"] - reading["elev_ft"]) > CONFIRM_TOL_FT:
                alerts.append(f"datum-mismatch: {lake['id']} {reading['source']} {reading['elev_ft']} ft "
                              f"({reading['reading_at']}) vs {key} {layer['elev_ft']} ft ({layer['at']}): intraday hidden")
                layer = None
        if layer:
            acc["intraday"] = layer
    layer = acc.get("intraday")
    if layer and (now - parse_ts(layer["at"])).total_seconds() / 3600 > INTRADAY_MAX_AGE_H:
        layer = None
    return layer


# ---------------------------------------------------------------- main

def main(argv=None, get=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source")
    ap.add_argument("--lake")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--daily", action="store_true", help="run the once-a-day step now")
    ap.add_argument("--no-daily", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--lakes")
    a = ap.parse_args(argv)
    get = get or http_fetch
    dry = a.dry_run
    now = utcnow()
    D = common.DATA

    lakes = [lk for lk in common.load_lakes(a.lakes) if lk.get("primary")]
    if a.lake:
        lakes = [lk for lk in lakes if lk["id"] == a.lake]
        if not lakes:
            print(f"unknown lake {a.lake!r}")
            return 2
    sources = common.load_sources()
    current = load_json(D / "current.json", {}) or {}
    status = load_json(D / "status.json", {}) or {}
    health = status.setdefault("sources", {})
    state = load_json(D / "pipeline-state.json", {}) or {}
    state.setdefault("accepted", {})
    state.setdefault("pending", {})
    for lid, r in current.items():  # first run after state loss: seed from what is published
        if r and not r.get("used_fallback"):
            state["accepted"].setdefault(lid, {}).setdefault("primary", r)
    log, alerts, notes = [], [], {}

    # 1-3. fetch + parse, per source
    targets = targets_for(lakes)
    results = {}
    for sid, tg in targets.items():
        if a.source and sid != a.source and not (a.source == "cdec" and sid == "cdec_hourly"):
            continue
        if sid not in ADAPTERS:
            log.append(f"{sid}: no adapter; {len(tg)} target(s) skipped")
            continue
        src = sources.get(sid, {"id": sid})
        forced = a.force or a.source or a.lake
        if not forced and not is_due(sid, src, health, now):
            log.append(f"{sid}: not due (last success {health[sid].get('last_success')})")
            continue
        h = health.setdefault(sid, {"ok": None, "last_success": None, "last_error": None, "consecutive_failures": 0})
        h["last_attempt"] = iso(now)
        rec = Recorder(get)
        try:
            smap, errs = ADAPTERS[sid](tg, now, rec, src)
            results[sid] = (smap, errs)
            if not a.lake:
                h.update(ok=True, last_success=iso(now), last_error=None, consecutive_failures=0)
            h["series_errors"] = len(errs)
            save_raw(sid, rec.bodies, now, dry)
            log.append(f"{sid}: OK ({len(smap)} series, {len(errs)} target error(s))")
            for k, e in list(errs.items())[:20]:
                log.append(f"    {k}: {e}")
        except (FetchError, SourceError, ValueError) as e:
            h.update(ok=False, last_error=f"{iso(now)} {e}", consecutive_failures=h.get("consecutive_failures", 0) + 1)
            log.append(f"{sid}: FAILED: {e}")
            if isinstance(e, SourceError) or (isinstance(e, ValueError) and not isinstance(e, FetchError)):
                alerts.append(f"count-mismatch: {sid}: {e}" if "count" in str(e) or "fewer" in str(e)
                              else f"schema-change: {sid}: {e}")
            elif h["consecutive_failures"] >= FAILURE_ALERT_AFTER:
                alerts.append(f"feed-down: {sid} failed {h['consecutive_failures']} times: {e}")

    # 4-9. per lake
    accepted_now, window = {}, {}
    for lake in lakes:
        lid = lake["id"]
        tz = lake.get("timezone") or "UTC"
        try:
            rows = series.read(lid)
            release = bool(ov.active(now, lid, "release_jump"))
            r, pts, action = process_slot(lake, "primary", results, state, now, log, alerts, notes, sources, rows,
                                          release)
            if pts is not None and action in ("accept", "same"):
                window[lid] = (slot_source(lake["primary"]), pts)
            acc = state["accepted"].setdefault(lid, {})
            prim = acc.get("primary")
            if prim is not None and r is None and slot_source(lake["primary"]) in results:
                prim = acc["primary"] = dict(prim, last_attempt_at=iso(now))
            pub = prim
            # 7. fallback when the primary is hard-stale
            if lake.get("fallback"):
                fr, _, _ = process_slot(lake, "fallback", results, state, now, log, alerts, notes, sources, rows,
                                        release)
                fr = fr or acc.get("fallback")
                psrc = common.source_for(sources, slot_source(lake["primary"]), (prim or {}).get("reading_kind"))
                if fr and (prim is None or effective(prim, lake, psrc, now)["freshness"] == "unknown"):
                    fsrc = common.source_for(sources, slot_source(lake["fallback"]), fr.get("reading_kind"))
                    if effective(fr, lake, fsrc, now)["freshness"] != "unknown":
                        pub = dict(fr, used_fallback=True, primary_last=(
                            {k: prim.get(k) for k in ("elev_ft", "reading_at", "source")} if prim else None))
                        log.append(f"  {lid}: using fallback {fr['source']} (primary hard-stale)")
            if pub is None:
                continue
            layer = attach_intraday(lake, pub, results, state, now, alerts, notes)
            pub = dict(pub, intraday=layer)
            # the page also shows the intraday reading: a new one moves content_modified (sitemap lastmod) too
            old = current.get(lid) or {}
            if layer:
                same = (old.get("intraday") or {}).get("at") == layer.get("at")
                cm = (old.get("content_modified") or "") if same else iso(now)
                if cm > (pub.get("content_modified") or ""):
                    pub["content_modified"] = cm
            if r is not None and not pub.get("used_fallback"):
                accepted_now[lid] = pub
            current[lid] = pub
        except Exception as e:  # noqa: BLE001  one broken lake must never stop the others
            notes[lid] = f"error: {type(e).__name__}: {e}"
            alerts.append(f"schema-change: {lid}: {type(e).__name__}: {e}")
            log.append(f"  {lid}: ERROR {type(e).__name__}: {e}")

    # series: daily-source values on every accepted reading (sources revise the current day)
    today_by_tz = {}
    for lake in lakes:
        lid = lake["id"]
        if lid not in window or lid not in accepted_now:
            continue
        sid, s = window[lid]
        kind = s.get("kind")
        if kind in common.DAILY_KINDS:
            last_day = dt.date.fromisoformat(accepted_now[lid]["reading_at"][:10])
            pts = [(d, v) for d, v in s["points"] if in_range(v, lake) and d <= last_day]
            ex = s.get("extras") or {}
            new = series.rows_from_points(pts, lake.get("timezone"), accepted_now[lid]["source"], kind,
                                          ex.get("storage_points"), ex.get("pct_full_points"))
            add, chg = series.upsert(lid, new, dry)
            if add or chg:
                log.append(f"  {lid}: series +{add} ~{chg}")

    # 10. once a day: hourly lakes' complete days, stats for every lake
    today = now.date().isoformat()
    do_daily = (a.daily or (now.hour >= DAILY_HOUR_UTC and status.get("last_daily") != today)) and not a.no_daily
    if do_daily:
        n_stats = 0
        for lake in lakes:
            lid = lake["id"]
            tz = lake.get("timezone") or "UTC"
            if lid in window and window[lid][1].get("kind") not in common.DAILY_KINDS:
                sid, s = window[lid]
                before = today_by_tz.setdefault(tz, local_date(now, tz))
                pts = [(t, v) for t, v in s["points"] if in_range(v, lake)]
                ex = s.get("extras") or {}
                new = series.rows_from_points(pts, tz, current[lid]["source"], s.get("kind"),
                                              ex.get("storage_points"), None, before=before)
                add, chg = series.upsert(lid, new, dry)
                if add or chg:
                    log.append(f"  {lid}: series +{add} ~{chg} (end of day)")
            rows = series.read(lid)
            if rows:
                stats.write(lid, rows, dry)
                n_stats += 1
            gaps = series.gaps(rows, start=max(rows[0]["date"], now.date() - dt.timedelta(days=30))) if rows else []
            if gaps:
                notes.setdefault(lid, f"series gaps in the last 30 days: {len(gaps)}")
        log.append(f"daily step: stats for {n_stats} lake(s)")
        if not a.lake and not a.source:
            status["last_daily"] = today

    # overrides
    for o in ov.expired(now):
        alerts.append(f"override-expired: {o.get('lake_id')} {o.get('action')} expired {o.get('expires_at')} "
                      f"({o.get('reason')})")
    for o, why in ov.invalid():
        alerts.append(f"schema-change: data/overrides.json entry for {o.get('lake_id') if isinstance(o, dict) else o}: {why}")

    # freshness as of this run, per source counts
    all_lakes = {lk["id"]: lk for lk in common.load_lakes(a.lakes) if lk.get("primary")}
    counts = {}
    for lid, r in current.items():
        lake = all_lakes.get(lid)
        if not lake or not r:
            continue
        src = common.source_for(sources, r.get("source"), r.get("reading_kind"))
        eff = effective(r, lake, src, now)
        r["freshness"] = eff["freshness"]
        c = counts.setdefault(slot_source(lake["primary"]), {"lakes": 0, "current": 0, "delayed": 0, "unknown": 0})
        c["lakes"] += 1
        c[eff["freshness"]] += 1
    for lid, lake in all_lakes.items():
        if lid not in current:
            c = counts.setdefault(slot_source(lake["primary"]), {"lakes": 0, "current": 0, "delayed": 0, "unknown": 0})
            c["lakes"] += 1
            c["unknown"] += 1
    for sid, c in counts.items():
        h = health.setdefault(sid, {"ok": None, "last_success": None, "last_error": None, "consecutive_failures": 0})
        h.update(c)
        if c["unknown"] and c["unknown"] == c["lakes"] and h.get("ok") is False:
            last = parse_ts(h.get("last_stale_alert"))
            if last is None or (now - last).total_seconds() / 3600 >= STALE_ALERT_EVERY_H:
                alerts.append(f"feed-down: {sid}: every lake is past its hard threshold ({h.get('last_error')})")
                h["last_stale_alert"] = iso(now)

    status["generated_at"] = iso(now)
    status["lakes_total"] = len(all_lakes)
    status["lakes_current"] = sum(c["current"] for c in counts.values())
    status["lakes_delayed"] = sum(c["delayed"] for c in counts.values())
    status["lakes_unknown"] = sum(c["unknown"] for c in counts.values())
    status.setdefault("pages_built", None)
    status["build_id"] = (os.environ.get("GITHUB_SHA") or "")[:7] or status.get("build_id")
    status["lake_notes"] = {**({} if not (a.lake or a.source) else status.get("lake_notes", {})), **notes}
    status["held"] = {lid: p for lid, roles in state["pending"].items() for p in roles.values() if p} or {}
    status["alerts"] = alerts

    msg = commit_message(current, all_lakes, status, accepted_now, now)
    print("\n".join(log))
    print(f"lakes: {status['lakes_total']} total, {status['lakes_current']} current, "
          f"{status['lakes_delayed']} delayed, {status['lakes_unknown']} unknown; accepted this run: {len(accepted_now)}")
    if notes:
        print("NOTES:\n  " + "\n  ".join(f"{k}: {v}" for k, v in sorted(notes.items())))
    if alerts:
        print("ALERTS:\n  " + "\n  ".join(alerts))
    if dry:
        print("(dry run: nothing written)")
        for lid in sorted(accepted_now):
            r = accepted_now[lid]
            print(f"  {lid}: {r['elev_ft']} ft at {r['reading_at']} ({r['source']}), band {r['band']}, "
                  f"trend {r['trend']}, 24h {r.get('change_24h_ft')}, pct {r.get('pct_full')}"
                  + (f", intraday {r['intraday']['elev_ft']} at {r['intraday']['at']}" if r.get("intraday") else ""))
        return 0
    save_json(D / "current.json", dict(sorted(current.items())))
    save_json(D / "status.json", status)
    save_json(D / "alerts.json", {"generated_at": iso(now), "alerts": alerts})
    save_json(D / "pipeline-state.json", state)
    if not a.no_commit:
        (common.ROOT / ".commitmsg").write_text(msg + "\n", encoding="utf-8")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            subprocess.run([str(common.ROOT / "scripts" / "commit-data.sh")], check=False)
    return 0


def commit_message(current, lakes, status, accepted, now):
    by_state = {}
    for lid, r in accepted.items():
        st = (lakes.get(lid) or {}).get("state")
        if st and r.get("change_24h_ft") is not None:
            by_state.setdefault(st, []).append(r["change_24h_ft"])
    parts = [f"{st} {r2(median(v)):+.2f} ft median" for st, v in
             sorted(by_state.items(), key=lambda kv: -len(kv[1]))[:3]]
    return (f"data: {status['lakes_total']} lakes, {status['lakes_current']} current"
            + (f"; {', '.join(parts)}" if parts else "") + f" ({iso(now)})")


if __name__ == "__main__":
    sys.exit(main())
