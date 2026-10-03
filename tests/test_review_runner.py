import datetime as dt

import pytest
from conftest import add_source, make_project, ready_source
from localsite import CAR_MODULE, cars_site, start

from harvest_ai import review, runner, sandbox, scaffold
from harvest_ai.db import REPLACE_MIN_FRACTION, now_iso


@pytest.mark.parametrize("code,needle", [
    ("import subprocess\n", "imports subprocess"),
    ("from subprocess import run\n", "imports from subprocess"),
    ("import os\nos.system('ls')\n", "os.system"),
    ("from os import system\n", "os.system"),
    ("import os\nf = os.popen\n", "os.popen"),
    ("eval('1')\n", "eval()"),
    ("exec('x=1')\n", "exec()"),
    ("__import__('subprocess')\n", "__import__"),
    ("import importlib\n", "importlib"),
    ("import requests\n", "requests"),
    ("import urllib.request\n", "urllib.request"),
    ("from urllib import request\n", "urllib.request"),
    ("import socket\n", "socket"),
    ("x = ().__class__.__bases__[0].__subclasses__()\n", "__subclasses__"),
    ("open('/etc/passwd', 'w')\n", "open()"),
    ("import ctypes\n", "ctypes"),
    ("import sys\n", "sys"),
    ("getattr(object, 'x')\n", "getattr()"),
])
def test_ast_policy_catches_forbidden_code(code, needle):
    found = review.static_findings(code)
    assert any(needle in f for f in found), found


def test_ast_policy_allows_normal_scrapers():
    assert review.static_findings(CAR_MODULE) == []
    assert review.contract_findings(CAR_MODULE) == []
    assert review.contract_findings("def fetch(page):\n    return []\n") == ["fetch() must accept http, ctx"]
    assert "must define" in review.contract_findings("x = 1\n")[0]
    assert review.static_findings("def f(:\n")[0].startswith("syntax error")


def test_review_passes_and_binds_sha(cars):
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    rv = p.store.latest_review(p.name, sid)
    rep = rv["report"]
    assert rv["verdict"] == "pass" and rep["smoke"]["rows"] == 10 and rep["schema"]["valid_fraction"] == 1.0
    assert rep["schema"]["field_coverage"]["make"] == 1.0 and rep["schema"]["sample"][0]["currency"] == "KZT"
    src = p.store.get_source(p.name, sid)
    assert src["reviewed_sha"] == sandbox.sha256_file(p.module_path(sid)) and src["enabled"] == 1
    # any edit invalidates the review: enabling again is refused, the runner will not run it
    p.module_path(sid).write_text(CAR_MODULE + "\n# tweak\n", encoding="utf-8")
    assert "changed after its passing review" in " ".join(review.enable(p, sid)["refused"])
    res = runner.run_source(p, sid)
    assert res["status"] == "review_stale" and p.store.get_source(p.name, sid)["status"] == "review_stale"
    assert p.store.count_records(p.name, sid) == 0


def test_enable_refuses_without_policy(cars):
    p = make_project()
    sid = add_source(p, cars.url + "/cars")
    p.module_path(sid).write_text(CAR_MODULE, encoding="utf-8")
    p.store.upsert_source(p.name, sid, {"lane": "html", "robots_status": "allowed", "terms_status": "unknown"})
    assert review.review(p, sid, timeout_s=60)["verdict"] == "pass"
    refused = review.enable(p, sid)["refused"]
    assert any("terms status is unknown" in r for r in refused)
    p.store.upsert_source(p.name, sid, {"lane": "none"})
    assert any("lane is none" in r for r in review.enable(p, sid)["refused"])


def _review_module(p, url, code):
    sid = add_source(p, url)
    p.store.upsert_source(p.name, sid, {"lane": "html"})
    p.module_path(sid).write_text(code, encoding="utf-8")
    return review.review(p, sid, timeout_s=8)


def test_review_fails_lint(cars):
    rep = _review_module(make_project(), cars.url + "/cars", "import subprocess\ndef fetch(page, *, http, ctx):\n    return []\n")
    assert rep["verdict"] == "fail" and not rep["lint"]["ok"] and rep["smoke"]["errors"] == ["skipped: lint failed"]


def test_review_sandbox_timeout(cars):
    rep = _review_module(make_project(), cars.url + "/cars", "def fetch(page, *, http, ctx):\n    while True:\n        pass\n")
    assert rep["verdict"] == "fail" and "timed out" in rep["smoke"]["errors"][0]


def test_sandbox_audit_hook_blocks_writes_and_spawns(cars):
    # dodges the AST scan via a builtin alias; the runtime guard still refuses
    code = ("def fetch(page, *, http, ctx):\n    o = __builtins__['open'] if isinstance(__builtins__, dict) else __builtins__.open\n"
            "    o('/tmp/harvest-escape.txt', 'w').write('x')\n    return [{'source_id': '1', 'url': 'http://x/1', 'make': 'a', 'price': '1000000'}]\n")
    assert review.static_findings(code)  # the AST gate catches it first ...
    p = make_project()
    sid = add_source(p, cars.url + "/cars")
    p.module_path(sid).write_text(code, encoding="utf-8")
    res = sandbox.run(review.job_for(p, p.store.get_source(p.name, sid), None, max_pages=1), 20)  # ... and the sandbox blocks it at run time
    assert res["pages"][0]["error"].startswith("PermissionError") and "harvest sandbox" in res["pages"][0]["error"]
    code2 = "import os\ndef fetch(page, *, http, ctx):\n    getattr(os, 'sys' + 'tem')('echo hi')\n    return []\n"
    p.module_path(sid).write_text(code2, encoding="utf-8")
    res = sandbox.run(review.job_for(p, p.store.get_source(p.name, sid), None, max_pages=1), 20)
    assert "PermissionError" in res["pages"][0]["error"]


def test_review_schema_failure(cars):
    code = "def fetch(page, *, http, ctx):\n    return [{'source_id': str(i), 'url': 'http://x/%d' % i, 'price': 'call us'} for i in range(5)]\n"
    rep = _review_module(make_project(), cars.url + "/cars", code)
    assert rep["verdict"] == "fail" and rep["smoke"]["ok"] and not rep["schema"]["ok"]
    assert rep["schema"]["quarantine_reasons"]["missing required field make"] == 5


def test_sandbox_sha_mismatch_refused(cars):
    p = make_project()
    sid = add_source(p, cars.url + "/cars")
    p.module_path(sid).write_text(CAR_MODULE, encoding="utf-8")
    res = sandbox.run(review.job_for(p, p.store.get_source(p.name, sid), "0" * 64, max_pages=1), 20)
    assert res["end"]["stopped"] == "sha_mismatch" and not res["pages"]


def test_scaffold_templates_pass_the_static_gate(cars):
    p = make_project()
    sid = add_source(p, cars.url + "/cars")
    for lane in ("embedded_json", "html", "feed", "sitemap", "openapi", "mcp", "browser"):
        p.store.upsert_source(p.name, sid, {"lane": lane})
        out = scaffold.template(p, sid, overwrite=True)
        code = p.module_path(sid).read_text(encoding="utf-8")
        assert out["written"] and "def fetch(page, *, http, ctx)" in code
        assert review.static_findings(code) == [], (lane, review.static_findings(code))
        compile(code, "x", "exec")
    p.store.upsert_source(p.name, sid, {"lane": "none"})
    with pytest.raises(ValueError):
        scaffold.template(p, sid)


# ---------------------------------------------------------------- runner + store
def test_run_walks_pages_and_stores(cars):
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    res = runner.run(p)
    r = res["ran"][0]
    assert r["status"] == "ok" and r["complete"] and r["stopped"] == "empty_page" and r["pages"] == 4 and r["rows_stored"] == 25
    assert r["write_mode"] == "replace" and p.store.count_records(p.name, sid) == 25
    assert [h for h in cars.paths() if h.startswith("/cars")][-4:] == ["/cars", "/cars?page=2", "/cars?page=3", "/cars?page=4"]
    run = p.store.list_runs(p.name, sid)[0]
    assert run["status"] == "ok" and run["http_stats"]["ok"] >= 4 and p.store.get_source(p.name, sid)["baseline"] == 25


def test_partial_walk_never_wipes(cars):
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    runner.run(p)
    assert p.store.count_records(p.name, sid) == 25
    r = runner.run(p, max_pages=1)["ran"][0]  # capped walk: incomplete
    assert r["status"] == "partial" and not r["complete"] and r["write_mode"] == "upsert" and p.store.count_records(p.name, sid) == 25


def test_complete_walk_below_threshold_does_not_replace(cars):
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    runner.run(p)
    small = start(cars_site(pages=1, per_page=5))  # the site now shows 5 of 25 ads
    try:
        p.store.upsert_source(p.name, sid, {"url": small.url + "/cars"})
        r = runner.run(p)["ran"][0]
        assert r["complete"] and r["rows_stored"] == 5 and 5 < REPLACE_MIN_FRACTION * 25
        assert r["write_mode"] == "upsert" and r["status"] == "degraded" and p.store.count_records(p.name, sid) == 25
        assert any(a["kind"] == "degraded" for a in p.store.list_alerts(p.name))
    finally:
        small.server.shutdown()


def test_complete_walk_above_threshold_replaces(cars):
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    runner.run(p)
    smaller = start(cars_site(pages=2, per_page=10))  # 20 of 25 ads remain: >= 60 %
    try:
        p.store.upsert_source(p.name, sid, {"url": smaller.url + "/cars"})
        r = runner.run(p)["ran"][0]
        assert r["complete"] and r["write_mode"] == "replace" and r["pruned"] == 5 and p.store.count_records(p.name, sid) == 20
    finally:
        smaller.server.shutdown()


def test_prune_rows_unseen_for_n_days(cars):
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    runner.run(p)
    old = now_iso(-30 * 86400)
    p.store.execute("UPDATE records SET last_seen = ? WHERE project = ? AND source_id = ? AND source_key IN ('a23', 'a24', 'a25')",
                    (old, p.name, sid))
    r = runner.run(p, max_pages=1)["ran"][0]
    assert r["write_mode"] == "upsert" and r["pruned"] == 3
    assert p.store.count_records(p.name, sid) == 22


def test_fetch_failures_make_a_walk_incomplete(cars):
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    runner.run(p)
    calls = {"n": 0}
    orig = cars.routes["/cars"]

    def flaky(req):
        if req.query.get("page") == "2":
            calls["n"] += 1
            return (503, {"content-type": "text/plain"}, "down")
        return orig(req)
    cars.route("/cars", flaky)
    r = runner.run(p)["ran"][0]
    assert r["stopped"] == "fetch_failed" and not r["complete"] and r["write_mode"] == "upsert" and p.store.count_records(p.name, sid) == 25


def test_quarantine_and_time_budget(cars):
    p = make_project()
    code = CAR_MODULE.replace('"price": card.find("span", cls="price").text,', '"price": card.find("span", cls="price").text if card.get("data-id") != "a3" else "0",')
    sid = ready_source(p, cars.url + "/cars", module=code)
    r = runner.run(p)["ran"][0]
    assert r["rows_quarantined"] == 1 and r["rows_stored"] == 24
    q = p.store.list_quarantine(p.name)
    assert "sanity bound" in q[0]["reasons"][0] and q[0]["raw"]["source_id"] == "a3"
    # the same bad row in the next run is the same quarantine entry (refreshed), not a duplicate
    r2 = runner.run(p)["ran"][0]
    q2 = p.store.list_quarantine(p.name)
    assert r2["rows_quarantined"] == 1 and len(q2) == 1 and q2[0]["run_id"] != q[0]["run_id"]
    # a walk that exceeds its budget is killed and recorded
    slow = CAR_MODULE.replace("    return rows\n", "    while page > 1:\n        pass\n    return rows\n")
    p.module_path(sid).write_text(slow, encoding="utf-8")
    p.store.upsert_source(p.name, sid, {"reviewed_sha": sandbox.sha256_file(p.module_path(sid)), "time_budget_s": 2})
    r = runner.run(p)["ran"][0]
    assert r["stopped"] == "time_budget" and not r["complete"] and r["status"] in ("partial", "degraded")


def test_run_lock_and_due(cars):
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    with runner.project_lock(p) as got:
        assert got
        assert runner.run(p)["busy"]
    first = runner.run(p, due=True)
    assert first["ran"][0]["source_id"] == sid
    assert runner.run(p, due=True)["ran"] == []  # just ran; daily cadence not due
    with pytest.raises(LookupError):
        runner.run(p, ["nope"])


def test_cross_source_duplicates_and_distinct_query(cars):
    from harvest_ai import export
    p = make_project()
    ready_source(p, cars.url + "/cars")
    other = start(cars_site())
    try:
        ready_source(p, other.url + "/cars")
        runner.run(p)
        assert p.store.count_records(p.name) == 50
        assert export.query(p, limit=100)["total"] == 50
        assert export.query(p, limit=100, distinct=True)["total"] == 25
    finally:
        other.server.shutdown()


def test_walk_logic_unit():
    class H:
        stats = {"errors": 0, "blocked": 0, "gated": 0, "robots_denied": 0}
    pages = {1: [{"source_id": "a"}, {"source_id": "b"}], 2: [{"source_id": "b"}], 3: [{"source_id": "b"}], 4: [{"source_id": "c"}]}
    ev = []
    res = sandbox.walk(lambda page, http, ctx: pages.get(page, []), http=H(), ctx={}, max_pages=10, max_rows=100, emit=ev.append)
    assert res["stopped"] == "repeating" and res["complete"] and res["rows"] == 2
    res = sandbox.walk(lambda page, http, ctx: {"rows": [{"source_id": str(page)}], "done": page == 2}, http=H(), ctx={}, max_pages=10, max_rows=100, emit=ev.append)
    assert res["stopped"] == "done" and res["complete"] and res["pages"] == 2

    def boom(page, http, ctx):
        raise RuntimeError("x")
    res = sandbox.walk(boom, http=H(), ctx={}, max_pages=10, max_rows=100, emit=ev.append)
    assert res["stopped"] == "errors" and not res["complete"]
    res = sandbox.walk(lambda page, http, ctx: [{"source_id": f"{page}-{i}"} for i in range(10)], http=H(), ctx={}, max_pages=10, max_rows=25, emit=ev.append)
    assert res["stopped"] == "max_rows" and res["rows"] == 25 and not res["complete"]
    assert dt.datetime.fromisoformat(now_iso())


def test_page1_only_browser_walks_never_replace(cars):
    """Pilot: century21.pt (browser lane, page 1 by policy) returned done -> complete -> replace, which would
    delete every listing that dropped out of the newest 20 on the next run."""
    p = make_project()
    done_module = CAR_MODULE.replace("    return rows\n", "    return {\"rows\": rows, \"done\": True}\n")
    sid = ready_source(p, cars.url + "/cars", module=done_module)
    first = runner.run(p)["ran"][0]
    assert first["complete"] and first["pages"] == 1  # an html source claiming done is trusted
    p.store.execute("UPDATE records SET run_id = 'old' WHERE project = ?", (p.name,))
    p.store.upsert_source(p.name, sid, {"lane": "browser"})
    r = runner.run(p)["ran"][0]
    assert not r["complete"] and r["stopped"] == "page1_only" and r["write_mode"] == "upsert" and r["pruned"] == 0
    assert p.store.count_records(p.name, sid) == 10
