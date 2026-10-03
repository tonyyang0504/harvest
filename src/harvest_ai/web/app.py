"""harvest web app: a JSON API over `harvest_ai.service` plus a small single-page frontend.

Run: `harvest web --host 0.0.0.0 --port 8080` (or `uvicorn harvest_ai.web.app:create_app --factory`).
Long work (agent census/build jobs, lane detection, reviews, runs) is started as a background job;
the frontend polls the job and the project status to show progress.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .. import __version__, service
from ..project import home
from .auth import Principal, admin_token, authenticate


class ProjectIn(BaseModel):
    name: str
    target: str
    regions: list[str] | str
    record_type: str
    fields: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    max_sources: int = 30
    cadence: str = "daily"
    report_currency: str = "USD"
    max_pages: int = 20
    time_budget_s: int = 900
    rate_s: float = 2.0


class AgentJobIn(BaseModel):
    kind: str = "census"  # census | audit
    cli: str | None = None
    model: str | None = None
    extra: str = ""


class ResumeIn(BaseModel):
    max_sources: int
    start_agent: bool = False
    cli: str | None = None
    model: str | None = None


class CandidatesIn(BaseModel):
    candidates: list[dict[str, Any]]
    round_label: str = "manual"


class PolicyIn(BaseModel):
    terms_status: str | None = None
    terms_url: str | None = None
    terms_clause: str | None = None
    lane: str | None = None
    reason: str | None = None


class TuneIn(BaseModel):
    url: str | None = None
    field_map: dict[str, str] | None = None
    max_pages: int | None = None
    time_budget_s: int | None = None
    cadence_hours: float | str | None = None
    rate_s: float | None = None
    notes: str | None = None


class RunIn(BaseModel):
    source_ids: list[str] | None = None
    due: bool = False
    wait: bool = False
    use_proxy: bool = True
    use_browser_ua: bool = True


class UaIn(BaseModel):
    enabled: bool
    reason: str | None = None
    source_id: str | None = None


class SessionHeadersIn(BaseModel):
    enabled: bool
    reason: str | None = None


class ProxyIn(BaseModel):
    enabled: bool
    reason: str | None = None
    source_id: str | None = None
    daily_bytes: int | None = None
    daily_requests: int | None = None
    mode: str | None = None
    country: str | None = None


class ScheduleIn(BaseModel):
    kind: str = "both"
    cadence: str | None = None


class RejectIn(BaseModel):
    reason: str


class FxIn(BaseModel):
    rates: dict[str, float]
    base: str = "USD"


class BuildIn(BaseModel):
    cli: str | None = None
    model: str | None = None
    extra: str = ""


def _msg(exc: BaseException) -> str:
    """An error message for a client: the server's state directory is not the browser's business."""
    text = str(exc)
    h = str(home())
    return text.replace(h, "$HARVEST_HOME") if h and h != "/" else text


def _errors(app: FastAPI) -> None:
    @app.exception_handler(ValueError)
    async def bad(_: Request, exc: ValueError):
        return JSONResponse({"error": "invalid_input", "message": _msg(exc)}, status_code=400)

    @app.exception_handler(LookupError)
    async def missing(_: Request, exc: LookupError):
        return JSONResponse({"error": "not_found", "message": _msg(exc).strip("'\"")}, status_code=404)

    @app.exception_handler(FileNotFoundError)
    async def nofile(_: Request, exc: FileNotFoundError):
        return JSONResponse({"error": "not_found", "message": _msg(exc)}, status_code=404)

    @app.exception_handler(RuntimeError)
    async def env(_: Request, exc: RuntimeError):
        return JSONResponse({"error": "unavailable", "message": _msg(exc)}, status_code=409)


def api_router() -> APIRouter:
    r = APIRouter(prefix="/api", dependencies=[Depends(authenticate)])

    @r.get("/me")
    def me(principal: Principal = Depends(authenticate)):
        return {"name": principal.name, "role": principal.role}

    @r.get("/templates")
    def templates():
        return service.list_templates()

    @r.get("/templates/{record_type}")
    def template(record_type: str):
        return service.get_template(record_type)

    @r.get("/projects")
    def projects():
        return service.list_projects()

    @r.post("/projects", status_code=201)
    def create(body: ProjectIn):
        return service.new_project(**body.model_dump())

    @r.get("/projects/{p}")
    def status(p: str):
        return service.status(p)

    # census
    @r.get("/projects/{p}/census/plan")
    def plan(p: str):
        return service.census_plan(p)

    @r.post("/projects/{p}/census/run", status_code=202)
    def census_run(p: str, body: AgentJobIn):
        if body.kind not in ("census", "audit"):
            raise ValueError("kind: census | audit")
        return service.start_job(p, f"agent:{body.kind}", {"cli": body.cli, "model": body.model, "extra": body.extra})

    @r.post("/projects/{p}/census/resume")
    def census_resume(p: str, body: ResumeIn):
        return service.census_resume(p, body.max_sources, body.start_agent, body.cli, body.model)

    @r.post("/projects/{p}/census/candidates")
    def candidates(p: str, body: CandidatesIn):
        return service.census_add(p, body.candidates, body.round_label)

    @r.get("/projects/{p}/census/gaps")
    def gaps(p: str):
        return service.census_gaps(p)

    # sources
    @r.get("/projects/{p}/sources")
    def sources(p: str, status: str | None = None):
        return service.list_sources(p, status)

    @r.get("/projects/{p}/sources/{sid}")
    def source(p: str, sid: str):
        return service.list_sources(p, source_id=sid)

    @r.patch("/projects/{p}/sources/{sid}")
    def tune(p: str, sid: str, body: TuneIn):
        return service.update_source(p, sid, **{k: v for k, v in body.model_dump().items() if v is not None})

    @r.post("/projects/{p}/sources/detect", status_code=202)
    def detect_all(p: str, wait: bool = False):
        return service.start_job(p, "detect_lanes", {}, wait=wait)

    @r.post("/projects/{p}/sources/{sid}/detect", status_code=202)
    def detect_one(p: str, sid: str, wait: bool = False):
        return service.start_job(p, "detect_lanes", {"source_id": sid}, wait=wait)

    @r.post("/projects/{p}/sources/{sid}/policy")
    def policy(p: str, sid: str, body: PolicyIn):
        return service.record_policy(p, sid, **body.model_dump())

    @r.post("/projects/{p}/sources/{sid}/scaffold")
    def scaffold(p: str, sid: str, overwrite: bool = False):
        res = service.template_scraper(p, sid, overwrite)
        return {k: v for k, v in res.items() if k != "contract"}

    @r.post("/projects/{p}/sources/{sid}/build", status_code=202)
    def build(p: str, sid: str, body: BuildIn):
        return service.start_job(p, "agent:build", {"source_id": sid, "cli": body.cli, "model": body.model, "extra": body.extra})

    @r.post("/projects/{p}/sources/{sid}/review", status_code=202)
    def review(p: str, sid: str, wait: bool = False):
        return service.start_job(p, "review", {"source_id": sid}, wait=wait)

    @r.post("/projects/{p}/sources/{sid}/approve")
    def approve(p: str, sid: str):
        res = service.enable_source(p, sid, True)
        if res.get("refused"):
            return JSONResponse({"error": "refused", **res}, status_code=409)
        return res

    @r.post("/projects/{p}/sources/{sid}/disable")
    def disable(p: str, sid: str):
        return service.enable_source(p, sid, False)

    @r.post("/projects/{p}/sources/{sid}/reject")
    def reject(p: str, sid: str, body: RejectIn):
        return service.reject_source(p, sid, body.reason)

    # runs, schedule, health
    @r.post("/projects/{p}/run", status_code=202)
    def run(p: str, body: RunIn):
        return service.start_job(p, "run", {"source_ids": body.source_ids, "due": body.due, "use_proxy": body.use_proxy,
                                            "use_browser_ua": body.use_browser_ua}, wait=body.wait)

    # browser user-agent mode (operator decisions)
    @r.get("/projects/{p}/ua")
    def ua_project(p: str):
        return service.ua_status(p)

    @r.post("/projects/{p}/ua")
    def ua_set(p: str, body: UaIn, principal: Principal = Depends(authenticate)):
        by = f"web:{principal.name}"
        if body.enabled:
            return service.ua_enable(p, body.reason or "", body.source_id, by)
        return service.ua_disable(p, body.source_id, body.reason, by)

    # session headers on module requests (operator decision, per source; refused by default)
    @r.post("/projects/{p}/sources/{sid}/session-headers")
    def session_headers(p: str, sid: str, body: SessionHeadersIn, principal: Principal = Depends(authenticate)):
        by = f"web:{principal.name}"
        if body.enabled:
            return service.session_headers_enable(p, sid, body.reason or "", by)
        return service.session_headers_disable(p, sid, body.reason, by)

    # residential-proxy route (operator decisions)
    @r.get("/proxy")
    def proxy_pool(check: int = 0):
        return service.proxy_status(None, check)

    @r.get("/projects/{p}/proxy")
    def proxy_project(p: str):
        return service.proxy_status(p)

    @r.post("/projects/{p}/proxy")
    def proxy_set(p: str, body: ProxyIn, principal: Principal = Depends(authenticate)):
        by = f"web:{principal.name}"
        if body.enabled:
            return service.proxy_enable(p, body.reason or "", body.source_id, body.daily_bytes, body.daily_requests, body.mode, body.country, by)
        return service.proxy_disable(p, body.source_id, body.reason, by)

    @r.post("/projects/{p}/schedule")
    def schedule(p: str, body: ScheduleIn):
        return service.schedule(p, body.kind, body.cadence)

    @r.post("/projects/{p}/watchdog")
    def watchdog(p: str):
        return service.run_watchdog(p, False)

    @r.post("/projects/{p}/fx")
    def fx(p: str, body: FxIn):
        return service.set_fx(p, body.rates, body.base)

    @r.get("/projects/{p}/runs")
    def runs(p: str, source_id: str | None = None, limit: int = 50):
        return service.runs(p, source_id, limit)

    @r.get("/projects/{p}/quarantine")
    def quarantine(p: str, source_id: str | None = None, limit: int = 100):
        return service.quarantine(p, source_id, limit)

    @r.get("/projects/{p}/jobs")
    def jobs(p: str):
        return service.jobs(p)

    @r.get("/projects/{p}/jobs/{jid}")
    def job(p: str, jid: str):
        return service.jobs(p, jid)

    # data
    @r.get("/projects/{p}/records")
    def records(p: str, filters: str | None = None, fields: str | None = None, source_id: str | None = None, order_by: str | None = None,
                desc: bool = True, limit: int = 50, offset: int = 0, distinct: bool = False):
        try:
            flt = json.loads(filters) if filters else None
        except ValueError as exc:
            raise ValueError(f"filters must be JSON: {exc}") from None
        return service.query(p, flt, [f for f in (fields or "").split(",") if f] or None, source_id, order_by, desc, limit, offset, distinct)

    @r.get("/projects/{p}/export")
    def export(p: str, format: str = "csv", filters: str | None = None, distinct: bool = False, raw: bool = False):
        res = service.export(p, format, None, json.loads(filters) if filters else None, distinct, raw)
        media = {"csv": "text/csv", "jsonl": "application/x-ndjson", "parquet": "application/vnd.apache.parquet"}[format]
        return FileResponse(res["path"], media_type=media, filename=Path(res["path"]).name)

    return r


def create_app() -> FastAPI:
    app = FastAPI(title="harvest", version=__version__, description="regions + target -> discovered sources -> reviewed scrapers -> clean stored data")
    app.state.admin_token = admin_token()
    _errors(app)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            # the frontend renders scraped text; no inline script and no javascript: URLs can run in its origin,
            # which holds the admin token (/docs keeps FastAPI's own CDN-hosted Swagger UI and is not covered)
            resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                                    "form-action 'self'")
        return resp
    app.include_router(api_router())
    static = resources.files("harvest_ai.web").joinpath("static")

    @app.get("/api/health", include_in_schema=False)
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index():
        return HTMLResponse(static.joinpath("index.html").read_text(encoding="utf-8"))

    @app.get("/static/{name}", include_in_schema=False)
    def static_file(name: str):
        if name not in ("app.js", "app.css"):
            raise HTTPException(404)
        media = "text/javascript" if name.endswith(".js") else "text/css"
        return Response(static.joinpath(name).read_text(encoding="utf-8"), media_type=media)

    return app
