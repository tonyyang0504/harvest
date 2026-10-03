"""The durable job queue: claim/lease/heartbeat, workers, crash recovery, modes."""

import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time

import pytest
from conftest import make_project, ready_source
from localsite import cars_site, start

from harvest_ai import agents, jobs, service
from harvest_ai.db import Store, now_iso


def _wait(pred, timeout=30, step=0.1):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not reached")


def test_only_one_worker_wins_a_claim():
    p = make_project()
    jid = p.store.add_job(p.name, "watchdog", {})
    stores = [Store(p.dsn) for _ in range(6)]  # separate connections, as separate processes would have
    wins, barrier = [], threading.Barrier(len(stores))

    def claim(st, i):
        barrier.wait()
        if st.claim_job(p.name, f"w{i}", 30):
            wins.append(i)
    ts = [threading.Thread(target=claim, args=(st, i)) for i, st in enumerate(stores)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1
    job = p.store.get_job(jid)
    assert job["status"] == "running" and job["worker_id"] == f"w{wins[0]}" and job["attempts"] == 1 and job["lease_until"] > now_iso()
    assert p.store.claim_job(p.name, "late", 30) is None


def test_lease_ownership_renew_and_finish():
    p = make_project()
    jid = p.store.add_job(p.name, "watchdog", {})
    assert p.store.claim_job(p.name, "a", 30)["id"] == jid
    assert p.store.renew_job(jid, "a", 30) and not p.store.renew_job(jid, "b", 30)
    assert not p.store.finish_job(jid, "b", "done", {"x": 1})
    assert p.store.finish_job(jid, "a", "done", {"x": 1})
    job = p.store.get_job(jid)
    assert job["status"] == "done" and job["result"] == {"x": 1} and job["lease_until"] is None
    assert not p.store.finish_job(jid, "a", "failed", {})  # finished jobs are final


def test_expired_lease_is_reclaimed_until_attempts_run_out():
    p = make_project()
    jid = p.store.add_job(p.name, "watchdog", {}, max_attempts=2)
    assert p.store.claim_job(p.name, "a", 0.05)
    time.sleep(0.1)
    job = p.store.claim_job(p.name, "b", 30)
    assert job["id"] == jid and job["attempts"] == 2 and job["worker_id"] == "b"
    assert not p.store.finish_job(jid, "a", "done", {})  # the dead worker's late result is refused
    p.store.update_job(jid, lease_until=now_iso(-1))
    assert p.store.claim_job(p.name, "c", 30) is None  # attempts exhausted
    assert p.store.reap_jobs(p.name) == {"lost": 1, "requeued": 0} and p.store.get_job(jid)["status"] == "lost"


def test_queue_mode_enqueues_and_a_worker_runs_it(cars, monkeypatch):
    monkeypatch.setenv("HARVEST_JOB_MODE", "queue")
    p = make_project()
    ready_source(p, cars.url + "/cars")
    sub = service.start_job(p.name, "run", {})
    assert sub["status"] == "queued" and p.store.get_job(sub["job_id"])["status"] == "queued"
    out = jobs.work_once(jobs.worker_id())
    assert out["job_id"] == sub["job_id"] and out["status"] == "done" and out["result"]["ran"][0]["rows_stored"] == 25
    assert jobs.work_once(jobs.worker_id()) is None
    job = service.jobs(p.name, sub["job_id"])["job"]
    assert job["status"] == "done" and job["attempts"] == 1 and job["heartbeat_at"]


def test_inline_mode_runs_in_process(monkeypatch):
    monkeypatch.setenv("HARVEST_JOB_MODE", "inline")
    p = make_project()
    sub = service.start_job(p.name, "watchdog", {})
    assert sub["status"] == "running"
    assert _wait(lambda: p.store.get_job(sub["job_id"])["status"] == "done")


def test_worker_kinds_filter_and_failure_recording(monkeypatch):
    monkeypatch.setenv("HARVEST_JOB_MODE", "queue")
    p = make_project()
    a = service.start_job(p.name, "watchdog", {})["job_id"]
    b = service.start_job(p.name, "review", {"source_id": "nope"})["job_id"]
    assert jobs.work_once("w", kinds=["review"])["job_id"] == b
    job = p.store.get_job(b)
    assert job["status"] == "failed" and "LookupError" in job["result"]["error"]
    assert p.store.get_job(a)["status"] == "queued"
    jobs.worker(once=True, log=lambda e: None)
    assert p.store.get_job(a)["status"] == "done"


def test_worker_crash_mid_job_is_recovered(tmp_path, home):
    """kill -9 a worker process while it runs a job; after the lease expires another worker takes over."""
    slow = start(cars_site(pages=3))
    orig = slow.routes["/cars"]
    slow.route("/cars", lambda req: (lambda r: (200, {"content-type": "text/html", "x-delay": "0.8"}, r))(orig(req)))
    try:
        p = make_project()
        sid = ready_source(p, slow.url + "/cars")
        p.store.execute("DELETE FROM runs")
        jid = p.store.add_job(p.name, "run", {})
        # a crashed worker's source lock (locks.py) outlives it by its lease, as its job lease does: keep both short here
        env = {**os.environ, "HARVEST_HOME": str(home), "PYTHONPATH": os.pathsep.join(x for x in sys.path if x), "HARVEST_SOURCE_LOCK_LEASE_S": "2"}
        proc = subprocess.Popen([sys.executable, "-m", "harvest_ai.cli", "worker", "--once", "--lease", "2", "--poll", "0.1"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        _wait(lambda: p.store.get_job(jid)["status"] == "running" and p.store.list_runs(p.name, sid), timeout=60)
        first = p.store.get_job(jid)
        os.killpg(proc.pid, signal.SIGKILL)  # the worker and its sandbox child die mid-walk
        proc.wait(timeout=10)
        time.sleep(2.5)  # the job lease and the source lock run out; nobody renews them
        assert p.store.get_job(jid)["status"] == "running" and p.store.get_job(jid)["lease_until"] < now_iso()
        out = jobs.work_once(jobs.worker_id("second"), lease_s=5)
        job = p.store.get_job(jid)
        assert out["job_id"] == jid and job["status"] == "done" and job["attempts"] == 2 and job["worker_id"] != first["worker_id"]
        assert p.store.count_records(p.name, sid) == 25
    finally:
        slow.server.shutdown()


def test_agent_is_killed_when_the_lease_is_lost(tmp_path, monkeypatch):
    script = tmp_path / "slow-agent"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HARVEST_AGENT_BIN", str(script))
    p = make_project()
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    t0 = time.monotonic()
    status, result = agents.run_agent("census", p, None, log_path=tmp_path / "a.log", cancel=cancel)
    assert status == "failed" and "lost its lease" in result["error"] and time.monotonic() - t0 < 10


def test_web_api_enqueues(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from harvest_ai.web.app import create_app
    monkeypatch.setenv("HARVEST_JOB_MODE", "queue")
    make_project()
    c = TestClient(create_app())
    auth = {"Authorization": "Bearer test-token"}
    r = c.post("/api/projects/cars/watchdog", headers=auth)
    assert r.status_code == 200  # synchronous endpoint
    sub = c.post("/api/projects/cars/run", headers=auth, json={}).json()
    assert sub["status"] == "queued"
    jobs.work_once(jobs.worker_id())
    assert c.get(f"/api/projects/cars/jobs/{sub['job_id']}", headers=auth).json()["job"]["status"] == "done"


def test_old_jobs_table_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT, status TEXT, params TEXT, result TEXT, log_path TEXT, "
                "created_at TEXT, started_at TEXT, finished_at TEXT, pid INTEGER)")
    con.execute("INSERT INTO jobs (id, project, kind, status, params, created_at) VALUES ('j1', 'p', 'run', 'queued', '{}', ?)", (now_iso(),))
    con.commit()
    con.close()
    st = Store(f"sqlite:///{db}")
    job = st.claim_job("p", "w", 30)
    assert job["id"] == "j1" and job["attempts"] == 1
