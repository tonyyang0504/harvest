"""harvest as an MCP server (stdio by default, `--http --port N` for streamable HTTP).

Every tool is a thin wrapper over `harvest_ai.service`; failures come back as `isError` results.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import __version__, service

server = MCPServer(
    name="harvest", title="harvest: regions + target -> collected data", version=__version__,
    instructions=(
        "Collect public data for any industry and region. Flow: harvest_new_project (target, regions, record_type) -> "
        "harvest_census_plan -> research every angle and language, harvest_census_add candidates WITH evidence -> "
        "harvest_census_gaps until two dry rounds -> harvest_detect_lane -> harvest_template_scraper -> write fetch() -> "
        "harvest_review_source until pass -> harvest_enable_source -> harvest_run -> harvest_query / harvest_export -> "
        "harvest_schedule. Never log in, bypass captchas or paywalls, or use mirrors; a block is a finding. "
        "harvest_agent_brief returns the full instructions for census, audit, build and repair work."),
)

RO = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
RO_NET = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
WRITE_NET = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)


def _res(payload: Any, is_error: bool = False) -> CallToolResult:
    if not isinstance(payload, dict):
        payload = {"result": payload}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))],
                          structured_content=json.loads(json.dumps(payload, default=str)), is_error=is_error)


def _agent_kind() -> str | None:
    """Set when this server was started for a headless harvest agent (agents.mcp_config_for)."""
    import os
    return os.environ.get("HARVEST_AGENT_KIND") or None


def _refusal(tool: str) -> str | None:
    kind = _agent_kind()
    if not kind:
        return None
    from .agents import MCP_ALLOWED
    if tool not in MCP_ALLOWED.get(kind, ()):
        return f"{tool} is not available to a {kind} agent; it is an operator action"
    return None


async def _call(fn, *args, **kw) -> CallToolResult:
    import inspect
    frame = inspect.currentframe()
    tool = frame.f_back.f_code.co_name if frame is not None and frame.f_back is not None else ""
    why = _refusal(tool)
    if why:
        return _res({"error": "forbidden", "message": why}, True)
    if fn is service.record_policy and _agent_kind():
        kw["actor"] = f"agent:{_agent_kind()}"
    try:
        return _res(await asyncio.to_thread(fn, *args, **kw))
    except (ValueError, LookupError, FileNotFoundError, RuntimeError, PermissionError) as exc:
        kind = "not_found" if isinstance(exc, (LookupError, FileNotFoundError)) else "invalid_input" if isinstance(exc, ValueError) else "error"
        return _res({"error": kind, "message": str(exc)}, True)
    except Exception as exc:  # never a protocol error
        return _res({"error": "internal", "message": f"{exc.__class__.__name__}: {exc}"}, True)


@server.tool(name="harvest_templates", title="Record templates", description="Record types (vehicles, real_estate_sale, real_estate_rent, rentals, jobs, products, events, businesses, generic) with their fields, plus the named region groups. Pass record_type for one template in full (types, units, currency and period rules, bounds, dedup keys).", annotations=RO)
async def harvest_templates(record_type: str | None = None) -> CallToolResult:
    return await _call(service.get_template, record_type) if record_type else await _call(service.list_templates)


@server.tool(name="harvest_new_project", title="New project", description="Create a project from the spec: name, target (e.g. 'used cars'), regions (ISO-3166 alpha-2 codes, country names or groups such as EU, GCC, CAUCASUS), record_type, extra fields wanted, languages (default: the regions' languages), max_sources, cadence (hourly|6h|12h|daily|weekly|monthly|manual), report_currency.", annotations=WRITE)
async def harvest_new_project(name: str, target: str, regions: list[str], record_type: str, fields: list[str] | None = None,
                              languages: list[str] | None = None, max_sources: int = 30, cadence: str = "daily", report_currency: str = "USD",
                              max_pages: int = 20) -> CallToolResult:
    return await _call(service.new_project, name=name, target=target, regions=regions, record_type=record_type, fields=fields, languages=languages,
                       max_sources=max_sources, cadence=cadence, report_currency=report_currency, max_pages=max_pages)


@server.tool(name="harvest_list_projects", title="List projects", description="All projects under HARVEST_HOME.", annotations=RO)
async def harvest_list_projects() -> CallToolResult:
    return await _call(service.list_projects)


@server.tool(name="harvest_census_plan", title="Census plan", description="Per region: name, languages, currency, ccTLD and search queries for every discovery angle (classifieds, marketplaces, vertical portals, aggregators, official/government, operators, local-language, API platforms), plus the evidence rules.", annotations=RO)
async def harvest_census_plan(project: str) -> CallToolResult:
    return await _call(service.census_plan, project)


@server.tool(name="harvest_census_add", title="Add census candidates", description="Record candidate sources. Each: {url, name, regions, angle, kind, notes, evidence: [{url (on the site's own domain), observation (the inventory you saw)}], mirror_of?, blocked?}. Sites you could not open (403, captcha, login) go in with blocked=true and the block as the observation; they are recorded outside the budget and never collected unless lane detection finds them public. Dedups by registrable domain (merges regions/evidence), rejects mirrors and evidence-less rows. round_label names the round (angle:<a>:<region>, critic:rN, audit:rN).", annotations=WRITE)
async def harvest_census_add(project: str, candidates: list[dict[str, Any]], round_label: str = "manual") -> CallToolResult:
    return await _call(service.census_add, project, candidates, round_label)


@server.tool(name="harvest_census_gaps", title="Census gaps", description="Coverage matrix (region x angle), empty cells, census rounds, dry streak and the next step. Loop the completeness critic until two dry rounds, then run the adversarial audit.", annotations=RO)
async def harvest_census_gaps(project: str) -> CallToolResult:
    return await _call(service.census_gaps, project)


@server.tool(name="harvest_census_resume", title="Resume a census", description="When harvest_census_gaps reports budget_reached: raise max_sources, re-add the candidates that were deferred by the budget (already verified), and get a continuation brief listing covered cells and known domains so the next census round searches only what is still empty. start_agent=true also queues a census agent with that brief.", annotations=WRITE)
async def harvest_census_resume(project: str, max_sources: int, start_agent: bool = False) -> CallToolResult:
    return await _call(service.census_resume, project, max_sources, start_agent)


@server.tool(name="harvest_list_sources", title="List sources", description="The source registry (status, lane, robots/terms status, review verdict, records, last run). With source_id: the full row incl. evidence, lane signals, recent runs and the latest review report.", annotations=RO)
async def harvest_list_sources(project: str, status: str | None = None, source_id: str | None = None) -> CallToolResult:
    return await _call(service.list_sources, project, status, source_id)


@server.tool(name="harvest_detect_lane", title="Detect lane", description="Probe a source (or every undetected candidate): robots.txt, terms page, configured MCP servers, OpenAPI, feeds, sitemaps, embedded JSON (Next/Nuxt/JSON-LD), HTML or headless browser. First match wins; robots or terms that forbid collection, logins, captchas and paywalls give lane none.", annotations=WRITE_NET)
async def harvest_detect_lane(project: str, source_id: str | None = None) -> CallToolResult:
    return await _call(service.detect_lane, project, source_id)


@server.tool(name="harvest_record_policy", title="Record a policy verdict", description="Record a terms verdict you read yourself (terms_status allowed|no_clause|forbids with terms_url and a <=25-word clause) or override the lane. A forbids verdict or robots disallow always forces lane none.", annotations=WRITE)
async def harvest_record_policy(project: str, source_id: str, terms_status: str | None = None, terms_url: str | None = None,
                                terms_clause: str | None = None, lane: str | None = None, reason: str | None = None) -> CallToolResult:
    return await _call(service.record_policy, project, source_id, terms_status, terms_url, terms_clause, lane, reason)


@server.tool(name="harvest_probe_url", title="Fetch a page politely", description="GET one URL through harvest's polite, robots-checked client and return the status, meta, JSON-LD types, Next.js keys, links and the HTML (a max_chars window from offset; next_offset pages on) or JSON. grep=<regex> (at most 300 characters, matched within a 3 s limit) returns snippets around each match, e.g. grep='data-listing-id' to read listing cards deep in a large page.", annotations=RO_NET)
async def harvest_probe_url(url: str, max_chars: int = 20000, accept: str = "html", offset: int = 0, grep: str | None = None,
                            context: int = 1500) -> CallToolResult:
    return await _call(service.probe_url, url, max_chars, accept, offset, grep, context)


@server.tool(name="harvest_template_scraper", title="Scraper template", description="Write the starting scraper module for a source's lane (sources/<id>.py) and return the fetch(page, *, http, ctx) contract, the template fields and the code. Set overwrite=true to reset an existing module.", annotations=WRITE)
async def harvest_template_scraper(project: str, source_id: str, overwrite: bool = False) -> CallToolResult:
    return await _call(service.template_scraper, project, source_id, overwrite)


@server.tool(name="harvest_review_source", title="Review gate", description="Mandatory gate before enabling: compile + AST policy lint (no subprocess/os.system/eval/raw sockets/direct HTTP clients), a sandboxed page-1 fetch with a timeout, and schema validation against the template. The verdict is bound to the module's sha256.", annotations=WRITE_NET)
async def harvest_review_source(project: str, source_id: str, timeout_s: int | None = None, pages: int = 1) -> CallToolResult:
    return await _call(service.review_source, project, source_id, timeout_s, pages)


@server.tool(name="harvest_enable_source", title="Enable a source", description="Enable (or disable) collection. Refused unless the lane is not none, robots allow, terms are allowed/no_clause, and the current module sha has a passing review.", annotations=WRITE)
async def harvest_enable_source(project: str, source_id: str, enabled: bool = True) -> CallToolResult:
    return await _call(service.enable_source, project, source_id, enabled)


@server.tool(name="harvest_update_source", title="Tune a source", description="Set a source's field_map ({template_field: raw.dotted.path}), max_pages, time_budget_s, cadence_hours, rate_s, notes, or a new entry url on the same site (resets its lane and review). The proxy route is not a knob: see harvest_proxy_enable.", annotations=WRITE)
async def harvest_update_source(project: str, source_id: str, field_map: dict[str, str] | None = None, max_pages: int | None = None,
                                time_budget_s: int | None = None, cadence_hours: float | None = None, rate_s: float | None = None,
                                notes: str | None = None, url: str | None = None) -> CallToolResult:
    kw = {k: v for k, v in dict(field_map=field_map, max_pages=max_pages, time_budget_s=time_budget_s, cadence_hours=cadence_hours, rate_s=rate_s,
                                   notes=notes, url=url).items() if v is not None}
    return await _call(service.update_source, project, source_id, **kw)


@server.tool(name="harvest_proxy_status", title="Residential-proxy status", description="The residential-proxy pool (configured, size, cooling exits; never its contents or credentials). With project: the operator decision, daily byte/request caps, today's usage and usage per source. check=N live-probes N random exits (<= 5).", annotations=RO_NET)
async def harvest_proxy_status(project: str | None = None, check: int = 0) -> CallToolResult:
    return await _call(service.proxy_status, project, check)


@server.tool(name="harvest_proxy_enable", title="Switch the proxy route on", description="OPERATOR DECISION: switch the residential-proxy route on for a project or one source (source_id), recording the reason and date. The route is then used only when robots and the terms verdict allow collection AND the direct request met an IP-level block (401/403 without a challenge page, a connection reset, a persistent 429) - never for captchas, bot challenges, login walls, paywalls or signed-in sessions. Optionally set the project's daily_bytes / daily_requests caps, mode (sticky|rotate) and country (auto|off|CC).", annotations=WRITE)
async def harvest_proxy_enable(project: str, reason: str, source_id: str | None = None, daily_bytes: int | None = None,
                               daily_requests: int | None = None, mode: str | None = None, country: str | None = None) -> CallToolResult:
    return await _call(service.proxy_enable, project, reason, source_id, daily_bytes, daily_requests, mode, country, "mcp")


@server.tool(name="harvest_ua_status", title="Browser user-agent mode status", description="The browser user-agent decision for a project and its sources, the header set it sends, and the decision history.", annotations=RO)
async def harvest_ua_status(project: str) -> CallToolResult:
    return await _call(service.ua_status, project)


@server.tool(name="harvest_ua_enable", title="Switch the browser user agent on", description="OPERATOR DECISION: send a current desktop Chrome user agent (and matching Accept/Accept-Language) instead of harvest's own, for a project or one source, recording the reason and date. Only the UA header changes: no fingerprint spoofing, no challenge or captcha solving, no login or paywall work-arounds; challenge pages still stop the source; robots.txt is checked for harvest's token and for *, the stricter wins; terms must allow collection.", annotations=WRITE)
async def harvest_ua_enable(project: str, reason: str, source_id: str | None = None) -> CallToolResult:
    return await _call(service.ua_enable, project, reason, source_id, "mcp")


@server.tool(name="harvest_ua_disable", title="Switch the browser user agent off", description="Back to harvest's own user agent for a project or one source (recorded with the date and optional reason).", annotations=WRITE)
async def harvest_ua_disable(project: str, source_id: str | None = None, reason: str | None = None) -> CallToolResult:
    return await _call(service.ua_disable, project, source_id, reason, "mcp")


@server.tool(name="harvest_proxy_disable", title="Switch the proxy route off", description="Switch the residential-proxy route off for a project or one source (recorded with the date and optional reason).", annotations=WRITE)
async def harvest_proxy_disable(project: str, source_id: str | None = None, reason: str | None = None) -> CallToolResult:
    return await _call(service.proxy_disable, project, source_id, reason, "mcp")


@server.tool(name="harvest_run", title="Run collection", description="Walk enabled sources (all, the given ids, or only those due by cadence), normalise, dedup and store. Never wipes a source on a partial walk.", annotations=WRITE_NET)
async def harvest_run(project: str, source_ids: list[str] | None = None, due: bool = False, max_pages: int | None = None,
                      use_proxy: bool = True, use_browser_ua: bool = True) -> CallToolResult:
    return await _call(service.run, project, source_ids, due, max_pages, "manual", use_proxy, use_browser_ua)


@server.tool(name="harvest_status", title="Project status", description="Sources by status and lane, records, cross-source duplicates, quarantine size, recent runs, alerts, jobs and next due times.", annotations=RO)
async def harvest_status(project: str) -> CallToolResult:
    return await _call(service.status, project)


@server.tool(name="harvest_quarantine", title="Quarantined rows", description="Rows that failed normalisation or sanity checks, with the reasons.", annotations=RO)
async def harvest_quarantine(project: str, source_id: str | None = None, limit: int = 50) -> CallToolResult:
    return await _call(service.quarantine, project, source_id, limit)


@server.tool(name="harvest_query", title="Query records", description="Query stored records. filters: {field: value | {eq|ne|gt|gte|lt|lte|contains|in|exists: v}}; order_by a field; distinct=true keeps one row per cross-source duplicate group.", annotations=RO)
async def harvest_query(project: str, filters: dict[str, Any] | None = None, fields: list[str] | None = None, source_id: str | None = None,
                        order_by: str | None = None, desc: bool = True, limit: int = 50, offset: int = 0, distinct: bool = False) -> CallToolResult:
    return await _call(service.query, project, filters, fields, source_id, order_by, desc, limit, offset, distinct)


@server.tool(name="harvest_export", title="Export records", description="Export to CSV, JSONL or Parquet (Parquet needs pyarrow). Returns the file path. CSV cells starting with = + - @ (or a tab/CR) get a leading ' so spreadsheets do not evaluate them; raw=true writes them unchanged.", annotations=WRITE)
async def harvest_export(project: str, format: str = "csv", path: str | None = None, filters: dict[str, Any] | None = None, distinct: bool = False,
                         raw: bool = False) -> CallToolResult:
    return await _call(service.export, project, format, path, filters, distinct, raw)


@server.tool(name="harvest_schedule", title="Schedule", description="Write cron and/or systemd timer snippets that run `harvest run <project> --due` hourly (per-source cadence decides) plus the watchdog. Nothing is installed.", annotations=WRITE)
async def harvest_schedule(project: str, kind: str = "both", cadence: str | None = None) -> CallToolResult:
    return await _call(service.schedule, project, kind, cadence)


@server.tool(name="harvest_watchdog", title="Watchdog", description="Detect zero, degraded, stuck, stale and failing sources and raise alerts. dispatch=true launches repair agents only when HARVEST_REPAIR_DISPATCH=on (explicit tool allowlist, budget and cooldown).", annotations=WRITE)
async def harvest_watchdog(project: str, dispatch: bool = False) -> CallToolResult:
    return await _call(service.run_watchdog, project, dispatch)


@server.tool(name="harvest_set_fx", title="Set FX rates", description="Set the project's FX table: units of each currency per 1 base unit (default base USD). Used for *_report amounts and USD sanity bounds.", annotations=WRITE)
async def harvest_set_fx(project: str, rates: dict[str, float], base: str = "USD", as_of: str | None = None) -> CallToolResult:
    return await _call(service.set_fx, project, rates, base, as_of)


@server.tool(name="harvest_agent_brief", title="Agent brief", description="The full instructions for a census, audit, build or repair agent on this project (and the tool allowlist a headless run gets).", annotations=RO)
async def harvest_agent_brief(project: str, kind: str, source_id: str | None = None) -> CallToolResult:
    return await _call(service.agent_brief, project, kind, source_id)


@server.tool(name="harvest_jobs", title="Background jobs", description="Background jobs (agent runs, lane detection, reviews, runs) and their results.", annotations=RO)
async def harvest_jobs(project: str, job_id: str | None = None) -> CallToolResult:
    return await _call(service.jobs, project, job_id)


def main() -> None:
    if "--http" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8091
        asyncio.run(server.run_streamable_http_async(host="127.0.0.1", port=port, stateless_http=True))
    else:
        asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
