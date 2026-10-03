"""The one HTTP client every lane probe and scraper module uses.

Per request: URL gate (http/https, public addresses only unless `allow_private`) -> robots.txt ->
per-host politeness (one request at a time per host, `rate_s` seconds apart, raised to the site's
Crawl-delay up to a ceiling) -> request -> retries on 429/5xx/network with backoff; a 429 doubles the
host's delay (honouring Retry-After) for the rest of the run -> a per-URL budget of attempts and
seconds -> block detection (401/403/451, challenge pages). Anything disallowed, blocked or failing
returns None and is logged in `events`; callers never see exceptions.

Never logs in and never solves challenges: a block is a finding. The one exception is the gated
residential-proxy route (`proxy.Route`, see proxy.py): when a walk was given one and the direct
request met an IP-level block (401/403 with no challenge/login/paywall page, a connection reset, or
a 429 that outlived the back-off), the same request is retried through the route, under the same
per-host politeness, and later requests to that host go through it for the rest of the walk. A
challenge, login wall or paywall is never retried through it.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from . import proxy as proxy_mod
from . import robots as robots_mod

DEFAULT_UA = os.environ.get("HARVEST_USER_AGENT", "harvest-bot/0.1 (+public data collection; robots.txt honoured)")
RETRY_STATUSES = (429, 500, 502, 503, 504)
BLOCK_STATUSES = (401, 403, 407, 451)
CHALLENGE_MARKERS = ("cf-chl", "just a moment...", "attention required", "captcha", "are you a human", "are you a robot",
                     "verify you are human", "access denied", "enable javascript and cookies", "ddos-guard", "perimeterx", "distil_r_captcha")
# bot-wall interstitials served with HTTP 200: their scripts also appear on ordinary protected pages, so they count only
# when the page has (almost) no text of its own (trials 2026-10: Imperva/Incapsula stubs on dubizzle.com, regus.com)
STUB_MARKERS = ("_incapsula_resource", "incapsula incident", "captcha-delivery.com", "px-captcha", "/cdn-cgi/challenge-platform",
                "kpsdk", "awswaf", "aws-waf-token", "sucuri_cloudproxy", "ddos-guard")
STUB_MAX_TEXT = 300


def is_challenge_page(text: str | None) -> bool:
    """A bot challenge or interstitial (never content): a known challenge phrase on a small page, or a bot-wall script
    on a page with no real text."""
    text = text or ""
    if len(text) >= 20000:
        return False
    low = text[:6000].lower()
    if any(mk in low for mk in CHALLENGE_MARKERS):
        return True
    if any(mk in text.lower() for mk in STUB_MARKERS):
        from .extract import visible_text
        return len(visible_text(text)) < STUB_MAX_TEXT
    return False


MAX_HOST_DELAY = 120.0
CHROME_MAJOR = 153  # current stable desktop Chrome (2026-09); HARVEST_BROWSER_UA overrides the whole string
BROWSER_HEADERS = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                   "Accept-Language": "en-US,en;q=0.9"}


def _platform() -> str:
    import sys
    if sys.platform == "darwin":
        return "Macintosh; Intel Mac OS X 10_15_7"
    if sys.platform.startswith("win"):
        return "Windows NT 10.0; Win64; x64"
    return "X11; Linux x86_64"


def chrome_ua(version: str | int) -> str:
    """Chrome's reduced user-agent string for a major version (minor parts are frozen at 0.0.0 by Chrome itself)."""
    major = str(version).split(".")[0]
    return f"Mozilla/5.0 ({_platform()}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


def browser_ua() -> str:
    return os.environ.get("HARVEST_BROWSER_UA") or chrome_ua(CHROME_MAJOR)
CHUNK_ESTIMATE = 65536  # bytes a cut-off proxied response is charged for what the socket had already buffered
# "harvest's own trusted code is running" (browser, MCP client). Thread-local rather than a contextvar:
# Playwright spawns its driver from a greenlet, and greenlets get their own contextvars context.
_trust = threading.local()


class _Trusted:
    def __enter__(self):
        _trust.depth = getattr(_trust, "depth", 0) + 1

    def __exit__(self, *exc):
        _trust.depth -= 1


_TRUSTED_CALLERS: set = set()


def trusted_caller(fn):
    """Register one of harvest's own functions (the browser render, the MCP-lane client) as allowed to open a trusted
    section. Only these code objects can switch the sandbox audit hook off; a scraper module that imports
    `trusted_section` and calls it directly gets a PermissionError."""
    _TRUSTED_CALLERS.add(fn.__code__)
    return fn


def trusted_section() -> _Trusted:
    import sys
    if sys._getframe(1).f_code not in _TRUSTED_CALLERS:
        raise PermissionError("harvest sandbox: trusted_section is reserved for harvest's own browser and MCP helpers")
    return _Trusted()


@dataclass
class Response:
    url: str
    status: int
    headers: dict
    text: str
    elapsed: float = 0.0
    via_proxy: bool = False

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError:
            return None


@dataclass
class _Host:
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_at: float = 0.0
    delay: float = 1.0
    robots: robots_mod.Robots | None = None
    robots_at: float = 0.0
    via_proxy: bool = False  # the direct route met an IP block here; this walk goes through the proxy route
    proxy_off: bool = False  # the proxy route met a challenge/login/paywall here; never again this walk
    trigger: str | None = None
    last_kind: str | None = None  # classification of the last failure (ip_block, challenge, login, ...)


@dataclass
class _Out:
    status: int | None
    body: bytes
    headers: dict
    final_url: str
    enc: str
    err: str | None
    gave_up: bool = False
    via_proxy: bool = False
    budget_cut: bool = False
    elapsed: float = 0.0
    text: str = ""


class Blocked(Exception):
    pass


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """A globally routable unicast address. `is_global` rather than `not is_private`: the latter lets through the
    shared address space 100.64.0.0/10 (a cloud metadata service lives at 100.100.100.200) and other special
    ranges. IPv6 forms that embed an IPv4 address (mapped, 6to4, Teredo, NAT64) are judged by that address."""
    if ip.version == 6:
        embedded = [ip.ipv4_mapped, ip.sixtofour, ip.teredo[1] if ip.teredo else None]
        if ip in _NAT64:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if any(e is not None and not public_ip(e) for e in embedded):
            return False
    return bool(ip.is_global) and not ip.is_multicast


class _PublicOnlyBackend(httpcore.SyncBackend):
    """Resolve once at connect time, refuse any non-public answer, and connect to the vetted address. The URL gate
    resolves too, but a second, independent lookup by the socket layer could get a different answer (DNS
    rebinding: public for the gate, 127.0.0.1 or a metadata address for the connection). TLS still verifies
    the original host name (httpcore passes the origin host as server_hostname)."""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise httpcore.ConnectError(f"dns: {exc}") from None
        addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
        bad = next((a for a in addrs if not public_ip(a)), None)
        if bad is not None or not addrs:
            raise Blocked(f"non-public address {bad} for {host} at connect time")
        return super().connect_tcp(str(addrs[0]), port, timeout=timeout, local_address=local_address, socket_options=socket_options)


class Http:
    def __init__(self, *, user_agent: str | None = None, rate_s: float = 2.0, timeout: float = 20.0, retries: int = 3,
                 url_budget_s: float = 90.0, max_body: int = 8 * 1024 * 1024, respect_robots: bool = True,
                 allow_private: bool | None = None, route: proxy_mod.Route | None = None,
                 max_crawl_delay: float = 60.0, backoff_s: float = 1.0, transport: httpx.BaseTransport | None = None,
                 ua_mode: str = "own", allow_session_headers: bool = False):
        if ua_mode not in ("own", "browser"):
            raise ValueError("ua_mode: own | browser")
        self.ua_mode = ua_mode
        # harvest never logs in: Cookie / Authorization only with an operator decision for the source (sessionhdr.py)
        self.allow_session_headers = allow_session_headers
        # robots.txt is always read for harvest's own token; in browser mode `*` is checked too and the stricter wins
        self.robots_ua = user_agent or DEFAULT_UA
        self.ua = browser_ua() if ua_mode == "browser" else self.robots_ua
        self.rate_s = rate_s
        self.timeout = timeout
        self.retries = retries
        self.url_budget_s = url_budget_s
        self.max_body = max_body
        self.respect_robots = respect_robots
        self.allow_private = allow_private if allow_private is not None else os.environ.get("HARVEST_ALLOW_PRIVATE") == "1"
        self._route = route  # the gated residential-proxy route, or None (private: scraper modules may not touch it)
        self.max_crawl_delay = max_crawl_delay
        self.backoff_s = backoff_s
        self._transport = transport
        self._hosts: dict[str, _Host] = {}
        self._lock = threading.Lock()
        self._clients: dict[str | None, httpx.Client] = {}
        self.stats = {"requests": 0, "ok": 0, "blocked": 0, "robots_denied": 0, "errors": 0, "retries": 0, "rate_limited": 0, "gated": 0,
                      "ip_blocked": 0, "proxy_fallbacks": 0, "proxy_requests": 0, "proxy_ok": 0, "proxy_bytes": 0,
                      "proxy_budget_stop": 0, "proxy_exit_failures": 0, "proxy_refused": 0, "proxy_no_help": 0, "session_refused": 0}
        self.events: list[dict] = []

    # ------------------------------------------------------------------ plumbing
    def _log(self, event: str, url: str, /, **kw) -> None:
        clean = {k: (proxy_mod.redact(v)[:300] if isinstance(v, str) else v) for k, v in kw.items() if v is not None}
        self.events.append({"t": round(time.time(), 3), "event": event, "url": proxy_mod.redact(url)[:300], **clean})
        del self.events[:-200]

    def _client(self, proxy_url: str | None) -> httpx.Client:
        with self._lock:
            c = self._clients.get(proxy_url)
            if c is None:
                if len(self._clients) > 12:  # rotating walks: keep the pool of open clients small
                    for k in [k for k in self._clients if k is not None][:4]:
                        self._clients.pop(k).close()
                kw: dict[str, Any] = {"timeout": self.timeout, "follow_redirects": True, "headers": self._base_headers(),
                                      "event_hooks": {"request": [self._gate_request]}}
                if self._transport is not None:
                    kw["transport"] = self._transport
                elif proxy_url:
                    kw["proxy"] = proxy_url
                    kw["trust_env"] = False
                elif not self.allow_private:
                    t = httpx.HTTPTransport()
                    t._pool._network_backend = _PublicOnlyBackend()
                    kw["transport"] = t
                c = self._clients[proxy_url] = httpx.Client(**kw)
            return c

    def _base_headers(self) -> dict:
        if self.ua_mode == "browser":  # only the header set changes: no fingerprint, no client hints beyond what the UA states
            return dict(BROWSER_HEADERS, **{"User-Agent": self.ua})
        return {"User-Agent": self.ua, "Accept-Language": "*;q=0.5"}

    def robots_allows(self, rb: robots_mod.Robots, url: str) -> bool:
        """harvest's own token; in browser mode also `*` (whichever is stricter)."""
        ok = rb.can_fetch(self.robots_ua, url)
        if self.ua_mode == "browser":
            ok = ok and rb.can_fetch("*", url)
        return ok

    def close(self) -> None:
        for c in self._clients.values():
            c.close()
        self._clients.clear()
        eg = getattr(self, "_egress_proxy", None)
        if eg is not None:
            eg.close()
            self._egress_proxy = None

    def _host(self, host: str) -> _Host:
        with self._lock:
            h = self._hosts.get(host)
            if h is None:
                h = self._hosts[host] = _Host(delay=self.rate_s)
            return h

    def proxy_report(self) -> dict | None:
        """Usage of the proxy route in this walk (ids and counters only), or None without a route."""
        return self._route.report() if self._route is not None else None

    def gate(self, url: str) -> None:
        """Raise Blocked for non-http(s) URLs and, unless allowed, private/loopback/link-local addresses."""
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise Blocked(f"unsupported URL {url[:120]}")
        if u.username or u.password:
            raise Blocked("credentials in URLs are refused")
        if self.allow_private or self._transport is not None:
            return
        try:
            infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise Blocked(f"dns: {exc}") from None
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if not public_ip(ip):
                raise Blocked(f"non-public address {ip} for {u.hostname}")

    def _gate_request(self, request: httpx.Request) -> None:  # redirects pass the gate too
        self.gate(str(request.url))

    # ------------------------------------------------------------------ robots
    def robots_for(self, url: str) -> robots_mod.Robots:
        u = urlsplit(url)
        origin = f"{u.scheme}://{u.netloc}"
        h = self._host(u.netloc)
        if h.robots is not None and time.time() - h.robots_at < 6 * 3600:
            return h.robots
        rurl = origin + "/robots.txt"
        status, text, err, hdrs = None, "", None, {}
        try:
            self.gate(rurl)
            with h.lock:
                self._wait(h)
                r = self._client(None).get(rurl)
                h.next_at = time.monotonic() + h.delay
            status, text, hdrs = r.status_code, r.text[:500_000], {k.lower(): v for k, v in r.headers.items()}
        except Exception as exc:  # network error -> unreachable -> disallow all
            err = f"{exc.__class__.__name__}: {str(exc)[:120]}"
            self._log("robots_error", rurl, error=err)
        # an IP block on robots.txt itself: read it through the route (when this walk may use one), never guess
        trig = self._trigger(_Out(status, b"", hdrs, rurl, "utf-8", err, gave_up=True, text=text))
        if trig and self._route_ok(h, {}):
            if self._route.usable():
                self._log("proxy_fallback", rurl, trigger=trig)
                self.stats["proxy_fallbacks"] += 1
                out = self._run("GET", rurl, h, {}, None, None, via=True)
                if out.status is not None and out.status < 500 and proxy_mod.classify(out.status, out.headers, out.text) in ("ok", "other", "ip_block"):
                    status, text = out.status, out.text[:500_000]
                    if out.status < 400:
                        h.via_proxy, h.trigger = True, trig
                if self._trigger(out):
                    self._no_help(rurl, h, out)
            else:
                self._budget_stop(rurl)
        rb = robots_mod.from_status(status, text, rurl)
        cds = [rb.crawl_delay(self.robots_ua)] + ([rb.crawl_delay("*")] if self.ua_mode == "browser" else [])
        cd = max((c for c in cds if c), default=None)
        if cd:
            h.delay = max(h.delay, min(cd, self.max_crawl_delay))
        h.robots, h.robots_at = rb, time.time()
        return rb

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        return self.robots_allows(self.robots_for(url), url)

    # ------------------------------------------------------------------ requests
    def _wait(self, h: _Host) -> None:
        now = time.monotonic()
        if h.next_at > now:
            time.sleep(h.next_at - now)

    def _route_ok(self, h: _Host, headers: dict | None) -> bool:
        """The route exists for this walk, this host has not shown a challenge through it, and the request carries no
        session (a signed-in request never goes through a proxy)."""
        return self._route is not None and not h.proxy_off and not proxy_mod.session_headers(headers)

    def _trigger(self, out: _Out) -> str | None:
        """Gate (c): is this failure an IP-level block the route may work around?"""
        if out.status in (401, 403):
            kind = proxy_mod.classify(out.status, out.headers, out.text)
            return f"http_{out.status}" if kind == "ip_block" else None
        if out.status == 429 and out.gave_up:
            return "http_429"
        if out.status is None and out.gave_up and proxy_mod.is_reset(out.err):
            return "reset"
        return None

    def _no_help(self, url: str, h: _Host, out: _Out) -> None:
        """The same block through a residential exit: it is not about the datacenter IP (a user-agent or account
        rule, a geo rule ...). The route is not tried again on this host for the rest of the walk."""
        h.proxy_off = True
        self.stats["proxy_no_help"] = self.stats.get("proxy_no_help", 0) + 1
        self._log("proxy_no_help", url, status=out.status, error=out.err)

    def _budget_stop(self, url: str) -> None:
        """The route was needed but cannot serve: its allowance is spent (a budget stop) or no exit is alive."""
        if self._route is not None and not self._route.spent():
            self._log("proxy_no_exit", url)
            return
        self.stats["proxy_budget_stop"] += 1
        if self._route is not None:
            self._route.budget_stop = True
        self._log("proxy_budget_stop", url)

    def _fetch(self, client: httpx.Client, method: str, url: str, hdrs: dict, json_body: Any, data: Any, via: bool):
        """One request. Through the route it is streamed and cut at the walk's remaining byte allowance.
        -> (status, body, headers, final_url, encoding, wire_bytes, budget_cut)"""
        if not via:
            r = client.request(method, url, headers=hdrs, json=json_body, data=data)
            return r.status_code, r.content[: self.max_body], {k.lower(): v for k, v in r.headers.items()}, str(r.url), r.encoding or "utf-8", 0, False
        limit = self._route.remaining_bytes()
        req_bytes = len(method) + len(url) + sum(len(k) + len(str(v)) + 4 for k, v in hdrs.items()) + 200
        if json_body is not None:
            req_bytes += len(json.dumps(json_body, default=str))
        elif isinstance(data, (bytes, str)):
            req_bytes += len(data)
        chunks, got, cut = [], 0, False
        with client.stream(method, url, headers=hdrs, json=json_body, data=data) as r:
            head = sum(len(k) + len(v) + 4 for k, v in r.headers.items()) + 20
            try:
                announced = int(r.headers.get("content-length") or -1)
            except ValueError:
                announced = -1
            if announced >= 0 and req_bytes + head + announced > limit:
                # the body would overrun the allowance: do not read it. What the socket already buffered is charged
                # conservatively (one read chunk), so the cap can be overrun by at most ~64 KiB.
                return (r.status_code, b"", {k.lower(): v for k, v in r.headers.items()}, str(r.url), r.encoding or "utf-8",
                        req_bytes + head + min(announced, CHUNK_ESTIMATE), True)
            for chunk in r.iter_bytes():
                chunks.append(chunk)
                got += len(chunk)
                if req_bytes + head + r.num_bytes_downloaded >= limit:
                    cut = True
                    break
                if got >= self.max_body:
                    break
            wire = req_bytes + head + r.num_bytes_downloaded
            return (r.status_code, b"".join(chunks)[: self.max_body], {k.lower(): v for k, v in r.headers.items()}, str(r.url),
                    r.encoding or "utf-8", wire, cut)

    def _run(self, method: str, url: str, h: _Host, hdrs: dict, json_body: Any, data: Any, *, via: bool) -> _Out:
        """The attempt loop (retries, back-off, 429 escalation, per-URL budget), direct or through the route."""
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            retry_after: float | None = None
            ex = None
            if via:
                if not self._route.usable():
                    spent = self._route.spent()
                    if spent:
                        self._route.budget_stop = True
                    return _Out(None, b"", {}, url, "utf-8", "proxy route: budget spent" if spent else "proxy route: no live exit",
                                gave_up=True, via_proxy=True, budget_cut=spent)
                ex = self._route.next_exit()
            with h.lock:  # per-host concurrency 1, whichever route
                self._wait(h)
                self.stats["requests"] += 1
                if via:
                    self.stats["proxy_requests"] += 1
                t0 = time.monotonic()
                wire, cut, exit_bad = 0, False, None
                try:
                    status, body, rhdr, final_url, enc, wire, cut = self._fetch(self._client(proxy_mod.to_url(ex) if ex else None), method, url,
                                                                               hdrs, json_body, data, via)
                    err = None
                    if via and status in (402, 407) and ("x-webshare-reason" in rhdr or "proxy-authenticate" in rhdr or status == 407):
                        exit_bad = rhdr.get("x-webshare-reason") or f"http_{status}"  # the proxy refused, not the site
                except Blocked as exc:
                    h.next_at = time.monotonic() + h.delay
                    self.stats["gated"] += 1
                    self._log("gated", url, reason=str(exc))
                    return _Out(None, b"", {}, url, "utf-8", f"gated: {exc}", via_proxy=via)
                except httpx.HTTPError as exc:
                    status, body, rhdr, final_url, enc = None, b"", {}, url, "utf-8"
                    err = f"{exc.__class__.__name__}: {proxy_mod.redact(str(exc))[:160]}"
                    if via and isinstance(exc, (httpx.ProxyError, httpx.ConnectError)):
                        exit_bad = exc.__class__.__name__
                    wire = 300 if via else 0
                h.next_at = time.monotonic() + h.delay
            elapsed = time.monotonic() - t0
            if via:
                self._route.charge(wire)
                self.stats["proxy_bytes"] += wire
                if exit_bad:
                    self._route.fail(ex, exit_bad)
                    self.stats["proxy_exit_failures"] += 1
                    self._log("proxy_exit_failed", url, exit=proxy_mod.exit_id(ex), reason=exit_bad)
                    status, err = None, f"ProxyError: exit {proxy_mod.exit_id(ex)} failed ({exit_bad})"
                if cut:
                    self._route.budget_stop = True
                    self._log("proxy_budget_stop", url, bytes=self._route.used_bytes)
                    self.stats["proxy_budget_stop"] += 1
                    return _Out(None, b"", {}, url, "utf-8", "proxy route: byte budget spent mid-response", gave_up=True, via_proxy=True,
                                budget_cut=True, elapsed=elapsed)
            if status == 429:
                self.stats["rate_limited"] += 1
                h.delay = min(max(h.delay * 2, self.rate_s * 2, 1.0), MAX_HOST_DELAY)  # escalate for the rest of the run
                try:
                    retry_after = float(rhdr.get("retry-after", ""))
                except ValueError:
                    retry_after = None
            if status is not None and status not in RETRY_STATUSES:
                break
            spent = time.monotonic() - started
            wait = retry_after if retry_after is not None else self.backoff_s * (2 ** (attempt - 1))
            if exit_bad:
                wait = 0.0
            if attempt > self.retries or spent + wait > self.url_budget_s:
                return _Out(status, body, rhdr, final_url, enc, err, gave_up=True, via_proxy=via, elapsed=elapsed, text=_decode(body, enc))
            self.stats["retries"] += 1
            self._log("retry", url, status=status, error=err, wait=round(wait, 2), via_proxy=via or None)
            h.next_at = max(h.next_at, time.monotonic() + wait)
        return _Out(status, body, rhdr, final_url, enc, err, via_proxy=via, elapsed=elapsed, text=_decode(body, enc))

    def request(self, method: str, url: str, *, params: dict | None = None, headers: dict | None = None,
                json_body: Any = None, data: Any = None, accept: str | None = None) -> Response | None:
        try:
            self.gate(url)
        except Blocked as exc:
            self.stats["gated"] += 1
            self._log("gated", url, reason=str(exc))
            return None
        from .sessionhdr import carries_session
        if carries_session(headers) and not self.allow_session_headers:
            self.stats["gated"] += 1
            self.stats["session_refused"] += 1
            self._log("session_refused", url, reason="Cookie/Authorization needs an operator decision for this source "
                                                     "(harvest session-headers enable <project> --source <id> --reason ...)")
            return None
        if params:
            url = str(httpx.URL(url, params=params))
        if not self.allowed(url):
            self.stats["robots_denied"] += 1
            self._log("robots_denied", url)
            return None
        u = urlsplit(url)
        h = self._host(u.netloc)
        hdrs = dict(headers or {})
        if accept:
            hdrs["Accept"] = {"json": "application/json, */*;q=0.5", "html": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                              "xml": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.5"}.get(accept, accept)
        route_ok = self._route_ok(h, hdrs)
        if route_ok and h.via_proxy and self._route.usable():
            out = self._run(method, url, h, hdrs, json_body, data, via=True)
        else:
            out = self._run(method, url, h, hdrs, json_body, data, via=False)
            trig = self._trigger(out)
            if trig:
                self.stats["ip_blocked"] += 1
                h.last_kind = "ip_block"
                if route_ok and self._route.usable():
                    self.stats["proxy_fallbacks"] += 1
                    self._log("proxy_fallback", url, trigger=trig, status=out.status)
                    pout = self._run(method, url, h, hdrs, json_body, data, via=True)
                    if pout.status is not None or pout.budget_cut or pout.err:
                        out = pout
                    if out.via_proxy and out.status is not None and out.status < 400 and _wall(out.status, out.headers, out.text) == "ok":
                        h.via_proxy, h.trigger = True, trig
                    elif out.via_proxy and self._trigger(out):
                        self._no_help(url, h, out)
                elif route_ok:
                    self._budget_stop(url)
            elif out.status in (401, 403) and self._route is not None:
                self.stats["proxy_refused"] += 1  # a challenge / login / paywall: never worked around
        return self._finish(url, h, out)

    def _finish(self, url: str, h: _Host, out: _Out) -> Response | None:
        status, text, rhdr = out.status, out.text, out.headers
        if out.via_proxy and status is not None:
            kind = _wall(status, rhdr, text)
            if kind in ("challenge", "login", "paywall", "auth"):
                h.proxy_off = True  # the route met a wall here: this host is direct-only for the rest of the walk
                self.stats["proxy_refused"] += 1
                self._log("proxy_wall", url, status=status, kind=kind)
        if status is None or (out.gave_up and status in RETRY_STATUSES):
            self.stats["errors"] += 1
            self._log("gave_up", url, status=status, error=out.err, via_proxy=out.via_proxy or None)
            return None
        if status in BLOCK_STATUSES:
            kind = proxy_mod.classify(status, rhdr, text)
            h.last_kind = kind
            self.stats["blocked"] += 1
            self._log("blocked", url, status=status, kind=kind, via_proxy=out.via_proxy or None)
            return None
        if status >= 400:
            self.stats["errors"] += 1
            self._log("http_error", url, status=status, via_proxy=out.via_proxy or None)
            return None
        if "json" not in rhdr.get("content-type", "") and is_challenge_page(text):
            h.last_kind = "challenge"
            if out.via_proxy:
                h.proxy_off = True
            self.stats["blocked"] += 1
            self._log("challenge", url, status=status, via_proxy=out.via_proxy or None)
            return None
        self.stats["ok"] += 1
        if out.via_proxy:
            self.stats["proxy_ok"] += 1
        return Response(url=out.final_url, status=status, headers=rhdr, text=text, elapsed=round(out.elapsed, 3), via_proxy=out.via_proxy)

    def get(self, url: str, **kw) -> Response | None:
        return self.request("GET", url, **kw)

    def get_text(self, url: str, **kw) -> str | None:
        r = self.get(url, accept=kw.pop("accept", "html"), **kw)
        return r.text if r else None

    def get_json(self, url: str, **kw) -> Any:
        r = self.get(url, accept=kw.pop("accept", "json"), **kw)
        return r.json() if r else None

    def post_json(self, url: str, body: Any, **kw) -> Any:
        """For public search endpoints that only answer POST; same robots/politeness ladder."""
        r = self.request("POST", url, json_body=body, accept="json", **kw)
        return r.json() if r else None

    # ------------------------------------------------------------------ browser lane
    def render(self, url: str, wait_ms: int = 1500) -> str | None:
        """Headless-browser lane: HTML after JavaScript, via Playwright when installed. Same robots
        and URL gate; page 1 only by policy. Returns None when no browser is available. The proxy
        route applies as for `request`: only after the direct render met an IP-level block."""
        try:
            self.gate(url)
        except Blocked as exc:
            self._log("gated", url, reason=str(exc))
            return None
        if not self.allowed(url):
            self.stats["robots_denied"] += 1
            self._log("robots_denied", url)
            return None
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401  optional dependency
        except ImportError:
            self._log("no_browser", url)
            return None
        h = self._host(urlsplit(url).netloc)
        route_ok = self._route_ok(h, None)
        via = bool(route_ok and h.via_proxy and self._route.usable())
        status, html, err, cut = self._render_once(url, wait_ms, h, via)
        if not via:
            direct = _Out(status, b"", {}, url, "utf-8", err, gave_up=True, text=html or "")
            if err and not proxy_mod.is_reset(err) and any(m in err for m in ("ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_EMPTY_RESPONSE",
                                                                                "ERR_CONNECTION_REFUSED")):
                direct.err = "ConnectError: " + err
            trig = self._trigger(direct)
            if trig:
                self.stats["ip_blocked"] += 1
                if route_ok and self._route.usable():
                    self.stats["proxy_fallbacks"] += 1
                    self._log("proxy_fallback", url, trigger=trig, status=status, browser=True)
                    via = True
                    status, html, err, cut = self._render_once(url, wait_ms, h, True)
                    again = _Out(status, b"", {}, url, "utf-8", err, gave_up=True, text=html or "")
                    if not cut and self._trigger(again):
                        self._no_help(url, h, again)
                elif route_ok:
                    self._budget_stop(url)
        if cut:
            self._route.budget_stop = True
            self._log("proxy_budget_stop", url, browser=True)
            self.stats["proxy_budget_stop"] += 1
            return None
        if err is not None:
            self.stats["errors"] += 1
            self._log("render_error", url, error=err, via_proxy=via or None)
            return None
        if via and status is not None:
            kind = _wall(status, {}, html or "")
            if kind in ("challenge", "login", "paywall", "auth"):
                h.proxy_off = True
                self.stats["proxy_refused"] += 1
                self._log("proxy_wall", url, status=status, kind=kind, browser=True)
        if status is not None and status >= 400:
            self.stats["blocked" if status in BLOCK_STATUSES else "errors"] += 1
            self._log("blocked" if status in BLOCK_STATUSES else "http_error", url, status=status, via_proxy=via or None)
            return None
        if is_challenge_page(html):
            if via:
                h.proxy_off = True
            self.stats["blocked"] += 1
            self._log("challenge", url, status=status, via_proxy=via or None)
            return None
        if via:
            h.via_proxy = True
            self.stats["proxy_ok"] += 1
        self.stats["ok"] += 1
        return html

    @trusted_caller
    def _render_once(self, url: str, wait_ms: int, h: _Host, via: bool) -> tuple[int | None, str, str | None, bool]:
        """One headless render, direct or through the route. -> (status, html, error, budget_cut)"""
        from playwright.sync_api import sync_playwright
        ex = self._route.next_exit() if via else None
        if via and ex is None:
            return None, "", "proxy route: no live exit", False
        limit = self._route.remaining_bytes() if via else 0
        seen: dict[str, Any] = {"bytes": 0, "cut": False, "finished": []}
        status, html, err = None, "", None
        try:
            with h.lock, trusted_section():
                self._wait(h)
                self.stats["requests"] += 1
                if via:
                    self.stats["proxy_requests"] += 1
                with sync_playwright() as p:
                    kw: dict[str, Any] = {}
                    if not ex and not self.allow_private and self._transport is None:
                        # DNS pinning for the browser: Chromium hands host names to harvest's filtering proxy, which
                        # connects only to the public address it vetted itself (egress.py)
                        from .egress import FilteringProxy
                        egress = self._egress_proxy = getattr(self, "_egress_proxy", None) or FilteringProxy()
                        kw["proxy"] = {"server": egress.url}
                        kw["args"] = ["--proxy-bypass-list=<-loopback>"]
                    if ex:
                        kw["proxy"] = {k: v for k, v in (("server", ex["server"]), ("username", ex.get("username")), ("password", ex.get("password"))) if v}
                        if self.allow_private:  # tests: Chromium bypasses proxies for loopback unless told not to
                            kw["args"] = ["--proxy-bypass-list=<-loopback>"]
                    b = p.chromium.launch(**kw)
                    try:
                        if self.ua_mode == "browser":  # the launched build's own Chrome UA, without the "Headless" marker
                            page = b.new_page(user_agent=chrome_ua(b.version), locale="en-US",
                                              extra_http_headers={"Accept-Language": BROWSER_HEADERS["Accept-Language"]})
                        else:
                            page = b.new_page(user_agent=self.ua)

                        def guard(route):  # every sub-request passes the same URL gate (no private addresses)
                            req = route.request
                            try:
                                self.gate(req.url) if req.url.startswith("http") else None
                            except Blocked:
                                return route.abort()
                            if ex is not None:
                                if req.resource_type in ("image", "media", "font"):  # proxied bandwidth is for the data
                                    return route.abort()
                                if seen["bytes"] >= limit:
                                    seen["cut"] = True
                                    return route.abort()
                            return route.continue_()
                        page.route("**/*", guard)
                        if ex is not None:
                            def on_response(resp):
                                try:
                                    seen["bytes"] += int(resp.headers.get("content-length") or 0) + 400
                                except (ValueError, TypeError):
                                    seen["bytes"] += 400
                            page.on("response", on_response)
                            page.on("requestfinished", lambda req: seen["finished"].append(req))
                        try:
                            resp = page.goto(url, timeout=int(self.timeout * 1000))
                            status = resp.status if resp else None
                            page.wait_for_timeout(wait_ms)
                            html = _settled_content(page, int(self.timeout * 1000))
                        except Exception as exc:
                            err = proxy_mod.redact(f"{exc.__class__.__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")[:200]
                        if ex is not None:
                            exact = 0
                            for req in seen["finished"]:
                                try:
                                    sz = req.sizes()
                                    exact += sum(max(0, int(sz.get(k) or 0)) for k in ("requestHeadersSize", "requestBodySize",
                                                                                         "responseHeadersSize", "responseBodySize"))
                                except Exception:
                                    exact += 400
                            seen["bytes"] = max(seen["bytes"], exact)
                    finally:
                        b.close()
                h.next_at = time.monotonic() + h.delay
        except Exception as exc:
            err = proxy_mod.redact(f"{exc.__class__.__name__}: {str(exc)[:160]}")
        if ex is not None:
            self._route.charge(seen["bytes"])
            self.stats["proxy_bytes"] += seen["bytes"]
            if err and any(m in err for m in ("ERR_PROXY", "ERR_TUNNEL", "407")):
                self._route.fail(ex, "proxy_error")
                self.stats["proxy_exit_failures"] += 1
        return status, html, err, bool(seen["cut"] or (ex is not None and self._route.used_bytes > limit))


def _settled_content(page, timeout_ms: int) -> str:
    """page.content(), after a client-side redirect settles (a JS shell that navigates on load)."""
    for _ in range(3):
        try:
            page.wait_for_load_state("load", timeout=timeout_ms)
            return page.content()
        except Exception as exc:
            if "navigating" not in str(exc):
                raise
            page.wait_for_timeout(1000)
    return page.content()


def _wall(status: int, headers: dict, text: str) -> str:
    """`proxy.classify`, with the client's rule for success pages: a large 2xx page that merely mentions a captcha
    (a form widget, a script) is content, not a challenge page."""
    if status < 400 and len(text) >= 20000 and not any(h in {k.lower() for k in headers} for h in proxy_mod.CHALLENGE_HEADERS):
        return "ok"
    return proxy_mod.classify(status, headers, text)


def _decode(body: bytes, enc: str) -> str:
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def trusted() -> bool:
    return getattr(_trust, "depth", 0) > 0
