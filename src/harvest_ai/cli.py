"""`harvest` command line. Every subcommand calls `harvest_ai.service` and prints JSON."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, project, service


def _out(obj) -> None:
    print(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


def _json_arg(v: str | None):
    if v is None:
        return None
    if v.startswith("@"):
        with open(v[1:], encoding="utf-8") as f:
            return json.load(f)
    return json.loads(v)


def daemon(interval_s: int, once: bool = False, dispatch: bool = False) -> None:
    """Scheduler loop for containers: a queue worker that also runs due sources + the watchdog every `interval_s`."""
    from . import jobs
    jobs.worker(schedule_s=interval_s, once=once, dispatch=dispatch)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="harvest", description="regions + target -> discovered sources -> reviewed scrapers -> clean stored data")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("templates", help="record templates and region groups")
    s.add_argument("record_type", nargs="?")

    s = sub.add_parser("new", help="create a project")
    s.add_argument("name")
    s.add_argument("--target", required=True)
    s.add_argument("--regions", required=True, help="comma-separated ISO codes, country names or groups (EU, GCC, CAUCASUS ...)")
    s.add_argument("--record-type", required=True)
    s.add_argument("--fields", default="", help="extra fields, comma-separated")
    s.add_argument("--languages", default="")
    s.add_argument("--max-sources", type=int, default=30)
    s.add_argument("--cadence", default="daily")
    s.add_argument("--report-currency", default="USD")
    s.add_argument("--store-dsn")
    s.add_argument("--max-pages", type=int, default=20)
    s.add_argument("--time-budget", type=int, default=900)
    s.add_argument("--rate", type=float, default=2.0, help="seconds between requests to one host")

    sub.add_parser("projects", help="list projects")
    for name, hlp in (("census-plan", "discovery plan per region and angle"), ("gaps", "census coverage and dry streak"), ("status", "project status")):
        sub.add_parser(name, help=hlp).add_argument("project")

    s = sub.add_parser("census-add", help="add candidates: JSON list or @file.json")
    s.add_argument("project")
    s.add_argument("candidates")
    s.add_argument("--round", default="manual")

    s = sub.add_parser("census-resume", help="raise max_sources after budget_reached, re-add deferred candidates, print the continuation brief")
    s.add_argument("project")
    s.add_argument("--max-sources", type=int, required=True)
    s.add_argument("--agent", action="store_true", help="also submit a census agent job with the continuation brief")
    s.add_argument("--wait", action="store_true", help="run that agent here and wait for it (always so with HARVEST_JOB_MODE=inline)")

    s = sub.add_parser("sources", help="list sources (or one with --id)")
    s.add_argument("project")
    s.add_argument("--status")
    s.add_argument("--id")

    s = sub.add_parser("reject", help="reject a candidate")
    s.add_argument("project")
    s.add_argument("source_id")
    s.add_argument("reason")

    s = sub.add_parser("detect-lane", help="detect lanes (one source or all candidates)")
    s.add_argument("project")
    s.add_argument("source_id", nargs="?")
    s.add_argument("--no-mcp", action="store_true")
    s.add_argument("--no-proxy", action="store_true", help="never arm the residential-proxy route for this detection")
    s.add_argument("--own-ua", action="store_true", help="send harvest's own user agent even where the browser UA mode is on")

    s = sub.add_parser("policy", help="record a terms verdict or lane override")
    s.add_argument("project")
    s.add_argument("source_id")
    s.add_argument("--terms-status", choices=["allowed", "no_clause", "forbids", "unknown"])
    s.add_argument("--terms-url")
    s.add_argument("--terms-clause")
    s.add_argument("--lane")
    s.add_argument("--reason")

    s = sub.add_parser("probe", help="fetch one URL politely and summarise it")
    s.add_argument("url")
    s.add_argument("--max-chars", type=int, default=4000)

    s = sub.add_parser("scaffold", help="write the scraper template for a source")
    s.add_argument("project")
    s.add_argument("source_id")
    s.add_argument("--overwrite", action="store_true")

    s = sub.add_parser("review", help="review gate: lint + sandboxed page-1 fetch + schema")
    s.add_argument("project")
    s.add_argument("source_id")
    s.add_argument("--timeout", type=int)
    s.add_argument("--pages", type=int, default=1)
    s.add_argument("--no-proxy", action="store_true", help="never arm the residential-proxy route for this review")
    s.add_argument("--own-ua", action="store_true", help="send harvest's own user agent even where the browser UA mode is on")

    for name in ("enable", "disable"):
        s = sub.add_parser(name, help=f"{name} a source")
        s.add_argument("project")
        s.add_argument("source_id")

    s = sub.add_parser("tune", help="set field_map / max_pages / cadence_hours ... on a source")
    s.add_argument("project")
    s.add_argument("source_id")
    s.add_argument("--field-map", help="JSON object")
    s.add_argument("--max-pages", type=int)
    s.add_argument("--time-budget", type=int)
    s.add_argument("--cadence")
    s.add_argument("--rate", type=float)
    s.add_argument("--url", help="new entry URL on the same site (resets lane and review)")

    s = sub.add_parser("run", help="collect")
    s.add_argument("project")
    s.add_argument("source_ids", nargs="*")
    s.add_argument("--due", action="store_true", help="only sources whose cadence is due")
    s.add_argument("--max-pages", type=int)
    s.add_argument("--no-proxy", action="store_true", help="never arm the residential-proxy route for this run")
    s.add_argument("--own-ua", action="store_true", help="send harvest's own user agent even where the browser UA mode is on")

    s = sub.add_parser("ua", help="the browser user-agent mode: status, enable/disable with an operator decision")
    us = s.add_subparsers(dest="ua_cmd", required=True)
    x = us.add_parser("status", help="decision, per-source mode and the header set sent")
    x.add_argument("project")
    x = us.add_parser("enable", help="send a desktop Chrome UA for a project or one source; the reason is recorded with the date")
    x.add_argument("project")
    x.add_argument("--source")
    x.add_argument("--reason", required=True)
    x = us.add_parser("disable", help="back to harvest's own user agent")
    x.add_argument("project")
    x.add_argument("--source")
    x.add_argument("--reason")

    s = sub.add_parser("session-headers", help="allow one source's module to send Cookie/Authorization (refused by default)")
    ss = s.add_subparsers(dest="sh_cmd", required=True)
    x = ss.add_parser("enable", help="operator decision for one source; the reason is recorded with the date")
    x.add_argument("project")
    x.add_argument("--source", required=True)
    x.add_argument("--reason", required=True)
    x = ss.add_parser("disable", help="refuse session headers for the source again")
    x.add_argument("project")
    x.add_argument("--source", required=True)
    x.add_argument("--reason")

    s = sub.add_parser("proxy", help="the residential-proxy route: status, enable/disable with an operator decision")
    ps = s.add_subparsers(dest="proxy_cmd", required=True)
    x = ps.add_parser("status", help="pool size and health (never its contents); with a project: decision, caps, usage per source")
    x.add_argument("project", nargs="?")
    x.add_argument("--check", type=int, default=0, help="live-probe N random exits (<= 5)")
    x = ps.add_parser("enable", help="switch the route on for a project or one source; the reason is recorded with the date")
    x.add_argument("project")
    x.add_argument("--source", help="one source only (default: the whole project)")
    x.add_argument("--reason", required=True, help="why (e.g. 'market leader 403s the datacenter IP; terms allow collection')")
    x.add_argument("--daily-mb", type=float, help="the project's daily proxied-bandwidth cap in MB")
    x.add_argument("--daily-requests", type=int, help="the project's daily proxied-request cap")
    x.add_argument("--mode", choices=["sticky", "rotate"], help="one stable exit per source, or rotate per request")
    x.add_argument("--country", help="auto (the source's region) | off | a 2-letter code")
    x = ps.add_parser("disable", help="switch the route off for a project or one source")
    x.add_argument("project")
    x.add_argument("--source")
    x.add_argument("--reason")

    s = sub.add_parser("runs", help="run history")
    s.add_argument("project")
    s.add_argument("--source")
    s.add_argument("--limit", type=int, default=30)

    s = sub.add_parser("quarantine", help="quarantined rows")
    s.add_argument("project")
    s.add_argument("--source")
    s.add_argument("--limit", type=int, default=30)

    s = sub.add_parser("query", help="query records")
    s.add_argument("project")
    s.add_argument("--filters", help='JSON, e.g. {"price": {"lte": 20000}}')
    s.add_argument("--fields", help="comma-separated")
    s.add_argument("--source")
    s.add_argument("--order-by")
    s.add_argument("--asc", action="store_true")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--distinct", action="store_true")

    s = sub.add_parser("export", help="export records")
    s.add_argument("project")
    s.add_argument("--format", default="csv", choices=["csv", "jsonl", "parquet"])
    s.add_argument("--out")
    s.add_argument("--filters")
    s.add_argument("--distinct", action="store_true")
    s.add_argument("--raw", action="store_true", help="CSV: write cells unchanged (by default a cell starting with = + - @ "
                                                      "or a tab/CR gets a leading ' so spreadsheets do not run it as a formula)")

    s = sub.add_parser("schedule", help="write cron / systemd snippets")
    s.add_argument("project")
    s.add_argument("--kind", default="both", choices=["cron", "systemd", "both"])
    s.add_argument("--cadence")
    s.add_argument("--user")

    s = sub.add_parser("watchdog", help="health check + alerts (+ repair dispatch when enabled)")
    s.add_argument("project")
    s.add_argument("--dispatch", action="store_true")

    s = sub.add_parser("fx", help="set the FX table: JSON {CUR: units per base} or @file")
    s.add_argument("project")
    s.add_argument("rates")
    s.add_argument("--base", default="USD")

    s = sub.add_parser("agent", help="run a headless agent job (census | audit | build | repair)")
    s.add_argument("project")
    s.add_argument("kind", choices=["census", "audit", "build", "repair"])
    s.add_argument("source_id", nargs="?")
    s.add_argument("--cli")
    s.add_argument("--model")
    s.add_argument("--print-command", action="store_true", help="show the command and exit")
    s.add_argument("--brief", action="store_true", help="print the agent brief and exit")

    s = sub.add_parser("daemon", help="scheduler loop: run due sources + watchdog for every project")
    s.add_argument("--interval", type=int, default=300)
    s.add_argument("--once", action="store_true")
    s.add_argument("--dispatch", action="store_true")

    s = sub.add_parser("worker", help="durable job worker: claims queued jobs (census, build, run ...) with a lease and heartbeat")
    s.add_argument("--poll", type=float, default=2.0, help="seconds between queue polls when idle")
    s.add_argument("--lease", type=float, default=None, help="lease seconds (renewed every lease/3 while a job runs)")
    s.add_argument("--kinds", help="only these job kinds, comma-separated (e.g. run,review or agent)")
    s.add_argument("--schedule", type=float, help="also run due sources + watchdog every N seconds")
    s.add_argument("--once", action="store_true", help="drain the queue, then exit")

    s = sub.add_parser("jobs", help="list jobs (or one with --id)")
    s.add_argument("project")
    s.add_argument("--id")

    s = sub.add_parser("web", help="serve the web app + API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--inline-jobs", action="store_true", help="dev: run submitted jobs in this process instead of the queue")

    s = sub.add_parser("mcp", help="serve the MCP server (stdio; --http for streamable HTTP)")
    s.add_argument("--http", action="store_true")
    s.add_argument("--port", type=int, default=8091)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        c = a.cmd
        if c == "templates":
            _out(service.get_template(a.record_type) if a.record_type else service.list_templates())
        elif c == "new":
            _out(service.new_project(a.name, a.target, a.regions, a.record_type, fields=[f for f in a.fields.split(",") if f.strip()],
                                     languages=[x for x in a.languages.split(",") if x.strip()], max_sources=a.max_sources, cadence=a.cadence,
                                     report_currency=a.report_currency, store_dsn=a.store_dsn, max_pages=a.max_pages, time_budget_s=a.time_budget, rate_s=a.rate))
        elif c == "projects":
            _out(service.list_projects())
        elif c == "census-plan":
            _out(service.census_plan(a.project))
        elif c == "gaps":
            _out(service.census_gaps(a.project))
        elif c == "status":
            _out(service.status(a.project))
        elif c == "census-add":
            _out(service.census_add(a.project, _json_arg(a.candidates), a.round))
        elif c == "census-resume":
            # an inline job runs in a thread of this process, which would die with it: the CLI waits for it instead
            _out(service.census_resume(a.project, a.max_sources, a.agent, wait=a.wait or os.environ.get("HARVEST_JOB_MODE") == "inline"))
        elif c == "sources":
            _out(service.list_sources(a.project, a.status, a.id))
        elif c == "reject":
            _out(service.reject_source(a.project, a.source_id, a.reason))
        elif c == "detect-lane":
            _out(service.detect_lane(a.project, a.source_id, use_mcp=not a.no_mcp, use_proxy=not a.no_proxy, use_browser_ua=not a.own_ua))
        elif c == "policy":
            _out(service.record_policy(a.project, a.source_id, a.terms_status, a.terms_url, a.terms_clause, a.lane, a.reason))
        elif c == "probe":
            _out(service.probe_url(a.url, a.max_chars))
        elif c == "scaffold":
            r = service.template_scraper(a.project, a.source_id, a.overwrite)
            r.pop("code", None)
            _out(r)
        elif c == "review":
            r = service.review_source(a.project, a.source_id, a.timeout, a.pages, use_proxy=not a.no_proxy, use_browser_ua=not a.own_ua)
            _out(r)
            return 0 if r["verdict"] == "pass" else 1
        elif c in ("enable", "disable"):
            r = service.enable_source(a.project, a.source_id, c == "enable")
            _out(r)
            return 1 if r.get("refused") else 0
        elif c == "tune":
            kw = {k: v for k, v in dict(field_map=_json_arg(a.field_map), max_pages=a.max_pages, time_budget_s=a.time_budget, cadence_hours=a.cadence,
                                        rate_s=a.rate, url=a.url).items() if v is not None}
            _out(service.update_source(a.project, a.source_id, **kw))
        elif c == "run":
            _out(service.run(a.project, a.source_ids or None, a.due, a.max_pages, trigger="schedule" if a.due else "manual", use_proxy=not a.no_proxy,
                             use_browser_ua=not a.own_ua))
        elif c == "ua":
            if a.ua_cmd == "status":
                _out(service.ua_status(a.project))
            elif a.ua_cmd == "enable":
                _out(service.ua_enable(a.project, a.reason, a.source, by="cli"))
            else:
                _out(service.ua_disable(a.project, a.source, a.reason, by="cli"))
        elif c == "session-headers":
            if a.sh_cmd == "enable":
                _out(service.session_headers_enable(a.project, a.source, a.reason, by="cli"))
            else:
                _out(service.session_headers_disable(a.project, a.source, a.reason, by="cli"))
        elif c == "proxy":
            if a.proxy_cmd == "status":
                _out(service.proxy_status(a.project, a.check))
            elif a.proxy_cmd == "enable":
                _out(service.proxy_enable(a.project, a.reason, a.source, int(a.daily_mb * 1024 * 1024) if a.daily_mb is not None else None,
                                          a.daily_requests, a.mode, a.country, by="cli"))
            else:
                _out(service.proxy_disable(a.project, a.source, a.reason, by="cli"))
        elif c == "runs":
            _out(service.runs(a.project, a.source, a.limit))
        elif c == "quarantine":
            _out(service.quarantine(a.project, a.source, a.limit))
        elif c == "query":
            _out(service.query(a.project, _json_arg(a.filters), [f for f in (a.fields or "").split(",") if f] or None, a.source, a.order_by,
                               not a.asc, a.limit, a.offset, a.distinct))
        elif c == "export":
            _out(service.export(a.project, a.format, a.out, _json_arg(a.filters), a.distinct, a.raw))
        elif c == "schedule":
            _out(service.schedule(a.project, a.kind, a.cadence, a.user))
        elif c == "watchdog":
            _out(service.run_watchdog(a.project, a.dispatch))
        elif c == "fx":
            _out(service.set_fx(a.project, _json_arg(a.rates), a.base))
        elif c == "agent":
            if a.brief:
                print(service.agent_brief(a.project, a.kind, a.source_id)["brief"])
            elif a.print_command:
                _out(service.agent_command(a.project, a.kind, a.source_id, a.cli, a.model))
            else:
                _out(service.start_job(a.project, f"agent:{a.kind}", {"source_id": a.source_id, "cli": a.cli, "model": a.model}, wait=True))
        elif c == "daemon":
            daemon(a.interval, a.once, a.dispatch)
        elif c == "worker":
            from . import jobs
            jobs.worker(poll_s=a.poll, lease_s=a.lease or jobs.DEFAULT_LEASE_S, once=a.once,
                        kinds=[k.strip() for k in a.kinds.split(",")] if a.kinds else None, schedule_s=a.schedule)
        elif c == "jobs":
            _out(service.jobs(a.project, a.id))
        elif c == "web":
            if a.inline_jobs:
                os.environ["HARVEST_JOB_MODE"] = "inline"
            import uvicorn

            from .web.app import create_app
            app = create_app()
            src = "HARVEST_ADMIN_TOKEN" if os.environ.get("HARVEST_ADMIN_TOKEN") else str(project.home() / "admin_token")
            print(f"harvest web on http://{a.host}:{a.port}  (admin token: {src})", file=sys.stderr, flush=True)
            uvicorn.run(app, host=a.host, port=a.port)
        elif c == "mcp":
            from . import mcp_server
            sys.argv = ["harvest-mcp", *(["--http", "--port", str(a.port)] if a.http else [])]
            mcp_server.main()
        return 0
    except (ValueError, LookupError, FileNotFoundError, RuntimeError) as exc:
        print(json.dumps({"error": exc.__class__.__name__, "message": str(exc)}), file=sys.stderr)
        return 2


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    entry()
