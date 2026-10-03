"""Browser lane with a real headless Chromium (skipped unless Playwright and a browser are installed:
`pip install 'harvest[browser]' && playwright install chromium`)."""

import pytest
from conftest import add_source, make_project
from localsite import html_page

from harvest_ai import review, runner, sandbox

pw = pytest.importorskip("playwright.sync_api")


def _browser_ok() -> bool:
    try:
        with pw.sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _browser_ok(), reason="no Playwright browser installed")

JS_SHELL = html_page("<div id='root'></div><script>"
                     "const ads=[['b1','Hyundai Tucson','9 500 000 ₸'],['b2','Kia Rio','5 100 000 ₸'],['b3','Lada Vesta','4 200 000 ₸']];"
                     "document.getElementById('root').innerHTML=ads.map(a=>`<article class='ad' data-id='${a[0]}'><a href='/c/${a[0]}'><h2>${a[1]}</h2></a>"
                     "<span class='price'>${a[2]}</span></article>`).join('');</script>")

MODULE = '''from harvest_ai import extract


def fetch(page, *, http, ctx):
    if page > 1:
        return {"rows": [], "done": True}
    html = http.render(ctx["source"]["url"], wait_ms=300)
    if not html:
        return []
    rows = []
    for card in extract.parse_html(html).find_all("article", cls="ad"):
        a = card.find("a")
        make, _, model = card.find("h2").text.partition(" ")
        rows.append({"source_id": card.get("data-id"), "url": a.get("href"), "title": card.find("h2").text,
                     "make": make, "model": model, "price": card.find("span", cls="price").text})
    return {"rows": rows, "done": True}
'''


def test_render_in_sandbox_and_browser_lane_end_to_end(site):
    site.route("/robots.txt", (200, {}, "User-agent: *\nAllow: /\n"))
    site.route("/js", JS_SHELL)
    from harvest_ai import lanes
    from harvest_ai.http import Http
    assert lanes.detect(site.url + "/js", http=Http(rate_s=0.01), use_mcp=False)["lane"] == "browser"
    p = make_project()
    sid = add_source(p, site.url + "/js")
    p.store.upsert_source(p.name, sid, {"lane": "browser", "robots_status": "allowed", "terms_status": "no_clause", "terms_url": site.url + "/t"})
    p.module_path(sid).write_text(MODULE, encoding="utf-8")
    res = sandbox.run(review.job_for(p, p.store.get_source(p.name, sid), None, max_pages=1), 90)
    assert res["pages"] and not res["pages"][0]["error"], res
    assert [r["source_id"] for r in res["pages"][0]["rows"]] == ["b1", "b2", "b3"]
    rep = review.review(p, sid, timeout_s=90)
    assert rep["verdict"] == "pass", rep
    assert review.enable(p, sid)["enabled"]
    r = runner.run(p)["ran"][0]
    assert r["rows_stored"] == 3 and r["pages"] == 1 and not r["complete"] and r["stopped"] == "page1_only" and r["write_mode"] == "upsert"


def test_render_respects_robots(site):
    site.route("/robots.txt", (200, {}, "User-agent: *\nDisallow: /js\n"))
    site.route("/js", JS_SHELL)
    from harvest_ai.http import Http
    h = Http(rate_s=0.01)
    assert h.render(site.url + "/js") is None and h.stats["robots_denied"] == 1 and "/js" not in site.paths()


def test_render_treats_4xx_as_a_block(site):
    site.route("/robots.txt", (200, {}, "User-agent: *\n"))
    site.route("/denied", (403, {"content-type": "text/html"}, html_page("<p>Access denied</p>")))
    from harvest_ai.http import Http
    h = Http(rate_s=0.01)
    assert h.render(site.url + "/denied") is None and h.stats["blocked"] == 1



def test_browser_lane_uses_the_proxy_route_only_after_an_ip_block(site):
    from fakeproxy import FakeProxy
    from localsite import Site, start

    from harvest_ai.http import Http
    from harvest_ai.proxy import Route, parse_line
    fp = FakeProxy().start()
    wall = start(Site())
    try:
        site.route("/robots.txt", (200, {}, "User-agent: *\nAllow: /\n"))
        site.route("/js", lambda req: JS_SHELL if fp.is_proxied(req.peer) else (403, {"content-type": "text/html"}, "<h1>403 Forbidden</h1>"))
        wall.route("/robots.txt", (200, {}, "User-agent: *\nAllow: /\n"))
        wall.route("/cf", (403, {"content-type": "text/html"}, "<title>Just a moment...</title><div class='cf-chl-widget'></div>"))
        h = Http(rate_s=0.01, route=Route(_exits=[parse_line(fp.line)], allowance_bytes=5_000_000, allowance_requests=10))
        html = h.render(site.url + "/js", wait_ms=300)
        assert html and "Hyundai Tucson" in html, h.events
        assert h.stats["proxy_fallbacks"] == 1 and h.stats["proxy_ok"] == 1 and h.proxy_report()["bytes"] > 0
        assert "puser1" not in str(h.events) and "s3cret" not in str(h.events)
        n = len(fp.hits)
        assert h.render(wall.url + "/cf", wait_ms=100) is None and len(fp.hits) == n  # a challenge is never proxied
    finally:
        wall.server.shutdown()
        fp.stop()


def test_browser_ua_mode_uses_the_launched_builds_chrome_ua(site):
    from harvest_ai.http import Http
    seen = []
    site.route("/robots.txt", (200, {}, "User-agent: *\nAllow: /\n"))
    site.route("/js", lambda req: seen.append(req.headers.get("User-Agent")) or JS_SHELL)
    assert Http(rate_s=0.01, ua_mode="browser").render(site.url + "/js", wait_ms=200)
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        major = b.version.split(".")[0]
        b.close()
    assert seen and f"Chrome/{major}.0.0.0" in seen[0] and "Headless" not in seen[0] and "harvest" not in seen[0]


def test_browser_egress_is_pinned_against_dns_rebinding(site, monkeypatch):
    """Security review 2026-10 (O3): the gate's lookups say public, the browser's connection would reach loopback.
    Chromium goes through harvest's filtering proxy, which resolves once, refuses, and the site never sees a request."""
    import socket as _socket

    from harvest_ai.http import Http
    site.route("/cars", (200, {"content-type": "text/html"}, JS_SHELL))
    port = int(site.url.rsplit(":", 1)[1])
    real = _socket.getaddrinfo
    calls = {"n": 0}

    def flip(host, *a, **k):
        if host != "rebind.test":
            return real(host, *a, **k)
        calls["n"] += 1
        ip = "93.184.215.14" if calls["n"] <= 2 else "127.0.0.1"
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(_socket, "getaddrinfo", flip)
    http = Http(allow_private=False, respect_robots=False, timeout=8)
    try:
        assert http.render(f"http://rebind.test:{port}/cars", wait_ms=200) is None
        assert http._egress_proxy is not None and any("127.0.0.1" in r for r in http._egress_proxy.refused)
        assert "/cars" not in site.paths()
    finally:
        http.close()
