"""The core API. The MCP server, the CLI and the web app are thin wrappers over these functions;
every function takes plain arguments and returns JSON-serialisable dicts. Errors are raised as
ValueError (bad input), LookupError (unknown project/source) or RuntimeError (environment)."""

from __future__ import annotations

import json
from typing import Any

from . import agents, census, extract, lanes, proxy, regions, runner, scaffold, scheduler, templates, uamode, watchdog
from . import export as export_mod
from . import jobs as jobs_mod
from . import project as project_mod
from . import review as review_mod
from .http import Http


def _p(name: str) -> project_mod.Project:
    return project_mod.load(name)


def _jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str, ensure_ascii=False))


# ------------------------------------------------------------------ projects
def list_templates() -> dict:
    return {"templates": templates.listing(), "region_groups": {k: v for k, v in regions.GROUPS.items()}}


def get_template(record_type: str) -> dict:
    t = templates.get(record_type)
    return {"record_type": t["record_type"], "label": t["label"], "fields": t["fields"], "periods": t.get("periods") or [],
            "dedup": t.get("dedup") or [], "conflicts": [c["reason"] for c in t.get("conflicts") or []], "columns": templates.output_columns(t)}


def new_project(name: str, target: str, regions: list[str] | str, record_type: str, fields: list | dict | None = None,
                languages: list[str] | None = None, max_sources: int = 30, cadence: str = "daily", report_currency: str = "USD",
                store_dsn: str | None = None, max_pages: int = 20, time_budget_s: int = 900, rate_s: float = 2.0,
                prune_after_days: float = 14) -> dict:
    p = project_mod.create(name=name, target=target, regions=regions, record_type=record_type, fields=fields or [], languages=languages or [],
                           max_sources=max_sources, cadence=cadence, report_currency=report_currency, store_dsn=store_dsn, max_pages=max_pages,
                           time_budget_s=time_budget_s, rate_s=rate_s, prune_after_days=prune_after_days)
    return {"project": p.to_dict(), "template": get_template(p.spec.record_type)["fields"],
            "next": "harvest_census_plan, then add candidates with harvest_census_add"}


def list_projects() -> dict:
    return {"projects": project_mod.list_projects()}


def get_project(name: str) -> dict:
    return {"project": _p(name).to_dict()}


def set_fx(project: str, rates: dict, base: str = "USD", as_of: str | None = None) -> dict:
    return _p(project).set_fx(rates, base, as_of)


# ------------------------------------------------------------------ census
def census_plan(project: str) -> dict:
    return census.plan(_p(project))


def census_add(project: str, candidates: list[dict], round_label: str = "manual") -> dict:
    return census.add(_p(project), candidates, round_label)


def census_gaps(project: str) -> dict:
    return census.gaps(_p(project))


def census_resume(project: str, max_sources: int, start_agent: bool = False, cli: str | None = None, model: str | None = None,
                  wait: bool = False) -> dict:
    """Raise the budget of a census that hit it, re-add deferred candidates and (optionally) start a census agent
    briefed with what is already covered. `wait=True` runs the agent here and returns its outcome."""
    res = census.resume(_p(project), max_sources)
    if start_agent and res.get("budget_reached"):
        # the re-added deferred candidates already fill the new budget: an agent could only defer what it verifies
        res["job"] = {"status": "skipped", "reason": f"the deferred candidates filled max_sources={max_sources} again "
                      f"({len(res['still_deferred'])} still deferred); raise the budget further to search for more"}
    elif start_agent:
        res["job"] = start_job(project, "agent:census", {"extra": res["continuation"], "cli": cli, "model": model}, wait=wait)
    return _jsonable(res)


def reject_source(project: str, source_id: str, reason: str) -> dict:
    return census.reject(_p(project), source_id, reason)


def _source_view(p: project_mod.Project, s: dict, detail: bool = False) -> dict:
    keys = ["id", "name", "url", "domain", "regions", "angles", "status", "lane", "robots_status", "terms_status", "terms_url", "enabled",
            "review_verdict", "max_pages", "baseline"]
    d = {k: s.get(k) for k in keys}
    d["enabled"] = bool(s.get("enabled"))
    ld = s.get("lane_detail") or {}
    # switched_on is gate (a), the operator decision; eligible/why add gate (b), so a UI can say when a decision has no effect
    px_ok, px_why = proxy.eligible(p, s, "run")
    ua_ok, ua_why = uamode.eligible(p, s, "run")
    d["proxy"] = {"switched_on": proxy.switched_on(p, s)[0], "decision": ld.get("proxy"), "via_proxy": bool(ld.get("via_proxy")),
                  "ip_block": bool(ld.get("ip_block")), "eligible": px_ok, "why": px_why}
    d["ua"] = {"switched_on": uamode.switched_on(p, s)[0], "decision": ld.get("ua"), "ua_mode": "browser" if ua_ok else "own", "eligible": ua_ok, "why": ua_why}
    d["lane_reason"] = (s.get("lane_detail") or {}).get("reason")
    d["module_exists"] = p.module_path(s["id"]).is_file()
    last = p.store.last_finished_run(p.name, s["id"])
    d["last_run"] = {k: last.get(k) for k in ("status", "started_at", "rows_stored", "complete", "write_mode", "error")} if last else None
    d["records"] = p.store.count_records(p.name, s["id"])
    if detail:
        d.update({k: s.get(k) for k in ("evidence", "lane_detail", "terms_clause", "notes", "field_map", "kind", "reviewed_sha", "module_sha")})
        d["next_due"] = scheduler.next_due(p, s) if s.get("enabled") else None
        d["runs"] = p.store.list_runs(p.name, s["id"], limit=5)
        rv = p.store.latest_review(p.name, s["id"])
        d["latest_review"] = {"verdict": rv["verdict"], "created_at": rv["created_at"], "report": rv["report"]} if rv else None
    return d


def list_sources(project: str, status: str | None = None, source_id: str | None = None) -> dict:
    p = _p(project)
    if source_id:
        s = p.store.get_source(p.name, source_id)
        if not s:
            raise LookupError(f"no source {source_id}")
        return {"source": _jsonable(_source_view(p, s, detail=True))}
    rows = p.store.list_sources(p.name, status=status)
    return {"project": p.name, "count": len(rows), "sources": _jsonable([_source_view(p, s) for s in rows])}


def update_source(project: str, source_id: str, **fields) -> dict:
    """Operator knobs: field_map, max_pages, time_budget_s, cadence_hours, rate_s, notes, name, url.
    The proxy route is not a knob: it needs an operator decision with a reason (`proxy_enable`)."""
    allowed = {"field_map", "max_pages", "time_budget_s", "cadence_hours", "rate_s", "notes", "name", "url"}
    if "ua_mode" in fields or "user_agent" in fields:
        raise ValueError("the browser user-agent mode needs a recorded operator decision: harvest ua enable <project> --source <id> --reason '...'")
    if "use_proxy" in fields:
        raise ValueError("the residential-proxy route needs a recorded operator decision: harvest proxy enable <project> --source <id> --reason '...'")
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot set {sorted(bad)} here; allowed: {sorted(allowed)}")
    p = _p(project)
    if not p.store.get_source(p.name, source_id):
        raise LookupError(f"no source {source_id}")
    if fields.get("url"):
        from .census import domain_key
        cur = p.store.get_source(p.name, source_id)
        if not str(fields["url"]).startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        if domain_key(fields["url"]) != cur["domain"]:
            raise ValueError(f"the new URL must stay on {cur['domain']}; add a different site through the census")
        # a new entry URL needs a new lane detection and a new review before anything runs again
        fields.update(status="candidate", lane=None, enabled=0, reviewed_sha=None, review_verdict=None)
    if "cadence_hours" in fields and fields["cadence_hours"] is not None:
        fields["cadence_hours"] = project_mod.cadence_hours(fields["cadence_hours"])
    p.store.upsert_source(p.name, source_id, {k: v for k, v in fields.items()})
    return list_sources(project, source_id=source_id)


# ------------------------------------------------------------------ lanes / policy
def detect_lane(project: str, source_id: str | None = None, use_mcp: bool = True, use_proxy: bool = True, use_browser_ua: bool = True) -> dict:
    """Detect lanes. A source the operator switched the proxy route on for (and whose robots/terms allow it) is
    probed with the route armed: it is used only if the direct probe meets an IP-level block (`via_proxy`)."""
    p = _p(project)
    ids = [source_id] if source_id else [s["id"] for s in p.store.list_sources(p.name) if s.get("status") in ("candidate", "blocked", None)]
    http = Http(rate_s=min(p.spec.rate_s, 2.0))
    from .locks import SourceBusy

    def one(sid: str) -> dict:
        src = p.store.get_source(p.name, sid)
        if src is None:
            raise LookupError(f"no source {sid}")
        ua = uamode.mode_for(p, src, "detect", use_browser_ua)
        h2, lease = proxy.detect_http(p, src, rate_s=min(p.spec.rate_s, 2.0), enabled=use_proxy, ua_mode=ua)
        if lease is None and ua == "browser":
            try:
                return _jsonable(lanes.detect_for_source(p, sid, http=h2, use_mcp=use_mcp))
            finally:
                h2.close()
        if lease is None:
            h2.close()
            return _jsonable(lanes.detect_for_source(p, sid, http=http, use_mcp=use_mcp))
        try:
            res = _jsonable(lanes.detect_for_source(p, sid, http=h2, use_mcp=use_mcp))
        finally:
            h2.close()
            rep = proxy.settle(p, lease, lease.route.report()) or {}
        res["proxy"] = {"armed": True, "requests": rep.get("requests", 0), "bytes": rep.get("bytes", 0), "budget_stop": bool(rep.get("budget_stop"))}
        return res

    out = []
    try:
        for sid in ids:
            try:
                out.append(one(sid))
            except SourceBusy as exc:
                if source_id:
                    raise
                out.append({"id": sid, "busy": True, "error": str(exc)})
    finally:
        http.close()
    return out[0] if source_id else {"detected": out, "count": len(out)}


def record_policy(project: str, source_id: str, terms_status: str | None = None, terms_url: str | None = None, terms_clause: str | None = None,
                  lane: str | None = None, reason: str | None = None, actor: str = "operator") -> dict:
    s = lanes.record_policy(_p(project), source_id, terms_status=terms_status, terms_url=terms_url, terms_clause=terms_clause, lane=lane, reason=reason,
                            actor=actor)
    return {"source": {k: s.get(k) for k in ("id", "lane", "status", "terms_status", "terms_url", "terms_clause", "robots_status")}}


from .grepsafe import grep_spans  # noqa: E402  (re-exported: probe_url's bounded regex search)


def probe_url(url: str, max_chars: int = 20000, accept: str = "html", offset: int = 0, grep: str | None = None, context: int = 1500) -> dict:
    """Fetch one page through the polite client (robots-checked) so an agent can read raw markup.
    `offset` pages through long documents; `grep` (a regex) returns up to 8 snippets of +-`context` characters
    around its matches, so listing cards deep in a large page can be read without dumping it all."""
    http = Http(rate_s=1.0)
    try:
        r = http.get(url, accept=accept)
        if r is None:
            return {"url": url, "ok": False, "events": http.events[-5:]}
        text = r.text
        info: dict[str, Any] = {"url": r.url, "ok": True, "status": r.status, "content_type": r.headers.get("content-type"), "bytes": len(text)}
        if "json" in (r.headers.get("content-type") or ""):
            info["json_preview"] = text[:max_chars]
        else:
            nd = extract.next_data(text)
            info["meta"] = extract.meta(text)
            info["jsonld_types"] = extract.jsonld_types(text)
            info["next_data_keys"] = list((nd or {}).get("props", {}).get("pageProps", {}).keys())[:40] if isinstance(nd, dict) else None
            info["links_sample"] = extract.links(text, r.url)[:40]
            body = extract.visible_text(text)
            info["visible_text"] = body[: max_chars // 4]
            info["html"] = text[offset: offset + max_chars]
            info["html_offset"] = offset
            info["html_total"] = len(text)
            if offset + max_chars < len(text):
                info["next_offset"] = offset + max_chars
        if grep:
            spans, total, capped = grep_spans(grep, text)
            context = max(0, min(int(context), 5000))
            info["grep"] = {"pattern": grep, "matches": total, **({"matches_capped": True} if capped else {}),
                            "snippets": [{"at": a, "text": text[max(0, a - context): b + context]} for a, b in spans]}
        return info
    finally:
        http.close()


# ------------------------------------------------------------------ scrapers
def template_scraper(project: str, source_id: str, overwrite: bool = False) -> dict:
    return scaffold.template(_p(project), source_id, overwrite=overwrite)


def review_source(project: str, source_id: str, timeout_s: int | None = None, pages: int = 1, use_proxy: bool = True,
                  use_browser_ua: bool = True) -> dict:
    return _jsonable(review_mod.review(_p(project), source_id, timeout_s=timeout_s, pages=pages, use_proxy=use_proxy, use_browser_ua=use_browser_ua))


def enable_source(project: str, source_id: str, enabled: bool = True) -> dict:
    return review_mod.enable(_p(project), source_id, enabled)


# ------------------------------------------------------------------ run / status / data
def run(project: str, source_ids: list[str] | None = None, due: bool = False, max_pages: int | None = None, trigger: str = "manual",
        use_proxy: bool = True, use_browser_ua: bool = True, cancel=None) -> dict:
    return _jsonable(runner.run(_p(project), source_ids, due=due, trigger=trigger, max_pages=max_pages, use_proxy=use_proxy,
                                use_browser_ua=use_browser_ua, cancel=cancel))


# ------------------------------------------------------------------ browser user-agent mode
def ua_status(project: str) -> dict:
    """The browser user-agent decision (project and per source), the header set it sends, and its history."""
    return _jsonable(uamode.summary(_p(project)))


def ua_enable(project: str, reason: str, source_id: str | None = None, by: str = "operator") -> dict:
    """Record an operator decision switching the browser user-agent mode on for a project or one source."""
    return _jsonable(uamode.enable(_p(project), reason=reason, source_id=source_id, by=by))


def ua_disable(project: str, source_id: str | None = None, reason: str | None = None, by: str = "operator") -> dict:
    return _jsonable(uamode.disable(_p(project), source_id=source_id, reason=reason, by=by))


# ------------------------------------------------------------------ session headers (operator decision, per source)
def session_headers_enable(project: str, source_id: str, reason: str, by: str = "operator") -> dict:
    """Allow one source's module to send Cookie / Authorization (e.g. a published public API key). Refused by default."""
    from . import sessionhdr
    return _jsonable(sessionhdr.enable(_p(project), source_id, reason=reason, by=by))


def session_headers_disable(project: str, source_id: str, reason: str | None = None, by: str = "operator") -> dict:
    from . import sessionhdr
    return _jsonable(sessionhdr.disable(_p(project), source_id, reason=reason, by=by))


# ------------------------------------------------------------------ residential-proxy route
def proxy_status(project: str | None = None, check: int = 0) -> dict:
    """The pool (size, cooling exits; never its contents) and, with a project, its decision, caps and usage."""
    out: dict[str, Any] = {"pool": proxy.pool_status(check=max(0, min(int(check or 0), 5)))}
    if project:
        out["project"] = proxy.summary(_p(project))
    return _jsonable(out)


def proxy_enable(project: str, reason: str, source_id: str | None = None, daily_bytes: int | None = None, daily_requests: int | None = None,
                 mode: str | None = None, country: str | None = None, by: str = "operator") -> dict:
    """Record an operator decision switching the proxy route on for a project (or one source), with the reason, and
    optionally set the project's daily byte/request caps, mode (sticky|rotate) and country (auto|off|CC)."""
    return _jsonable(proxy.enable(_p(project), reason=reason, source_id=source_id, by=by, daily_bytes=daily_bytes,
                                  daily_requests=daily_requests, mode=mode, country=country))


def proxy_disable(project: str, source_id: str | None = None, reason: str | None = None, by: str = "operator") -> dict:
    return _jsonable(proxy.disable(_p(project), source_id=source_id, reason=reason, by=by))


def status(project: str) -> dict:
    p = _p(project)
    s = p.store
    sources = s.list_sources(p.name)
    by_status: dict[str, int] = {}
    by_lane: dict[str, int] = {}
    for x in sources:
        by_status[x.get("status") or "?"] = by_status.get(x.get("status") or "?", 0) + 1
        by_lane[x.get("lane") or "undetected"] = by_lane.get(x.get("lane") or "undetected", 0) + 1
    dup = s.one("SELECT COUNT(*) AS n FROM (SELECT fingerprint FROM records WHERE project = ? AND fingerprint IS NOT NULL "
                "GROUP BY fingerprint HAVING COUNT(DISTINCT source_id) > 1) t", (p.name,))["n"]
    q = s.one("SELECT COUNT(*) AS n FROM quarantine WHERE project = ?", (p.name,))["n"]
    g = census.gaps(p)
    pu = {u["source_id"]: {"requests": u["requests"], "bytes": u["bytes"]} for u in s.proxy_usage(p.name, since=proxy.today())}
    return _jsonable({"project": p.to_dict(), "sources": len(sources), "by_status": by_status, "by_lane": by_lane,
                      "enabled": sum(1 for x in sources if x.get("enabled")), "records": s.count_records(p.name), "cross_source_duplicates": dup,
                      "quarantined": q, "recent_runs": s.list_runs(p.name, limit=15), "alerts": s.list_alerts(p.name, 10),
                      "jobs": s.list_jobs(p.name, 10), "census": g["rounds"][-5:],
                      "census_state": {"budget_reached": g["budget_reached"], "saturated": g["saturated"], "deferred": len(g["deferred"]),
                                       "empty_cells": len(g["empty_cells"]), "max_sources": p.spec.max_sources},
                      "proxy": {k: v for k, v in proxy.summary(p).items() if k not in ("history", "usage_by_day")},
                      "ua": {k: v for k, v in uamode.summary(p).items() if k != "history"},
                      "per_source": [{"id": x["id"], "status": x.get("status"), "lane": x.get("lane"), "enabled": bool(x.get("enabled")),
                                      "records": s.count_records(p.name, x["id"]),
                                      "next_due": scheduler.next_due(p, x) if x.get("enabled") else None,
                                      "via_proxy": bool((x.get("lane_detail") or {}).get("via_proxy")),
                                      "proxy_today": pu.get(x["id"]), "ua_mode": uamode.mode_for(p, x, "run")} for x in sources]})


MAX_LIST = 1000


def _clamp(limit) -> int:
    return max(1, min(int(limit), MAX_LIST))


def runs(project: str, source_id: str | None = None, limit: int = 50) -> dict:
    p = _p(project)
    return _jsonable({"runs": p.store.list_runs(p.name, source_id, _clamp(limit))})


def quarantine(project: str, source_id: str | None = None, limit: int = 100) -> dict:
    p = _p(project)
    return _jsonable({"quarantine": p.store.list_quarantine(p.name, source_id, _clamp(limit))})


def query(project: str, filters: dict | None = None, fields: list[str] | None = None, source_id: str | None = None, order_by: str | None = None,
          desc: bool = True, limit: int = 100, offset: int = 0, distinct: bool = False) -> dict:
    return _jsonable(export_mod.query(_p(project), filters=filters, fields=fields, source_id=source_id, order_by=order_by, desc=desc,
                                      limit=max(1, min(int(limit), 5000)), offset=max(0, int(offset)), distinct=distinct))


def export(project: str, format: str = "csv", path: str | None = None, filters: dict | None = None, distinct: bool = False,
           raw: bool = False) -> dict:
    """raw=True writes CSV cells unchanged (no spreadsheet-formula neutralising); JSONL and Parquet are always unchanged."""
    return export_mod.export(_p(project), format, path, raw=raw, filters=filters, distinct=distinct)


def schedule(project: str, kind: str = "both", cadence: str | None = None, user: str | None = None) -> dict:
    return scheduler.generate(_p(project), kind=kind, cadence=cadence, user=user)


def run_watchdog(project: str, dispatch: bool = False) -> dict:
    return _jsonable(watchdog.run(_p(project), dispatch=dispatch))


# ------------------------------------------------------------------ agents and jobs
def agent_brief(project: str, kind: str, source_id: str | None = None) -> dict:
    p = _p(project)
    return {"kind": kind, "brief": agents.brief(kind, p, source_id=source_id),
            "allowed_tools": agents.allowed_tools(kind, p, source_id) if (kind in ("census", "audit") or source_id) else None}


def agent_command(project: str, kind: str, source_id: str | None = None, cli: str | None = None, model: str | None = None) -> dict:
    argv = agents.build_command(kind, _p(project), source_id, cli=cli, model=model)
    return {"argv": [a if len(a) < 400 else a[:120] + f"... <{len(a)} chars>" for a in argv]}


JOB_KINDS = jobs_mod.KINDS


def start_job(project: str, kind: str, params: dict | None = None, wait: bool = False, mode: str | None = None) -> dict:
    """Submit background work (census/audit/build/repair agents, lane detection, reviews, runs, watchdog).
    Queue mode (default) enqueues for `harvest worker`; inline mode runs it in this process; wait=True runs it now."""
    return _jsonable(jobs_mod.submit(_p(project), kind, params, wait=wait, mode=mode))


def jobs(project: str, job_id: str | None = None) -> dict:
    p = _p(project)
    jobs_mod.reap(p)
    if job_id:
        j = p.store.get_job(job_id)
        if not j or j.get("project") != p.name:  # job ids are global; a project's API path shows only its own jobs
            raise LookupError(f"no job {job_id}")
        return _jsonable({"job": j})
    return _jsonable({"jobs": p.store.list_jobs(p.name)})
