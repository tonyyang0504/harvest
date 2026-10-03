"""The runner: walk enabled sources, normalise and store what they return.

- one sandbox child per source walk, killed at the source's time budget (+30 s grace);
- sources that share a registrable domain run one after another (per-host concurrency 1 across
  processes); different domains run in parallel (`workers`);
- a project-level lock file stops two runs of one project overlapping (cron + manual);
- rows are normalised as pages arrive: good rows are upserted, bad rows go to quarantine with the
  reasons; the run row carries a heartbeat so the watchdog can see a stuck walk;
- the never-wipe rule is applied at the end (see `db.Store.finalize_run`);
- a module whose sha differs from its passing review is not run (status `review_stale`);
- the residential-proxy route is armed per walk only when the operator switched it on and the source's
  robots/terms verdict allows collection (proxy.py); the walk uses it only after an IP-level block.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from . import locks, proxy, sandbox, scheduler, uamode
from .db import now_iso
from .normalize import record_uid
from .project import Project
from .review import job_for, normalizer_for

DEGRADED_FRACTION = 0.5
MIN_BASELINE_FOR_DEGRADED = 20


@contextmanager
def project_lock(p: Project):
    path = p.root / "run.lock"
    p.root.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # pragma: no cover
            pass
        except OSError:
            yield False
            return
        yield True
    finally:
        f.close()


def run_source(p: Project, sid: str, *, trigger: str = "manual", max_pages: int | None = None, use_proxy: bool = True,
               proxy_pending: proxy.Pending | None = None, use_browser_ua: bool = True, cancel: threading.Event | None = None) -> dict:
    """`cancel`: set by a job whose queue lease was lost (another worker owns the job now); the walk stops and stores
    nothing more, like a lost source lock."""
    store = p.store
    src = store.get_source(p.name, sid)
    if not src:
        raise LookupError(f"no source {sid}")
    path = p.module_path(sid)
    cur_sha = sandbox.sha256_file(path) if path.is_file() else None
    if not src.get("enabled"):
        return {"source_id": sid, "status": "skipped", "reason": "not enabled"}
    if not cur_sha or cur_sha != src.get("reviewed_sha"):
        store.upsert_source(p.name, sid, {"status": "review_stale"})
        from . import watchdog
        watchdog.raise_alerts(p, [{"source_id": sid, "kind": "review_stale", "message": "module changed after its passing review; not run until reviewed again"}])
        return {"source_id": sid, "status": "review_stale", "reason": "module sha differs from the reviewed sha"}
    try:
        with locks.held(p, sid, "run") as lk:
            return _run_locked(p, sid, src, cur_sha, lk, trigger=trigger, max_pages=max_pages, use_proxy=use_proxy, proxy_pending=proxy_pending,
                               use_browser_ua=use_browser_ua, cancel=cancel)
    except locks.SourceBusy as exc:
        return {"source_id": sid, "status": "busy", "reason": str(exc)}


def _run_locked(p: Project, sid: str, src: dict, cur_sha: str, lk, *, trigger: str, max_pages: int | None, use_proxy: bool,
                proxy_pending: proxy.Pending | None, use_browser_ua: bool, cancel: threading.Event | None = None) -> dict:
    store = p.store

    def gone() -> bool:  # this walk no longer owns the source (lock) or its job (queue lease)
        return lk.lost or bool(cancel is not None and cancel.is_set())
    run_id = store.start_run(p.name, sid, trigger, cur_sha)
    budget = int(src.get("time_budget_s") or p.spec.time_budget_s)
    pages_cap = int(max_pages or src.get("max_pages") or p.spec.max_pages)
    if src.get("lane") == "browser":
        pages_cap = 1
    norm = normalizer_for(p, src)
    ua = uamode.mode_for(p, src, "run", use_browser_ua)
    store.heartbeat(run_id, ua_mode=ua)
    counters = {"pages": 0, "fetched": 0, "stored": 0, "quarantined": 0, "last_beat": 0.0}
    lock = threading.Lock()

    def on_event(ev: dict) -> None:
        if gone():  # another worker owns the source or the job now: nothing from this walk is stored
            return
        if ev.get("type") == "page":
            good, n_bad = [], 0
            for raw in ev.get("rows") or []:
                rec, errs, _warns = norm.normalize(raw)
                if rec is None:
                    store.add_quarantine(p.name, sid, run_id, errs, raw)
                    n_bad += 1
                    continue
                good.append((record_uid(sid, rec), rec, norm.fingerprints(rec)))
            if good:
                store.upsert_records(p.name, sid, run_id, good)
            with lock:
                counters["pages"] = ev.get("page", counters["pages"])
                counters["fetched"] += len(ev.get("rows") or [])
                counters["stored"] += len(good)
                counters["quarantined"] += n_bad
            store.heartbeat(run_id, pages=counters["pages"], rows_fetched=counters["fetched"], rows_stored=counters["stored"],
                            rows_quarantined=counters["quarantined"])
            counters["last_beat"] = time.monotonic()
        elif ev.get("type") == "tick" and time.monotonic() - counters["last_beat"] > 15:
            store.heartbeat(run_id)
            counters["last_beat"] = time.monotonic()

    error = None
    try:
        res = proxy.sandboxed(p, src, job_for(p, src, cur_sha, max_pages=pages_cap, ua_mode=ua), budget + 30, on_event, purpose="run",
                              pending=proxy_pending, enabled=use_proxy, stop=gone)
    except Exception as exc:  # the runner itself never dies on one source
        res = {"pages": [], "end": None, "timed_out": False, "stderr": f"{exc.__class__.__name__}: {exc}"}
    end = res.get("end") or {}
    if res.get("timed_out"):
        stopped, complete = "time_budget", False
    elif end:
        stopped, complete = end.get("stopped"), bool(end.get("complete"))
        error = end.get("error")
        if src.get("lane") == "browser" and complete:
            # page 1 only by policy: the walk never sees the whole inventory, so it must never trigger a replace
            complete, stopped = False, "page1_only"
    else:
        stopped, complete = "crashed", False
        error = (res.get("stderr") or "").strip().splitlines()[-1:] or ["no result"]
        error = error[0][:500]
    page_errors = [pg["error"] for pg in res.get("pages") or [] if pg.get("error")]
    if page_errors and not error:
        error = page_errors[-1][:500]
    if not error and counters["stored"] == 0:
        # nothing stored because requests failed (a 5xx, a persistent 429, robots.txt unreachable): say so, instead of
        # looking like a source that is simply empty (soak 2026-10: outages were recorded as `empty`, alerted "(done: )")
        hs = end.get("http") or {}
        parts = [f"{hs[k]} {label}" for k, label in (("errors", "request(s) failed"), ("blocked", "blocked"),
                                                     ("robots_denied", "denied by robots.txt"), ("gated", "refused by the URL gate"))
                 if int(hs.get(k) or 0) > 0]
        if parts:
            error = "no rows: " + ", ".join(parts)
    if not lk.valid() or gone():
        # the walk lost the source lock (this worker stalled past its lease and another took the source) or its job's queue
        # lease (the job was handed to another worker): never replace or prune on behalf of a walk that no longer owns it
        why, what = ("lock_lost", "the source lock") if lk.lost else ("lease_lost", "the job's queue lease")
        store.heartbeat(run_id, finished_at=now_iso(), status="superseded", stopped=why, complete=0, write_mode="none", pruned=0,
                        error=f"lost {what} to another worker; nothing replaced or pruned")
        return {"source_id": sid, "run_id": run_id, "status": "superseded", "stopped": why, "complete": False, "pages": counters["pages"],
                "rows_fetched": counters["fetched"], "rows_stored": counters["stored"], "rows_quarantined": counters["quarantined"],
                "write_mode": "none", "pruned": 0, "error": f"lost {what}", "ua_mode": ua}
    # degraded is judged against what a run of this source normally yields: the baseline of its last complete walk, or,
    # for a source whose walks never complete (a page cap), the median of its recent healthy runs. Not the row count of
    # the table, which grows with every upsert walk (soak 2026-10: 31 false `degraded` alerts for a capped API source)
    baseline = int(src.get("baseline") or store.recent_yield(p.name, sid, run_id) or 0)
    fin = store.finalize_run(p.name, sid, run_id, complete=complete, stored=counters["stored"], prune_after_days=p.spec.prune_after_days)
    if stopped in ("import_error", "sha_mismatch", "crashed", "errors"):
        status = "error"
    elif stopped == "time_budget" and counters["stored"] == 0:
        status = "timeout"
    elif counters["stored"] == 0 and error:
        status = "error"  # a page failed (e.g. an unparsable, truncated feed) and nothing was stored: not a quiet empty source
    elif counters["stored"] == 0:
        status = "empty"
    elif (baseline >= MIN_BASELINE_FOR_DEGRADED and counters["stored"] < DEGRADED_FRACTION * baseline
          and not (max_pages is not None and stopped == "max_pages")):  # an explicitly capped walk is not a degradation
        status = "degraded"
    elif complete:
        status = "ok"
    else:
        status = "partial"
    store.heartbeat(run_id, finished_at=now_iso(), status=status, stopped=stopped, complete=1 if complete else 0, write_mode=fin["write_mode"],
                    pruned=fin["pruned"], error=error, http_stats={**(end.get("http") or {}), "ua_mode": ua, "isolation": res.get("isolation"),
                                                                **({"proxy": res["proxy"]} if res.get("proxy") else {})})
    if status == "degraded":
        from . import watchdog
        watchdog.raise_alerts(p, [{"source_id": sid, "kind": "degraded",
                                   "message": f"{counters['stored']} rows, under {DEGRADED_FRACTION:.0%} of the baseline {baseline}"}])
    return {"source_id": sid, "run_id": run_id, "status": status, "stopped": stopped, "complete": complete, "pages": counters["pages"],
            "rows_fetched": counters["fetched"], "rows_stored": counters["stored"], "rows_quarantined": counters["quarantined"],
            "write_mode": fin["write_mode"], "pruned": fin["pruned"], "error": error, "ua_mode": ua, **({"proxy": res["proxy"]} if res.get("proxy") else {})}


def run(p: Project, source_ids: list[str] | None = None, *, due: bool = False, trigger: str = "manual", workers: int | None = None,
        max_pages: int | None = None, use_proxy: bool = True, use_browser_ua: bool = True, cancel: threading.Event | None = None) -> dict:
    store = p.store
    sources = [s for s in store.list_sources(p.name, enabled=True)]
    if source_ids:
        wanted = set(source_ids)
        unknown = wanted - {s["id"] for s in store.list_sources(p.name)}
        if unknown:
            raise LookupError(f"unknown sources {sorted(unknown)}")
        sources = [s for s in sources if s["id"] in wanted]
    if due:
        sources = [s for s in sources if scheduler.is_due(p, s)]
    if not sources:
        return {"project": p.name, "ran": [], "note": "nothing to run" + (" (no source is due)" if due else " (no enabled source matches)")}
    pending = proxy.Pending(sum(1 for s in sources if use_proxy and proxy.eligible(p, s, "run")[0]))
    groups: dict[str, list[str]] = {}
    for s in sources:
        groups.setdefault(s.get("domain") or s["id"], []).append(s["id"])
    results: list[dict] = []
    with project_lock(p) as got:
        if not got:
            return {"project": p.name, "ran": [], "busy": True, "note": "another run of this project is in progress"}

        def do_group(ids: list[str]) -> list[dict]:
            out = []
            for sid in ids:
                try:
                    if cancel is not None and cancel.is_set():
                        out.append({"source_id": sid, "status": "skipped", "reason": "the job's lease was lost"})
                        continue
                    out.append(run_source(p, sid, trigger=trigger, max_pages=max_pages, use_proxy=use_proxy, proxy_pending=pending,
                                          use_browser_ua=use_browser_ua, cancel=cancel))
                except Exception as exc:
                    out.append({"source_id": sid, "status": "error", "error": f"{exc.__class__.__name__}: {exc}"})
            return out
        n = workers or int(os.environ.get("HARVEST_WORKERS", "4"))
        with ThreadPoolExecutor(max_workers=max(1, min(n, len(groups)))) as ex:
            for r in ex.map(do_group, list(groups.values())):
                results.extend(r)
    return {"project": p.name, "ran": results, "ok": sum(1 for r in results if r["status"] == "ok"),
            "rows_stored": sum(r.get("rows_stored", 0) for r in results),
            "proxy": {"requests": sum((r.get("proxy") or {}).get("requests", 0) for r in results),
                      "bytes": sum((r.get("proxy") or {}).get("bytes", 0) for r in results)}}
