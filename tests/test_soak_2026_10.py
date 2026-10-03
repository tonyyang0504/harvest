"""Regressions for the defects the 24 h soak test of 2026-10 found (docs/SOAK_TEST_2026-10.md)."""

import datetime as dt
import os
import subprocess
import sys
import threading

import pytest
from conftest import add_source, make_project, ready_source
from localsite import Site, cars_site, html_page, start

from harvest_ai import extract, jobs, lanes, runner, sandbox, scheduler, watchdog
from harvest_ai.db import Store, now_iso

PG = os.environ.get("HARVEST_TEST_PG_DSN")
UTC = dt.timezone.utc


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="microseconds")


def _fake_run(p, sid, status, stored, started, finished=None, heartbeat=None):
    rid = p.store.start_run(p.name, sid, "test", None)
    p.store.execute("UPDATE runs SET status = ?, rows_stored = ?, started_at = ?, heartbeat_at = ?, finished_at = ? WHERE id = ?",
                    (status, stored, started, heartbeat or started, None if status == "running" else (finished or started), rid))
    return rid


def _hook(tmp_path, monkeypatch, name, fail_first=0):
    """An alert hook module that records what it receives; it raises on its first `fail_first` calls."""
    mod = tmp_path / f"{name}.py"
    mod.write_text(f"SEEN = []\nCALLS = [0]\ndef send(project, alerts):\n    CALLS[0] += 1\n    if CALLS[0] <= {fail_first}:\n"
                   f"        raise RuntimeError('receiver down')\n    SEEN.extend(alerts)\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("HARVEST_ALERT_HOOK", f"{name}:send")
    import importlib
    return importlib.import_module(name)


def _enabled_source(p, url="https://example.org/list"):
    sid = add_source(p, url)
    p.store.upsert_source(p.name, sid, {"enabled": 1, "status": "enabled", "cadence_hours": 1})
    return sid


# ---------------------------------------------------------------- alerts
def test_runner_degraded_alert_is_delivered_once_per_incident(tmp_path, monkeypatch):
    """Soak: `degraded` alerts were written by the runner straight into the table, so the webhook never got them, and
    one row was added per degraded run (31 for one source)."""
    seen = _hook(tmp_path, monkeypatch, "soakhook_degraded")
    site = start(cars_site(pages=3))
    try:
        p = make_project()
        sid = ready_source(p, site.url + "/cars")
        assert runner.run(p)["ran"][0]["status"] == "ok"  # 25 cars, complete: baseline 25
        site.route("/cars", cars_site(pages=1).routes["/cars"])  # the site now shows 10
        for _ in range(3):
            assert runner.run(p)["ran"][0]["status"] == "degraded"
    finally:
        site.server.shutdown()
    alerts = [a for a in p.store.list_alerts(p.name) if a["kind"] == "degraded"]
    assert len(alerts) == 1 and alerts[0]["delivered"] == 1
    assert [a["kind"] for a in seen.SEEN] == ["degraded"] and seen.SEEN[0]["id"] == alerts[0]["id"]
    assert watchdog.run(p)["new_alerts"] == 0  # the watchdog sees the same incident: not alerted twice
    assert p.store.count_records(p.name, sid) == 25  # and nothing was wiped


def test_a_recurring_incident_is_alerted_again_after_a_healthy_run(tmp_path, monkeypatch):
    """Soak: a source that recovered and failed again within 24 h was never re-alerted (the 2nd, 3rd and 4th
    zero-row incidents of the fixture source went unnoticed)."""
    seen = _hook(tmp_path, monkeypatch, "soakhook_recur")
    p = make_project()
    sid = _enabled_source(p)
    now = dt.datetime.now(UTC)
    t = lambda m: _iso(now - dt.timedelta(minutes=m))  # noqa: E731
    _fake_run(p, sid, "empty", 0, t(300))
    _fake_run(p, sid, "empty", 0, t(290))
    assert watchdog.run(p, now=now - dt.timedelta(minutes=289))["new_alerts"] == 1
    _fake_run(p, sid, "empty", 0, t(280))  # same incident continues: no new alert
    assert watchdog.run(p, now=now - dt.timedelta(minutes=279))["new_alerts"] == 0
    _fake_run(p, sid, "ok", 30, t(200))  # recovered
    _fake_run(p, sid, "empty", 0, t(20))
    _fake_run(p, sid, "empty", 0, t(10))  # a new incident, well inside 24 h of the first alert
    res = watchdog.run(p, now=now)
    assert [a["kind"] for a in seen.SEEN if a["kind"] == "zero"] == ["zero", "zero"]
    assert [f for f in res["findings"] if f["kind"] == "zero"] and res["new_alerts"] >= 1


def test_each_stuck_run_is_alerted(tmp_path, monkeypatch):
    """Soak: the second killed run (a day later than the first, but inside 24 h) was closed as stuck without any alert."""
    seen = _hook(tmp_path, monkeypatch, "soakhook_stuck")
    p = make_project()
    sid = _enabled_source(p)
    now = dt.datetime.now(UTC)
    _fake_run(p, sid, "running", 0, _iso(now - dt.timedelta(hours=5)), heartbeat=_iso(now - dt.timedelta(hours=5)))
    assert watchdog.run(p, now=now)["new_alerts"] >= 1
    _fake_run(p, sid, "running", 0, _iso(now - dt.timedelta(hours=1)), heartbeat=_iso(now - dt.timedelta(hours=1)))
    watchdog.run(p, now=now)
    assert [a["kind"] for a in seen.SEEN].count("stuck") == 2


def test_failed_deliveries_are_retried_and_channels_are_independent(tmp_path, monkeypatch):
    """A receiver that is down, a webhook answering 500 and an unwritable alerts.jsonl: the alert stays pending and
    is delivered on a later check; one failing channel does not stop the others."""
    seen = _hook(tmp_path, monkeypatch, "soakhook_retry", fail_first=1)
    posts = []

    class R:
        def __init__(self, code):
            self.status_code = code
    codes = iter([500, 204])
    import httpx
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: (posts.append(json), R(next(codes)))[1])
    monkeypatch.setenv("HARVEST_ALERT_WEBHOOK", "http://127.0.0.1:9/hook")
    p = make_project()
    sid = _enabled_source(p)
    (p.root / "alerts.jsonl").mkdir()  # as unwritable as a full disk: open() fails
    first = watchdog.raise_alerts(p, [{"source_id": sid, "kind": "zero", "message": "m"}])
    assert first["pending"] == 1 and any("alerts.jsonl failed" in n for n in first["delivered"])
    assert any(n.startswith("hook failed") for n in first["delivered"]) and any("HTTP 500" in n for n in first["delivered"])
    assert len(posts) == 1  # the webhook was still tried
    (p.root / "alerts.jsonl").rmdir()
    second = watchdog.raise_alerts(p, [])  # the next check
    assert second["pending"] == 0 and set(second["delivered"]) == {"hook", "webhook"}
    assert [a["kind"] for a in seen.SEEN] == ["zero"] and posts[-1]["alerts"][0]["id"] == first["new"][0]["id"]
    assert p.store.list_alerts(p.name)[0]["delivered"] == 1
    assert watchdog.raise_alerts(p, [])["sent"] == 0  # nothing left to send


def test_alerts_from_before_the_upgrade_are_not_resent(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE alerts (id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT, kind TEXT, message TEXT, created_at TEXT, "
              "delivered INTEGER DEFAULT 0)")
    c.execute("INSERT INTO alerts VALUES ('a1', 'x', 's', 'zero', 'm', ?, 0)", (now_iso(),))
    c.commit()
    c.close()
    st = Store(f"sqlite:///{db}")
    assert st.undelivered_alerts("x", now_iso(-3600)) == []


# ---------------------------------------------------------------- degraded baseline
def test_a_capped_source_is_not_degraded_by_its_accumulated_rows():
    """Soak: an API source capped at one page (never complete, so never a replace) was 'degraded' from the day its
    upserted rows passed twice one page: the reference was the row count of the table, not what a run yields."""
    site = Site()
    n = {"i": 0}

    def rotating(req):
        n["i"] += 1  # a fresh page of 10 cars on every request: the table keeps growing
        cards = "".join(f"<article class='ad' data-id='r{n['i']}-{k}'><a href='/cars/r{n['i']}-{k}'><h2>Toyota Camry</h2></a><span class='year'>2015</span>"
                        f"<span class='price'>5 000 000 ₸</span><span class='km'>90 тыс. км</span><span class='fuel'>Бензин</span>"
                        f"<span class='gear'>Автомат</span></article>" for k in range(10))
        return html_page(f"<main>{cards}</main>")
    site.route("/robots.txt", (200, {"content-type": "text/plain"}, "User-agent: *\nAllow: /\n"))
    site.route("/cars", rotating)
    start(site)
    try:
        p = make_project()
        sid = ready_source(p, site.url + "/cars")
        p.store.upsert_source(p.name, sid, {"max_pages": 1})
        statuses = [runner.run(p)["ran"][0]["status"] for _ in range(6)]
    finally:
        site.server.shutdown()
    assert statuses == ["partial"] * 6, statuses
    assert p.store.count_records(p.name, sid) >= 60 and p.store.recent_yield(p.name, sid) == 10


# ---------------------------------------------------------------- truncated feeds
FEED_MODULE = '''"""A feed source."""
from harvest_ai import extract


def fetch(page, *, http, ctx):
    if page > 1:
        return {"rows": [], "done": True}
    text = http.get_text(ctx["source"]["url"], accept="xml")
    rows = [{"source_id": it.get("guid"), "url": it.get("link"), "title": it.get("title"), "make": "Lada", "model": "Vesta",
             "year": "2018", "price": "4 000 000 ₸"} for it in extract.feed_items(text or "")]
    return {"rows": rows, "done": True}
'''


def test_a_truncated_feed_is_an_error_not_a_complete_empty_walk():
    """Soak: an RSS feed cut off at 55% parsed as zero items, and the walk was recorded as complete."""
    items = "".join(f"<item><title>Lada Vesta {i}</title><link>https://x.example/{i}</link><guid>g{i}</guid></item>" for i in range(12))
    full = f"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>{items}</channel></rss>"
    site = Site()
    site.route("/robots.txt", (200, {"content-type": "text/plain"}, "User-agent: *\nAllow: /\n"))
    site.route("/feed.rss", (200, {"content-type": "application/rss+xml"}, full))
    start(site)
    try:
        p = make_project()
        sid = ready_source(p, site.url + "/feed.rss", module=FEED_MODULE)
        assert runner.run(p)["ran"][0]["status"] == "ok"
        site.route("/feed.rss", (200, {"content-type": "application/rss+xml"}, full[: len(full) * 55 // 100]))
        r = runner.run(p)["ran"][0]
    finally:
        site.server.shutdown()
    assert r["status"] == "error" and r["complete"] is False and "FeedError" in (r["error"] or "")
    assert r["write_mode"] == "upsert" and r["pruned"] == 0 and p.store.count_records(p.name, sid) == 12


def test_a_walk_that_stored_nothing_because_requests_failed_is_an_error(cars):
    """Soak: a source answering 500 (or 429, or an unreachable robots.txt) produced `empty` runs and a zero alert
    reading "last two runs stored nothing (done: )"."""
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    assert runner.run(p)["ran"][0]["status"] == "ok"
    cars.route("/cars", (500, {"content-type": "text/html"}, "<p>Internal Server Error</p>"))
    r = runner.run(p)["ran"][0]
    assert r["status"] == "error" and "request(s) failed" in r["error"] and r["write_mode"] == "upsert" and r["pruned"] == 0
    assert p.store.count_records(p.name, sid) == 25
    cars.route("/cars", html_page("<main></main>"))  # really empty: still `empty`
    assert runner.run(p)["ran"][0]["status"] == "empty"


# ---------------------------------------------------------------- clocks
def test_a_run_stamped_in_the_future_does_not_stall_the_schedule():
    """Soak: two workers ran 2 h ahead for 25 min; back on the real clock, no source was due for ~2 h and the
    watchdog's stale check stayed silent (negative age)."""
    p = make_project()
    sid = _enabled_source(p)
    src = p.store.get_source(p.name, sid)
    now = dt.datetime.now(UTC)
    _fake_run(p, sid, "ok", 10, _iso(now - dt.timedelta(hours=3)))
    _fake_run(p, sid, "ok", 10, _iso(now + dt.timedelta(hours=2)))  # stamped by the clock that ran ahead
    assert scheduler.is_due(p, src, now)
    _fake_run(p, sid, "ok", 10, _iso(now - dt.timedelta(minutes=1)))  # the run made after the return
    assert not scheduler.is_due(p, src, now)  # and the cadence (1 h) holds again
    kinds = {f["kind"] for f in watchdog.check(p, now=now)}
    assert "clock_skew" in kinds
    p.store.execute("DELETE FROM runs WHERE started_at < ?", (_iso(now),))  # only the future-stamped run left
    stale_now = now + dt.timedelta(minutes=30)
    p.store.execute("UPDATE sources SET updated_at = ? WHERE id = ?", (_iso(now - dt.timedelta(hours=5)), sid))
    assert "stale" in {f["kind"] for f in watchdog.check(p, now=stale_now)}  # the future run no longer hides staleness


def test_a_run_job_whose_lease_is_lost_stores_nothing():
    """Soak: a job whose lease was taken over kept walking and finished as 'superseded' three times. The walk now stops
    with its job's lease and never stores or replaces on its behalf."""
    site = start(cars_site())
    try:
        p = make_project()
        sid = ready_source(p, site.url + "/cars")
        lost = threading.Event()
        lost.set()
        r = runner.run_source(p, sid, cancel=lost)
        out = runner.run(p, cancel=lost)
    finally:
        site.server.shutdown()
    assert r["status"] == "superseded" and r["stopped"] == "lease_lost" and r["rows_stored"] == 0 and r["pruned"] == 0
    assert out["ran"][0]["status"] == "skipped" and p.store.count_records(p.name, sid) == 0


@pytest.mark.skipif(not PG, reason="needs HARVEST_TEST_PG_DSN")
def test_pg_job_leases_use_the_server_clock(monkeypatch):
    """A worker whose own clock is 2 h behind claims a job; a reaper with a correct clock must not take it away."""
    from harvest_ai import db
    p = make_project()
    jid = p.store.add_job(p.name, "watchdog", {})
    real = db.now_iso
    monkeypatch.setattr(db, "now_iso", lambda delta_s=0: real(delta_s - 7200))
    skewed = Store(p.dsn)
    assert skewed.claim_job(p.name, "skewed", 120)["id"] == jid
    assert skewed.renew_job(jid, "skewed", 120)
    monkeypatch.setattr(db, "now_iso", real)
    assert p.store.reap_jobs(p.name) == {"lost": 0, "requeued": 0}
    assert p.store.get_job(jid)["status"] == "running"


# ---------------------------------------------------------------- Postgres sessions
@pytest.mark.skipif(not PG, reason="needs HARVEST_TEST_PG_DSN")
def test_pg_store_reconnects_after_its_session_is_killed():
    """Soak: after the server terminated the sessions, the web app answered 500 ('the connection is closed') on every
    request for that project until it was restarted."""
    st, killer = Store(PG), Store(PG)
    pid = st.one("SELECT pg_backend_pid() AS pid")["pid"]
    killer.one("SELECT pg_terminate_backend(?) AS ok", (pid,))
    assert st.one("SELECT 1 AS one")["one"] == 1  # a read is repeated once on a new session
    assert st.reconnects == 1
    pid = st.one("SELECT pg_backend_pid() AS pid")["pid"]
    killer.one("SELECT pg_terminate_backend(?) AS ok", (pid,))
    import psycopg
    with pytest.raises(psycopg.OperationalError):
        st.execute("UPDATE jobs SET status = status WHERE id = 'none'")  # a write is not replayed blindly ...
    assert st.execute("UPDATE jobs SET status = status WHERE id = 'none'") == 0  # ... but the next call works


# ---------------------------------------------------------------- worker loop
def test_the_worker_loop_survives_its_own_errors(monkeypatch):
    """Soak: an exception outside a job (a dead Postgres session; a failing sleep) ended the worker process."""
    calls, events, stop = {"n": 0}, [], threading.Event()

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("store unavailable")
        stop.set()
        return None
    monkeypatch.setattr(jobs, "work_once", flaky)
    jobs.worker(poll_s=0.01, stop=stop, log=events.append)
    assert calls["n"] == 3 and [e["event"] for e in events].count("worker_error") == 2


def test_one_broken_project_does_not_stop_the_queue(monkeypatch):
    make_project("good")
    make_project("bad")
    from harvest_ai import project
    good, bad = project.load("good"), project.load("bad")
    jid = good.store.add_job("good", "watchdog", {})

    def broken(*a, **kw):
        raise RuntimeError("database or disk is full")
    monkeypatch.setattr(bad.store, "claim_job", broken)
    events = []
    out = jobs.work_once("w", log=events.append)
    assert out and out["job_id"] == jid and events[0]["event"] == "queue_error"


# ---------------------------------------------------------------- sandbox scratch dirs
def test_scratch_dirs_of_dead_parents_are_swept(tmp_path):
    """Soak: every kill -9 of a worker left its walk's harvest-sbx-* directory behind."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    host = sandbox._hostname()
    for name, owner in (("harvest-sbx-dead", f"{dead.pid} {host}"), ("harvest-sbx-live", f"{os.getpid()} {host}"),
                        ("harvest-sbx-other", f"{dead.pid} another-host")):
        (tmp_path / name).mkdir()
        (tmp_path / (name + ".owner")).write_text(owner)
    (tmp_path / "harvest-sbx-unmarked").mkdir()
    removed = sandbox.sweep_stale(str(tmp_path))
    assert [os.path.basename(d) for d in removed] == ["harvest-sbx-dead"]
    assert not (tmp_path / "harvest-sbx-dead.owner").exists()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    os.symlink(victim, tmp_path / "harvest-sbx-planted")  # security: a planted symlink is never followed or removed
    os.utime(victim, (0, 0))
    old = (tmp_path / "harvest-sbx-unmarked")
    os.utime(old, (0, 0))
    assert [os.path.basename(d) for d in sandbox.sweep_stale(str(tmp_path))] == ["harvest-sbx-unmarked"]
    assert (victim / "keep.txt").exists() and (tmp_path / "harvest-sbx-planted").is_symlink()


def test_a_walk_leaves_nothing_in_the_temp_dir(cars, tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "t"))
    (tmp_path / "t").mkdir()
    p = make_project()
    ready_source(p, cars.url + "/cars")
    assert runner.run(p)["ran"][0]["status"] == "ok"
    assert not list((tmp_path / "t").glob("harvest-sbx-*"))


# ---------------------------------------------------------------- lanes
def test_a_short_page_without_scripts_is_html_not_a_javascript_shell(site):
    """Soak setup: a 15-item server-rendered list page without any script was sent to the browser lane."""
    cards = "".join(f"<article><a href='/q/{i}'>M 4.{i} quake near somewhere</a></article>" for i in range(15))
    site.route("/robots.txt", (200, {"content-type": "text/plain"}, "User-agent: *\nAllow: /\n"))
    site.route("/quakes", html_page(f"<h1>Recent quakes</h1>{cards}<a href='/terms'>Terms</a>"))
    site.route("/shell", html_page("<div id='app'></div><noscript>enable javascript</noscript>", head="<script src='/app.js'></script>"))
    site.route("/tiny-js", html_page("<p>Loading listings</p>", head="<script src='/bundle.js'></script>"))
    assert lanes.detect(site.url + "/quakes")["lane"] == "html"
    assert lanes.detect(site.url + "/shell")["lane"] == "browser"
    assert lanes.detect(site.url + "/tiny-js")["lane"] == "browser"


def test_feed_parse_errors_are_errors_but_detection_stays_lenient():
    with pytest.raises(extract.FeedError):
        extract.feed_items("<rss><channel><item><title>cut")
    assert lanes._feed_items("<rss><channel><item><title>cut") == []


# ---------------------------------------------------------------- history retention
def test_history_older_than_the_retention_is_pruned(monkeypatch):
    """Soak: runs grew by ~500 rows a day per instance with no retention."""
    monkeypatch.setenv("HARVEST_HISTORY_DAYS", "30")
    p = make_project()
    sid = _enabled_source(p)
    now = dt.datetime.now(UTC)
    old = _fake_run(p, sid, "ok", 5, _iso(now - dt.timedelta(days=40)))
    keep = _fake_run(p, sid, "ok", 5, _iso(now - dt.timedelta(days=2)))
    running = _fake_run(p, sid, "running", 0, _iso(now - dt.timedelta(days=40)), heartbeat=_iso(now))  # long, still alive
    p.store.add_quarantine(p.name, sid, old, ["x"], {"a": 1})
    p.store.execute("UPDATE quarantine SET created_at = ?", (_iso(now - dt.timedelta(days=40)),))
    res = watchdog.run(p, now=now)
    ids = {r["id"] for r in p.store.list_runs(p.name, sid, 50)}
    assert old not in ids and keep in ids and running in ids
    assert res["history_pruned"]["runs"] == 1 and res["history_pruned"]["quarantine"] == 1
    monkeypatch.setenv("HARVEST_HISTORY_DAYS", "0")
    _fake_run(p, sid, "ok", 5, _iso(now - dt.timedelta(days=400)))
    assert watchdog.run(p, now=now)["history_pruned"] == {}
