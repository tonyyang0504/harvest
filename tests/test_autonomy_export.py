import csv
import datetime as dt
import json
import os
import stat
import sys

import pytest
from conftest import make_project, ready_source

from harvest_ai import agents, export, runner, scheduler, service, watchdog
from harvest_ai.db import now_iso


def fake_cli(tmp_path, returncode=0):
    """A stand-in agent CLI that records its argv and prints a JSON result."""
    log = tmp_path / "argv.json"
    script = tmp_path / "fake-claude"
    script.write_text(f"""#!{sys.executable}
import json, sys
open({str(log)!r}, "w").write(json.dumps(sys.argv[1:]))
print(json.dumps({{"result": "census done: +3 sources", "total_cost_usd": 0.01}}))
sys.exit({returncode})
""", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


# ---------------------------------------------------------------- scheduler
def test_schedule_snippets_and_due(cars):
    p = make_project(cadence="6h")
    sid = ready_source(p, cars.url + "/cars")
    src = p.store.get_source(p.name, sid)
    assert scheduler.is_due(p, src) and scheduler.next_due(p, src) == "now"
    runner.run(p)
    assert not scheduler.is_due(p, src)
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=7)
    assert scheduler.is_due(p, src, later)
    p.store.upsert_source(p.name, sid, {"cadence_hours": 0})
    assert not scheduler.is_due(p, p.store.get_source(p.name, sid), later)  # manual
    out = scheduler.generate(p, kind="both", cadence="daily", user="svc")
    cron = open(out["files"]["cron"]["path"]).read()
    svc = open(out["files"]["systemd"]["service"]).read()
    timer = open(out["files"]["systemd"]["timer"]).read()
    assert f"run {p.name} --due" in cron and f"watchdog {p.name}" in cron
    assert "User=svc" in svc and f"run {p.name} --due" in svc and "NoNewPrivileges=true" in svc
    assert "OnCalendar=hourly" in timer and "Persistent=true" in timer
    assert p.spec.cadence == "daily" and "nothing was installed" in out["note"]
    with pytest.raises(ValueError):
        scheduler.generate(p, cadence="whenever")


# ---------------------------------------------------------------- watchdog
def _fake_run(p, sid, status, stored, started, **kw):
    rid = p.store.start_run(p.name, sid, "test", None)
    p.store.execute("UPDATE runs SET status = ?, rows_stored = ?, started_at = ?, heartbeat_at = ?, finished_at = ? WHERE id = ?",
                    (status, stored, started, kw.get("heartbeat", started), None if status == "running" else started, rid))
    return rid


def test_watchdog_findings_alerts_and_hook(cars, monkeypatch, tmp_path):
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    now = dt.datetime.now(dt.timezone.utc)
    t = lambda h: (now - dt.timedelta(hours=h)).isoformat(timespec="microseconds")  # noqa: E731
    _fake_run(p, sid, "empty", 0, t(80))
    _fake_run(p, sid, "empty", 0, t(60))
    _fake_run(p, sid, "running", 0, t(50), heartbeat=t(49))
    p.store.execute("UPDATE sources SET updated_at = ? WHERE id = ?", (t(90), sid))
    kinds = {f["kind"] for f in watchdog.check(p, now=now)}
    assert {"zero", "stuck", "stale"} <= kinds
    assert p.store.list_runs(p.name, sid)[0]["status"] == "stuck"
    hook_mod = tmp_path / "alerthook.py"
    hook_mod.write_text("SEEN = []\ndef send(project, alerts):\n    SEEN.append((project, alerts))\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("HARVEST_ALERT_HOOK", "alerthook:send")
    res = watchdog.run(p, now=now)
    import alerthook
    assert res["new_alerts"] >= 2 and res["delivered"] == ["hook"] and alerthook.SEEN[0][0] == p.name
    assert (p.root / "alerts.jsonl").read_text().count("\n") == res["new_alerts"]
    assert watchdog.run(p, now=now)["new_alerts"] == 0  # no re-alert within 24 h


def test_watchdog_degraded_and_error_streak(cars):
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    now = dt.datetime.now(dt.timezone.utc)
    for h in (3, 2, 1):
        _fake_run(p, sid, "error", 0, (now - dt.timedelta(hours=h)).isoformat(timespec="microseconds"))
    kinds = {f["kind"] for f in watchdog.check(p, now=now)}
    assert "error_streak" in kinds and "zero" in kinds
    _fake_run(p, sid, "degraded", 3, now.isoformat(timespec="microseconds"))
    assert "degraded" in {f["kind"] for f in watchdog.check(p, now=now)}


def test_watchdog_finds_a_module_edited_after_its_review(cars):
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    assert watchdog.check(p) == []
    p.module_path(sid).write_text(p.module_path(sid).read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    found = watchdog.check(p)  # found now, not only when the next run refuses the module
    assert [(f["source_id"], f["kind"]) for f in found] == [(sid, "review_stale")]
    assert p.store.get_source(p.name, sid)["status"] == "review_stale"
    assert runner.run(p)["ran"][0]["status"] == "review_stale"


def test_repair_dispatch_is_off_by_default_and_allowlisted(cars, tmp_path, monkeypatch):
    p = make_project()
    sid = ready_source(p, cars.url + "/cars")
    now = dt.datetime.now(dt.timezone.utc)
    for h in (3, 2):
        _fake_run(p, sid, "empty", 0, (now - dt.timedelta(hours=h)).isoformat(timespec="microseconds"))
    res = watchdog.run(p, dispatch=True, now=now)
    assert res["dispatched"] == [] and "not 'on'" in res["skipped"][0]
    script, log = fake_cli(tmp_path)
    monkeypatch.setenv("HARVEST_AGENT_BIN", str(script))
    monkeypatch.setenv("HARVEST_REPAIR_DISPATCH", "on")
    res = watchdog.run(p, dispatch=True, now=now)
    assert res["dispatched"][0]["source_id"] == sid and res["dispatched"][0]["status"] == "done"
    argv = json.loads(log.read_text())
    tools = argv[argv.index("--allowedTools") + 1].split(",")
    assert f"Edit(/{p.module_path(sid)})" in tools and not any(t.startswith("Write(") for t in tools) and "Bash" not in " ".join(tools) and "mcp__harvest__harvest_enable_source" not in tools
    assert not any("dangerously" in a or "bypassPermissions" in a for a in argv)
    res = watchdog.run(p, dispatch=True, now=now)
    assert res["dispatched"] == [] and any("cooldown" in s for s in res["skipped"])


# ---------------------------------------------------------------- agents
def test_agent_briefs_tools_and_commands(tmp_path, monkeypatch):
    p = make_project()
    b = agents.brief("census", p)
    assert "KZ, GE" in b and "used cars" in b and "{{" not in b
    # trials 2026-10: the census registered one-off articles and an API documentation page as sources
    assert "listicle" in b and "not** a source" in b and "documentation" in b and "listicle" in agents.brief("audit", p) and "**dead**" in b
    assert "never" in agents.brief("build", p, source_id="kolesa_kz").lower()
    census_tools = agents.allowed_tools("census", p)
    assert "WebSearch" in census_tools and "mcp__harvest__harvest_census_add" in census_tools and not any(t.startswith("Edit") for t in census_tools)
    with pytest.raises(ValueError):
        agents.allowed_tools("build", p)
    monkeypatch.setenv("HARVEST_AGENT_MODEL", "some-model")
    argv = agents.build_command("build", p, "kolesa_kz")
    assert argv[1] == "-p" and "--allowedTools" in argv and argv[argv.index("--model") + 1] == "some-model"
    assert "--strict-mcp-config" in argv and json.loads(open(argv[argv.index("--mcp-config") + 1]).read())["mcpServers"]["harvest"]
    with pytest.raises(ValueError):
        agents.get_cli("nope")


def test_pluggable_agent_cli(tmp_path, monkeypatch):
    mod = tmp_path / "mycli.py"
    mod.write_text("from harvest_ai.agents import AgentCli\nclass Echo(AgentCli):\n    name='echo'\n    def binary(self):\n        return 'echo'\n"
                   "    def argv(self, prompt, *, allowed, model, mcp_config, max_turns):\n        return ['echo', '--tools', ','.join(allowed)]\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    p = make_project()
    # security review 2026-10: a job parameter cannot import a module:Class; the operator's environment can
    with pytest.raises(ValueError):
        agents.build_command("census", p, cli="mycli:Echo")
    monkeypatch.setenv("HARVEST_AGENT_CLI", "mycli:Echo")
    assert agents.build_command("census", p)[:2] == ["echo", "--tools"]
    assert agents.build_command("census", p, cli="mycli:Echo")[:2] == ["echo", "--tools"]

    class Bad(agents.AgentCli):
        def argv(self, prompt, **kw):
            return ["x", "--dangerously-skip-permissions"]
    monkeypatch.setitem(agents.CLIS, "bad", Bad)
    with pytest.raises(RuntimeError):
        agents.build_command("census", p, cli="bad")


def test_headless_census_job_records_result(tmp_path, monkeypatch):
    p = make_project()
    script, log = fake_cli(tmp_path)
    monkeypatch.setenv("HARVEST_AGENT_BIN", str(script))
    res = service.start_job(p.name, "agent:census", {}, wait=True)
    assert res["status"] == "done" and res["result"]["result"] == "census done: +3 sources"
    job = service.jobs(p.name, res["job_id"])["job"]
    assert job["kind"] == "agent:census" and job["status"] == "done" and os.path.exists(job["log_path"])
    script2, _ = fake_cli(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, returncode=3)
    monkeypatch.setenv("HARVEST_AGENT_BIN", str(script2))
    assert service.start_job(p.name, "agent:audit", {}, wait=True)["status"] == "failed"
    monkeypatch.setenv("HARVEST_AGENT_BIN", str(tmp_path / "missing-binary"))
    assert service.start_job(p.name, "agent:census", {}, wait=True)["status"] == "failed"


# ---------------------------------------------------------------- query / export / fx
def test_query_filters_and_exports(cars, tmp_path):
    p = make_project()
    p.set_fx({"KZT": 500, "GEL": 2.7})
    ready_source(p, cars.url + "/cars")
    runner.run(p)
    q = export.query(p, filters={"price_report": {"lte": 20000}}, order_by="price", desc=False, limit=5)
    assert q["total"] == 5 and [r["price"] for r in q["rows"]] == sorted(r["price"] for r in q["rows"])
    assert all(r["price_report"] <= 20000 for r in q["rows"])
    assert export.query(p, filters={"make": "Toyota"})["total"] == 6
    assert export.query(p, filters={"make": {"in": ["Kia", "Lada"]}})["total"] == 13
    assert export.query(p, filters={"title": {"contains": "camry"}})["total"] == 6
    assert export.query(p, filters={"vin": {"exists": False}})["total"] == 25
    assert export.query(p, fields=["make", "price"], limit=1)["rows"][0].keys() >= {"make", "price", "source"}
    with pytest.raises(ValueError):
        export.query(p, filters={"nope": 1})
    with pytest.raises(ValueError):
        export.query(p, filters={"price": {"like": 1}})
    out = export.export(p, "csv", str(tmp_path / "o.csv"))
    rows = list(csv.DictReader(open(out["path"], encoding="utf-8")))
    assert out["rows"] == 25 and rows[0]["currency"] == "KZT" and "price_report" in rows[0]
    out = export.export(p, "jsonl", str(tmp_path / "o.jsonl"))
    assert len(open(out["path"]).read().splitlines()) == 25
    pq = pytest.importorskip("pyarrow.parquet")
    out = export.export(p, "parquet", str(tmp_path / "o.parquet"))
    assert pq.read_table(out["path"]).num_rows == 25
    with pytest.raises(ValueError):
        export.export(p, "xlsx")


def test_fx_provider_hook(tmp_path, monkeypatch):
    (tmp_path / "fxprov.py").write_text("def rates():\n    return {'base': 'USD', 'rates': {'EUR': 0.5}}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("HARVEST_FX_PROVIDER", "fxprov:rates")
    p = make_project()
    assert p.fx().convert(1, "USD", "EUR") == 0.5
    assert now_iso()


def test_stream_json_transcript_is_logged_and_parsed(tmp_path, monkeypatch):
    """Pilot: with --output-format json the job log held only the final answer, so an agent's tool calls
    could not be audited. Now the adapter streams the transcript into the log and parses the result event."""
    log_argv = tmp_path / "argv.json"
    script = tmp_path / "stream-claude"
    events = [{"type": "system", "subtype": "init"},
              {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__harvest__harvest_census_plan", "input": {}}]}},
              {"type": "assistant", "message": {"content": [{"type": "text", "text": "adding"}, {"type": "tool_use", "name": "WebSearch", "input": {}}]}},
              {"type": "result", "subtype": "success", "is_error": False, "result": "census done", "total_cost_usd": 0.2, "num_turns": 5}]
    script.write_text(f"""#!{sys.executable}
import json, sys
open({str(log_argv)!r}, "w").write(json.dumps(sys.argv[1:]))
for e in {events!r}:
    print(json.dumps(e), flush=True)
""", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HARVEST_AGENT_BIN", str(script))
    p = make_project()
    res = agents.run_job("census", p)
    assert res["status"] == "done" and res["result"]["result"] == "census done" and res["result"]["tool_calls"] == 2 and res["result"]["turns"] == 5
    argv = json.loads(log_argv.read_text())
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    log = open(service.jobs(p.name, res["job_id"])["job"]["log_path"]).read()
    assert "mcp__harvest__harvest_census_plan" in log  # the tool-call trace is auditable
    assert agents.parse_output(['{"type": "result", "is_error": true, "result": "limit"}'])["is_error"]
    assert agents.parse_output(["not json at all"])["result"] == "not json at all"


def test_agent_timeout_kills_the_cli(tmp_path, monkeypatch):
    script = tmp_path / "slow-claude"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HARVEST_AGENT_BIN", str(script))
    res = agents.run_job("census", make_project(), timeout_s=1)
    assert res["status"] == "timeout"


def test_edit_rule_uses_the_absolute_path_form():
    """Pilot: `Edit(/srv/...)` + `Write(/srv/...)` denied every module write (the CLI reads `/x` as project-relative
    and ignores Write(path) rules); the absolute form is `Edit(//srv/...)`."""
    p = make_project()
    rule = agents.edit_rule(p.module_path("s1"))
    assert rule.startswith("Edit(//") and rule.endswith("/sources/s1.py)")
    for kind in ("build", "repair"):
        tools = agents.allowed_tools(kind, p, "s1")
        assert [t for t in tools if t.startswith(("Edit(", "Write("))] == [rule]


def test_budget_ignores_sources_that_can_never_be_collected():
    """Pilot: 4 of 8 rent-pt sources turned out lane none (terms forbid) but still filled the budget, so the
    audit's five verified portals were rejected."""
    from harvest_ai import census
    p = make_project(max_sources=2, regions=("PT",))
    ev = lambda u: [{"url": u, "observation": "ads"}]  # noqa: E731
    ids = [a["id"] for a in census.add(p, [{"url": f"https://s{i}.pt", "regions": ["PT"], "evidence": ev(f"https://s{i}.pt")} for i in range(2)])["added"]]
    assert "max_sources" in census.add(p, [{"url": "https://t.pt", "regions": ["PT"], "evidence": ev("https://t.pt")}])["rejected"][0]["reason"]
    p.store.upsert_source(p.name, ids[0], {"status": "lane_none", "lane": "none"})
    assert census.add(p, [{"url": "https://t.pt", "regions": ["PT"], "evidence": ev("https://t.pt")}])["added"]
    assert census.gaps(p)["sources"] == 2


def test_expired_leases_requeue_or_mark_lost():
    """Lost-job detection uses the lease (not a PID, which is meaningless across hosts and containers)."""
    p = make_project()
    a = p.store.add_job(p.name, "run", {})
    b = p.store.add_job(p.name, "run", {}, max_attempts=1)
    for jid in (a, b):
        p.store.update_job(jid, status="running", worker_id="dead", attempts=1, lease_until=now_iso(-5))
    listing = {j["id"]: j for j in service.jobs(p.name)["jobs"]}
    assert listing[a]["status"] == "queued" and listing[a]["worker_id"] is None
    assert listing[b]["status"] == "lost" and "lease expired" in listing[b]["result"]["error"]

def test_headless_agents_get_only_their_builtin_tools(monkeypatch):
    """Pilot: --allowedTools only pre-approves; the builder session still had Bash, Task, Cron ... and tried Bash
    repeatedly. --tools now limits the built-in set to exactly what the job needs."""
    p = make_project()
    argv = agents.build_command("build", p, "s1")
    assert argv[argv.index("--tools") + 1] == "Edit,Read,WebFetch,Write"
    argv = agents.build_command("census", p)
    assert argv[argv.index("--tools") + 1] == "WebFetch,WebSearch"
    assert "Bash" not in argv[argv.index("--tools") + 1]


def test_probe_url_pages_and_greps_long_documents(site):
    """Pilot: listing cards sat beyond the 20k-character probe window and the builder had to smuggle HTML
    out through a throwaway scraper module."""
    body = "<p>" + "x" * 30000 + "</p>" + "".join(f"<div data-listing-id='{i}'>car {i}</div>" for i in range(3))
    site.route("/robots.txt", (200, {}, "User-agent: *\n"))
    site.route("/big", body)
    out = service.probe_url(site.url + "/big", max_chars=10000, grep=r"data-listing-id='\d+'", context=20)
    assert out["html_total"] > 30000 and out["next_offset"] == 10000
    assert out["grep"]["matches"] == 3 and "car 0" in out["grep"]["snippets"][0]["text"]
    assert "car 2" in service.probe_url(site.url + "/big", max_chars=40000, offset=30000)["html"]
    with pytest.raises(ValueError):
        service.probe_url(site.url + "/big", grep="(")


def test_cli_census_resume_waits_for_an_inline_agent(tmp_path, monkeypatch, capsys):
    """Trials 2026-10: with HARVEST_JOB_MODE=inline, `harvest census-resume --agent` handed the agent to a daemon thread and
    exited at once, killing it; the job stayed `running` until it was reaped as lost."""
    from harvest_ai import census, cli
    p = make_project(max_sources=1, regions=("PT",))
    census.add(p, [{"url": "https://a.pt", "regions": ["PT"], "evidence": [{"url": "https://a.pt", "observation": "ads"}]}])
    script, log = fake_cli(tmp_path)
    monkeypatch.setenv("HARVEST_AGENT_BIN", str(script))
    monkeypatch.setenv("HARVEST_JOB_MODE", "inline")
    assert cli.main(["census-resume", p.name, "--max-sources", "4", "--agent"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["job"]["status"] == "done" and out["job"]["result"]["result"] == "census done: +3 sources"
    assert "CONTINUATION" in " ".join(json.loads(log.read_text()))
    monkeypatch.setenv("HARVEST_JOB_MODE", "queue")
    assert cli.main(["census-resume", p.name, "--max-sources", "6", "--agent"]) == 0
    assert json.loads(capsys.readouterr().out)["job"]["status"] == "queued"
