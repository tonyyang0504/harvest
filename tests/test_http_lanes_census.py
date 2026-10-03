import json
import threading
import time

import pytest
from conftest import make_project
from localsite import html_page

from harvest_ai import census, lanes, service
from harvest_ai import project as project_mod
from harvest_ai.http import Blocked, Http


def _http(**kw):
    kw.setdefault("rate_s", 0.01)
    kw.setdefault("backoff_s", 0.02)
    return Http(**kw)


def test_robots_denial_and_basic_get(site):
    site.route("/robots.txt", (200, {"content-type": "text/plain"}, "User-agent: *\nDisallow: /secret\n"))
    site.route("/ok", "<p>hi</p>" * 50)
    site.route("/secret", "nope")
    h = _http()
    assert "hi" in h.get_text(site.url + "/ok")
    assert h.get_text(site.url + "/secret") is None and h.stats["robots_denied"] == 1
    assert "/secret" not in site.paths()
    assert site.count("/robots.txt") == 1  # cached per host


def test_per_host_concurrency_is_one(site):
    site.route("/slow", (200, {"content-type": "text/html", "x-delay": "0.1"}, "<p>x</p>" * 100))
    h = _http()
    threads = [threading.Thread(target=h.get_text, args=(site.url + "/slow",)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert site.count("/slow") == 4 and site.max_active == 1


def test_rate_limit_spacing(site):
    site.route("/a", "<p>a</p>" * 100)
    h = _http(rate_s=0.2)
    h.get_text(site.url + "/a")
    t0 = time.monotonic()
    h.get_text(site.url + "/a")
    h.get_text(site.url + "/a")
    assert time.monotonic() - t0 >= 0.38


def test_429_escalates_host_delay_and_honours_retry_after(site):
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        if calls["n"] <= 2:
            return (429, {"content-type": "text/plain", "retry-after": "0.2"}, "slow down")
        return "<p>ok</p>" * 50
    site.route("/r", flaky)
    h = _http(rate_s=0.01)
    t0 = time.monotonic()
    assert "ok" in h.get_text(site.url + "/r")
    assert time.monotonic() - t0 >= 0.4 and h.stats["rate_limited"] == 2 and h.stats["retries"] == 2
    assert h._host(site.url.split("//")[1]).delay >= 1.0  # escalated for the rest of the run


def test_5xx_retries_then_gives_up_within_url_budget(site):
    site.route("/down", (503, {"content-type": "text/plain"}, "down"))
    h = _http(retries=5, url_budget_s=0.3, backoff_s=0.1)
    t0 = time.monotonic()
    assert h.get_text(site.url + "/down") is None
    assert time.monotonic() - t0 < 1.0 and 2 <= site.count("/down") <= 4
    assert any(e["event"] == "gave_up" for e in h.events)


def test_blocks_and_challenges_are_findings(site):
    site.route("/forbidden", (403, {"content-type": "text/html"}, "no"))
    site.route("/challenge", html_page("<h1>Just a moment...</h1><div class='cf-chl'></div>"))
    h = _http()
    assert h.get_text(site.url + "/forbidden") is None and h.get_text(site.url + "/challenge") is None
    assert h.stats["blocked"] == 2 and site.count("/forbidden") == 1  # never retried


INCAPSULA_STUB = ('<html style="height:100%"><head><META NAME="ROBOTS" CONTENT="NOINDEX, NOFOLLOW"><script type="text/javascript" '
                  'src="/_Incapsula_Resource?SWJIYLWA=719d34d3"></script><script>if (sessionStorage) { sessionStorage.setItem(\'distil_referrer\', '
                  'document.referrer); }</script></head><body style="margin:0px;height:100%"><iframe id="main-iframe" '
                  'src="/_Incapsula_Resource?SWUDNSAI=31&incident_id=444000150565" frameborder=0>Request unsuccessful. Incapsula incident ID: '
                  '444000150565</iframe></body></html>')


def test_bot_wall_stubs_served_with_200_are_challenges_not_content(site):
    """Trials 2026-10: dubizzle.com and regus.com answer harvest with an HTTP 200 Imperva/Incapsula stub; it was returned as
    content, so lane detection called it a JavaScript shell (browser lane)."""
    site.route("/stub", INCAPSULA_STUB)
    site.route("/stub2", '<html><head><script src="/_Incapsula_Resource?SWJIYLWA=5074a7"></script><body></body></html>')
    real = html_page("<h1>Offices for rent</h1>" + "".join(f"<p><a href='/o/{i}'>Office {i}, 1,200 sq ft, S$ 8,000/mo</a></p>" for i in range(40)),
                     head='<script src="/_Incapsula_Resource?SWJIYLWA=5074a7"></script>')
    site.route("/real", real)
    h = _http()
    assert h.get_text(site.url + "/stub") is None and h.get_text(site.url + "/stub2") is None
    assert h.stats["blocked"] == 2 and h._host(site.url.split("//")[1]).last_kind == "challenge"
    assert "Office 39" in (h.get_text(site.url + "/real") or "")  # a protected page with content is content
    r = lanes.detect(site.url + "/stub", http=_http())
    assert r["lane"] == "none" and "challenge" in r["reason"]


def test_private_addresses_are_gated_unless_allowed(site, monkeypatch):
    monkeypatch.delenv("HARVEST_ALLOW_PRIVATE")
    h = Http(rate_s=0.01)
    assert h.get_text(site.url + "/x") is None and h.stats["gated"] == 1
    with pytest.raises(Blocked):
        h.gate("file:///etc/passwd")
    with pytest.raises(Blocked):
        h.gate("https://user:pw@example.com/")


def test_no_route_means_direct_only(site):
    site.route("/p", (403, {"content-type": "text/html"}, "denied"))
    h = _http()
    assert h.get_text(site.url + "/p") is None
    assert h.stats["ip_blocked"] == 1 and h.stats["proxy_requests"] == 0 and h.proxy_report() is None


# ---------------------------------------------------------------- census
def test_census_plan_and_add_rules():
    p = make_project(max_sources=3)
    plan = census.plan(p)
    assert [r["region"] for r in plan["regions"]] == ["KZ", "GE"]
    kz = plan["regions"][0]
    assert kz["currency"] == "KZT" and kz["cctld"] == ".kz" and set(kz["queries"]) == set(census.ANGLES)
    assert any("into kk" in q for q in kz["queries"]["local_language"])
    ev = lambda u: [{"url": u, "observation": "20 car ads with prices"}]  # noqa: E731
    res = census.add(p, [
        {"url": "https://kolesa.kz/cars", "regions": ["KZ"], "angle": "classifieds", "evidence": ev("https://kolesa.kz/cars")},
        {"url": "https://m.kolesa.kz/", "regions": ["KZ"], "angle": "marketplaces", "evidence": ev("https://m.kolesa.kz/cars/1")},
        {"url": "https://myauto.ge", "regions": ["Georgia"], "angle": "vertical_portals", "evidence": ev("https://www.myauto.ge/ka/s/cars")},
        {"url": "https://nothing.kz", "regions": ["KZ"], "angle": "classifieds", "evidence": [{"url": "https://google.com/search?q=x", "observation": "seen"}]},
        {"url": "https://copy.kz", "regions": ["KZ"], "angle": "classifieds", "mirror_of": "kolesa.kz", "evidence": ev("https://copy.kz")},
        {"url": "https://fr.fr", "regions": ["FR"], "angle": "classifieds", "evidence": ev("https://fr.fr")},
        {"url": "https://x.kz", "regions": ["KZ"], "angle": "gossip", "evidence": ev("https://x.kz")},
        {"url": "ftp://x.kz", "regions": ["KZ"]},
    ], round_label="angle:classifieds:KZ")
    assert [a["id"] for a in res["added"]] == ["kolesa_kz", "myauto_ge"]
    assert res["merged"] == [{"id": "kolesa_kz", "domain": "kolesa.kz"}]
    reasons = " | ".join(r["reason"] for r in res["rejected"])
    assert "own domain" in reasons and "mirror" in reasons and "outside the project" in reasons and "unknown angle" in reasons and "http" in reasons
    src = p.store.get_source(p.name, "kolesa_kz")
    assert src["angles"] == ["classifieds", "marketplaces"] and len(src["evidence"]) == 2 and src["status"] == "candidate"
    census.add(p, [{"url": "https://a.ge", "regions": ["GE"], "evidence": ev("https://a.ge")}])
    res = census.add(p, [{"url": "https://b.ge", "regions": ["GE"], "evidence": ev("https://b.ge")}])
    assert "max_sources" in res["rejected"][0]["reason"]


def test_census_plan_queries_fit_the_record_type():
    """Trials 2026-10: a jobs census was told to search 'software engineering jobs dealers agencies ... inventory', and the
    classifieds, marketplaces and vertical-portal queries were identical."""
    p = make_project("swe", "jobs", ("PL",), target="software engineering jobs")
    q = census.plan(p)["regions"][0]["queries"]
    flat = " | ".join(x for qs in q.values() for x in qs)
    assert "dealer" not in flat and "inventory" not in flat
    assert any("employment service" in x for x in q["official"]) and any("employers" in x for x in q["operators"])
    assert len({tuple(q[a]) for a in ("classifieds", "marketplaces", "vertical_portals")}) == 3
    assert all("Poland" in x for a, qs in q.items() if a != "local_language" for x in qs)


def test_shared_hosts_are_keyed_by_owner_not_host():
    """Trials 2026-10: confs.tech's JSON on raw.githubusercontent.com was registered as the site `githubusercontent.com`,
    so every other GitHub-hosted dataset would have merged into it."""
    p = make_project(max_sources=10)
    ev = lambda u: [{"url": u, "observation": "JSON list of conferences with dates"}]  # noqa: E731
    a = "https://raw.githubusercontent.com/tech-conferences/conference-data/main/conferences/2026/javascript.json"
    b = "https://raw.githubusercontent.com/other-org/events/main/uk.json"
    res = census.add(p, [{"url": a, "regions": ["KZ"], "evidence": ev(a)}, {"url": b, "regions": ["KZ"], "evidence": ev(b)},
                         {"url": "https://github.com/acme/listings", "regions": ["KZ"], "evidence": ev("https://github.com/acme/listings/blob/main/a.csv")}])
    assert [x["domain"] for x in res["added"]] == ["raw.githubusercontent.com/tech-conferences/conference-data",
                                                    "raw.githubusercontent.com/other-org/events", "github.com/acme"]
    assert res["added"][0]["id"] == "raw_githubusercontent_com_tech_conferences_conference_data"
    # evidence on another repo of the same host is not the candidate's own site
    res = census.add(p, [{"url": a, "regions": ["KZ"], "evidence": ev(b)}])
    assert res["merged"] == [] and "own domain" in res["rejected"][0]["reason"]
    assert census.domain_key("https://api.github.com/x") == "github.com" and census.domain_key("https://kolesa.kz/") == "kolesa.kz"
    # newsletters: the Dublin Tech Events Substack was filed under `substack.com`, a Buttondown list under `buttondown.com`
    assert census.domain_key("https://dublintechevents.substack.com/p/x") == "dublintechevents.substack.com"
    assert census.domain_key("https://substack.com/discover") == "substack.com"
    assert census.domain_key("https://buttondown.com/londontechevents/archive/") == "buttondown.com/londontechevents"


def test_census_gaps_dry_streak():
    p = make_project()
    g = census.gaps(p)
    assert g["sources"] == 0 and "census plan" in g["next_step"]
    census.add(p, [{"url": "https://kolesa.kz", "regions": ["KZ"], "angle": "classifieds", "evidence": [{"url": "https://kolesa.kz/a", "observation": "ads"}]}], "r1")
    census.add(p, [], "critic:r1")
    census.add(p, [], "critic:r2")
    g = census.gaps(p)
    assert g["matrix"]["KZ"]["classifieds"] == 1 and g["dry_streak"] == 2 and not g["saturated"] and "cells are empty" in g["next_step"]
    assert {"region": "GE", "angle": "classifieds"} in g["empty_cells"] and g["known_domains"] == ["kolesa.kz"]


# ---------------------------------------------------------------- lanes
FILLER = "<p>" + "Plenty of listings here. " * 100 + "</p>" + "".join(f"<a href='/l/{i}'>l{i}</a>" for i in range(15))


def _lane(site, body_html, terms="Be nice.", robots="User-agent: *\nDisallow: /private\n", extra_routes=None):
    site.route("/robots.txt", (200, {"content-type": "text/plain"}, robots))
    site.route("/terms", html_page(f"<p>{terms}</p>"))
    site.route("/list", body_html)
    for k, v in (extra_routes or {}).items():
        site.route(k, v)
    return lanes.detect(site.url + "/list", http=_http(), use_mcp=False)


def test_lane_html(site):
    r = _lane(site, html_page(FILLER + "<a href='/terms'>Terms and conditions</a>"))
    assert r["lane"] == "html" and r["robots_status"] == "allowed" and r["terms"]["status"] == "no_clause" and r["terms"]["url"].endswith("/terms")


def test_lane_embedded_json(site):
    head = '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"ads":[]}}}</script>'
    r = _lane(site, html_page(FILLER, head=head))
    assert r["lane"] == "embedded_json" and "Next.js" in r["reason"]
    ld = '<script type="application/ld+json">{"@type":"ItemList","itemListElement":[{"item":{"@type":"Car","name":"x"}}]}</script>'
    r = _lane(site, html_page(FILLER, head=ld))
    assert r["lane"] == "embedded_json" and "JSON-LD" in r["reason"]


def test_lane_feed_and_openapi_and_sitemap(site):
    head = '<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
    rss = "<rss><channel>" + "".join(f"<item><title>{i}</title><link>/l/{i}</link></item>" for i in range(3)) + "</channel></rss>"
    r = _lane(site, html_page(FILLER, head=head), extra_routes={"/feed.xml": (200, {"content-type": "application/rss+xml"}, rss)})
    assert r["lane"] == "feed" and r["signals"]["feed_items"] == 3
    # trials 2026-10: a site-wide blog feed whose posts the listing page does not link is not this page's inventory
    blog = "<rss><channel>" + "".join(f"<item><title>News {i}</title><link>/blog/post-{i}</link></item>" for i in range(10)) + "</channel></rss>"
    r = _lane(site, html_page(FILLER, head=head), extra_routes={"/feed.xml": (200, {"content-type": "application/rss+xml"}, blog)})
    assert r["lane"] == "html" and r["signals"]["feed_unrelated"] and r["signals"]["feed_items"] == 10
    # the feed of the listing path itself (Shopify /collections/x.atom for /collections/x) counts even without links
    assert lanes.feed_relates_to_page("https://s.in/collections/phones.atom", [{"link": "/products/a"}], "https://s.in/collections/phones", [])
    assert not lanes.feed_relates_to_page("https://s.in/feed/", [{"link": "/blog/a"}], "https://s.in/", ["https://s.in/products/b"])
    spec = {"openapi": "3.0.0", "info": {"title": "Ads API"}, "paths": {"/ads": {"get": {}}}}
    r = _lane(site, html_page(FILLER, head=head), extra_routes={"/openapi.json": spec})
    assert r["lane"] == "openapi" and r["signals"]["openapi"]["operations"] == ["GET /ads"]
    sm = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>/item/1</loc></url></urlset>'
    r = _lane(site, html_page("<p>tiny</p>"), robots="User-agent: *\nSitemap: " + site.url + "/sm.xml\n",
              extra_routes={"/sm.xml": (200, {"content-type": "application/xml"}, sm), "/openapi.json": (404, {}, "")})
    assert r["lane"] == "sitemap"


def test_lane_when_the_source_url_is_itself_a_json_document_or_a_feed(site):
    """Trials 2026-10: arbeitnow.com's job API, jobs.booking.com/api/jobs and a JSON file of conferences were labelled lane
    `html` ("HTML"); germantechjobs.de/rss (the RSS feed itself) was labelled `sitemap`. Their terms were never found."""
    home = html_page(FILLER + "<a href='/terms'>Terms of use</a>")
    terms = "Do not scrape or crawl this site with automated means."
    api = {"data": [{"slug": f"job-{i}", "title": "Software Engineer"} for i in range(5)], "links": {"next": "/api?page=2"}}
    r = _lane(site, (200, {"content-type": "application/json"}, json.dumps(api)), terms="Be nice.", extra_routes={"/": home})
    assert r["lane"] == "json_api" and r["signals"]["json_items"] == 5 and "JSON document" in r["reason"]
    assert r["terms"]["status"] == "no_clause" and r["terms"]["url"].endswith("/terms")  # found through the home page
    r = _lane(site, (200, {"content-type": "text/plain"}, json.dumps([{"name": "JSConf", "country": "U.K."}] * 3)), extra_routes={"/": home})
    assert r["lane"] == "json_api" and r["signals"]["json_items"] == 3
    rss = '<?xml version="1.0"?><rss version="2.0"><channel>' + "".join(f"<item><title>Job {i}</title><link>/j/{i}</link></item>" for i in range(4)) + "</channel></rss>"
    r = _lane(site, (200, {"content-type": "application/rss+xml"}, rss), extra_routes={"/": home})
    assert r["lane"] == "feed" and r["signals"]["feed_items"] == 4
    r = _lane(site, (200, {"content-type": "application/json"}, json.dumps(api)), terms=terms, extra_routes={"/": home})
    assert r["lane"] == "none" and r["terms"]["status"] == "forbids"  # the home page's terms still bind a JSON endpoint


def test_builders_can_record_the_json_api_lane_and_scaffold_it(site):
    """Trials 2026-10: six builders built on a key-less JSON endpoint (Shopify products.json, WooCommerce Store API, WordPress
    REST, Lazada's ajax list) and had to record lane `openapi` because `json`/`api` were rejected."""
    from conftest import add_source

    from harvest_ai import scaffold
    p = make_project("phones", "products", ("IN",), target="used smartphones")
    sid = add_source(p, site.url + "/collections/phones", regions=("IN",))
    p.store.upsert_source(p.name, sid, {"lane": "feed", "robots_status": "allowed", "terms_status": "no_clause", "status": "lane_detected"})
    s = lanes.record_policy(p, sid, lane="json_api", reason="Shopify products.json carries every product; the .atom feed only the newest 25")
    assert s["lane"] == "json_api" and s["lane_detail"]["override"].startswith("Shopify")
    out = scaffold.template(p, sid)
    assert out["lane"] == "json_api" and "get_json(API_URL" in out["code"]
    compile(out["code"], "m.py", "exec")


def test_lane_browser_for_js_shells(site):
    r = _lane(site, html_page("<div id='root'></div><script src='/app.js'></script>"))
    assert r["lane"] == "browser"


@pytest.mark.parametrize("terms,why", [
    ("You may not use robots, spiders or scrapers to access the site.", "terms"),
    ("Es ist untersagt, die Inhalte automatisiert auszulesen (Crawler).", "terms"),
    ("Запрещается автоматизированный сбор данных с сайта.", "terms"),
    ("Queda prohibido el uso de robots o scraping.", "terms"),
    ("本サイトへのスクレイピングは禁止します。", "terms"),
])
def test_lane_none_when_terms_forbid(site, terms, why):
    r = _lane(site, html_page(FILLER + "<a href='/terms'>Terms</a>"), terms=terms)
    assert r["lane"] == "none" and why in r["reason"] and r["terms"]["status"] == "forbids" and r["terms"]["clause"]


def test_lane_none_for_robots_login_and_blocks(site):
    r = _lane(site, html_page(FILLER), robots="User-agent: *\nDisallow: /list\n")
    assert r["lane"] == "none" and "robots" in r["reason"] and "/list" not in site.paths()
    r = _lane(site, html_page("<form><input type='password' name='p'></form><p>Please log in to see the ads</p>"))
    assert r["lane"] == "none" and "login" in r["reason"]
    r = _lane(site, (403, {"content-type": "text/html"}, "denied"))
    assert r["lane"] == "none" and "blocked" in r["reason"]
    site.route("/robots.txt", (500, {}, "err"))
    r = lanes.detect(site.url + "/list", http=_http(), use_mcp=False)
    assert r["lane"] == "none" and "unreachable" in r["reason"]


def test_lane_via_configured_mcp_server(site, tmp_path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"servers": {"cars-api": {"command": "true", "domains": ["127.0.0.1"]}}}', encoding="utf-8")
    monkeypatch.setenv("HARVEST_MCP_CONFIG", str(cfg))
    site.route("/robots.txt", (200, {}, "User-agent: *\n"))
    site.route("/terms", html_page("<p>ok</p>"))
    site.route("/list", html_page(FILLER + "<a href='/terms'>Terms</a>"))
    r = lanes.detect(site.url + "/list", http=_http())
    assert r["lane"] == "mcp" and r["signals"]["mcp"] == [{"server": "cars-api", "how": "declared"}]


def test_detect_for_source_and_policy_overrides(site):
    p = make_project()
    site.route("/robots.txt", (200, {}, "User-agent: *\n"))
    site.route("/list", html_page(FILLER))  # no terms link -> unknown
    sid = census.add(p, [{"url": site.url + "/list", "regions": ["KZ"], "evidence": [{"url": site.url + "/list", "observation": "ads"}]}])["added"][0]["id"]
    out = service.detect_lane(p.name, sid)
    assert out["lane"] == "html" and out["terms"]["status"] == "unknown"
    src = p.store.get_source(p.name, sid)
    assert src["status"] == "lane_detected" and src["terms_status"] == "unknown"
    with pytest.raises(ValueError):
        lanes.record_policy(p, sid, terms_status="allowed")  # needs the URL read
    lanes.record_policy(p, sid, terms_status="no_clause", terms_url=site.url + "/about", terms_clause=None)
    assert p.store.get_source(p.name, sid)["terms_status"] == "no_clause"
    lanes.record_policy(p, sid, terms_status="forbids", terms_url=site.url + "/about", terms_clause="no bots")
    src = p.store.get_source(p.name, sid)
    assert src["lane"] == "none" and src["status"] == "lane_none"
    with pytest.raises(ValueError):
        lanes.record_policy(p, sid, lane="html")


def test_classify_terms_negatives():
    assert lanes.classify_terms("<p>We love our users. Contact us for API access.</p>")["status"] == "no_clause"
    assert lanes.classify_terms("<p>You agree not to use any automated means to collect listings.</p>")["status"] == "forbids"


def test_terms_link_is_the_footer_terms_not_an_article_slug(site):
    """Trials 2026-10: projectsegy.com's 'terms' were an article (/cost-conditions-opening-delivery-order-saudi/), and
    lazada.co.id's terms URL kept a raw '&amp;'."""
    site.route("/cost-conditions-opening-delivery-order-saudi/", html_page("<p>" + "An article about delivery orders. " * 40 + "</p>"))
    site.route("/terms-of-use/", html_page("<p>You must not scrape this site with automated means.</p>" + "<p>More.</p>" * 50))
    body = (FILLER + "<a href='/cost-conditions-opening-delivery-order-saudi/'>How to open a delivery order</a>"
            "<footer><a href='/terms-of-use/?a=1&amp;b=2'>Terms of Use</a></footer>")
    r = _lane(site, html_page(body))
    assert r["terms"]["url"].endswith("/terms-of-use/?a=1&b=2") and r["terms"]["status"] == "forbids" and r["lane"] == "none"


def test_pdf_and_binary_terms_are_unknown_not_no_clause(site):
    """Trials 2026-10: nofluffjobs.com and germantechjobs.de publish their terms as PDFs whose text forbids collection;
    the heuristic read the binary as 'no_clause', which the enable gate accepts."""
    pdf = "%PDF-1.4\n%\ufffd\ufffd\ufffd\ufffd\n1 0 obj\n<</Title (Terms)/Filter /FlateDecode>>stream\nx\ufffd\x03\x01" + "\ufffd\x07" * 500
    assert lanes.classify_terms(pdf)["status"] == "unknown"
    assert lanes.classify_terms("PK\x03\x04" + "x" * 500)["status"] == "unknown"
    site.route("/terms.pdf", (200, {"content-type": "application/pdf"}, b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + bytes(range(256)) * 20))
    r = _lane(site, html_page(FILLER + "<a href='/terms.pdf'>Terms and Conditions</a>"))
    assert r["lane"] == "html" and r["terms"]["status"] == "unknown" and r["terms"]["url"].endswith("/terms.pdf")
    assert lanes.classify_terms("<p>" + "Ordinary terms text. " * 40 + "</p>")["status"] == "no_clause"


@pytest.mark.parametrize("clause", [
    "Do not scrape or crawl this site with automated means.",  # trials 2026-10: 'do not' was not a prohibition
    "You don't use bots to collect data from the site.",
    "Het is niet toegestaan om geautomatiseerde middelen te gebruiken om vacatures te verzamelen.",
    "Pengguna dilarang menggunakan robot atau perayap otomatis.",
    "Zakazane jest w szczególności automatyczne pobieranie przez kogokolwiek danych zawartych w Serwisie.",  # justjoin.it §11.3
    "يحظر استخدام برامج الزحف أو الوسائل الآلية لاستخراج البيانات",
])
def test_classify_terms_trial_languages(clause):
    assert lanes.classify_terms(f"<p>{clause} " + "Other terms. " * 60 + "</p>")["status"] == "forbids"
    assert lanes.classify_terms("<p>Do not hesitate to contact us. " + "Other terms. " * 60 + "</p>")["status"] == "no_clause"


# ---------------------------------------------------------------- regressions from the 2026-09-25 live pilot
def test_blocked_sites_are_recorded_outside_the_budget():
    """Pilot: the census brief said 'add blocked sites with a note' but the evidence rule rejected them,
    so market leaders answering 403 (myauto.ge, turbo.az) were silently missing from the registry."""
    p = make_project(max_sources=1, regions=("GE", "AZ"))
    ev = lambda u, o: [{"url": u, "observation": o}]  # noqa: E731
    res = census.add(p, [
        {"url": "https://auto.ge", "regions": ["GE"], "angle": "vertical_portals", "evidence": ev("https://auto.ge/cars", "30 ads")},
        {"url": "https://www.myauto.ge/ka/s/cars", "regions": ["GE"], "angle": "vertical_portals", "blocked": True,
         "evidence": ev("https://www.myauto.ge/ka/s/cars", "HTTP 403 to anonymous fetch")},
        {"url": "https://turbo.az", "regions": ["AZ"], "angle": "classifieds", "blocked": True, "evidence": ev("https://turbo.az", "HTTP 403")},
        {"url": "https://other.ge", "regions": ["GE"], "evidence": ev("https://other.ge", "ads")},
    ])
    assert [a["id"] for a in res["added"]] == ["auto_ge", "myauto_ge", "turbo_az"]
    assert "max_sources" in res["rejected"][0]["reason"] and res["total_sources"] == 1 and res["blocked_sources"] == 2
    assert p.store.get_source(p.name, "myauto_ge")["status"] == "blocked"
    g = census.gaps(p)
    assert g["blocked"] == ["myauto.ge", "turbo.az"] and g["matrix"]["AZ"]["classifieds"] == 1 and g["sources"] == 1
    bad = census.add(p, [{"url": "https://x.ge", "regions": ["GE"], "blocked": True, "evidence": ev("https://google.com", "403")}])
    assert "own domain" in bad["rejected"][0]["reason"]


def test_dry_rounds_do_not_claim_saturation_with_empty_cells_or_a_full_budget():
    """Pilot: two dry critic rounds were reported as 'saturated' although 11 of 16 cells were empty,
    and in another project only because max_sources was reached."""
    p = make_project(max_sources=2)
    ev = lambda u: [{"url": u, "observation": "ads"}]  # noqa: E731
    census.add(p, [{"url": "https://a.ge", "regions": ["GE"], "angle": "classifieds", "evidence": ev("https://a.ge")}], "r1")
    census.add(p, [], "critic:r1")
    census.add(p, [], "critic:r2")
    g = census.gaps(p)
    assert g["dry_streak"] == 2 and not g["saturated"] and "cells are empty" in g["next_step"]
    census.add(p, [{"url": "https://b.ge", "regions": ["GE"], "angle": "classifieds", "evidence": ev("https://b.ge")}], "r2")
    census.add(p, [{"url": "https://c.ge", "regions": ["GE"], "evidence": ev("https://c.ge")}], "critic:r3")
    census.add(p, [], "critic:r4")
    g = census.gaps(p)
    assert g["budget_reached"] and not g["saturated"] and "do not prove coverage" in g["next_step"]


def test_organization_jsonld_is_not_item_data(site):
    """Pilot: mitula.pt got lane embedded_json from its own Organization JSON-LD, which almost every site has."""
    ld = '<script type="application/ld+json">{"@type":"Organization","name":"Portal","url":"/"}</script>'
    r = _lane(site, html_page(FILLER, head=ld))
    assert r["lane"] == "html" and r["signals"]["jsonld_types"] == ["Organization"]
    # trials 2026-10: for a businesses project the site's own LocalBusiness/Organization block counted as listings, so three
    # articles got lane embedded_json; only entities that are not the site itself count
    site.route("/list", html_page(FILLER, head=ld.replace("Organization", "LocalBusiness")))
    r = lanes.detect(site.url + "/list", http=_http(), use_mcp=False, record_type="businesses")
    assert r["lane"] == "html" and r["signals"]["jsonld_business_listings"] == 0
    listing = "".join(f'<script type="application/ld+json">{{"@type":"LocalBusiness","name":"Solar Co {i}","url":"/company/{i}","telephone":"04 1"}}</script>'
                      for i in range(3))
    site.route("/list", html_page(FILLER, head=ld + listing))
    r = lanes.detect(site.url + "/list", http=_http(), use_mcp=False, record_type="businesses")
    assert r["lane"] == "embedded_json" and r["signals"]["jsonld_business_listings"] == 3


def test_tiny_feeds_do_not_win_the_lane(site):
    """Pilot: mankana.com got lane feed from a one-item 'last ad' feed."""
    head = '<link rel="alternate" type="application/rss+xml" href="/last.xml">'
    rss = "<rss><channel><item><title>A</title><link>/a</link></item></channel></rss>"
    r = _lane(site, html_page(FILLER, head=head), extra_routes={"/last.xml": (200, {"content-type": "application/rss+xml"}, rss),
                                                                   "/openapi.json": (404, {}, "")})
    assert r["lane"] == "html" and r["signals"]["feed_items"] == 1


def test_redetection_keeps_a_reviewed_terms_verdict(site):
    """Pilot: re-running lane detection overwrote the auditor's reviewed 'no_clause' verdicts with 'unknown'."""
    p = make_project()
    site.route("/robots.txt", (200, {}, "User-agent: *\n"))
    site.route("/list", html_page(FILLER))  # no terms link: the heuristic says unknown
    sid = census.add(p, [{"url": site.url + "/list", "regions": ["KZ"], "evidence": [{"url": site.url + "/list", "observation": "ads"}]}])["added"][0]["id"]
    lanes.record_policy(p, sid, terms_status="no_clause", terms_url=site.url + "/legal", reason="auditor read §1-9")
    out = service.detect_lane(p.name, sid)
    src = p.store.get_source(p.name, sid)
    assert out["terms"]["reviewed"] and src["terms_status"] == "no_clause" and src["terms_url"] == site.url + "/legal" and src["lane"] == "html"
    # a heuristic 'forbids' still wins over an older permissive verdict
    site.route("/terms", html_page("<p>You may not use robots or scrapers.</p>"))
    site.route("/list", html_page(FILLER + "<a href='/terms'>Terms</a>"))
    assert service.detect_lane(p.name, sid)["lane"] == "none"
    # and a reviewed 'forbids' keeps the lane none even when the page shows no clause
    lanes.record_policy(p, sid, terms_status="forbids", terms_url=site.url + "/terms", terms_clause="no robots")
    site.route("/list", html_page(FILLER))
    assert service.detect_lane(p.name, sid)["lane"] == "none"


def test_operator_can_correct_a_source_url(site):
    """Pilot: a wrong entry URL (404) could only be fixed by editing the database."""
    p = make_project()
    sid = census.add(p, [{"url": "https://ss.ge/ka/transport", "regions": ["GE"], "evidence": [{"url": "https://ss.ge/x", "observation": "ads"}]}])["added"][0]["id"]
    p.store.upsert_source(p.name, sid, {"lane": "none", "status": "lane_none", "reviewed_sha": "abc"})
    out = service.update_source(p.name, sid, url="https://ss.ge/ka/transport/cars")["source"]
    assert out["url"].endswith("/cars") and out["status"] == "candidate" and out["lane"] is None and out["reviewed_sha"] is None
    with pytest.raises(ValueError):
        service.update_source(p.name, sid, url="https://other.ge/")


def test_census_resume_readds_deferred_and_skips_covered_cells():
    """Pilot: both censuses hit max_sources=8; the auditors' verified finds were rejected and would have had to be
    found again. Budget rejections are now deferred with their evidence and re-added on resume."""
    p = make_project(max_sources=2, regions=("PT",))
    ev = lambda u: [{"url": u, "observation": "rental ads"}]  # noqa: E731
    census.add(p, [{"url": "https://a.pt", "regions": ["PT"], "angle": "vertical_portals", "evidence": ev("https://a.pt")},
                   {"url": "https://b.pt", "regions": ["PT"], "angle": "classifieds", "evidence": ev("https://b.pt")}], "r1")
    res = census.add(p, [{"url": "https://c.pt", "regions": ["PT"], "angle": "aggregators", "evidence": ev("https://c.pt/list")},
                         {"url": "https://d.pt", "regions": ["PT"], "angle": "operators", "evidence": ev("https://d.pt")}], "audit:r1")
    assert all(r.get("deferred") for r in res["rejected"])
    g = census.gaps(p)
    assert g["budget_reached"] and [d["domain"] for d in g["deferred"]] == ["c.pt", "d.pt"] and "census-resume" in g["next_step"]
    with pytest.raises(ValueError):
        census.resume(p, 2)
    out = service.census_resume(p.name, 3)
    assert [a["domain"] for a in out["readded"]] == ["c.pt"] and [d["domain"] for d in out["still_deferred"]] == ["d.pt"]
    assert out["covered"]["PT"] == {"vertical_portals": 1, "classifieds": 1, "aggregators": 1}
    assert "a.pt" in out["continuation"] and "PT/official" in out["continuation"] and "PT/classifieds" not in out["continuation"].split("ONLY")[1]
    assert project_mod.load(p.name).spec.max_sources == 3
    src = p.store.get_source(p.name, "c_pt")
    assert src["evidence"][0]["url"] == "https://c.pt/list"  # the original evidence travelled with the deferral
    plan = census.plan(p)["regions"][0]
    assert "official" in plan["todo_angles"] and plan["covered_angles"]["classifieds"] == 1
    out = service.census_resume(p.name, 10)
    assert [a["domain"] for a in out["readded"]] == ["d.pt"] and out["still_deferred"] == []


def test_census_resume_can_start_an_agent_with_the_continuation(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_JOB_MODE", "queue")
    p = make_project(max_sources=1, regions=("PT",))
    census.add(p, [{"url": "https://a.pt", "regions": ["PT"], "evidence": [{"url": "https://a.pt", "observation": "ads"}]}])
    out = service.census_resume(p.name, 5, start_agent=True)
    job = p.store.get_job(out["job"]["job_id"])
    assert job["kind"] == "agent:census" and job["status"] == "queued" and "CONTINUATION" in job["params"]["extra"]
    from harvest_ai import agents
    assert "CONTINUATION" in agents.brief("census", p, extra=job["params"]["extra"])
    # trials 2026-10: when the re-added deferred candidates fill the raised budget, an agent could only defer what it finds
    ev = lambda u: [{"url": u, "observation": "ads"}]  # noqa: E731
    census.add(p, [{"url": f"https://s{i}.pt", "regions": ["PT"], "evidence": ev(f"https://s{i}.pt")} for i in range(8)])
    assert len(service.census_gaps(p.name)["deferred"]) == 4
    out = service.census_resume(p.name, 7, start_agent=True)
    assert out["budget_reached"] and out["job"]["status"] == "skipped" and len(out["still_deferred"]) == 2
