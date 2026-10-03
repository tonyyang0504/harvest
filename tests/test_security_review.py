"""Regression tests from the 2026-10 security review (docs/SECURITY_REVIEW_2026-10.md). Each test pins one
control: it fails on the code before the fix and passes after it."""

import pytest
from conftest import add_source, make_project
from localsite import CAR_MODULE

from harvest_ai import review, sandbox


# ---------------------------------------------------------------- S1: the AST gate
@pytest.mark.parametrize("code,needle", [
    ("from harvest_ai.http import trusted_section\n", "harvest_ai.http"),          # the audit-hook switch
    ("from harvest_ai import http\n", "harvest_ai.http"),
    ("import harvest_ai.proxy\n", "harvest_ai.proxy"),
    ("import posix\n", "posix"),                                            # os under another name
    ("import os as o\n", "another name"),
    ("import _posixsubprocess\n", "_posixsubprocess"),                      # private C modules
    ("import operator\n", "operator"),                                      # attrgetter('...') by string
    ("import pathlib\n", "pathlib"),                                        # file reads outside open()
    ("import io\n", "io"),
    ("from . import x\n", "relative import"),
    ("x = '{0.y}'.format(1)\n", "format"),                                  # attribute access inside a format string
    ("x = s.format(1)\n", "format"),
])
def test_lint_closes_review_gaps(code, needle):
    found = review.static_findings(code)
    assert any(needle in f for f in found), found


def test_lint_refuses_the_old_import_name():
    # harvest-ai's package is harvest_ai; `harvest` on PyPI is an unrelated project, so a module may not import it
    for code in ("from harvest import extract\n", "import harvest.extract\n", "import harvest\n"):
        assert any("harvest" in f for f in review.static_findings(code)), code


def test_lint_still_allows_the_contract_helpers():
    ok = ("from harvest_ai import extract\nfrom harvest_ai import mcp_client\nimport json, re, datetime\n"
          "x = '{} {name}'.format(1, name=2)\n")
    assert review.static_findings(ok) == []
    assert review.static_findings(CAR_MODULE) == []


# ---------------------------------------------------------------- S2: the trusted section is not callable by modules
def test_module_cannot_open_a_trusted_section(cars):
    # the lint refuses the import; the runtime refuses the call even when the lint is skipped (expected_sha None)
    code = ("from harvest_ai.http import trusted_section\n"
            "def fetch(page, *, http, ctx):\n    with trusted_section():\n        pass\n    return []\n")
    p = make_project()
    sid = add_source(p, cars.url + "/cars")
    p.module_path(sid).write_text(code, encoding="utf-8")
    res = sandbox.run(review.job_for(p, p.store.get_source(p.name, sid), None, max_pages=1), 20)
    assert "PermissionError" in res["pages"][0]["error"]


# ---------------------------------------------------------------- S3: secrets are unreadable from the sandbox
def test_sandbox_refuses_reads_of_harvest_state(cars, home):
    p = make_project()
    sid = add_source(p, cars.url + "/cars")
    secret = home / "admin_token"
    secret.write_text("s3cr3t-token", encoding="utf-8")
    # builtins open is reached past the lint on purpose: this pins the runtime layer
    code = ("def fetch(page, *, http, ctx):\n    o = __builtins__['open'] if isinstance(__builtins__, dict) else __builtins__.open\n"
            f"    return [{{'source_id': '1', 'url': 'http://x/1', 'make': o({str(secret)!r}).read(), 'price': '1000000'}}]\n")
    p.module_path(sid).write_text(code, encoding="utf-8")
    res = sandbox.run(review.job_for(p, p.store.get_source(p.name, sid), None, max_pages=1), 20)
    page = res["pages"][0]
    assert "PermissionError" in (page["error"] or "") and "reading" in page["error"]
    assert "s3cr3t-token" not in str(res)


def test_read_policy_unit(tmp_path):
    work, hard, h = "/w/sbx", "/home/op/.harvest", "/home/op"
    deny = {"hard": [hard], "home": h}
    allow = ["/home/op/proj/.venv/lib/python3.12/site-packages"]
    assert not sandbox.read_denied("/w/sbx/a", work, deny, allow)
    assert sandbox.read_denied("/home/op/.harvest/admin_token", work, deny, allow)
    assert sandbox.read_denied("/home/op/.ssh/id_ed25519", work, deny, allow)
    assert not sandbox.read_denied("/home/op/proj/.venv/lib/python3.12/site-packages/httpx/__init__.py", work, deny, allow)
    assert sandbox.read_denied("/proc/1/environ", work, deny, allow)
    assert not sandbox.read_denied("/usr/lib/python3.12/json/__init__.py", work, deny, allow)
    # a hard root inside an import root is still refused
    assert sandbox.read_denied("/home/op/.harvest/x", work, deny, ["/home/op"])


# ---------------------------------------------------------------- S4: the verdict describes the hashed bytes
def test_review_lints_the_bytes_it_hashed(cars, monkeypatch):
    p = make_project()
    sid = add_source(p, cars.url + "/cars")
    p.store.upsert_source(p.name, sid, {"lane": "html"})
    bad = "import subprocess\n" + CAR_MODULE
    p.module_path(sid).write_text(bad, encoding="utf-8")
    real_lint = review.lint

    def swap_then_lint(path, *a, **kw):  # the file changes between the hash and the lint
        path.write_text(CAR_MODULE, encoding="utf-8")
        return real_lint(path, *a, **kw)
    monkeypatch.setattr(review, "lint", swap_then_lint)
    rep = review.review(p, sid, timeout_s=30)
    assert rep["module_sha"] == sandbox.sha256_bytes(bad.encode())
    assert rep["verdict"] == "fail" and not rep["lint"]["ok"]
    assert not p.store.get_source(p.name, sid).get("reviewed_sha")


# ---------------------------------------------------------------- S5: agents and the MCP server
def test_file_agents_read_only_the_sources_dir():
    from harvest_ai import agents
    p = make_project()
    for kind in ("build", "repair"):
        tools = agents.allowed_tools(kind, p, "s1")
        assert "Read" not in tools
        reads = [t for t in tools if t.startswith("Read(")]
        assert reads == [agents.read_rule(p.sources_dir)] and reads[0].startswith("Read(//") and reads[0].endswith("/sources/**)")


def test_agent_env_drops_operator_secrets(tmp_path, monkeypatch):
    from harvest_ai import agents
    p = make_project()
    seen = tmp_path / "env.txt"
    script = tmp_path / "cli.sh"
    script.write_text(f"#!/bin/sh\nenv > {seen}\npwd >> {seen}\necho '{{\"type\": \"result\", \"result\": \"ok\"}}'\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("HARVEST_AGENT_BIN", str(script))
    monkeypatch.setenv("HARVEST_ADMIN_TOKEN", "tok-should-not-leak")
    monkeypatch.setenv("HARVEST_ALERT_WEBHOOK", "https://hooks.example/secret")
    status, _ = agents.run_agent("build", p, "s1", log_path=tmp_path / "log")
    out = seen.read_text()
    assert status == "done" and "tok-should-not-leak" not in out and "hooks.example" not in out
    assert "HARVEST_AGENT_KIND=build" in out and out.strip().endswith("/sources")
    cfg = __import__("json").loads((p.agents_dir / "mcp-build.json").read_text())
    assert cfg["mcpServers"]["harvest"]["env"]["HARVEST_AGENT_KIND"] == "build"


@pytest.mark.asyncio
async def test_mcp_server_enforces_the_agent_allowlist(monkeypatch):
    from harvest_ai import mcp_server
    p = make_project()
    monkeypatch.setenv("HARVEST_AGENT_KIND", "build")
    for coro in (mcp_server.harvest_enable_source(p.name, "x"), mcp_server.harvest_proxy_enable(p.name, "a long enough reason"),
                 mcp_server.harvest_ua_enable(p.name, "a long enough reason"), mcp_server.harvest_run(p.name),
                 mcp_server.harvest_export(p.name, "csv", "/tmp/hsecreview-x.csv")):
        res = await coro
        assert res.is_error and res.structured_content["error"] == "forbidden"
    ok = await mcp_server.harvest_list_sources(p.name)
    assert not ok.is_error
    monkeypatch.delenv("HARVEST_AGENT_KIND")
    assert not (await mcp_server.harvest_ua_status(p.name)).is_error


# ---------------------------------------------------------------- S6: agents cannot loosen policy
def _policy_source(p, **fields):
    from harvest_ai import census
    sid = census.add(p, [{"url": "https://shop.example.kz/cars", "regions": ["KZ"],
                          "evidence": [{"url": "https://shop.example.kz/cars", "observation": "20 ads"}]}])["added"][0]["id"]
    p.store.upsert_source(p.name, sid, fields)
    return sid


def test_agent_cannot_lift_forbids_or_open_a_none_lane():
    from harvest_ai import service
    p = make_project()
    sid = _policy_source(p, lane="none", status="lane_none", robots_status="allowed", terms_status="forbids",
                         lane_detail={"reason": "terms forbid automated collection"})
    agent = "agent:build"
    with pytest.raises(PermissionError):
        service.record_policy(p.name, sid, terms_status="allowed", terms_url="https://shop.example.kz/terms", lane="html", actor=agent)
    p.store.upsert_source(p.name, sid, {"terms_status": "unknown", "lane_detail": {"reason": "content is behind a login"}})
    with pytest.raises(PermissionError):
        service.record_policy(p.name, sid, lane="html", reason="looks public", actor=agent)
    src = p.store.get_source(p.name, sid)
    assert src["lane"] == "none" and src["terms_status"] == "unknown"
    # an agent can still make things stricter
    service.record_policy(p.name, sid, terms_status="forbids", terms_url="https://shop.example.kz/terms", actor=agent)
    assert p.store.get_source(p.name, sid)["terms_status"] == "forbids"
    # the operator keeps the override (heuristic false positives happen), and it is attributed
    service.record_policy(p.name, sid, terms_status="no_clause", terms_url="https://shop.example.kz/terms", lane="html", reason="read it")
    src = p.store.get_source(p.name, sid)
    assert src["lane"] == "html" and src["lane_detail"]["policy"]["by"] == "operator"


def test_agent_terms_verdict_must_cite_the_sources_own_site():
    from harvest_ai import service
    p = make_project()
    sid = _policy_source(p, lane="html", status="lane_detected", robots_status="allowed", terms_status="unknown")
    with pytest.raises(PermissionError):
        service.record_policy(p.name, sid, terms_status="allowed", terms_url="https://elsewhere.example.com/terms", actor="agent:audit")
    # on-site, but the page cannot be fetched (O6: the cited page is re-read), so the verdict stays unknown
    with pytest.raises(ValueError):
        service.record_policy(p.name, sid, terms_status="no_clause", terms_url="https://www.shop.example.kz/legal", actor="agent:audit")
    assert p.store.get_source(p.name, sid)["terms_status"] == "unknown"


@pytest.mark.asyncio
async def test_mcp_record_policy_runs_as_the_agent(monkeypatch):
    from harvest_ai import mcp_server
    p = make_project()
    sid = _policy_source(p, lane="none", status="lane_none", robots_status="allowed", terms_status="forbids")
    monkeypatch.setenv("HARVEST_AGENT_KIND", "audit")
    res = await mcp_server.harvest_record_policy(p.name, sid, "allowed", "https://shop.example.kz/terms", None, "html", "x")
    assert res.is_error and p.store.get_source(p.name, sid)["terms_status"] == "forbids"


# ---------------------------------------------------------------- S7: SSRF
@pytest.mark.parametrize("addr,public", [
    ("93.184.215.14", True), ("2606:2800:21f:cb07:6820:80da:af6b:8b2c", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("169.254.169.254", False), ("100.100.100.200", False),  # shared space / metadata
    ("::ffff:127.0.0.1", False), ("64:ff9b::a9fe:a9fe", False), ("2002:7f00:1::", False), ("fd00::1", False), ("0.0.0.0", False),
])
def test_public_ip(addr, public):
    import ipaddress

    from harvest_ai.http import public_ip
    assert public_ip(ipaddress.ip_address(addr)) is public


def test_gate_refuses_shared_address_space(monkeypatch):
    import socket as _socket

    from harvest_ai.http import Blocked, Http
    monkeypatch.setattr(_socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("100.100.100.200", 80))])
    with pytest.raises(Blocked):
        Http(allow_private=False).gate("http://metadata.example/latest")


def test_connection_is_pinned_against_dns_rebinding(cars, monkeypatch):
    """The gate's lookup says public, the connection's lookup says loopback: the request must not reach the server."""
    import ipaddress
    import socket as _socket

    from harvest_ai.http import Http
    real = _socket.getaddrinfo
    port = int(cars.url.rsplit(":", 1)[1].split("/")[0])
    calls = {"n": 0}

    def flip(host, *a, **k):
        try:
            ipaddress.ip_address(host)
            return real(host, *a, **k)  # the socket layer connecting to an already-vetted literal
        except ValueError:
            pass
        calls["n"] += 1
        ip = "93.184.215.14" if calls["n"] <= 2 else "127.0.0.1"  # the two gate checks see public; the connection sees loopback
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(_socket, "getaddrinfo", flip)
    http = Http(allow_private=False, respect_robots=False, retries=0, timeout=3)
    try:
        assert http.get_text(f"http://rebind.example:{port}/cars") is None
        assert any(e["event"] == "gated" and "connect time" in e.get("reason", "") for e in http.events), http.events
    finally:
        http.close()


# ---------------------------------------------------------------- S8: web app
@pytest.fixture
def web():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from harvest_ai.web.app import create_app
    return TestClient(create_app())


def test_every_api_route_requires_the_token(web):
    import re
    open_paths = {"/api/health"}
    checked = 0
    for path, ops in web.get("/openapi.json").json()["paths"].items():
        if not path.startswith("/api") or path in open_paths:
            continue
        for m in ops:
            if m.upper() not in ("GET", "POST", "PATCH", "PUT", "DELETE"):
                continue
            assert web.request(m.upper(), re.sub(r"\{[^}]+\}", "x", path)).status_code == 401, (m, path)
            checked += 1
    assert checked > 30


def test_frontend_has_a_csp_and_safe_links(web):
    page = web.get("/")
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert page.headers["x-content-type-options"] == "nosniff"
    js = web.get("/static/app.js").text
    assert 'href="${esc(x.url)}"' not in js and 'href="${esc(r[c])}"' not in js and "safeUrl" in js


def test_store_dsn_password_never_leaves_project_json(web):
    from harvest_ai import project as project_mod
    assert project_mod.redact_dsn("postgresql://h:pw0rd@db:5432/x") == "postgresql://h:***@db:5432/x"
    assert "pw0rd" not in project_mod.redact_dsn("host=db password=pw0rd dbname=x")
    p = make_project()
    p.spec.store_dsn = "postgresql://h:pw0rd@127.0.0.1:1/x"  # not connected: to_dict only formats the spec
    assert "pw0rd" not in str(p.to_dict())


def test_job_ids_are_scoped_to_their_project(web):
    from harvest_ai import service
    a, b = make_project("proj-a"), make_project("proj-b")
    jid = a.store.add_job(a.name, "watchdog", {})
    auth = {"Authorization": "Bearer test-token"}
    assert web.get(f"/api/projects/{a.name}/jobs/{jid}", headers=auth).status_code == 200
    assert web.get(f"/api/projects/{b.name}/jobs/{jid}", headers=auth).status_code == 404
    with pytest.raises(LookupError):
        service.jobs(b.name, jid)


def test_list_limits_are_clamped():
    from harvest_ai import service
    p = make_project()
    seen = {}
    real = p.store.list_runs
    p.store.list_runs = lambda project, sid=None, limit=50: seen.setdefault("n", limit) and real(project, sid, limit)
    service.runs(p.name, limit=10**9)
    assert seen["n"] == service.MAX_LIST


# ---------------------------------------------------------------- S9: data integrity
@pytest.mark.parametrize("rates", [{"KZT": 0}, {"KZT": -480}, {"KZT": float("nan")}, {"KZT": float("inf")}, {"K'T": 480}, {"KZT": True}])
def test_fx_rates_are_validated(rates):
    p = make_project()
    with pytest.raises(ValueError):
        p.set_fx(rates)
    assert p.set_fx({"KZT": 480})["rates"]["KZT"] == 480


def test_replace_spares_rows_an_overlapping_walk_refreshed():
    from harvest_ai.db import now_iso
    p = make_project()
    st = p.store
    sid = "s1"
    st.upsert_source(p.name, sid, {"name": "s", "url": "https://s.example/", "domain": "s.example", "status": "enabled"})
    old = st.start_run(p.name, sid, "manual", None)
    st.upsert_records(p.name, sid, old, [(f"u{i}", {"source_id": str(i), "url": f"https://s.example/{i}"}, None) for i in range(10)])
    st.upsert_source(p.name, sid, {"baseline": 10})
    mine = st.start_run(p.name, sid, "manual", None)
    st.execute("UPDATE runs SET started_at = ? WHERE id = ?", (now_iso(-5), mine))
    other = st.start_run(p.name, sid, "job", None)  # a second worker walking the same source meanwhile
    st.upsert_records(p.name, sid, other, [("u9", {"source_id": "9", "url": "https://s.example/9"}, None)])
    st.execute("UPDATE records SET last_seen = ? WHERE run_id = ?", (now_iso(-60), old))
    st.upsert_records(p.name, sid, mine, [(f"u{i}", {"source_id": str(i), "url": f"https://s.example/{i}"}, None) for i in range(8)])
    fin = st.finalize_run(p.name, sid, mine, complete=True, stored=8, prune_after_days=14)
    assert fin["write_mode"] == "replace" and fin["pruned"] == 1  # u8 only; u9 was refreshed by the overlapping walk
    assert st.count_records(p.name, sid) == 9
