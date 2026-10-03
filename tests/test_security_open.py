"""Regression tests for the items left open by the first pass of the 2026-10 security review (O1-O7 in
docs/SECURITY_REVIEW_2026-10.md). Each was written before its fix."""

import threading
import time

import pytest
from conftest import add_source, make_project, ready_source
from localsite import CAR_MODULE as CAR_MODULE_TEXT
from localsite import cars_site, start

from harvest_ai import review, sandbox, service
from harvest_ai.http import Http


# ---------------------------------------------------------------- O4: session headers
def test_http_refuses_session_headers_by_default(site):
    site.route("/api", (200, {"content-type": "application/json"}, "{}"))
    h = Http(rate_s=0, respect_robots=False)
    try:
        for hdr in ({"Cookie": "sid=1"}, {"authorization": "Bearer k"}, {"Proxy-Authorization": "Basic x"}):
            assert h.get(site.url + "/api", headers=hdr) is None
        assert site.count("/api") == 0 and h.stats["session_refused"] == 3 and h.stats["gated"] == 3
        assert h.get(site.url + "/api", headers={"X-Api-Key": "public"}) is not None
    finally:
        h.close()
    h = Http(rate_s=0, respect_robots=False, allow_session_headers=True)
    try:
        assert h.get(site.url + "/api", headers={"Authorization": "Bearer published-key"}) is not None
    finally:
        h.close()


def test_session_headers_need_an_operator_decision_per_source(cars):
    p = make_project()
    sid = add_source(p, cars.url + "/cars")
    code = ("def fetch(page, *, http, ctx):\n    t = http.get_text(ctx['source']['url'], headers={'Cookie': 'session=1'})\n"
            "    return [] if t is None else [{'source_id': '1', 'url': 'http://x/1', 'make': 'a', 'price': '1000000'}]\n")
    p.module_path(sid).write_text(code, encoding="utf-8")
    src = p.store.get_source(p.name, sid)
    assert review.job_for(p, src, None, max_pages=1)["http"]["allow_session_headers"] is False
    res = sandbox.run(review.job_for(p, src, None, max_pages=1), 30)
    assert res["pages"][0]["rows"] == [] and res["end"]["http"]["session_refused"] == 1
    with pytest.raises(ValueError):
        service.session_headers_enable(p.name, sid, "short")
    service.session_headers_enable(p.name, sid, "the site's public API documents this key", by="test")
    src = p.store.get_source(p.name, sid)
    assert src["lane_detail"]["session_headers"]["by"] == "test"
    res = sandbox.run(review.job_for(p, src, None, max_pages=1), 30)
    assert len(res["pages"][0]["rows"]) == 1
    service.session_headers_disable(p.name, sid)
    assert review.job_for(p, p.store.get_source(p.name, sid), None, max_pages=1)["http"]["allow_session_headers"] is False


# ---------------------------------------------------------------- O6: an agent's permissive verdict is re-read
TERMS_OK = ("<html><body><h1>Terms of use</h1><p>" + "General conditions of the marketplace. " * 20 +
            "</p><p>Data published on this site may be reused, including by automated means, with attribution.</p></body></html>")
TERMS_FORBID = ("<html><body><h1>Terms</h1><p>" + "General conditions. " * 30 +
                "</p><p>You may not use robots, scrapers or other automated means to access the site.</p></body></html>")


def _terms_source(site):
    p = make_project()
    site.route("/cars", (200, {"content-type": "text/html"}, "<html><body>cars</body></html>"))
    site.route("/terms", (200, {"content-type": "text/html; charset=utf-8"}, TERMS_OK))
    site.route("/terms-forbid", (200, {"content-type": "text/html; charset=utf-8"}, TERMS_FORBID))
    sid = add_source(p, site.url + "/cars")
    p.store.upsert_source(p.name, sid, {"lane": "html", "status": "lane_detected", "robots_status": "allowed", "terms_status": "unknown"})
    return p, sid


def test_agent_allowed_verdict_needs_the_quoted_clause_on_the_cited_page(site):
    p, sid = _terms_source(site)
    agent = "agent:audit"
    with pytest.raises(ValueError, match="quote"):  # allowed without a quote
        service.record_policy(p.name, sid, terms_status="allowed", terms_url=site.url + "/terms", actor=agent)
    with pytest.raises(ValueError, match="does not appear"):  # a clause the page does not contain
        service.record_policy(p.name, sid, terms_status="allowed", terms_url=site.url + "/terms",
                              terms_clause="scraping is explicitly welcome", actor=agent)
    with pytest.raises(ValueError, match="prohibition"):  # the page itself forbids, whatever the agent quotes
        service.record_policy(p.name, sid, terms_status="no_clause", terms_url=site.url + "/terms-forbid", actor=agent)
    with pytest.raises(ValueError, match="could not be fetched"):
        service.record_policy(p.name, sid, terms_status="no_clause", terms_url=site.url + "/missing", actor=agent)
    assert p.store.get_source(p.name, sid)["terms_status"] == "unknown"
    service.record_policy(p.name, sid, terms_status="allowed", terms_url=site.url + "/terms",
                          terms_clause="may be reused,  including by AUTOMATED means", actor=agent)
    src = p.store.get_source(p.name, sid)
    assert src["terms_status"] == "allowed" and src["lane_detail"]["policy"]["verified"]["ok"] is True


def test_operator_verdicts_are_not_refetched(site):
    p, sid = _terms_source(site)
    before = len(site.hits)
    service.record_policy(p.name, sid, terms_status="no_clause", terms_url=site.url + "/elsewhere", reason="read by the operator")
    assert len(site.hits) == before and p.store.get_source(p.name, sid)["terms_status"] == "no_clause"


# ---------------------------------------------------------------- O7: bounded grep, streamed exports
def test_probe_grep_is_bounded(site):
    import time

    from harvest_ai.grepsafe import GREP_MAX_LEN, grep_spans
    spans, total, capped = grep_spans("data-id=\"(\\d+)\"", '<a data-id="1"></a>' * 20)
    assert total == 20 and len(spans) == 8 and not capped
    with pytest.raises(ValueError, match="longer than"):
        grep_spans("a" * (GREP_MAX_LEN + 1), "x")
    with pytest.raises(ValueError, match="bad grep"):
        grep_spans("(", "x")
    t0 = time.monotonic()
    with pytest.raises(ValueError, match="took longer"):
        grep_spans("(a+)+$", "a" * 40 + "!", timeout_s=1.5)  # catastrophic backtracking
    assert time.monotonic() - t0 < 10
    site.route("/p", (200, {"content-type": "text/html"}, "<html><body>" + "<i data-x='1'></i>" * 3 + "</body></html>"))
    out = service.probe_url(site.url + "/p", grep="data-x")
    assert out["grep"]["matches"] == 3


@pytest.mark.parametrize("fmt", ["csv", "jsonl", "parquet"])
def test_exports_stream(fmt, monkeypatch, tmp_path):
    import tracemalloc

    from harvest_ai import export as export_mod
    if fmt == "parquet":
        pytest.importorskip("pyarrow")
    p = make_project()
    n = 30000

    def rows(_p, **kw):
        for i in range(n):
            yield {"source_id": str(i), "url": f"https://x.example/{i}", "make": "Toyota", "model": "Corolla " * 8, "price": 1000.0 + i,
                   "year": 2000 + i % 20, "description": f"{i:08d}" + "d" * 600, "source": "s", "first_seen": "t", "last_seen": "t", "fingerprint": None}
    monkeypatch.setattr(export_mod, "_iter_all", rows)
    tracemalloc.start()
    res = export_mod.export(p, fmt, str(tmp_path / f"out.{fmt}"))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert res["rows"] == n
    assert peak < 20 * 1024 * 1024, peak  # 30,000 rows with unique text are well over 20 MB together; a stream holds one batch
    if fmt == "parquet":
        import pyarrow.parquet as pq
        t = pq.read_table(tmp_path / "out.parquet")
        assert t.num_rows == n and str(t.schema.field("price").type) == "double" and str(t.schema.field("year").type) == "int64"


# ---------------------------------------------------------------- O2: one walk per source, across workers
def test_source_lock_primitive():
    p = make_project()
    st = p.store
    a = st.acquire_source_lock(p.name, "s1", "worker-a", 0.6, "run")
    assert a is not None and a.valid()
    assert st.acquire_source_lock(p.name, "s1", "worker-b", 0.6, "run") is None  # busy
    assert st.acquire_source_lock(p.name, "s2", "worker-b", 0.6, "run") is not None  # other sources are independent
    time.sleep(0.9)  # worker A stalls: no renewal past its lease
    b = st.acquire_source_lock(p.name, "s1", "worker-b", 5, "run")
    if st.pg:  # the advisory lock lives with A's session: a stalled but alive holder keeps the source
        assert b is None and a.valid()
        a.release()
        b = st.acquire_source_lock(p.name, "s1", "worker-b", 5, "run")
        assert b is not None and b.valid()
    else:  # the lease expired: B takes the source with a higher fence, and A learns it lost it
        assert b is not None and b.fence == a.fence + 1 and b.valid()
        assert not a.valid() and not a.renew() and a.lost
        a.release()  # a stale release does not free B's hold
        assert b.valid()
    b.release()
    assert st.acquire_source_lock(p.name, "s1", "worker-c", 5, "run") is not None


def _slow_cars(delay: float):
    s = cars_site(pages=4)
    listing = s.routes["/cars"]
    s.route("/cars", lambda req: (time.sleep(delay), listing(req))[1])
    return start(s)


def test_two_workers_and_a_stalled_job_lease_walk_a_source_once(monkeypatch):
    """Worker A claims a run job and walks slowly; its job lease expires (its heartbeat stalled), the job is
    requeued and worker B claims it. B must not walk the source while A still does. The runner's file lock
    is disabled to model two hosts."""
    from contextlib import contextmanager

    from harvest_ai import jobs, runner
    site = _slow_cars(0.5)
    try:
        p = make_project()
        sid = ready_source(p, site.url + "/cars")

        @contextmanager
        def no_file_lock(_p):
            yield True
        monkeypatch.setattr(runner, "project_lock", no_file_lock)
        jid = p.store.add_job(p.name, "run", {"source_ids": [sid]})
        job_a = p.store.claim_job(p.name, "worker-a", 0.3)
        out_a = {}
        ta = threading.Thread(target=lambda: out_a.update(jobs.execute(p, job_a, threading.Event())[1]))  # no job heartbeat: it stalls
        ta.start()
        time.sleep(1.0)
        assert p.store.reap_jobs(p.name)["requeued"] == 1
        res_b = jobs.run_claimed(p, p.store.claim_job(p.name, "worker-b", 30), "worker-b")
        ta.join(60)
        ran_b = res_b["result"]["ran"][0]
        assert ran_b["status"] == "busy" and "busy" in ran_b["reason"]
        assert out_a["ran"][0]["status"] in ("ok", "partial") and out_a["ran"][0]["rows_stored"] > 0
        walks = [r for r in p.store.list_runs(p.name, sid) if r["trigger_kind"] == "job"]
        assert len(walks) == 1  # exactly one walk of the source happened
        # the review's page 1, then exactly one walk's pages: B never fetched anything
        assert sum(1 for x in site.paths() if x.startswith("/cars")) == 1 + out_a["ran"][0]["pages"]
        assert p.store.get_job(jid)["status"] == "done"
    finally:
        site.server.shutdown()


def test_a_walk_that_loses_its_lock_stores_nothing_more_and_never_replaces(monkeypatch):
    from harvest_ai import locks, runner
    site = _slow_cars(0.7)
    try:
        p = make_project()
        sid = ready_source(p, site.url + "/cars")
        if p.store.pg:
            pytest.skip("on Postgres the advisory lock cannot be taken from a live holder (see test_source_lock_primitive)")
        monkeypatch.setattr(locks, "LEASE_S", 0.8)
        real_held = locks.held
        monkeypatch.setattr(locks, "held", lambda *a, **k: real_held(*a, **{**k, "renew": False}))  # A stalls: never renews
        out = {}
        t = threading.Thread(target=lambda: out.update(runner.run_source(p, sid)))
        t.start()
        time.sleep(1.6)
        thief = p.store.acquire_source_lock(p.name, sid, "worker-b", 60, "run")
        assert thief is not None
        t.join(60)
        assert out["status"] == "superseded" and out["write_mode"] == "none" and out["pruned"] == 0
        assert out["rows_stored"] < 40  # it stopped storing once the lock was gone
        run = p.store.list_runs(p.name, sid, limit=1)[0]
        assert run["status"] == "superseded" and run["stopped"] == "lock_lost"
        thief.release()
    finally:
        site.server.shutdown()


def test_review_and_detect_refuse_a_busy_source(cars):
    from harvest_ai import locks
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    hold = p.store.acquire_source_lock(p.name, sid, "someone-else", 60, "run")
    with pytest.raises(locks.SourceBusy):
        review.review(p, sid, timeout_s=30)
    with pytest.raises(locks.SourceBusy):
        service.detect_lane(p.name, sid)
    assert runner_status(p, sid) == "busy"
    hold.release()
    assert review.review(p, sid, timeout_s=30)["verdict"] == "pass"


def runner_status(p, sid):
    from harvest_ai import runner
    return runner.run_source(p, sid)["status"]


# ---------------------------------------------------------------- O1: the module process
def test_child_job_carries_no_route_and_no_state_paths(cars, home):
    import json
    p = make_project()
    sid = add_source(p, cars.url + "/cars")
    p.module_path(sid).write_text(CAR_MODULE_TEXT, encoding="utf-8")
    job = {**review.job_for(p, p.store.get_source(p.name, sid), None, max_pages=1),
           "proxy": {"exits": [{"server": "http://203.0.113.9:8080", "username": "puser-xyz", "password": "ppass-xyz"}]}}
    cj = sandbox.child_job(job, "/tmp/hsec-w", p.module_path(sid).read_bytes())
    text = json.dumps(cj)
    assert "proxy" not in cj and "module_path" not in cj and "ppass-xyz" not in text and "puser-xyz" not in text
    assert str(p.module_path(sid)) not in text


def test_the_child_has_no_network_of_its_own(cars, monkeypatch):
    """A module that opens its own connection (sandbox.run does not lint; this pins the runtime layer) is refused,
    and the site never sees it; the same module's http.get_text goes through the parent."""
    monkeypatch.setenv("HARVEST_SANDBOX", "policy")
    p = make_project()
    sid = add_source(p, cars.url + "/cars")
    port = int(cars.url.rsplit(":", 1)[1])
    code = ("import socket\ndef fetch(page, *, http, ctx):\n    ok = http.get_text(ctx['source']['url']) is not None\n"
            f"    socket.create_connection(('127.0.0.1', {port}), timeout=3).sendall(b'GET /direct HTTP/1.0\\r\\n\\r\\n')\n"
            "    return [{'source_id': '1', 'url': 'http://x/1', 'make': str(ok), 'price': '1'}]\n")
    p.module_path(sid).write_text(code, encoding="utf-8")
    res = sandbox.run(review.job_for(p, p.store.get_source(p.name, sid), None, max_pages=1), 30)
    assert "PermissionError" in (res["pages"][0]["error"] or "")
    assert "/direct" not in cars.paths() and "/cars" in cars.paths()
    assert res["end"]["http"]["requests"] >= 1  # counted by the parent's client


def test_isolation_selection_and_the_loud_fallback(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_BWRAP_OK", {})
    monkeypatch.setattr(sandbox, "_WARNED", [])
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)  # no bubblewrap on this host
    monkeypatch.setenv("HARVEST_SANDBOX", "auto")
    assert sandbox.isolation() == "policy" and "WARNING: bubblewrap is not available" in capsys.readouterr().err
    monkeypatch.setenv("HARVEST_SANDBOX", "bwrap")
    with pytest.raises(RuntimeError, match="bubblewrap"):
        sandbox.isolation()
    monkeypatch.setenv("HARVEST_SANDBOX", "policy")
    assert sandbox.isolation() == "policy" and capsys.readouterr().err == ""
    monkeypatch.setenv("HARVEST_SANDBOX", "chroot")
    with pytest.raises(RuntimeError):
        sandbox.isolation()


def test_review_reports_its_isolation(cars, monkeypatch):
    monkeypatch.setenv("HARVEST_SANDBOX", "policy")
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    rep = p.store.latest_review(p.name, sid)["report"]
    assert rep["smoke"]["isolation"] == "policy"


needs_bwrap = pytest.mark.skipif(not getattr(sandbox, "bwrap_usable", lambda: False)(), reason="bubblewrap not usable here (Linux with user namespaces needed)")


@needs_bwrap
def test_bwrap_layer_hides_state_blocks_writes_and_network(cars, home, tmp_path):
    """The OS layer on its own (plain shell commands under the same bwrap argv; no Python audit hook involved)."""
    import subprocess
    home.mkdir(parents=True, exist_ok=True)
    (home / "admin_token").write_text("tok-hsec", encoding="utf-8")
    work = tmp_path / "w"
    work.mkdir()
    port = cars.url.rsplit(":", 1)[1]
    argv = sandbox.bwrap_argv(str(work), sandbox.secret_paths())
    sh = (f"cat {home}/admin_token; echo rc_read=$?; echo x > /etc/hsecreview 2>/dev/null; echo rc_etc=$?; echo ok > {work}/f; echo rc_work=$?; "
          f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=3)\" 2>/dev/null; echo rc_net=$?; "
          "ls /proc | grep -c '^[0-9]'")
    out = subprocess.run([*argv, "--", "sh", "-c", sh], capture_output=True, text=True, timeout=60).stdout
    assert "tok-hsec" not in out and "rc_read=1" in out
    assert "rc_etc=0" not in out and "rc_work=0" in out and (work / "f").read_text() == "ok\n"
    assert "rc_net=0" not in out and "/cars" not in "".join(cars.paths())
    assert int(out.strip().splitlines()[-1]) < 10  # a fresh pid namespace: none of the host's processes


@needs_bwrap
def test_review_passes_under_bwrap(cars, monkeypatch):
    monkeypatch.setenv("HARVEST_SANDBOX", "bwrap")
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    rep = p.store.latest_review(p.name, sid)["report"]
    assert rep["smoke"]["isolation"] == "bwrap" and rep["verdict"] == "pass"


# ---------------------------------------------------------------- O3: the browser's egress proxy
def _raw(proxy_url: str, payload: bytes) -> bytes:
    import socket as _s
    host, port = proxy_url.rsplit("/", 1)[1].split(":")
    with _s.create_connection((host, int(port)), timeout=10) as c:
        c.sendall(payload)
        out = b""
        while True:
            d = c.recv(65536)
            if not d:
                return out
            out += d
            if payload.startswith(b"CONNECT") and b"\r\n\r\n" in out:
                return out


def test_egress_proxy_refuses_non_public_destinations(site):
    from harvest_ai.egress import FilteringProxy
    site.route("/x", (200, {"content-type": "text/plain"}, "hello"))
    port = int(site.url.rsplit(":", 1)[1])
    with FilteringProxy() as fp:
        assert b" 403 " in _raw(fp.url, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        assert b" 403 " in _raw(fp.url, f"GET http://localhost:{port}/x HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        assert b" 403 " in _raw(fp.url, b"GET ftp://example.com/ HTTP/1.1\r\n\r\n")
        assert len(fp.refused) == 3 and "/x" not in site.paths()


def test_egress_proxy_forwards_to_the_address_it_vetted(site):
    from harvest_ai.egress import FilteringProxy
    site.route("/x", (200, {"content-type": "text/plain"}, "hello"))
    port = int(site.url.rsplit(":", 1)[1])
    with FilteringProxy(vet=lambda ip: True) as fp:  # a test vet that accepts loopback, standing in for a public address
        out = _raw(fp.url, f"GET http://127.0.0.1:{port}/x HTTP/1.1\r\nHost: 127.0.0.1\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
        assert b"200" in out.split(b"\r\n", 1)[0] and out.endswith(b"hello")
        assert b" 200 " in _raw(fp.url, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        assert fp.connections[0] == ("127.0.0.1", "127.0.0.1")


# ---------------------------------------------------------------- CSV formula injection (found by the UI e2e run)
HOSTILE = ["=HYPERLINK(\"http://x.example\",\"click\")", "+1+1", "-2+3", "@SUM(A1)", "\tlead-tab", "\rlead-cr"]


def _hostile_rows(_p, **kw):
    for i, v in enumerate(HOSTILE):
        yield {"source_id": str(i), "url": f"https://x.example/{i}", "make": v, "model": "plain", "price": -500.0,
               "source": "s", "first_seen": "t", "last_seen": "t", "fingerprint": None}


def test_csv_export_neutralises_formula_cells(monkeypatch, tmp_path):
    import csv
    import json

    from harvest_ai import export as export_mod
    p = make_project()
    monkeypatch.setattr(export_mod, "_iter_all", _hostile_rows)
    export_mod.export(p, "csv", str(tmp_path / "safe.csv"))
    rows = list(csv.DictReader(open(tmp_path / "safe.csv", encoding="utf-8", newline="")))
    assert [r["make"] for r in rows] == ["'" + v for v in HOSTILE]
    assert all(r["model"] == "plain" and r["price"] == "-500.0" for r in rows)  # numbers and plain text unchanged
    import io
    assert [r["make"] for r in csv.DictReader(io.StringIO(export_mod.to_csv_text(p), newline=""))] == ["'" + v for v in HOSTILE]
    # the explicit opt-out writes values unchanged
    export_mod.export(p, "csv", str(tmp_path / "raw.csv"), raw=True)
    assert [r["make"] for r in csv.DictReader(open(tmp_path / "raw.csv", encoding="utf-8", newline=""))] == HOSTILE
    # JSONL is data, not a spreadsheet: unchanged
    export_mod.export(p, "jsonl", str(tmp_path / "x.jsonl"))
    assert [json.loads(x)["make"] for x in open(tmp_path / "x.jsonl", encoding="utf-8")] == HOSTILE


def test_raw_csv_is_an_explicit_choice_on_every_surface(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from harvest_ai import export as export_mod
    from harvest_ai.web.app import create_app
    p = make_project()
    monkeypatch.setattr(export_mod, "_iter_all", _hostile_rows)
    web = TestClient(create_app())
    auth = {"Authorization": "Bearer test-token"}
    assert "'=HYPERLINK" in web.get(f"/api/projects/{p.name}/export?format=csv", headers=auth).text
    raw = web.get(f"/api/projects/{p.name}/export?format=csv&raw=true", headers=auth).text
    assert "'=HYPERLINK" not in raw and "=HYPERLINK" in raw
    assert service.export(p.name, "csv", str(tmp_path / "a.csv"), raw=True)["raw"] is True
    from harvest_ai import cli
    assert cli.main(["export", p.name, "--format", "csv", "--out", str(tmp_path / "b.csv"), "--raw"]) in (0, None)
    assert "=HYPERLINK" in (tmp_path / "b.csv").read_text() and "'=HYPERLINK" not in (tmp_path / "b.csv").read_text()
