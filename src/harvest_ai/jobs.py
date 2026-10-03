"""Durable jobs: a queue in the jobs table with claim, lease and heartbeat, run by worker processes.

Lifecycle: `queued` -> (a worker claims it, attempts += 1, lease set) `running` -> `done` | `failed` | `timeout`.
While it runs, the owner renews the lease every lease/3 seconds. When a worker dies (crash, kill -9,
host restart) its lease expires: the job goes back to `queued` and the next worker takes it, until
`max_attempts` is used up, then it is `lost`. A worker that lost its lease cannot record an outcome
(ownership check in `finish_job`), and a running agent is killed.

Modes (`HARVEST_JOB_MODE`): `queue` (default: the web app and API enqueue, `harvest worker` executes) or
`inline` (dev: the submitting process runs the job in a thread, through the same claim/lease path).
`wait=True` always runs the job synchronously in the caller, also through claim/lease.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback
import uuid
from typing import Callable

from . import sandbox
from .project import Project, list_projects, load

KINDS = ("agent:census", "agent:audit", "agent:build", "agent:repair", "detect_lanes", "review", "run", "watchdog")
DEFAULT_LEASE_S = float(os.environ.get("HARVEST_JOB_LEASE_S", "120"))


def worker_id(tag: str = "worker") -> str:
    return f"{tag}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _jsonable(x):
    return json.loads(json.dumps(x, default=str, ensure_ascii=False))


def execute(p: Project, job: dict, cancel: threading.Event) -> tuple[str, dict]:
    """Run one claimed job. Returns (status, result)."""
    from . import agents, service
    kind, params = job["kind"], job.get("params") or {}
    if isinstance(params, str):
        params = json.loads(params)
    if kind.startswith("agent:"):
        log = p.agents_dir / f"{job['id']}.log"
        p.agents_dir.mkdir(parents=True, exist_ok=True)
        p.store.update_job(job["id"], log_path=str(log))
        return agents.run_agent(kind.split(":", 1)[1], p, params.get("source_id"), log_path=log, extra=params.get("extra") or "",
                                cli=params.get("cli"), model=params.get("model"), timeout_s=params.get("timeout_s"),
                                on_start=lambda pid: p.store.update_job(job["id"], pid=pid), cancel=cancel)
    if kind == "detect_lanes":
        return "done", service.detect_lane(p.name, params.get("source_id"))
    if kind == "review":
        return "done", service.review_source(p.name, params["source_id"])
    if kind == "run":
        return "done", service.run(p.name, params.get("source_ids"), due=bool(params.get("due")), trigger="job",
                                   use_proxy=params.get("use_proxy", True) is not False,
                                   use_browser_ua=params.get("use_browser_ua", True) is not False, cancel=cancel)
    if kind == "watchdog":
        return "done", service.run_watchdog(p.name, bool(params.get("dispatch")))
    raise ValueError(f"unknown job kind {kind!r}")


def run_claimed(p: Project, job: dict, wid: str, lease_s: float = DEFAULT_LEASE_S) -> dict:
    """Execute a job this worker has claimed, renewing its lease until it finishes."""
    stop, cancel = threading.Event(), threading.Event()

    def beat():
        while not stop.wait(max(0.2, lease_s / 3)):
            if not p.store.renew_job(job["id"], wid, lease_s):
                cancel.set()  # somebody else owns it now (our lease expired): stop working on it
                return
    hb = threading.Thread(target=beat, daemon=True)
    hb.start()
    try:
        status, result = execute(p, job, cancel)
    except Exception as exc:
        from .proxy import redact
        status, result = "failed", {"error": redact(f"{exc.__class__.__name__}: {exc}"), "trace": redact(traceback.format_exc()[-2000:])}
    finally:
        stop.set()
        hb.join(timeout=5)
    recorded = p.store.finish_job(job["id"], wid, status, _jsonable(result))
    return {"job_id": job["id"], "id": job["id"], "kind": job["kind"], "status": status if recorded else "superseded", "result": _jsonable(result)}


def submit(p: Project, kind: str, params: dict | None = None, *, wait: bool = False, mode: str | None = None, max_attempts: int = 3) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    params = {k: v for k, v in (params or {}).items() if v is not None}
    if kind.startswith("agent:"):
        max_attempts = 1  # an agent run is not idempotent enough to replay automatically; a failed one is re-submitted by a person
    jid = p.store.add_job(p.name, kind, params, max_attempts=max_attempts)
    mode = mode or os.environ.get("HARVEST_JOB_MODE", "queue")
    if wait or mode == "inline":
        wid = worker_id("inline")
        job = p.store.claim_job(p.name, wid, DEFAULT_LEASE_S, job_id=jid)
        if job is None:  # pragma: no cover - a queue worker grabbed it first
            return {"job_id": jid, "status": "queued"}
        if wait:
            return run_claimed(p, job, wid)
        threading.Thread(target=run_claimed, args=(p, job, wid), daemon=True).start()
        return {"job_id": jid, "status": "running"}
    return {"job_id": jid, "status": "queued"}


def reap(p: Project) -> dict:
    return p.store.reap_jobs(p.name)


def work_once(wid: str, lease_s: float = DEFAULT_LEASE_S, kinds: list[str] | None = None, projects: list[str] | None = None,
              log: Callable[[dict], None] | None = None) -> dict | None:
    """Claim and run at most one job from any project. Returns the job outcome, or None when idle."""
    for pr in list_projects():
        if projects and pr["name"] not in projects:
            continue
        try:
            p = load(pr["name"])
            reap(p)
            job = p.store.claim_job(p.name, wid, lease_s, kinds=kinds)
        except (LookupError, ValueError):
            continue
        except Exception as exc:  # one project's store being down (a Postgres restart, a full disk) must not stop the others
            if log:
                log({"event": "queue_error", "project": pr["name"], "error": f"{exc.__class__.__name__}: {str(exc)[:300]}"})
            continue
        if job:
            return {"project": p.name, "kind": job["kind"], **run_claimed(p, job, wid, lease_s)}
    return None


def worker(*, poll_s: float = 2.0, lease_s: float = DEFAULT_LEASE_S, once: bool = False, kinds: list[str] | None = None,
           schedule_s: float | None = None, dispatch: bool = False, log: Callable[[dict], None] | None = None,
           stop: threading.Event | None = None) -> None:
    """The worker loop. With `schedule_s`, every that many seconds it also runs due sources and the watchdog of
    every project (what `harvest daemon` used to do). `once`: drain the queue, then return.

    The loop never dies on an error of its own: it logs `worker_error`, backs off (up to 60 s) and carries on. In the
    2026-10 soak an uncaught exception (a dead Postgres session) ended the process and only systemd's restart brought
    it back; under `harvest daemon` without a restart policy that would have stopped collection."""
    from . import service
    wid = worker_id()
    log = log or (lambda e: print(json.dumps(e, default=str), flush=True))
    log({"event": "worker_start", "worker": wid, "lease_s": lease_s, "kinds": kinds})
    try:
        swept = sandbox.sweep_stale()
        if swept:
            log({"event": "sandbox_swept", "dirs": swept})
    except Exception:  # pragma: no cover - housekeeping only
        pass
    pause = stop or threading.Event()
    next_tick = time.monotonic()
    errors = 0
    while not pause.is_set():
        try:
            if schedule_s and time.monotonic() >= next_tick:
                next_tick = time.monotonic() + schedule_s
                for pr in list_projects():
                    try:
                        r = service.run(pr["name"], due=True, trigger="schedule")
                        w = service.run_watchdog(pr["name"], dispatch=dispatch)
                        log({"event": "tick", "project": pr["name"], "ran": len(r.get("ran", [])), "findings": len(w["findings"])})
                    except Exception as exc:
                        log({"event": "tick_error", "project": pr["name"], "error": f"{exc.__class__.__name__}: {exc}"})
            out = work_once(wid, lease_s, kinds, log=log)
            errors = 0
        except Exception as exc:
            errors += 1
            log({"event": "worker_error", "error": f"{exc.__class__.__name__}: {str(exc)[:300]}", "consecutive": errors})
            pause.wait(min(60.0, max(poll_s, 0.1) * 2 ** min(errors, 5)))
            continue
        if out:
            log({"event": "job", **{k: out.get(k) for k in ("project", "job_id", "kind", "status")}})
            continue
        if once:
            return
        pause.wait(poll_s)
