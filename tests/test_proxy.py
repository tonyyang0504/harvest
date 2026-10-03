"""The residential-proxy route: pool parsing, redaction, selection, the three gates, budgets, and end-to-end
through a local fake proxy with a target that 403s the direct connection and answers the proxied one."""

import json
import random
import sys

import pytest
from conftest import add_source, make_project
from fakeproxy import FakeProxy
from localsite import CAR_MODULE, RESET, Site, cars_site, start

from harvest_ai import cli, proxy, review, service
from harvest_ai.http import Http
from harvest_ai.proxy import Route, classify, exit_id, parse_line, redact, to_url, with_country

PW = "s3cret-Pw9"


@pytest.fixture
def fp():
    f = FakeProxy(password=PW).start()
    yield f
    f.stop()


@pytest.fixture
def pool(fp, tmp_path, monkeypatch):
    """A pool file with the fake proxy as its only exit (plus a comment and a malformed line)."""
    path = tmp_path / "pool.txt"
    path.write_text(f"# residential pool\n{fp.line}\nnot-a-proxy\n", encoding="utf-8")
    monkeypatch.setenv("HARVEST_PROXY_FILE", str(path))
    monkeypatch.setenv("HARVEST_PROXY_HEALTHCHECK", "0")
    monkeypatch.setenv("HARVEST_PROXY_COUNTRY", "off")
    return path


def _route(fp, **kw):
    kw.setdefault("allowance_bytes", 10_000_000)
    kw.setdefault("allowance_requests", 100)
    return Route(_exits=[parse_line(fp.line)], **kw)


def _http(route=None, **kw):
    kw.setdefault("rate_s", 0.01)
    kw.setdefault("backoff_s", 0.02)
    kw.setdefault("retries", 2)
    return Http(route=route, **kw)


def _gated_site(fp, body="<p>listing</p>" * 100):
    """403 plain for a direct connection, 200 for one that came through the fake proxy (told apart by peer port)."""
    s = start(Site())
    s.route("/robots.txt", (200, {"content-type": "text/plain"}, "User-agent: *\nDisallow: /private\n"))
    s.route("/list", lambda req: body if fp.is_proxied(req.peer) else (403, {"content-type": "text/html"}, "<h1>403 Forbidden</h1>"))
    return s


# ---------------------------------------------------------------- parsing
def test_parse_formats():
    assert parse_line("p.webshare.io:80:user-1:pw") == {"server": "http://p.webshare.io:80", "username": "user-1", "password": "pw"}
    assert parse_line("10.0.0.1:3128") == {"server": "http://10.0.0.1:3128", "username": None, "password": None}
    assert parse_line("http://us%40er:p%3Aw@gw.example.net:8080") == {"server": "http://gw.example.net:8080", "username": "us@er", "password": "p:w"}
    assert parse_line("socks5://u:p@h.example:1080")["server"] == "socks5://h.example:1080"
    for bad in ("", "   ", "# comment", "host", "host:port", "a:1:b", "http://nohost", "http://h:notaport", ":80:u:p"):
        assert parse_line(bad) is None, bad
    assert to_url({"server": "http://h:1", "username": "a b", "password": "p@ss:"}) == "http://a%20b:p%40ss%3A@h:1"
    assert to_url({"server": "http://h:1", "username": None, "password": None}) == "http://h:1"
    assert exit_id(parse_line("h.example:80:u:p")) == exit_id(parse_line("http://u:p@h.example:80"))
    assert "p" not in exit_id(parse_line("h.example:80:u:verysecret"))[1:] or "verysecret" not in exit_id(parse_line("h.example:80:u:verysecret"))


def test_load_file_limit_and_url_gateway(tmp_path, monkeypatch):
    f = tmp_path / "p.txt"
    f.write_text("\n".join(f"h{i}.example:80:user{i}:pw{i}" for i in range(50)) + "\n#x\nbad\n", encoding="utf-8")
    monkeypatch.setenv("HARVEST_PROXY_FILE", str(f))
    assert len(proxy.load()) == 50 and len(proxy.load(limit=7)) == 7 and proxy.configured()
    monkeypatch.setenv("HARVEST_PROXY_URL", "http://gwuser:gwpass@gw.example:9000")
    assert proxy.load() == [{"server": "http://gw.example:9000", "username": "gwuser", "password": "gwpass"}]
    monkeypatch.delenv("HARVEST_PROXY_URL")
    monkeypatch.setenv("HARVEST_PROXY_FILE", str(tmp_path / "missing.txt"))
    assert proxy.load() == []


def test_with_country():
    base = {"server": "http://h:1", "username": "user-7", "password": "p"}
    assert with_country(base, "ge")["username"] == "user-7-GE"
    assert with_country({**base, "username": "user-7-GE"}, "PT")["username"] == "user-7-GE"  # already targeted
    assert with_country(base, None) == base and with_country(base, "GEO") == base
    assert with_country({**base, "username": None}, "GE")["username"] is None
    assert base["username"] == "user-7"  # never mutated


# ---------------------------------------------------------------- redaction
def test_redaction_everywhere():
    proxy._remember({"username": "acctuser42", "password": "Zx9-pass-Qq"})
    cases = {
        "ProxyError: http://acctuser42:Zx9-pass-Qq@p.webshare.io:80 refused": "Zx9-pass-Qq",
        "line p.webshare.io:80:someone:hunter22 failed": "hunter22",
        "headers {'Proxy-Authorization': 'Basic dXNlcjpwYXNz'}": "dXNlcjpwYXNz",
        "the password Zx9-pass-Qq leaked alone": "Zx9-pass-Qq",
        "user acctuser42 alone": "acctuser42",
    }
    for text, secret in cases.items():
        out = redact(text)
        assert secret not in out and "***" in out, out
    assert redact(None) == "" and redact("nothing to hide at https://example.com/a:b") == "nothing to hide at https://example.com/a:b"
    r = Route(_exits=[{"server": "http://h:1", "username": "acctuser42", "password": "Zx9-pass-Qq"}], allowance_bytes=1, allowance_requests=1)
    assert "Zx9" not in repr(r) and "Zx9" not in json.dumps(r.report())
    assert "Zx9" not in str(proxy.ProxyError("boom http://acctuser42:Zx9-pass-Qq@h:1"))


def test_http_events_never_carry_credentials(fp, site):
    fp.refuse = 407  # every exit refuses: errors mention the proxy
    site.route("/list", (403, {"content-type": "text/html"}, "denied"))
    h = _http(_route(fp))
    assert h.get_text(site.url + "/list") is None
    blob = json.dumps(h.events) + json.dumps(h.stats) + json.dumps(h.proxy_report())
    assert PW not in blob and fp.user not in blob
    assert h.stats["proxy_exit_failures"] >= 1 and h.proxy_report()["failed_exits"]


# ---------------------------------------------------------------- selection, stickiness, cool-down
def test_sticky_rotate_and_cooldown(monkeypatch):
    monkeypatch.setenv("HARVEST_PROXY_HEALTHCHECK", "0")
    pool = [parse_line(f"h{i}.example:80:u{i}:p{i}") for i in range(40)]
    a1, _ = proxy.select(pool, key="proj:src_a", mode="sticky")
    a2, _ = proxy.select(pool, key="proj:src_a", mode="sticky")
    assert exit_id(a1[0]) == exit_id(a2[0])  # a stable session per source key
    firsts = {exit_id(proxy.select(pool, key=f"proj:s{i}", mode="sticky")[0][0]) for i in range(20)}
    assert len(firsts) > 5  # different sources spread over the pool
    proxy.cooldown(exit_id(a1[0]), "http_402")
    a3, _ = proxy.select(pool, key="proj:src_a", mode="sticky")
    assert exit_id(a3[0]) != exit_id(a1[0])  # a cooling exit is skipped
    assert "http_402" in proxy._health_read()[exit_id(a1[0])]["reason"]
    rot, _ = proxy.select(pool, key="k", mode="rotate", rng=random.Random(1))
    assert len(rot) == proxy.ROTATE_EXITS and len({exit_id(e) for e in rot}) == len(rot)
    r = Route(_exits=rot, mode="rotate", allowance_bytes=10, allowance_requests=10)
    seen = [exit_id(r.next_exit()) for _ in range(len(rot))]
    assert len(set(seen)) == len(rot)  # a new exit per request
    s = Route(_exits=a3, mode="sticky", allowance_bytes=10, allowance_requests=10)
    first = s.next_exit()
    assert all(exit_id(s.next_exit()) == exit_id(first) for _ in range(5))
    s.fail(first, "ProxyError")
    assert exit_id(s.next_exit()) != exit_id(first)  # fails over to a spare


def test_health_probe_cools_down_a_dead_exit(fp, site, monkeypatch):
    site.route("/probe", "ok")
    monkeypatch.setenv("HARVEST_PROXY_PROBE_URL", site.url + "/probe")
    good = parse_line(fp.line)
    dead = parse_line("127.0.0.1:1:u:p")  # nothing listens on port 1
    exits, spent = proxy.select([dead, good], key="x", mode="rotate", n=2, rng=random.Random(0))
    assert [exit_id(e) for e in exits] == [exit_id(good)] and spent > 0
    assert proxy.cooling(exit_id(dead)) and not proxy.cooling(exit_id(good))
    fp.refuse = 402
    ok, reason, _ = proxy.probe(good)
    assert not ok and reason == "bandwidthlimit"


# ---------------------------------------------------------------- gate (c): classification
def test_classify():
    assert classify(403, {}, "<h1>403 Forbidden</h1>") == "ip_block"
    assert classify(401, {}, "Unauthorized") == "ip_block"
    assert classify(403, {"cf-mitigated": "challenge"}, "") == "challenge"
    assert classify(403, {}, "<title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'>") == "challenge"
    assert classify(403, {"x-datadome": "protected"}, "blocked") == "challenge"
    assert classify(403, {}, "<script src='https://ct.captcha-delivery.com/c.js'>") == "challenge"
    assert classify(403, {}, "<div id='px-captcha'></div>") == "challenge"
    assert classify(401, {"www-authenticate": "Basic realm=x"}, "") == "auth"
    assert classify(403, {}, "<form><input type='password' name='pw'></form>") == "login"
    assert classify(403, {}, "Subscribe to continue reading") == "paywall"
    assert classify(402, {}, "") == "paywall"
    assert classify(429, {}, "") == "rate_limited" and classify(200, {}, "hello") == "ok"
    assert proxy.is_reset("ConnectError: [Errno 54] Connection reset by peer") and proxy.is_reset("RemoteProtocolError: Server disconnected")
    assert not proxy.is_reset("ConnectError: [Errno 8] nodename nor servname provided") and not proxy.is_reset("ReadTimeout: timed out")


# ---------------------------------------------------------------- gate (c) in the client, through the fake proxy
def test_ip_403_goes_through_the_proxy_and_the_host_stays_on_it(fp):
    s = _gated_site(fp)
    try:
        h = _http(_route(fp))
        r = h.get(s.url + "/list", accept="html")
        assert r is not None and r.via_proxy and "listing" in r.text
        assert h.stats["ip_blocked"] == 1 and h.stats["proxy_fallbacks"] == 1 and h.stats["proxy_ok"] == 1 and h.stats["blocked"] == 0
        direct_hits = s.count("/list")
        r2 = h.get(s.url + "/list?page=2", accept="html")
        assert r2 is not None and r2.via_proxy
        assert s.count("/list?page=2") == 1 and direct_hits == 2  # page 2 went straight through the route
        rep = h.proxy_report()
        assert rep["requests"] == 2 and rep["bytes"] > 1000 and not rep["budget_stop"]
        assert any(m == "GET" and "/list" in u for m, u in fp.hits)
    finally:
        s.server.shutdown()


def test_a_large_page_that_mentions_a_captcha_widget_is_content(fp):
    s = _gated_site(fp, body="<script src='https://www.google.com/recaptcha/api.js'></script>" + "<p>listing</p>" * 3000)
    try:
        h = _http(_route(fp))
        assert h.get_text(s.url + "/list") is not None and h.get_text(s.url + "/list?page=2") is not None
        assert s.count("/list?page=2") == 1 and h.stats["proxy_fallbacks"] == 1  # the host stayed on the route
    finally:
        s.server.shutdown()


@pytest.mark.parametrize("resp", [
    (403, {"content-type": "text/html"}, "<html><title>Just a moment...</title><div class='cf-chl-widget'></div></html>"),
    (403, {"content-type": "text/html", "x-datadome": "1"}, "blocked"),
    (403, {"content-type": "text/html"}, "<html><div id='px-captcha'></div> Access to this page has been denied (PerimeterX)</html>"),
    (401, {"content-type": "text/html", "www-authenticate": "Bearer"}, "sign in"),
    (403, {"content-type": "text/html"}, "<form>Please log in <input type='password'></form>"),
    (403, {"content-type": "text/html"}, "Subscribers only: subscribe to continue"),
    (200, {"content-type": "text/html"}, "<html><title>Just a moment...</title><div class='cf-chl'></div></html>"),
])
def test_never_proxied_on_challenge_login_or_paywall(fp, site, resp):
    site.route("/list", resp)
    h = _http(_route(fp))
    assert h.get_text(site.url + "/list") is None
    assert fp.hits == [] and h.stats["proxy_requests"] == 0 and h.stats["blocked"] == 1


def test_signed_in_requests_never_use_the_route(fp):
    s = _gated_site(fp)
    try:
        h = _http(_route(fp))
        h.allow_session_headers = True  # an operator allowed session headers for the source; still never proxied
        assert h.get(s.url + "/list", headers={"Cookie": "session=abc"}) is None
        assert h.get(s.url + "/list", headers={"Authorization": "Bearer x"}) is None
        assert fp.hits == [] and h.stats["ip_blocked"] == 2
    finally:
        s.server.shutdown()


def test_connection_reset_and_persistent_429_use_the_route(fp):
    s = start(Site())
    s.route("/robots.txt", (200, {"content-type": "text/plain"}, "User-agent: *\n"))
    s.route("/reset", lambda req: "<p>ok</p>" * 50 if fp.is_proxied(req.peer) else RESET)
    s.route("/slow", lambda req: "<p>ok</p>" * 50 if fp.is_proxied(req.peer) else (429, {"content-type": "text/plain", "retry-after": "0"}, "slow"))
    try:
        h = _http(_route(fp), retries=1)
        r = h.get(s.url + "/reset")
        assert r is not None and r.via_proxy
        assert any(e["event"] == "proxy_fallback" and e["trigger"] == "reset" for e in h.events)
        h2 = _http(_route(fp), retries=2)
        r = h2.get(s.url + "/slow")
        assert r is not None and r.via_proxy and h2.stats["rate_limited"] == 3  # the back-off ran first
        assert any(e["event"] == "proxy_fallback" and e["trigger"] == "http_429" for e in h2.events)
    finally:
        s.server.shutdown()


def test_a_wall_behind_the_proxy_switches_the_host_off(fp):
    s = start(Site())
    s.route("/robots.txt", (200, {"content-type": "text/plain"}, "User-agent: *\n"))
    s.route("/list", lambda req: (403, {"content-type": "text/html"}, "<title>Just a moment...</title>cf-chl") if fp.is_proxied(req.peer)
            else (403, {"content-type": "text/html"}, "denied"))
    try:
        h = _http(_route(fp))
        assert h.get_text(s.url + "/list") is None and h.stats["proxy_refused"] == 1
        n = len(fp.hits)
        assert h.get_text(s.url + "/list") is None and len(fp.hits) == n  # never again this walk
    finally:
        s.server.shutdown()


def test_a_block_that_survives_the_proxy_is_not_an_ip_block(fp, site):
    site.route("/list", (403, {"content-type": "text/html"}, "<h1>ERROR: The request could not be satisfied</h1>"))  # e.g. a UA rule
    h = _http(_route(fp))
    assert h.get_text(site.url + "/list") is None and h.stats["proxy_no_help"] == 1
    n = len(fp.hits)
    assert h.get_text(site.url + "/list?page=2") is None and len(fp.hits) == n  # not tried again on this host
    assert any(e["event"] == "proxy_no_help" for e in h.events)


def test_robots_behind_an_ip_block_is_read_through_the_route(fp):
    s = start(Site())
    s.route("/robots.txt", lambda req: (200, {"content-type": "text/plain"}, "User-agent: *\nDisallow: /list\n") if fp.is_proxied(req.peer)
            else (403, {"content-type": "text/html"}, "denied"))
    s.route("/list", "<p>x</p>" * 50)
    try:
        h = _http(_route(fp))
        assert h.get_text(s.url + "/list") is None and h.stats["robots_denied"] == 1  # the real robots.txt, not "403 = allow all"
        assert s.count("/list") == 0
    finally:
        s.server.shutdown()


# ---------------------------------------------------------------- budgets in the client
def test_byte_budget_stops_the_route_mid_response(fp):
    s = _gated_site(fp, body="<p>big</p>" * 50000)  # ~500 KB
    try:
        h = _http(_route(fp, allowance_bytes=100_000))  # the default 64 KiB headroom lets one request start
        assert h.get_text(s.url + "/list") is None
        rep = h.proxy_report()
        assert rep["budget_stop"] and 60_000 < rep["bytes"] <= 100_000 and h.stats["proxy_budget_stop"] == 1  # the body was never read
        h2 = _http(_route(fp, allowance_bytes=50_000))  # under the headroom: no request starts at all
        assert h2.get_text(s.url + "/list") is None and h2.proxy_report()["requests"] == 0 and h2.proxy_report()["budget_stop"]
        assert any(e["event"] == "proxy_budget_stop" for e in h.events)
    finally:
        s.server.shutdown()


def test_request_budget_and_zero_allowance(fp):
    s = _gated_site(fp)
    try:
        h = _http(_route(fp, allowance_requests=1))
        assert h.get_text(s.url + "/list") is not None
        assert h.get_text(s.url + "/list?page=2") is None and h.proxy_report()["budget_stop"]
        h0 = _http(_route(fp, allowance_requests=0))
        assert h0.get_text(s.url + "/list") is None and h0.proxy_report()["budget_stop"] and h0.stats["proxy_requests"] == 0
    finally:
        s.server.shutdown()


# ---------------------------------------------------------------- gates (a) + (b)
def test_decision_and_policy_gates(pool, cars):
    p = make_project(regions=("GE",))
    sid = add_source(p, cars.url + "/cars", regions=("GE",))
    src = lambda: p.store.get_source(p.name, sid)  # noqa: E731
    assert proxy.arm(p, src()) is None and "no operator decision" in proxy.eligible(p, src())[1]
    with pytest.raises(ValueError, match="reason"):
        proxy.enable(p, reason="")
    proxy.enable(p, reason="market leader 403s the datacenter IP", by="test")
    p.store.upsert_source(p.name, sid, {"robots_status": "allowed", "terms_status": "unknown"})
    assert not proxy.eligible(p, src(), "run")[0] and proxy.eligible(p, src(), "detect")[0]  # detection may read the terms
    p.store.upsert_source(p.name, sid, {"terms_status": "forbids"})
    assert not proxy.eligible(p, src(), "detect")[0] and not proxy.eligible(p, src(), "run")[0]
    p.store.upsert_source(p.name, sid, {"terms_status": "no_clause", "robots_status": "disallowed"})
    assert not proxy.eligible(p, src(), "detect")[0]
    p.store.upsert_source(p.name, sid, {"robots_status": "allowed"})
    assert proxy.eligible(p, src(), "run") == (True, "project decision")
    p.store.upsert_source(p.name, sid, {"lane": "none", "lane_detail": {"reason": "content is behind a login"}})
    assert not proxy.eligible(p, src(), "run")[0]
    p.store.upsert_source(p.name, sid, {"lane": "html", "lane_detail": {"reason": "page not publicly fetchable (blocked 403 challenge)"}})
    assert not proxy.eligible(p, src(), "run")[0]
    p.store.upsert_source(p.name, sid, {"lane_detail": {"reason": "server-rendered HTML"}})
    proxy.disable(p, source_id=sid, reason="operator wants it direct")
    assert proxy.eligible(p, src(), "run") == (False, "switched off for this source")  # a source decision wins
    proxy.disable(p)
    proxy.enable(p, reason="only this one source needs it", source_id=sid)
    assert proxy.eligible(p, src(), "run") == (True, "source decision")
    d = src()["lane_detail"]["proxy"]
    assert d["enabled"] and d["reason"] and d["at"] and src()["use_proxy"] == 1
    hist = proxy.settings(p)["history"]
    assert [h["action"] for h in hist] == ["enable", "disable", "disable", "enable"] and all(h["at"] for h in hist)
    with pytest.raises(ValueError, match="operator decision"):
        service.update_source(p.name, sid, use_proxy=True)


# ---------------------------------------------------------------- end to end: detect, review, run, budget stop
def _gated_cars(fp):
    s = cars_site()
    inner = s.routes["/cars"]
    s.route("/cars", lambda req: inner(req) if fp.is_proxied(req.peer) else (403, {"content-type": "text/html"}, "<h1>403 Forbidden</h1>"))
    return start(s)


def test_lane_detection_reprobes_through_the_proxy(fp, pool):
    s = _gated_cars(fp)
    try:
        p = make_project(regions=("GE",))
        sid = add_source(p, s.url + "/cars", regions=("GE",))
        res = service.detect_lane(p.name, sid, use_mcp=False)
        assert res["lane"] == "none" and res.get("ip_block") and "proxy" in res["reason"] and fp.hits == []
        proxy.enable(p, reason="GE market leader blocks the datacenter IP", source_id=sid)
        res = service.detect_lane(p.name, sid, use_mcp=False)
        assert res["lane"] in ("html", "embedded_json") and res["via_proxy"] and res["proxy"]["requests"] >= 1
        src = p.store.get_source(p.name, sid)
        assert src["lane_detail"]["via_proxy"] and src["lane_detail"]["proxy"]["enabled"] and src["status"] == "lane_detected"
        assert src["terms_status"] == "no_clause"  # the terms were read through the route
        assert p.store.proxy_usage_total(p.name, proxy.today())["requests"] >= 1
    finally:
        s.server.shutdown()


def test_review_and_run_through_the_proxy_then_budget_stop_alert(fp, pool, tmp_path, monkeypatch):
    hook = tmp_path / "proxyalerthook.py"
    hook.write_text("SEEN = []\n\ndef send(project, alerts):\n    SEEN.append((project, alerts))\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("HARVEST_ALERT_HOOK", "proxyalerthook:send")
    s = _gated_cars(fp)
    try:
        p = make_project(regions=("GE",))
        sid = add_source(p, s.url + "/cars", regions=("GE",))
        p.store.upsert_source(p.name, sid, {"lane": "html", "robots_status": "allowed", "terms_status": "no_clause", "terms_url": s.url + "/terms",
                                            "status": "lane_detected"})
        p.module_path(sid).write_text(CAR_MODULE, encoding="utf-8")
        assert review.review(p, sid, timeout_s=60)["verdict"] == "fail"  # direct only: the IP block is a finding
        proxy.enable(p, reason="GE leader blocks the datacenter IP; terms read, no clause", daily_bytes=5_000_000, daily_requests=50)
        rep = review.review(p, sid, timeout_s=60)
        assert rep["verdict"] == "pass" and rep["smoke"]["proxy"]["requests"] >= 1, rep["smoke"]
        assert review.enable(p, sid)["enabled"]
        res = service.run(p.name, max_pages=2)
        r = res["ran"][0]
        assert r["rows_stored"] == 20 and r["proxy"]["requests"] >= 2 and r["proxy"]["bytes"] > 0, r
        run = p.store.list_runs(p.name, sid, 1)[0]
        assert run["http_stats"]["proxy"]["requests"] == r["proxy"]["requests"] and run["http_stats"]["proxy_ok"] >= 2
        st = service.status(p.name)
        assert st["proxy"]["today"]["used_requests"] >= 3
        assert st["per_source"][0]["proxy_today"]["requests"] >= 2
        used = st["proxy"]["today"]["used_bytes"]
        # the day's cap is reached: the next run stops the route at the first IP block and alerts once
        service.proxy_enable(p.name, "tighten the cap for the test", daily_bytes=used + 100)
        res = service.run(p.name, max_pages=2)
        r = res["ran"][0]
        assert r["rows_stored"] == 0 and r["proxy"]["budget_stop"], r
        alerts = [a for a in p.store.list_alerts(p.name) if a["kind"] == "proxy_budget"]
        import proxyalerthook
        assert len(alerts) == 1 and proxyalerthook.SEEN and proxyalerthook.SEEN[-1][1][0]["kind"] == "proxy_budget"
        assert p.store.proxy_usage_total(p.name, proxy.today())["bytes"] == used  # nothing started: under the headroom
        service.run(p.name, max_pages=1)
        assert len([a for a in p.store.list_alerts(p.name) if a["kind"] == "proxy_budget"]) == 1  # once a day
        blob = json.dumps(service.status(p.name), default=str) + (p.root / "alerts.jsonl").read_text() + json.dumps(p.store.list_runs(p.name), default=str)
        assert PW not in blob and str(pool) not in blob
        # --no-proxy: a run that must stay direct
        assert service.run(p.name, max_pages=1, use_proxy=False)["ran"][0].get("proxy") is None
    finally:
        s.server.shutdown()


def test_allowance_is_shared_between_walks_of_a_run(pool):
    p = make_project(regions=("GE",))
    proxy.enable(p, reason="shared budget test for two sources", daily_bytes=1000, daily_requests=10)
    sids = []
    for n in ("a", "b"):
        sid = add_source(p, f"https://{n}.example.ge/", regions=("GE",))
        p.store.upsert_source(p.name, sid, {"robots_status": "allowed", "terms_status": "no_clause"})
        sids.append(sid)
    pend = proxy.Pending(2)
    l1 = proxy.arm(p, p.store.get_source(p.name, sids[0]), "run", pend)
    l2 = proxy.arm(p, p.store.get_source(p.name, sids[1]), "run", pend)
    assert l1.reserved == (500, 5) and l2.reserved == (500, 5)
    assert proxy.remaining(p)["bytes"] == 0
    proxy.settle(p, l1, {"requests": 1, "bytes": 100})
    proxy.settle(p, l2, {"requests": 0, "bytes": 0})
    left = proxy.remaining(p)
    assert left["bytes"] == 900 and left["requests"] == 9 and left["reserved_bytes"] == 0


def test_module_may_not_touch_the_route(pool):
    p = make_project()
    sid = add_source(p, "https://x.example.kz/")
    p.module_path(sid).write_text(CAR_MODULE.replace("    base = ctx", "    leak = http._route\n    base = ctx"), encoding="utf-8")
    assert any("private attribute _route" in e for e in review.lint(p.module_path(sid))["errors"])


# ---------------------------------------------------------------- surfaces
def test_cli_proxy_commands(pool, capsys):
    make_project(name="cliproxy")
    assert cli.main(["proxy", "enable", "cliproxy", "--reason", "leader blocks datacenter IPs", "--daily-mb", "2", "--mode", "rotate"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"]["enabled"] and out["decision"]["by"] == "cli" and out["settings"]["daily_bytes"] == 2 * 1024 * 1024
    assert out["settings"]["mode"] == "rotate"
    assert cli.main(["proxy", "status", "cliproxy"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["pool"]["exits"] == 1 and st["pool"]["kind"] == "file" and st["project"]["decision"]["enabled"]
    assert PW not in json.dumps(st)


def test_cli_enable_requires_reason():
    with pytest.raises(SystemExit):
        cli.main(["proxy", "enable", "whatever"])


def test_web_api_toggles(pool):
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from fastapi.testclient import TestClient

    from harvest_ai.web.app import create_app
    c = TestClient(create_app())
    auth = {"Authorization": "Bearer test-token"}
    make_project(name="webproxy")
    r = c.post("/api/projects/webproxy/proxy", json={"enabled": True}, headers=auth)
    assert r.status_code == 400 and "reason" in r.json()["message"]
    r = c.post("/api/projects/webproxy/proxy", json={"enabled": True, "reason": "leader blocks the datacenter IP"}, headers=auth)
    assert r.status_code == 200 and r.json()["decision"]["by"] == "web:admin"
    assert c.get("/api/projects/webproxy", headers=auth).json()["proxy"]["decision"]["enabled"]
    assert c.get("/api/proxy", headers=auth).json()["pool"]["exits"] == 1
    assert c.post("/api/projects/webproxy/proxy", json={"enabled": False}, headers=auth).json()["decision"]["enabled"] is False
    assert c.patch("/api/projects/webproxy/sources/nope", json={"use_proxy": True}, headers=auth).status_code in (404, 422)


def test_mcp_tools_exist_and_agents_cannot_enable():
    import asyncio

    from harvest_ai import agents
    from harvest_ai.mcp_server import server
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"harvest_proxy_status", "harvest_proxy_enable", "harvest_proxy_disable"} <= names
    p = make_project(name="agentsproxy")
    for kind in ("census", "audit"):
        assert not any("proxy" in t for t in agents.allowed_tools(kind, p))
    assert sys.modules.get("harvest_ai.proxy") is not None
