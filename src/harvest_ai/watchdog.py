"""Watchdog: find unhealthy sources, raise alerts, optionally dispatch repair agents.

Findings per enabled source:
  zero          the last two finished runs stored nothing
  degraded      the last run stored under half of the source's baseline
  stuck         a run is still `running` with no heartbeat for 2x its time budget (it is closed as `stuck`)
  stale         no successful run for 2x the cadence
  error_streak  the last three runs ended in error
  review_stale  the module changed after its passing review
  clock_skew    a run or heartbeat is stamped more than 5 min in the future (a process clock ran ahead)
Alerts go to the alerts table, `<project>/alerts.jsonl` and a pluggable hook: `HARVEST_ALERT_HOOK=
module:function` (called with the list of alerts) and/or `HARVEST_ALERT_WEBHOOK=<url>` (JSON POST).

Alert rules (soak test 2026-10):
  - one alert per incident: a (source, kind) finding is not re-alerted while the incident lasts (24 h re-alert
    window), but a new incident after a healthy run is alerted again; `stuck` is alerted once per run;
  - every alert, also those the runner raises (`degraded`, `review_stale`), goes through `raise_alerts`, so it is
    deduplicated and delivered the same way;
  - delivery is at least once: each channel is tried independently, a webhook answer >= 300 is a failure, and an
    alert that did not reach every channel is retried on the next check for 24 h (payloads carry the alert `id`).
Finished runs, quarantine entries, alerts and finished jobs older than `HARVEST_HISTORY_DAYS` (90; 0 = keep) are
deleted by the check, so the history tables do not grow without bound.

Repair dispatch is OFF by default. It needs both `dispatch=True` and `HARVEST_REPAIR_DISPATCH=on`,
runs at most `HARVEST_REPAIR_BUDGET` (2) agents per check with a 72 h cooldown per source, and each
agent gets an explicit tool allowlist (see agents.py); permission bypass is never used.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import os

from . import agents, sandbox, scheduler
from .db import now_iso
from .project import Project, cadence_hours

COOLDOWN_H = 72
REALERT_H = 24
SKEW = dt.timedelta(minutes=5)
INCIDENT_KINDS = ("zero", "degraded", "error_streak", "stale", "review_stale")


def _ts(s: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def check(p: Project, *, now: dt.datetime | None = None) -> list[dict]:
    now = now or dt.datetime.now(dt.timezone.utc)
    store = p.store
    findings = []
    for src in store.list_sources(p.name):
        sid = src["id"]
        runs = store.list_runs(p.name, sid, limit=5)
        budget = int(src.get("time_budget_s") or p.spec.time_budget_s)
        ahead = [r for r in runs if (_ts(r.get("heartbeat_at")) or _ts(r.get("started_at")) or now) > now + SKEW]
        if ahead:
            findings.append({"source_id": sid, "kind": "clock_skew",
                             "message": f"run {ahead[0]['id']} is stamped {ahead[0].get('heartbeat_at') or ahead[0].get('started_at')}, ahead of this "
                                        f"check's clock ({now.isoformat(timespec='seconds')}): a worker's clock ran ahead; scheduling ignores such runs"})
        runs = [r for r in runs if r not in ahead]
        for r in runs:
            if r["status"] == "running":
                hb = _ts(r.get("heartbeat_at"))
                if hb and now - hb > dt.timedelta(seconds=max(2 * budget, 600)):
                    store.heartbeat(r["id"], status="stuck", finished_at=now_iso(), error="no heartbeat; closed by the watchdog")
                    findings.append({"source_id": sid, "kind": "stuck", "key": r["id"], "message": f"run {r['id']} had no heartbeat since {r['heartbeat_at']}"})
        if not src.get("enabled"):
            if src.get("status") == "review_stale":
                findings.append({"source_id": sid, "kind": "review_stale", "message": "module changed after review"})
            continue
        if src.get("status") != "review_stale":
            # an edit since the passing review is found now, not at the next run: same rule as the runner's
            path = p.module_path(sid)
            if (sandbox.sha256_file(path) if path.is_file() else None) != src.get("reviewed_sha"):
                store.upsert_source(p.name, sid, {"status": "review_stale"})
                src["status"] = "review_stale"
        if src.get("status") == "review_stale":
            findings.append({"source_id": sid, "kind": "review_stale", "message": "module changed after its passing review; collection paused"})
        done = [r for r in runs if r["status"] not in ("running",)]
        if len(done) >= 2 and all((r.get("rows_stored") or 0) == 0 for r in done[:2]):
            findings.append({"source_id": sid, "kind": "zero", "message": f"last two runs stored nothing ({done[0].get('stopped')}: {done[0].get('error') or ''})".strip()})
        if done and done[0]["status"] == "degraded":
            findings.append({"source_id": sid, "kind": "degraded", "message": f"{done[0].get('rows_stored')} rows vs baseline {src.get('baseline')}"})
        if len(done) >= 3 and all(r["status"] == "error" for r in done[:3]):
            findings.append({"source_id": sid, "kind": "error_streak", "message": done[0].get("error") or "three errors in a row"})
        hours = float(src["cadence_hours"]) if src.get("cadence_hours") not in (None, "") else cadence_hours(p.spec.cadence)
        if hours > 0:  # runs stamped in the future were dropped above, so they cannot hide staleness
            good = next((r for r in done if r["status"] in ("ok", "partial", "degraded")), None)
            ref = _ts(good["started_at"]) if good else _ts(src.get("updated_at"))
            if ref and now - ref > dt.timedelta(hours=2 * hours):
                findings.append({"source_id": sid, "kind": "stale", "message": f"no successful run since {ref.isoformat()} (cadence {hours:g} h)"})
    return findings


def _send(p: Project, alerts: list[dict]) -> tuple[list[str], bool]:
    """Every channel on its own: a full disk (alerts.jsonl) must not stop the webhook, nor a dead webhook the hook."""
    notes, ok = [], True
    if not alerts:
        return notes, ok
    try:
        with open(p.root / "alerts.jsonl", "a", encoding="utf-8") as f:
            for a in alerts:
                f.write(json.dumps({**a, "project": p.name, "at": now_iso()}, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        ok = False
        notes.append(f"alerts.jsonl failed: {exc.__class__.__name__}: {exc}"[:200])
    hook = os.environ.get("HARVEST_ALERT_HOOK")
    if hook:
        try:
            mod, _, fn = hook.partition(":")
            getattr(importlib.import_module(mod), fn)(p.name, alerts)
            notes.append("hook")
        except Exception as exc:
            ok = False
            notes.append(f"hook failed: {exc.__class__.__name__}")
    url = os.environ.get("HARVEST_ALERT_WEBHOOK")
    if url:
        try:
            import httpx
            r = httpx.post(url, json={"project": p.name, "alerts": alerts}, timeout=15)
            if r.status_code >= 300:
                raise RuntimeError(f"HTTP {r.status_code}")
            notes.append("webhook")
        except Exception as exc:
            ok = False
            notes.append(f"webhook failed: {exc.__class__.__name__}: {exc}"[:200])
    return notes, ok


def deliver(p: Project, alerts: list[dict]) -> list[str]:
    return _send(p, alerts)[0]


_deliver = deliver


def _suppressed(p: Project, f: dict, now: dt.datetime) -> bool:
    store = p.store
    since = (now - dt.timedelta(hours=REALERT_H)).isoformat()
    if f.get("key"):  # one alert per keyed event (a stuck run, a proxy-budget day)
        return store.alert_key_seen(p.name, f.get("source_id"), f["kind"], str(f["key"]), since)
    last = store.last_alert(p.name, f.get("source_id"), f["kind"])
    created = _ts(last["created_at"]) if last else None
    if created is None:
        return False
    if f["kind"] in INCIDENT_KINDS and f.get("source_id") and store.good_run_since(p.name, f["source_id"], last["created_at"]):
        return False  # a healthy run since the last alert ended that incident: this is a new one
    return now - created < dt.timedelta(hours=REALERT_H)


def raise_alerts(p: Project, findings: list[dict], *, now: dt.datetime | None = None, dedup: bool = True) -> dict:
    """Record new alerts for findings that are not already alerted, then deliver every alert still pending (the new
    ones and earlier ones whose delivery failed). Used by the watchdog, the runner and the proxy budget."""
    now = now or dt.datetime.now(dt.timezone.utc)
    store = p.store
    new = []
    for f in findings:
        if dedup and _suppressed(p, f, now):
            continue
        aid = store.add_alert(p.name, f.get("source_id"), f["kind"], f["message"], key=str(f["key"]) if f.get("key") else None,
                              created_at=now.isoformat(timespec="microseconds"))
        new.append({**f, "id": aid})
    pending = store.undelivered_alerts(p.name, (now - dt.timedelta(hours=REALERT_H)).isoformat())
    batch = [{"id": a["id"], "source_id": a.get("source_id"), "kind": a["kind"], "message": a["message"], "created_at": a["created_at"]} for a in pending]
    notes, ok = _send(p, batch)
    if batch:
        store.mark_alerts([a["id"] for a in batch], ok)
    return {"new": new, "delivered": notes, "sent": len(batch), "pending": 0 if ok else len(batch)}


def run(p: Project, *, dispatch: bool = False, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    findings = check(p, now=now)
    store = p.store
    raised = raise_alerts(p, findings, now=now)
    new_alerts, delivered = raised["new"], raised["delivered"]
    history = {}
    days = float(os.environ.get("HARVEST_HISTORY_DAYS", "90") or 0)
    if days > 0:
        history = store.prune_history(p.name, (now - dt.timedelta(days=days)).isoformat())
    dispatched, skipped = [], []
    enabled_flag = os.environ.get("HARVEST_REPAIR_DISPATCH", "off") == "on"
    if dispatch and not enabled_flag:
        skipped.append("dispatch requested but HARVEST_REPAIR_DISPATCH is not 'on'")
    if dispatch and enabled_flag:
        budget = int(os.environ.get("HARVEST_REPAIR_BUDGET", "2"))
        for f in findings:
            if len(dispatched) >= budget:
                break
            if f["kind"] not in ("zero", "degraded", "error_streak"):
                continue
            last = store.last_repair(p.name, f["source_id"])
            if last and (_ts(last["dispatched_at"]) or now) > now - dt.timedelta(hours=COOLDOWN_H):
                skipped.append(f"{f['source_id']}: cooldown")
                continue
            res = agents.run_job("repair", p, f["source_id"], extra=f"Finding: {f['kind']}: {f['message']}", wait=True)
            store.add_repair(p.name, f["source_id"], res.get("status", "?"), res)
            dispatched.append({"source_id": f["source_id"], **{k: res.get(k) for k in ("job_id", "status")}})
    return {"project": p.name, "findings": findings, "new_alerts": len(new_alerts), "delivered": delivered, "pending_alerts": raised["pending"],
            "history_pruned": {k: v for k, v in history.items() if v},
            "dispatched": dispatched, "skipped": skipped,
            "due_now": [s["id"] for s in store.list_sources(p.name, enabled=True) if scheduler.is_due(p, s, now)]}
