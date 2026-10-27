"""Browser user-agent mode: the decision gate, the header set, runs recording, and that a
challenge page still stops the source."""

import asyncio
import json

import pytest
from conftest import add_source, make_project
from localsite import CAR_MODULE, Site, cars_site, html_page, start

from harvest_ai import cli, proxy, review, service, uamode
from harvest_ai.http import BROWSER_HEADERS, DEFAULT_UA, Http, browser_ua, chrome_ua


def _ua_gated_cars():
    """A site whose WAF refuses non-browser user agents (a plain 403), like the CloudFront rules seen live."""
    s = cars_site()
    inner = s.routes["/cars"]
    s.route("/cars", lambda req: inner(req) if "Chrome/" in req.headers.get("User-Agent", "") and "harvest" not in req.headers.get("User-Agent", "")
            else (403, {"content-type": "text/html"}, "<h1>ERROR: The request could not be satisfied</h1>"))
    return start(s)


def _ready(p, s):
    sid = add_source(p, s.url + "/cars", regions=("KZ",))
    p.store.upsert_source(p.name, sid, {"lane": "html", "status": "lane_detected"})
    p.module_path(sid).write_text(CAR_MODULE, encoding="utf-8")
    return sid


def test_header_set(site):
    seen = []
    site.route("/h", lambda req: seen.append(req.headers) or "<p>x</p>" * 50)
    Http(rate_s=0.01, ua_mode="browser").get_text(site.url + "/h")
    Http(rate_s=0.01).get_text(site.url + "/h")
    b, own = seen
    assert b["User-Agent"] == browser_ua() and "Chrome/153.0.0.0" in b["User-Agent"] and "harvest" not in b["User-Agent"].lower()
    assert b["Accept-Language"] == BROWSER_HEADERS["Accept-Language"] and b["Accept"].startswith("text/html")
    assert own["User-Agent"] == DEFAULT_UA
    assert chrome_ua("153.0.8010.12").endswith("Chrome/153.0.0.0 Safari/537.36")
    with pytest.raises(ValueError):
        Http(ua_mode="stealth")


def test_ua_override_env(monkeypatch):
    monkeypatch.setenv("HARVEST_BROWSER_UA", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
    assert Http(ua_mode="browser").ua.endswith("Chrome/154.0.0.0 Safari/537.36")


def test_robots_txt_is_ignored_in_both_modes(site):
    site.route("/robots.txt", (200, {"content-type": "text/plain"},
                               "User-agent: harvest-bot\nDisallow: /\n\nUser-agent: *\nDisallow: /\nCrawl-delay: 30\n"))
    site.route("/cars", "<p>x</p>" * 50)
    own, br = Http(rate_s=0.01), Http(rate_s=0.01, ua_mode="browser")
    assert own.get_text(site.url + "/cars") is not None and br.get_text(site.url + "/cars") is not None
    assert br._host(site.url.split("//")[1]).delay == 0.01 and site.count("/robots.txt") == 0


def test_decision_gate(cars):
    p = make_project()
    sid = add_source(p, cars.url + "/cars")
    src = lambda: p.store.get_source(p.name, sid)  # noqa: E731
    assert uamode.mode_for(p, src()) == "own" and "no operator decision" in uamode.eligible(p, src())[1]
    with pytest.raises(ValueError, match="reason"):
        uamode.enable(p, reason="short")
    uamode.enable(p, reason="the site refuses non-browser agents", by="test")
    # no robots/terms condition: the operator decision is enough
    assert uamode.mode_for(p, src(), "run") == "browser" and uamode.mode_for(p, src(), "detect") == "browser"
    assert uamode.mode_for(p, src(), "run", enabled=False) == "own"
    p.store.upsert_source(p.name, sid, {"lane": "none", "lane_detail": {"reason": "page not publicly fetchable (blocked 403 challenge)"}})
    assert uamode.mode_for(p, src(), "run") == "own"
    p.store.upsert_source(p.name, sid, {"lane": "html", "lane_detail": {"reason": "server-rendered HTML"}})
    uamode.disable(p, source_id=sid, reason="keep this one on harvest's UA")
    assert uamode.eligible(p, src()) == (False, "switched off for this source")  # the source decision wins
    assert not proxy.switched_on(p, src())[0]  # decided separately from the proxy route
    assert [h["action"] for h in uamode.settings(p)["history"]] == ["enable", "disable"]
    for bad in ({"ua_mode": "browser"}, {"user_agent": "x"}):
        with pytest.raises(ValueError, match="operator decision"):
            service.update_source(p.name, sid, **bad)


def test_review_and_run_record_ua_mode():
    s = _ua_gated_cars()
    try:
        p = make_project()
        sid = _ready(p, s)
        assert review.review(p, sid, timeout_s=60)["verdict"] == "fail"  # harvest's own UA is refused: a finding
        service.ua_enable(p.name, "the WAF refuses non-browser user agents on every page", source_id=sid)
        rep = review.review(p, sid, timeout_s=60)
        assert rep["verdict"] == "pass" and rep["ua_mode"] == "browser" and rep["smoke"]["http"]["ua_mode"] == "browser"
        assert review.enable(p, sid)["enabled"]
        r = service.run(p.name, max_pages=2)["ran"][0]
        assert r["rows_stored"] == 20 and r["ua_mode"] == "browser"
        run = p.store.list_runs(p.name, sid, 1)[0]
        assert run["ua_mode"] == "browser" and run["http_stats"]["ua_mode"] == "browser"
        st = service.status(p.name)
        assert st["per_source"][0]["ua_mode"] == "browser" and st["ua"]["sources"][0]["decision"]["enabled"]
        assert st["recent_runs"][0]["ua_mode"] == "browser"
        r = service.run(p.name, max_pages=1, use_browser_ua=False)["ran"][0]
        assert r["ua_mode"] == "own" and r["rows_stored"] == 0 and p.store.list_runs(p.name, sid, 1)[0]["ua_mode"] == "own"
    finally:
        s.server.shutdown()


def test_challenge_pages_still_stop_the_source():
    s = Site()
    s.route("/robots.txt", (200, {"content-type": "text/plain"}, "User-agent: *\nAllow: /\n"))
    s.route("/cars", html_page("<h1>Just a moment...</h1><div class='cf-chl-widget'></div>", "Just a moment..."))
    s = start(s)
    try:
        p = make_project()
        sid = add_source(p, s.url + "/cars")
        service.ua_enable(p.name, "try the browser user agent on this project")
        res = service.detect_lane(p.name, sid, use_mcp=False)
        assert res["lane"] == "none" and "challenge" in res["reason"] and res["ua_mode"] == "browser"
        src = p.store.get_source(p.name, sid)
        assert not src["enabled"] and uamode.mode_for(p, src, "detect") == "own"  # a challenge lane is never retried with it
        # a reviewed, enabled module still gets nothing from a challenge page
        p.store.upsert_source(p.name, sid, {"lane": "html", "lane_detail": {"reason": "server-rendered HTML"}})
        p.module_path(sid).write_text(CAR_MODULE, encoding="utf-8")
        rep = review.review(p, sid, timeout_s=60)
        assert rep["verdict"] == "fail" and rep["ua_mode"] == "browser" and rep["smoke"]["http"]["blocked"] >= 1
    finally:
        s.server.shutdown()


def test_surfaces(capsys):
    make_project(name="uacli")
    assert cli.main(["ua", "enable", "uacli", "--reason", "site refuses non-browser agents"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"]["enabled"] and out["decision"]["by"] == "cli" and "Chrome/" in out["browser_headers"]["User-Agent"]
    assert cli.main(["ua", "disable", "uacli"]) == 0 and json.loads(capsys.readouterr().out)["decision"]["enabled"] is False
    with pytest.raises(SystemExit):
        cli.main(["ua", "enable", "uacli"])
    from harvest_ai import agents
    from harvest_ai.mcp_server import server
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"harvest_ua_status", "harvest_ua_enable", "harvest_ua_disable"} <= names
    p = make_project(name="uaagents")
    for kind in ("census", "audit"):
        assert not any("_ua_" in t for t in agents.allowed_tools(kind, p))


def test_web_toggle():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from harvest_ai.web.app import create_app
    c = TestClient(create_app())
    auth = {"Authorization": "Bearer test-token"}
    make_project(name="uaweb")
    assert c.post("/api/projects/uaweb/ua", json={"enabled": True}, headers=auth).status_code == 400
    r = c.post("/api/projects/uaweb/ua", json={"enabled": True, "reason": "site refuses non-browser agents"}, headers=auth)
    assert r.status_code == 200 and r.json()["decision"]["by"] == "web:admin"
    assert c.get("/api/projects/uaweb", headers=auth).json()["ua"]["decision"]["enabled"]
    assert c.get("/api/projects/uaweb/ua", headers=auth).json()["decision"]["enabled"]

