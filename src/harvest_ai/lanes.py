"""Lane detection: how (and whether) a source may be collected. First match wins:

  mcp           an API already served by a configured MCP server (optional, generic)
  openapi       the site publishes an OpenAPI/Swagger document
  feed          RSS / Atom / JSON Feed
  sitemap       a sitemap listing the item pages
  json_api      a public, key-less JSON endpoint: the source URL answers JSON records (the site's own list API,
                Shopify products.json, WooCommerce Store API, WordPress REST, a JSON file)
  embedded_json Next.js / Nuxt state, JSON-LD items, `window.__STATE__` JSON
  html          server-rendered HTML with the items
  browser       a JavaScript shell that needs a headless browser (page 1 only)
  none          the content sits behind a login, captcha or paywall, a bot challenge or block stops the
                probe (challenge pages served with HTTP 200 included), or no usable route was found.
                Never logged in to, solved or bypassed.

A page the datacenter IP cannot open because of an IP-level block (401/403 without a challenge page, a
connection reset, a persistent 429) is re-probed through the residential-proxy route when the operator has
switched it on for the project or source (proxy.py); the result records `via_proxy`.

robots.txt and a site's terms of service are not part of lane detection: robots.txt is read only for its
`Sitemap:` lines (sitemap discovery), and never blocks a probe.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from . import extract, mcp_client
from .db import now_iso
from .http import Http
from .project import Project

LOGIN_MARKERS = re.compile(r"type=[\"']password[\"']|log ?in to (?:see|view|continue)|sign in to (?:see|view|continue)|"
                           r"please (?:log|sign) in|войдите|anmelden, um|inicia sesión para", re.I)
PAYWALL_MARKERS = re.compile(r"subscribe to (?:continue|read)|paywall|subscribers only|premium content", re.I)
OPENAPI_PATHS = ["/openapi.json", "/swagger.json", "/api/openapi.json", "/v3/api-docs", "/api-docs"]
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/json")
ITEM_TYPES = {"Product", "Offer", "AggregateOffer", "Car", "Vehicle", "Motorcycle", "JobPosting", "Event", "RealEstateListing", "Apartment", "House",
              "Residence", "SingleFamilyResidence", "Accommodation", "ItemList"}
# every site carries Organization/WebSite/Place JSON-LD about itself; only a businesses census treats them as items
BUSINESS_TYPES = {"LocalBusiness", "Organization", "Place", "Store", "AutoDealer", "RealEstateAgent", "Restaurant"}
MIN_FEED_ITEMS = 3


LANES = ("mcp", "openapi", "json_api", "feed", "sitemap", "embedded_json", "html", "browser", "none")
FEED_RELATED_SHARE = 0.3


def _path(u: str) -> str:
    return (urlsplit(u).path or "/").rstrip("/") or "/"


def feed_relates_to_page(feed_url: str, items: list[dict], page_url: str, page_links: list[str]) -> bool:
    """A feed is a collection lane for this source only when it carries this page's inventory: it is the feed of the
    listing path itself (Shopify's /collections/x.atom for /collections/x), or its items are linked from the page.
    A site-wide WordPress /feed/ of blog posts is neither (trials 2026-10: nine offices/solar sites got lane feed)."""
    src, fp = _path(page_url), re.sub(r"\.(atom|rss|xml|json)$", "", _path(feed_url))
    if src != "/" and (fp == src or fp.startswith(src + "/")):
        return True
    on_page = {_path(x) for x in page_links}
    item_links = [str(i.get("link") or i.get("url") or "") for i in items]
    item_links = [urljoin(page_url, x) for x in item_links if x]
    if not item_links:
        return False
    share = sum(1 for x in item_links if _path(x) in on_page) / len(item_links)
    return share >= FEED_RELATED_SHARE


def business_listings(page: str, page_url: str) -> int:
    """JSON-LD business entities that are listings, not the site describing itself: every site carries an
    Organization/LocalBusiness block about itself (trials 2026-10: three articles got lane embedded_json from it)."""
    from .domains import registrable_domain
    own = registrable_domain(page_url)
    found = []
    for d in extract.jsonld(page):
        objs = [d] + [el.get("item") if isinstance(el.get("item"), dict) else el for el in (d.get("itemListElement") or []) if isinstance(el, dict)]
        for o in objs:
            t = o.get("@type")
            if not set(t if isinstance(t, list) else [t]) & BUSINESS_TYPES:
                continue
            u = str(o.get("url") or o.get("@id") or "")
            if u and registrable_domain(urljoin(page_url, u)) == own and _path(urljoin(page_url, u)) == "/":
                continue  # the site itself
            found.append(o)
    named = [o for o in found if o.get("name") or o.get("url")]
    return len(named) if len(named) >= 2 else 0


def item_types(record_type: str | None) -> set[str]:
    return ITEM_TYPES | BUSINESS_TYPES if record_type == "businesses" else ITEM_TYPES


def robots_sitemaps(text: str) -> list[str]:
    """The `Sitemap:` lines of a robots.txt (sitemap discovery only; its allow/disallow rules are not read)."""
    out = []
    for raw in (text or "").splitlines():
        key, _, val = raw.split("#", 1)[0].partition(":")
        if key.strip().lower() == "sitemap" and val.strip().startswith(("http://", "https://")):
            out.append(val.strip())
    return out


def _sitemap_candidates(http: Http, origin: str) -> list[str]:
    """Sitemaps named in robots.txt (when it can be read), else /sitemap.xml."""
    listed = robots_sitemaps(http.get_text(origin + "/robots.txt", accept="text/plain") or "")
    return listed[:2] or [origin + "/sitemap.xml"]


def _feed_items(text: str) -> list[dict]:
    """Detection is lenient: a feed that does not parse has no items (the runner treats the same thing as an error)."""
    try:
        return extract.feed_items(text)
    except extract.FeedError:
        return []


def _as_document(resp) -> tuple[str, Any] | None:
    """The source URL is itself a data document: ('feed', items) for RSS/Atom/JSON Feed, ('json', data) for a JSON API answer."""
    ctype = (resp.headers.get("content-type") or "").lower()
    head = resp.text.lstrip()[:400].lower()
    if "xml" in ctype or head.startswith("<?xml") or head.startswith("<rss") or head.startswith("<feed"):
        if "<rss" in head or "<feed" in head or "<rdf" in head or "<channel" in resp.text[:4000].lower():
            return "feed", _feed_items(resp.text)
        return None
    if "json" in ctype or head[:1] in ("{", "["):
        try:
            data = json.loads(resp.text)
        except ValueError:
            return None
        if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
            return None  # an OpenAPI document: the openapi probe handles it
        if isinstance(data, dict) and str(data.get("version", "")).startswith("https://jsonfeed.org"):
            return "feed", _feed_items(resp.text)
        return "json", data
    return None


def _json_items(data: Any) -> int:
    """How many records a JSON answer carries: a top-level list, or the longest list of objects one level down."""
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        lists += [w for v in data.values() if isinstance(v, dict) for w in v.values() if isinstance(w, list) and w and isinstance(w[0], dict)]
        return max((len(v) for v in lists), default=0)
    return 0


def _probe_openapi(http: Http, origin: str, page_html: str) -> dict | None:
    urls = [u for u in extract.links(page_html, origin) if re.search(r"openapi|swagger", u, re.I)][:2] + [origin + p for p in OPENAPI_PATHS]
    for u in urls[:5]:
        r = http.get(u, accept="json")
        if not r:
            continue
        data = r.json()
        if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
            paths = data.get("paths") or {}
            ops = [f"{m.upper()} {p}" for p, item in paths.items() if isinstance(item, dict) for m in item if m.lower() in ("get", "post")]
            return {"url": u, "title": (data.get("info") or {}).get("title"), "operations": ops[:50], "operations_total": len(ops)}
    return None


def _feeds(page_html: str, base: str) -> list[str]:
    out = []
    for m in re.finditer(r"<link\b([^>]+)>", page_html or "", re.I):
        a = m.group(1)
        if re.search(r"rel\s*=\s*[\"']?alternate", a, re.I) and any(t in a.lower() for t in FEED_TYPES):
            h = re.search(r"href\s*=\s*[\"']([^\"']+)", a, re.I)
            if h:
                out.append(urljoin(base, h.group(1)))
    return out


def detect(url: str, *, http: Http | None = None, target_path: str | None = None, use_mcp: bool = True, record_type: str | None = None) -> dict:
    """Probe one source URL. Returns {lane, reason, signals{...}}."""
    own = http is None
    http = http or Http(rate_s=1.0)
    try:
        return _detect(url, http, target_path, use_mcp, item_types(record_type))
    finally:
        if own:
            http.close()


def _detect(url: str, http: Http, target_path: str | None, use_mcp: bool, wanted: set[str]) -> dict:
    from .domains import registrable_domain

    u = urlsplit(url)
    origin = f"{u.scheme}://{u.netloc}"
    signals: dict[str, Any] = {}
    out: dict[str, Any] = {"url": url, "lane": None, "reason": None, "signals": signals}
    out["ua_mode"] = http.ua_mode
    domain = registrable_domain(url)
    if use_mcp:
        try:
            hits = mcp_client.match_domain(domain)
        except Exception as exc:
            hits = []
            signals["mcp_error"] = str(exc)[:200]
        if hits:
            signals["mcp"] = hits
    resp = http.get(url, accept="html")
    host = http._host(u.netloc)
    if resp is None:
        last = http.events[-1] if http.events else {}
        kind = last.get("kind") or host.last_kind
        why = f"{last.get('event', 'error')}{' ' + str(last.get('status')) if last.get('status') else ''}{' ' + kind if kind else ''}"
        if kind == "ip_block" and last.get("via_proxy") or any(e.get("event") == "proxy_no_help" for e in http.events[-6:]):
            tail = "; blocked through a residential exit as well, so not an IP-level block; never bypassed"
        elif kind == "ip_block" and last.get("event") != "proxy_budget_stop" and not last.get("via_proxy"):
            out["ip_block"] = True
            tail = ("; an IP-level block: an operator may switch on the residential-proxy route (harvest proxy enable)"
                    if http._route is None else "; the proxy route could not serve it")
        else:
            tail = "; never bypassed"
        out.update(lane="none", reason=f"page not publicly fetchable ({why}){tail}")
        out["via_proxy"] = bool(last.get("via_proxy"))
        return out
    page = resp.text
    out["via_proxy"] = bool(resp.via_proxy)
    if resp.via_proxy:
        signals["via_proxy"] = True
        signals["proxy_trigger"] = host.trigger
    signals["status"] = resp.status
    signals["bytes"] = len(page)
    vis = extract.visible_text(page)
    if LOGIN_MARKERS.search(page) and len(vis) < 3000:
        out.update(lane="none", reason="content is behind a login")
        return out
    if PAYWALL_MARKERS.search(page) and len(vis) < 5000:
        out.update(lane="none", reason="content is behind a paywall")
        return out
    if signals.get("mcp"):
        out.update(lane="mcp", reason=f"served by configured MCP server {signals['mcp'][0]['server']}")
        return out
    doc = _as_document(resp)
    if doc and doc[0] == "feed" and len(doc[1]) >= MIN_FEED_ITEMS:
        signals["feed_items"] = len(doc[1])
        out.update(lane="feed", reason=f"the source URL is a feed with {len(doc[1])} items")
        return out
    if doc and doc[0] == "json" and _json_items(doc[1]) >= 1:
        signals["json_items"] = _json_items(doc[1])
        out.update(lane="json_api", reason=f"the source URL is a public JSON document ({signals['json_items']} records)")
        return out
    oa = _probe_openapi(http, origin, page)
    if oa:
        signals["openapi"] = oa
        out.update(lane="openapi", reason=f"OpenAPI document at {oa['url']}")
        return out
    feeds = _feeds(page, resp.url)
    if feeds:
        signals["feeds"] = feeds[:5]
        items = _feed_items(http.get_text(feeds[0], accept="xml") or "")
        signals["feed_items"] = len(items)
        related = feed_relates_to_page(feeds[0], items, resp.url, extract.links(page, resp.url))
        if not related:
            signals["feed_unrelated"] = True  # e.g. the site's blog feed: not this page's inventory
        if len(items) >= MIN_FEED_ITEMS and related:  # a one-item "latest ad" feed is not a collection lane
            out.update(lane="feed", reason=f"feed with {len(items)} items at {feeds[0]}")
            return out
    for sm in _sitemap_candidates(http, origin):
        txt = http.get_text(sm, accept="xml")
        if txt:
            pages, maps = extract.sitemap_urls(txt)
            if pages or maps:
                signals["sitemap"] = {"url": sm, "pages": len(pages), "child_sitemaps": len(maps), "sample": (pages or maps)[:5]}
                break
    nd = extract.next_data(page)
    nuxt = extract.nuxt_data(page)
    ld_types = extract.jsonld_types(page)
    states = [v for v in ("__INITIAL_STATE__", "__PRELOADED_STATE__", "__APOLLO_STATE__", "__DATA__") if extract.script_json(page, v) is not None]
    if nd is not None:
        signals["next_data"] = True
    if nuxt is not None:
        signals["nuxt"] = True
    if ld_types:
        signals["jsonld_types"] = ld_types
    if states:
        signals["state_vars"] = states
    ld_hits = set(ld_types) & (wanted - BUSINESS_TYPES)
    if wanted & BUSINESS_TYPES and set(ld_types) & BUSINESS_TYPES:
        n_biz = business_listings(page, resp.url)
        signals["jsonld_business_listings"] = n_biz
        if n_biz:
            ld_hits |= set(ld_types) & BUSINESS_TYPES
    if nd is not None or nuxt is not None or states or ld_hits:
        which = "Next.js data" if nd is not None else "Nuxt data" if nuxt is not None else ("state " + states[0]) if states else "JSON-LD " + ",".join(sorted(ld_hits))
        out.update(lane="embedded_json", reason=f"embedded {which}")
        return out
    if signals.get("sitemap") and signals["sitemap"]["pages"]:
        out.update(lane="sitemap", reason=f"sitemap with {signals['sitemap']['pages']} pages")
        return out
    links = extract.links(page, resp.url)
    signals["visible_chars"] = len(vis)
    signals["links"] = len(links)
    if len(vis) >= 1500 and len(links) >= 10:
        out.update(lane="html", reason="server-rendered HTML with content")
        return out
    scripted = bool(re.search(r"<script\b", page, re.I))
    if re.search(r"<div id=[\"'](?:root|app|__next)[\"']\s*>\s*</div>|enable javascript|requires javascript", page, re.I) or (len(vis) < 1500 and scripted):
        # a short page is a JavaScript shell only if it has scripts that could render the content; a short page
        # without any script is simply small server-rendered HTML (soak 2026-10: a 15-item, script-free list page
        # was sent to the browser lane)
        out.update(lane="browser", reason="JavaScript shell; needs a headless browser (page 1 only)")
        return out
    out.update(lane="html", reason="HTML")
    return out


def detect_for_source(p: Project, sid: str, *, http: Http | None = None, use_mcp: bool = True) -> dict:
    """Detect and record a source's lane, holding its lock (locks.py). Raises locks.SourceBusy when it is taken."""
    from . import locks
    if not p.store.get_source(p.name, sid):
        raise LookupError(f"no source {sid}")
    with locks.held(p, sid, "detect") as lk:
        return _detect_locked(p, sid, lk, http=http, use_mcp=use_mcp)


def _detect_locked(p: Project, sid: str, lk, *, http: Http | None, use_mcp: bool) -> dict:
    src = p.store.get_source(p.name, sid)
    res = detect(src["url"], http=http, use_mcp=use_mcp, record_type=p.spec.record_type)
    prev = src.get("lane_detail") or {}
    detail = {k: res[k] for k in ("reason", "signals")}
    detail["via_proxy"] = bool(res.get("via_proxy"))
    detail["ua_mode"] = res.get("ua_mode") or "own"
    if res.get("ip_block"):
        detail["ip_block"] = True
    for k in ("proxy", "ua"):
        if prev.get(k) is not None:
            detail[k] = prev[k]  # the operator's proxy / user-agent decisions survive a re-detection
    fields: dict[str, Any] = {"lane": res["lane"], "lane_detail": detail}
    if fields["lane"] == "none":
        fields.update(status="lane_none", enabled=0)
    elif src.get("status") in ("candidate", "lane_none", "blocked", None):
        fields["status"] = "lane_detected"
    if res["lane"] == "browser":
        fields["max_pages"] = 1
    if not lk.valid():
        raise RuntimeError("lane detection lost its source lock to another worker; nothing recorded")
    p.store.upsert_source(p.name, sid, fields)
    return {"id": sid, **res}


def set_lane(p: Project, sid: str, lane: str, *, reason: str | None = None, actor: str = "operator") -> dict:
    """Record the lane a source is built on, overriding detection (e.g. a public JSON API found while building on a
    thin feed). An agent (actor `agent:<kind>`, whose input includes pages anyone can write) may move a source between
    collectable lanes, but may not open one whose lane is none or was never detected (a login, captcha, paywall, bot
    challenge or block): that is an operator decision. Lane none disables the source."""
    src = p.store.get_source(p.name, sid)
    if not src:
        raise LookupError(f"no source {sid}")
    if lane not in LANES:
        raise ValueError(f"unknown lane; one of {', '.join(LANES)}")
    if actor.startswith("agent") and lane != "none" and src.get("lane") in (None, "none"):
        raise PermissionError(f"the lane is {src.get('lane') or 'undetected'}; an agent may not open it (operator decision)")
    detail = dict(src.get("lane_detail") or {})
    detail["override"] = {"lane": lane, "reason": reason, "by": actor, "at": now_iso()}
    fields: dict[str, Any] = {"lane": lane, "lane_detail": detail}
    if lane == "none":
        fields.update(status="lane_none", enabled=0)
    elif src.get("status") in ("candidate", "lane_none", None):
        fields["status"] = "lane_detected"
    p.store.upsert_source(p.name, sid, fields)
    return json.loads(json.dumps(p.store.get_source(p.name, sid), default=str))
