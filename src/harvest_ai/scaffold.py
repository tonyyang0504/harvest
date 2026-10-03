"""Scraper modules: the contract and a starting template per lane.

Contract (checked by the review gate):
    def fetch(page: int, *, http, ctx) -> list[dict]
      page  1-based; page 1 is the newest / first listing page
      http  harvest_ai.http.Http: get_text / get_json / post_json / render; returns None on block,
            robots denial or error. It is the only way out to the network (politeness, robots).
      ctx   {"source": {...registry row...}, "project": {...spec...}, "fields": {...template fields...},
             "region": "KZ", "currency": "KZT", "state": {}}  -- `state` persists across pages of one run
    Return a list of dicts keyed by template field names (raw strings are fine: the normaliser parses
    '1 250 000 ₸', '82,32 m²', '3 days ago'). Return [] when the page is empty or blocked; never raise
    for "no data", never sleep, never log in. To signal the last page explicitly return
    {"rows": [...], "done": True}.
Forbidden in modules (AST-checked): subprocess, os.system/popen/exec*/spawn*/fork, eval/exec/compile/
__import__, importlib, ctypes, socket, direct HTTP clients (requests, httpx, urllib.request, aiohttp).
"""

from __future__ import annotations

import json
import textwrap

from .project import Project

HEADER = '''"""{name} ({url}) -> {record_type} records.

Lane: {lane}. Generated from the harvest template on {date}; edit freely, then re-run the review
gate (`harvest review {project} {sid}`): any edit changes the module sha and disables collection
until a new review passes.
"""
from harvest_ai import extract  # noqa: F401  (parse_html, jsonld, next_data, feed_items, sitemap_urls, links)

SOURCE_URL = {url!r}
MAX_ROWS_PER_PAGE = 500

'''

BODIES = {
    "embedded_json": '''
def fetch(page, *, http, ctx):
    url = SOURCE_URL if page == 1 else f"{{SOURCE_URL}}?page={{page}}"  # TODO: the site's pagination parameter
    html = http.get_text(url)
    if not html:
        return []
    rows = []
    data = extract.next_data(html) or extract.nuxt_data(html)
    if data is not None:
        # TODO: pick the list of items out of the page state, e.g. data["props"]["pageProps"]["items"]
        for it in extract.find_items(data, ("id",))[:MAX_ROWS_PER_PAGE]:
            rows.append({{
{mapping}
            }})
        return rows
    for d in extract.jsonld(html):  # JSON-LD fallback (Product / Offer / Car / JobPosting / Event ...)
        items = [e.get("item", e) for e in d.get("itemListElement", [])] if d.get("@type") == "ItemList" else [d]
        for it in items:
            if not isinstance(it, dict):
                continue
            offer = it.get("offers") or {{}}
            if isinstance(offer, list):
                offer = offer[0] if offer else {{}}
            rows.append({{
                "source_id": it.get("sku") or it.get("@id") or it.get("url"),
                "url": it.get("url"),
                "title": it.get("name") or it.get("title"),
                "price": offer.get("price"),
                "currency": offer.get("priceCurrency"),
                "image": it.get("image") if isinstance(it.get("image"), str) else None,
            }})
    return rows[:MAX_ROWS_PER_PAGE]
''',
    "html": '''
def fetch(page, *, http, ctx):
    url = SOURCE_URL if page == 1 else f"{{SOURCE_URL}}?page={{page}}"  # TODO: the site's pagination parameter
    html = http.get_text(url)
    if not html:
        return []
    doc = extract.parse_html(html)
    rows = []
    for card in doc.find_all("article")[:MAX_ROWS_PER_PAGE]:  # TODO: the listing card selector, e.g. find_all("div", cls="listing")
        link = card.find("a")
        rows.append({{
            "source_id": card.get("data-id") or (link.get("href") if link else None),
            "url": link.get("href") if link else None,
            "title": (card.find("h2") or card).text,
{mapping_html}
        }})
    return rows
''',
    "feed": '''
FEED_URL = SOURCE_URL  # TODO: the feed URL found by lane detection

def fetch(page, *, http, ctx):
    if page > 1:  # most feeds have no pagination; return {{"rows": [...], "done": True}} otherwise
        return []
    text = http.get_text(FEED_URL, accept="xml")
    rows = []
    for it in extract.feed_items(text or "")[:MAX_ROWS_PER_PAGE]:
        rows.append({{
            "source_id": it.get("guid") or it.get("id") or it.get("link"),
            "url": it.get("link") or it.get("url"),
            "title": it.get("title"),
            "posted_at": it.get("pubdate") or it.get("published") or it.get("updated") or it.get("date_published"),
            "description": it.get("description") or it.get("summary") or it.get("content_text"),
        }})
    return {{"rows": rows, "done": True}}
''',
    "sitemap": '''
SITEMAP_URL = SOURCE_URL.rstrip("/") + "/sitemap.xml"  # TODO: the sitemap found by lane detection
PER_PAGE = 20  # detail pages fetched per harvest page (each is one polite request)

def fetch(page, *, http, ctx):
    state = ctx["state"]
    if "urls" not in state:
        pages, maps = extract.sitemap_urls(http.get_text(SITEMAP_URL, accept="xml") or "")
        for m in maps[:5]:
            more, _ = extract.sitemap_urls(http.get_text(m, accept="xml") or "")
            pages += more
        state["urls"] = list(pages)  # TODO: keep only item detail URLs, e.g. [u for u in pages if "/item/" in u]
    chunk = state["urls"][(page - 1) * PER_PAGE: page * PER_PAGE]
    if not chunk:
        return {{"rows": [], "done": True}}
    rows = []
    for u in chunk:
        html = http.get_text(u)
        if not html:
            continue
        m = extract.meta(html)
        rows.append({{"source_id": u, "url": u, "title": m.get("og:title") or m.get("title"), "image": m.get("og:image"),
                     "description": m.get("og:description")}})
    return rows
''',
    "openapi": '''
API_URL = SOURCE_URL  # TODO: the list endpoint from the OpenAPI document (public, no credentials)

def fetch(page, *, http, ctx):
    data = http.get_json(API_URL, params={{"page": page}})  # TODO: the documented pagination parameters
    if not data:
        return []
    items = data if isinstance(data, list) else (data.get("items") or data.get("results") or data.get("data") or [])
    rows = []
    for it in items[:MAX_ROWS_PER_PAGE]:
        rows.append({{
{mapping}
        }})
    return rows
''',
    "json_api": '''
API_URL = SOURCE_URL  # TODO: the site's public JSON list endpoint (no key, no login; robots.txt must allow it)
PER_PAGE = 50

def fetch(page, *, http, ctx):
    data = http.get_json(API_URL, params={{"page": page, "limit": PER_PAGE}})  # TODO: the endpoint's own paging parameters
    if not data:
        return []
    items = data if isinstance(data, list) else (data.get("products") or data.get("items") or data.get("results") or data.get("data") or [])
    rows = []
    for it in items[:MAX_ROWS_PER_PAGE]:
        rows.append({{
{mapping}
        }})
    if len(items) < PER_PAGE:
        return {{"rows": rows, "done": True}}
    return rows
''',
    "mcp": '''
from harvest_ai import mcp_client

SERVER = "TODO-server-name"  # from lane detection: the configured MCP server
TOOL = "TODO-search-tool"

def fetch(page, *, http, ctx):
    res = mcp_client.call_tool(SERVER, TOOL, {{"page": page}})  # TODO: the tool's arguments
    items = (res.get("items") or res.get("results") or []) if isinstance(res, dict) else []
    rows = []
    for it in items[:MAX_ROWS_PER_PAGE]:
        rows.append({{
{mapping}
        }})
    return rows
''',
    "browser": '''
def fetch(page, *, http, ctx):
    if page > 1:  # headless pages stay page 1 by policy: a renderer can hang past its timeout
        return {{"rows": [], "done": True}}
    html = http.render(SOURCE_URL)
    if not html:
        return []
    doc = extract.parse_html(html)
    rows = []
    for card in doc.find_all("article")[:MAX_ROWS_PER_PAGE]:  # TODO: the listing card selector
        link = card.find("a")
        rows.append({{
            "source_id": link.get("href") if link else None,
            "url": link.get("href") if link else None,
            "title": card.text[:200],
        }})
    return {{"rows": rows, "done": True}}
''',
}


def _mapping(fields: dict, indent: int, expr: str) -> str:
    lines = []
    for name, f in fields.items():
        if name in ("description",):
            continue
        lines.append(f'"{name}": {expr.format(name=name)},  # {f["type"]}{" (required)" if f.get("required") else ""}')
    return textwrap.indent("\n".join(lines), " " * indent)


def template(p: Project, sid: str, overwrite: bool = False) -> dict:
    import datetime as dt

    src = p.store.get_source(p.name, sid)
    if not src:
        raise LookupError(f"no source {sid}")
    lane = src.get("lane")
    if not lane:
        raise ValueError("detect the lane first (harvest_detect_lane)")
    if lane == "none":
        raise ValueError(f"lane is none ({(src.get('lane_detail') or {}).get('reason')}); this source is never collected")
    path = p.module_path(sid)
    fields = p.template["fields"]
    body = BODIES.get(lane, BODIES["html"]).format(
        mapping=_mapping(fields, 16, 'it.get("{name}")'),
        mapping_html=_mapping({k: v for k, v in fields.items() if k not in ("source_id", "url", "title")}, 12, "None"))
    code = HEADER.format(name=src.get("name") or sid, url=src["url"], record_type=p.spec.record_type, lane=lane, date=dt.date.today().isoformat(),
                         project=p.name, sid=sid) + body.lstrip("\n")
    written = False
    if overwrite or not path.exists():
        p.sources_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
        written = True
        if src.get("status") in ("candidate", "lane_detected", None):
            p.store.upsert_source(p.name, sid, {"status": "scaffolded"})
    return {"id": sid, "path": str(path), "written": written, "lane": lane, "contract": __doc__.split("Contract", 1)[1].strip(),
            "fields": {k: {kk: vv for kk, vv in v.items() if kk in ("type", "required", "unit", "enum", "desc")} for k, v in fields.items()},
            "helpers": {"http": ["get_text(url, params=None, headers=None, accept='html')", "get_json(url, params=None)", "post_json(url, body)",
                                 "render(url, wait_ms=1500)  # browser lane, page 1 only"],
                        "extract": ["parse_html(text) -> Node: .find(tag, cls=, id=, attrs=), .find_all(...), .text, .get(attr), .html",
                                    "jsonld(text) -> [dict]", "jsonld_types(text)", "next_data(text)", "nuxt_data(text)",
                                    "script_json(text, var)  # window.<var> = {...}", "find_items(obj, keys) -> [dict]",
                                    "feed_items(text) -> [dict]  # raises extract.FeedError on a truncated or non-feed document", "sitemap_urls(text) -> (pages, sitemaps)", "links(text, base, pattern)",
                                    "meta(text) -> {og:*, title, lang}", "visible_text(text)"],
                        "modules_allowed": "stdlib (re, json, datetime, math, html, urllib.parse ...) + harvest_ai.extract; no I/O besides http"},
            "lane_detail": src.get("lane_detail"), "code": code if written else path.read_text(encoding="utf-8"),
            "next": f"edit {path} until it returns real rows, then call harvest_review_source(project={p.name!r}, source_id={sid!r})"}


def fields_json(p: Project) -> str:
    return json.dumps(p.template["fields"], indent=1, ensure_ascii=False)
