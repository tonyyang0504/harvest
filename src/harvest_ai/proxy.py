"""The residential-proxy route: a gated fallback for sites that block the datacenter IP.

The pool is the operator's rotating residential proxies: `HARVEST_PROXY_FILE` (one exit per line,
`host:port:user:pass`, `host:port` or `http://user:pass@host:port`; blanks and `#` comments skipped) or one
gateway URL in `HARVEST_PROXY_URL`. The pool is a credential: it is read only by the parent process, never
logged, printed or stored; an exit is named by `exit_id` (a hash) everywhere else, and `redact` scrubs
`user:pass` from every log line, error and exception text that could carry one.

The route is used only when ALL of these hold (see `eligible` and `Http.request`):
  (a) the project or the source is switched on by a recorded operator decision (reason + date);
  (b) robots.txt allows the source and its terms verdict is allowed/no_clause (lane detection may also run with
      a not-yet-read `unknown` verdict so the proxied probe can read the terms; `forbids` always stops it);
  (c) the direct request hit an IP-level block: 401/403 without a challenge, login or paywall page, a connection
      reset, or a 429 that persisted through the back-off.
Never for captcha / bot-challenge pages (Cloudflare, DataDome, PerimeterX, Imperva, Kasada, AWS WAF ...),
login walls, paywalls, HTTP auth, or a request that carries a session (Cookie / Authorization).

Budgets: per project and UTC day, a byte cap and a request cap, accounted per source in `proxy_usage`. A walk
gets an allowance reserved from what is left; the child stops using the proxy when it is spent (the request is
then a block, as without a proxy) and the parent raises a `proxy_budget` alert through the alert hook.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

DEFAULT_DAILY_BYTES = 50 * 1024 * 1024
DEFAULT_DAILY_REQUESTS = 1000
HEALTH_TTL_S = 600
COOLDOWN_S = 900
ROTATE_EXITS = 6
HEADROOM = 64 * 1024
MODES = ("sticky", "rotate")

# ------------------------------------------------------------------ redaction
_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:'\"]+:[^\s/@'\"]*@")
_LINE_CRED = re.compile(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,}|\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5}):[^\s:'\"]+:[^\s'\"]+")
_AUTH_HDR = re.compile(r"(?i)(proxy-authorization|authorization)(['\"]?\s*[:=]\s*['\"]?)(basic|bearer)?\s*[A-Za-z0-9+/=._-]+")
_SECRETS: set[str] = set()  # the loaded pool's passwords/usernames (longer than 3 chars), scrubbed verbatim too
_SECRETS_LOCK = threading.Lock()


def redact(text: Any) -> str:
    """`scheme://user:pass@host` -> `scheme://***@host`, `host:port:user:pass` -> `host:port:***`, auth headers
    and any credential string of the loaded pool -> `***`. Safe on any input."""
    s = str(text if text is not None else "")
    s = _URL_CRED.sub(r"\1***@", s)
    s = _LINE_CRED.sub(r"\1:\2:***", s)
    s = _AUTH_HDR.sub(r"\1\2***", s)
    with _SECRETS_LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)
    for sec in secrets:
        if sec in s:
            s = s.replace(sec, "***")
    return s


def _remember(proxy: dict) -> None:
    with _SECRETS_LOCK:
        for k in ("username", "password"):
            v = proxy.get(k)
            if v and len(str(v)) > 3:
                _SECRETS.add(str(v))
                _SECRETS.add(quote(str(v), safe=""))


class ProxyError(RuntimeError):
    """An error about the proxy route whose message is already redacted."""

    def __init__(self, msg: str):
        super().__init__(redact(msg))


# ------------------------------------------------------------------ pool parsing
def parse_line(line: str) -> dict | None:
    """One pool line -> {"server": "http://host:port", "username", "password"}, or None for blanks, comments and
    malformed lines. Same semantics as the common provider export format (Webshare's)."""
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None
    if "://" in s:
        try:
            u = urlsplit(s)
            port = u.port
        except ValueError:
            return None
        if not u.hostname or not port:
            return None
        from urllib.parse import unquote
        return {"server": f"{u.scheme}://{u.hostname}:{port}", "username": unquote(u.username) if u.username else None,
                "password": unquote(u.password) if u.password else None}
    parts = s.split(":")
    if len(parts) not in (2, 4) or not parts[1].isdigit() or not parts[0]:
        return None
    out = {"server": f"http://{parts[0]}:{parts[1]}", "username": None, "password": None}
    if len(parts) == 4:
        out["username"], out["password"] = parts[2] or None, parts[3] or None
    return out


def to_url(proxy: dict) -> str:
    """The client form of a parsed exit (credentials percent-encoded). Never log this."""
    scheme, _, hostport = proxy["server"].partition("://")
    if proxy.get("username"):
        return f"{scheme}://{quote(proxy['username'], safe='')}:{quote(proxy.get('password') or '', safe='')}@{hostport}"
    return proxy["server"]


def exit_id(proxy: dict) -> str:
    """A stable, credential-free name for an exit (for logs, cool-downs and reports)."""
    return "x" + hashlib.sha256(f"{proxy.get('server')}|{proxy.get('username')}".encode()).hexdigest()[:10]


def pool_path() -> Path | None:
    v = os.environ.get("HARVEST_PROXY_FILE")
    return Path(v).expanduser() if v else None


def load(path: Path | None = None, *, limit: int = 20000) -> list[dict]:
    """The pool: `HARVEST_PROXY_URL` (one gateway) or the parsed `HARVEST_PROXY_FILE`. [] when unset/unreadable."""
    if path is None and os.environ.get("HARVEST_PROXY_URL"):
        one = parse_line(os.environ["HARVEST_PROXY_URL"])
        if one:
            _remember(one)
        return [one] if one else []
    path = path or pool_path()
    if not path:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in text.splitlines():
        parsed = parse_line(line)
        if parsed:
            _remember(parsed)
            out.append(parsed)
            if len(out) >= limit:
                break
    return out


def configured() -> bool:
    return bool(os.environ.get("HARVEST_PROXY_URL") or os.environ.get("HARVEST_PROXY_FILE"))


def country_code(country: str | None) -> str | None:
    cc = "".join(ch for ch in str(country or "") if ch.isalpha()).upper()
    return cc if len(cc) == 2 else None


def with_country(proxy: dict, country: str | None) -> dict:
    """Pin a session to one country by appending `-<CC>` to the username (the Webshare convention). A no-op without a
    country or username, or when a 2-letter suffix is already there. Returns a copy."""
    cc = country_code(country)
    user = proxy.get("username")
    if not cc or not user:
        return dict(proxy)
    tail = str(user).rsplit("-", 1)[-1]
    if "-" in str(user) and tail.isalpha() and len(tail) == 2:
        return dict(proxy)
    out = dict(proxy)
    out["username"] = f"{user}-{cc}"
    return out


# ------------------------------------------------------------------ gate (c): what kind of failure is this?
CHALLENGE_BODY = ("cf-chl", "challenge-platform", "just a moment...", "attention required", "checking your browser", "captcha",
                  "are you a human", "are you a robot", "verify you are human", "enable javascript and cookies", "ddos-guard",
                  "perimeterx", "px-captcha", "_pxhd", "captcha-delivery.com", "datadome", "distil_r_captcha", "incapsula",
                  "_incapsula_resource", "kpsdk", "awswaf", "aws-waf-token", "pardon our interruption", "unusual traffic",
                  "bot protection", "please wait while we verify", "sucuri website firewall", "turnstile")
CHALLENGE_HEADERS = ("cf-mitigated", "x-datadome", "x-dd-b", "x-px-block", "x-kpsdk-ct", "x-amzn-waf-action", "x-iinfo", "x-sucuri-block")
LOGIN_BODY = re.compile(r"type=[\"']password[\"']|log ?in to (?:see|view|continue)|sign in to (?:see|view|continue)|please (?:log|sign) in|"
                        r"войдите|anmelden, um|inicia sesión para|session expired|you must be logged in", re.I)
PAYWALL_BODY = re.compile(r"subscribe to (?:continue|read)|paywall|subscribers only|premium content|subscription required", re.I)


def classify(status: int | None, headers: dict | None, text: str | None) -> str:
    """-> ip_block | challenge | login | paywall | auth | rate_limited | other | ok. Only `ip_block` (and a persistent
    `rate_limited`, decided by the caller after the back-off) may be worked around with the proxy."""
    hdr = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    low = (text or "")[:60000].lower()
    if any(h in hdr for h in CHALLENGE_HEADERS) or "ddos-guard" in hdr.get("server", "").lower() or any(m in low for m in CHALLENGE_BODY):
        return "challenge"
    if status == 401 and hdr.get("www-authenticate"):
        return "auth"
    if LOGIN_BODY.search(low):
        return "login"
    if status == 402 or PAYWALL_BODY.search(low):
        return "paywall"
    if status in (401, 403):
        return "ip_block"
    if status == 429:
        return "rate_limited"
    if status is None or status >= 400:
        return "other"
    return "ok"


_RESET = ("ConnectError", "ReadError", "WriteError", "RemoteProtocolError")
_NOT_RESET = ("name or service", "nodename", "getaddrinfo", "name resolution", "no address associated", "certificate", "ssl")


def is_reset(err: str | None) -> bool:
    """A connection reset / refused / dropped mid-response (not DNS, TLS or a timeout)."""
    if not err:
        return False
    cls = err.split(":", 1)[0].strip()
    return cls in _RESET and not any(n in err.lower() for n in _NOT_RESET)


def session_headers(headers: dict | None) -> bool:
    return any(str(k).lower() in ("cookie", "authorization") for k in (headers or {}))


# ------------------------------------------------------------------ the in-walk route (also runs in the sandbox child)
@dataclass
class Route:
    """The exits a walk may use and its allowance. Lives in the parent (lane detection) or the sandbox child (runs,
    reviews). Nothing here is printable: `report()` carries ids and counters only."""
    _exits: list[dict]
    mode: str = "sticky"
    allowance_bytes: int = 0
    allowance_requests: int = 0
    key: str = ""
    headroom: int = -1  # a request starts only with this many allowance bytes left (default HARVEST_PROXY_HEADROOM, 64 KiB)
    used_bytes: int = 0
    used_requests: int = 0
    budget_stop: bool = False
    _i: int = 0
    _failed: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.headroom < 0:
            self.headroom = int(os.environ.get("HARVEST_PROXY_HEADROOM", HEADROOM))

    def __repr__(self) -> str:  # never show exits
        return f"Route(mode={self.mode}, exits={len(self._exits)}, used={self.used_requests}req/{self.used_bytes}B)"

    __str__ = __repr__

    @classmethod
    def from_job(cls, spec: dict | None) -> Route | None:
        if not spec or not spec.get("exits"):
            return None
        for e in spec["exits"]:
            _remember(e)
        return cls(_exits=list(spec["exits"]), mode=spec.get("mode", "sticky"), allowance_bytes=int(spec.get("allowance_bytes", 0)),
                   allowance_requests=int(spec.get("allowance_requests", 0)), key=str(spec.get("key", "")), headroom=int(spec.get("headroom", -1)))

    def to_job(self) -> dict:
        return {"exits": self._exits, "mode": self.mode, "allowance_bytes": self.allowance_bytes,
                "allowance_requests": self.allowance_requests, "key": self.key, "headroom": self.headroom}

    def live(self) -> list[dict]:
        return [e for e in self._exits if exit_id(e) not in self._failed]

    def remaining_bytes(self) -> int:
        return max(0, self.allowance_bytes - self.used_bytes)

    def spent(self) -> bool:
        """No request may start: fewer than `headroom` bytes or no request left. A started request whose announced body
        would overrun the allowance is not read (see Http._fetch), so the byte cap holds to within one read chunk."""
        left = self.allowance_bytes - self.used_bytes
        return left <= 0 or left < self.headroom or self.used_requests >= self.allowance_requests

    def usable(self) -> bool:
        return bool(self.live()) and not self.spent()

    def next_exit(self) -> dict | None:
        """Sticky: the walk's one session (the next live exit only after a failure). Rotate: round-robin per request."""
        with self._lock:
            live = self.live()
            if not live:
                return None
            if self.mode == "rotate":
                self._i += 1
                return live[self._i % len(live)]
            return live[0]

    def charge(self, nbytes: int, requests: int = 1) -> None:
        with self._lock:
            self.used_bytes += max(0, int(nbytes))
            self.used_requests += requests

    def fail(self, ex: dict, reason: str) -> None:
        with self._lock:
            self._failed[exit_id(ex)] = redact(reason)[:80]

    def report(self) -> dict:
        return {"requests": self.used_requests, "bytes": self.used_bytes, "allowance_bytes": self.allowance_bytes,
                "allowance_requests": self.allowance_requests, "budget_stop": self.budget_stop, "mode": self.mode,
                "exits": [exit_id(e) for e in self._exits], "failed_exits": dict(self._failed)}


# ------------------------------------------------------------------ health (parent side; ids only on disk)
_HEALTH_LOCK = threading.Lock()


def _health_file() -> Path:
    from .project import home
    return home() / "proxy_health.json"


def _health_read() -> dict:
    try:
        return json.loads(_health_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _health_write(data: dict) -> None:
    f = _health_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    now = time.time()
    data = {k: v for k, v in data.items() if max(v.get("cool_until", 0), v.get("checked_at", 0) + HEALTH_TTL_S) > now - 86400}
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, f)


def cooldown(xid: str, reason: str, seconds: float | None = None) -> None:
    secs = float(os.environ.get("HARVEST_PROXY_COOLDOWN_S", COOLDOWN_S)) if seconds is None else seconds
    with _HEALTH_LOCK:
        d = _health_read()
        d[xid] = {"ok": False, "reason": redact(reason)[:80], "checked_at": time.time(), "cool_until": time.time() + secs}
        _health_write(d)


def cooling(xid: str, health: dict | None = None) -> bool:
    h = (health if health is not None else _health_read()).get(xid) or {}
    return h.get("cool_until", 0) > time.time()


def probe(ex: dict, *, timeout: float = 8.0) -> tuple[bool, str, int]:
    """(ok, reason, bytes) for one plain request through the exit. A 402 (plan bandwidth spent), 407 (bad
    credentials) or 5xx, or no connection, is a failing exit."""
    import httpx
    url = os.environ.get("HARVEST_PROXY_PROBE_URL", "http://example.com/")
    try:
        with httpx.Client(proxy=to_url(ex), timeout=timeout, follow_redirects=False) as c:
            r = c.get(url)
            n = len(r.content) + sum(len(k) + len(v) + 4 for k, v in r.headers.items())
    except Exception as exc:  # any failure is the exit's
        return False, redact(f"unreachable:{exc.__class__.__name__}"), 0
    if r.status_code in (402, 407):
        return False, (r.headers.get("x-webshare-reason") or f"http_{r.status_code}").strip().lower()[:40], n
    if r.status_code >= 500:
        return False, f"http_{r.status_code}", n
    return True, "ok", n


def healthy(ex: dict) -> tuple[bool, str, int]:
    """`probe`, cached per exit for HEALTH_TTL_S; a failing exit is cooled down. Off with HARVEST_PROXY_HEALTHCHECK=0."""
    if os.environ.get("HARVEST_PROXY_HEALTHCHECK", "1") == "0":
        return True, "unchecked", 0
    xid = exit_id(ex)
    with _HEALTH_LOCK:
        h = _health_read().get(xid) or {}
    if h.get("cool_until", 0) > time.time():
        return False, h.get("reason", "cooling"), 0
    if h.get("ok") and time.time() - h.get("checked_at", 0) < HEALTH_TTL_S:
        return True, "cached", 0
    ok, reason, n = probe(ex)
    if ok:
        with _HEALTH_LOCK:
            d = _health_read()
            d[xid] = {"ok": True, "reason": "ok", "checked_at": time.time(), "cool_until": 0}
            _health_write(d)
    else:
        cooldown(xid, reason)
    return ok, reason, n


def select(pool: list[dict], *, key: str, mode: str = "sticky", country: str | None = None, n: int | None = None,
           check: bool = True, rng: random.Random | None = None) -> tuple[list[dict], int]:
    """Pick exits for one walk. Sticky: a stable exit per `key` (the same session for a source across runs; the next
    one in the ring when it is cooling or unhealthy). Rotate: `n` random healthy exits, used round-robin per request.
    Returns (exits, probe_bytes)."""
    if not pool:
        return [], 0
    health = _health_read()
    want = 1 if mode == "sticky" else (n or ROTATE_EXITS)
    if mode == "sticky":
        start = int(hashlib.sha256(key.encode()).hexdigest(), 16) % len(pool)
        order = [pool[(start + i) % len(pool)] for i in range(min(len(pool), 50))]
    else:
        order = (rng or random).sample(pool, min(len(pool), max(want * 4, 10)))
    out, spent, probes = [], 0, 0
    for ex in order:
        if cooling(exit_id(ex), health):
            continue
        ex = with_country(ex, country)
        if check:
            if probes >= want + 4:
                break
            probes += 1
            ok, _reason, nb = healthy(ex)
            spent += nb
            if not ok:
                continue
        out.append(ex)
        if len(out) >= want:
            break
    # a sticky walk keeps one or two spares for when its exit fails mid-walk
    if mode == "sticky" and out:
        spares = [with_country(e, country) for e in order if not cooling(exit_id(e), health) and exit_id(with_country(e, country)) != exit_id(out[0])][:2]
        out += spares
    return out, spent


# ------------------------------------------------------------------ decisions (gate a) and settings
def _now() -> str:
    from .db import now_iso
    return now_iso()


def settings(p) -> dict:
    cfg = dict(getattr(p.spec, "proxy", None) or {})
    return {"enabled": bool(cfg.get("enabled")), "reason": cfg.get("reason"), "at": cfg.get("at"), "by": cfg.get("by"),
            "daily_bytes": int(cfg.get("daily_bytes") or os.environ.get("HARVEST_PROXY_DAILY_BYTES") or DEFAULT_DAILY_BYTES),
            "daily_requests": int(cfg.get("daily_requests") or os.environ.get("HARVEST_PROXY_DAILY_REQUESTS") or DEFAULT_DAILY_REQUESTS),
            "mode": cfg.get("mode") or os.environ.get("HARVEST_PROXY_MODE") or "sticky",
            "country": cfg.get("country") or os.environ.get("HARVEST_PROXY_COUNTRY") or "auto",
            "history": list(cfg.get("history") or [])[-20:]}


def source_decision(src: dict) -> dict | None:
    return ((src or {}).get("lane_detail") or {}).get("proxy")


def switched_on(p, src: dict) -> tuple[bool, str]:
    """Gate (a): a recorded operator decision. A source-level decision wins over the project's."""
    d = source_decision(src)
    if d is not None:
        return (True, "source decision") if d.get("enabled") and d.get("reason") and d.get("at") else (False, "switched off for this source")
    s = settings(p)
    if s["enabled"] and s["reason"] and s["at"]:
        return True, "project decision"
    return False, "no operator decision (harvest proxy enable <project> [--source] --reason ...)"


def eligible(p, src: dict, purpose: str = "run") -> tuple[bool, str]:
    """Gates (a) + (b). (c) is decided per request by `Http`."""
    if not configured():
        return False, "no pool (HARVEST_PROXY_FILE / HARVEST_PROXY_URL unset)"
    ok, why = switched_on(p, src)
    if not ok:
        return False, why
    if src.get("robots_status") == "disallowed":
        return False, "robots.txt disallows the source"
    terms = src.get("terms_status")
    if terms == "forbids":
        return False, "the terms forbid automated collection"
    if purpose == "detect":
        pass  # an unread verdict may be read through the route; forbids stops it (above and in lanes)
    elif terms not in ("allowed", "no_clause"):
        return False, f"terms verdict is {terms}; record allowed/no_clause first"
    elif src.get("robots_status") != "allowed":
        return False, f"robots status is {src.get('robots_status')}"
    reason = ((src.get("lane_detail") or {}).get("reason") or "").lower()
    if any(w in reason for w in ("login", "paywall", "captcha", "challenge")):
        return False, f"lane none for a reason the proxy never works around ({reason})"
    return True, why


def _record(p, entry: dict) -> None:
    cfg = dict(getattr(p.spec, "proxy", None) or {})
    cfg["history"] = (list(cfg.get("history") or []) + [entry])[-50:]
    p.spec.proxy = cfg
    p.save()


def enable(p, *, reason: str, source_id: str | None = None, by: str = "operator", daily_bytes: int | None = None,
           daily_requests: int | None = None, mode: str | None = None, country: str | None = None) -> dict:
    reason = (reason or "").strip()
    if len(reason) < 8:
        raise ValueError("an operator decision needs a reason (at least a short sentence): --reason '...'")
    if mode and mode not in MODES:
        raise ValueError("mode: sticky | rotate")
    if country and country not in ("auto", "off") and not country_code(country):
        raise ValueError("country: auto | off | a 2-letter code")
    for name, v in (("daily_bytes", daily_bytes), ("daily_requests", daily_requests)):
        if v is not None and int(v) < 0:
            raise ValueError(f"{name} must be >= 0")
    at = _now()
    entry = {"action": "enable", "source_id": source_id, "reason": redact(reason), "at": at, "by": by}
    cfg = dict(getattr(p.spec, "proxy", None) or {})
    for k, v in (("daily_bytes", daily_bytes), ("daily_requests", daily_requests), ("mode", mode), ("country", country)):
        if v is not None:
            cfg[k] = v
    if source_id:
        src = p.store.get_source(p.name, source_id)
        if not src:
            raise LookupError(f"no source {source_id}")
        detail = dict(src.get("lane_detail") or {})
        detail["proxy"] = {"enabled": True, "reason": redact(reason), "at": at, "by": by}
        p.store.upsert_source(p.name, source_id, {"lane_detail": detail, "use_proxy": 1})
    else:
        cfg.update(enabled=True, reason=redact(reason), at=at, by=by)
    p.spec.proxy = cfg
    _record(p, entry)
    return summary(p)


def disable(p, *, source_id: str | None = None, reason: str | None = None, by: str = "operator") -> dict:
    at = _now()
    if source_id:
        src = p.store.get_source(p.name, source_id)
        if not src:
            raise LookupError(f"no source {source_id}")
        detail = dict(src.get("lane_detail") or {})
        detail["proxy"] = {"enabled": False, "reason": redact(reason or "switched off"), "at": at, "by": by}
        p.store.upsert_source(p.name, source_id, {"lane_detail": detail, "use_proxy": 0})
    else:
        cfg = dict(getattr(p.spec, "proxy", None) or {})
        cfg.update(enabled=False, reason=redact(reason or "switched off"), at=at, by=by)
        p.spec.proxy = cfg
    _record(p, {"action": "disable", "source_id": source_id, "reason": redact(reason or ""), "at": at, "by": by})
    return summary(p)


# ------------------------------------------------------------------ budgets and leases
def today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


_RES_LOCK = threading.RLock()
_RESERVED: dict[tuple[str, str], list[int]] = {}


class Pending:
    """How many proxied walks of one run have not started yet (a walk's allowance is its fair share of what is left)."""

    def __init__(self, n: int):
        self.n = max(0, n)
        self._lock = threading.Lock()

    def take(self) -> int:
        with self._lock:
            n = max(1, self.n)
            self.n = max(0, self.n - 1)
            return n


@dataclass
class Lease:
    project: str
    source_id: str
    day: str
    purpose: str
    route: Route
    reserved: tuple[int, int]
    probe_bytes: int = 0
    why: str = ""
    settled: bool = False

    def job(self) -> dict:
        return self.route.to_job()


def remaining(p, day: str | None = None) -> dict:
    s = settings(p)
    day = day or today()
    used = p.store.proxy_usage_total(p.name, day)
    with _RES_LOCK:
        rb, rr = _RESERVED.get((p.name, day), [0, 0])
    return {"bytes": max(0, s["daily_bytes"] - used["bytes"] - rb), "requests": max(0, s["daily_requests"] - used["requests"] - rr),
            "used_bytes": used["bytes"], "used_requests": used["requests"], "reserved_bytes": rb, "reserved_requests": rr,
            "daily_bytes": s["daily_bytes"], "daily_requests": s["daily_requests"]}


def _country_for(p, src: dict) -> str | None:
    c = settings(p)["country"]
    if c == "off":
        return None
    if c != "auto":
        return country_code(c)
    return next((country_code(r) for r in (src.get("regions") or []) if country_code(r)), None)


def arm(p, src: dict, purpose: str = "run", pending: Pending | int | None = None) -> Lease | None:
    """Gates (a)+(b), then reserve an allowance and pick exits. None when the route is not allowed or no exit is
    healthy. With nothing left in the day's budget the lease has a zero allowance: the walk runs direct and the
    first IP block it meets is reported as a budget stop."""
    ok, why = eligible(p, src, purpose)
    if not ok:
        return None
    s = settings(p)
    n = pending.take() if isinstance(pending, Pending) else max(1, int(pending or 1))
    day = today()
    with _RES_LOCK:
        left = remaining(p, day)
        ab = left["bytes"] // n
        ar = left["requests"] // n
        cur = _RESERVED.setdefault((p.name, day), [0, 0])
        cur[0] += ab
        cur[1] += ar
    try:
        exits, probe_bytes = select(load(), key=f"{p.name}:{src['id']}", mode=s["mode"], country=_country_for(p, src))
    except Exception:
        exits, probe_bytes = [], 0
    lease = Lease(p.name, src["id"], day, purpose, Route(_exits=exits, mode=s["mode"], allowance_bytes=ab, allowance_requests=ar,
                                                         key=f"{p.name}:{src['id']}"), (ab, ar), probe_bytes, why)
    if probe_bytes:
        p.store.add_proxy_usage(p.name, day, src["id"], 0, probe_bytes)
    if not exits:
        release(lease)
        return None
    return lease


def release(lease: Lease) -> None:
    if lease.settled:
        return
    lease.settled = True
    with _RES_LOCK:
        cur = _RESERVED.get((lease.project, lease.day))
        if cur:
            cur[0] = max(0, cur[0] - lease.reserved[0])
            cur[1] = max(0, cur[1] - lease.reserved[1])


def settle(p, lease: Lease | None, report: dict | None, *, already: dict | None = None) -> dict | None:
    """Book what the walk used (minus what was booked incrementally, `already`), cool down exits that failed, release
    the reservation and alert on a budget stop."""
    if lease is None:
        return None
    rep = report or {}
    done = already or {"requests": 0, "bytes": 0}
    dreq = max(0, int(rep.get("requests", 0)) - int(done.get("requests", 0)))
    dbytes = max(0, int(rep.get("bytes", 0)) - int(done.get("bytes", 0)))
    if dreq or dbytes:
        p.store.add_proxy_usage(p.name, lease.day, lease.source_id, dreq, dbytes)
    release(lease)
    for xid, reason in (rep.get("failed_exits") or {}).items():
        cooldown(xid, reason)
    if rep.get("budget_stop"):
        left = remaining(p, lease.day)
        headroom = int(os.environ.get("HARVEST_PROXY_HEADROOM", HEADROOM))
        if left["daily_bytes"] - left["used_bytes"] < headroom or left["used_requests"] >= left["daily_requests"]:
            budget_alert(p, lease.source_id, lease.purpose)
        else:  # this walk's share of a budget other walks were also drawing on; the day's cap is not reached
            rep = {**rep, "share_spent": True}
    return rep


def book(p, lease: Lease | None, report: dict | None, booked: dict) -> None:
    """Incremental booking from a page event (so a killed walk is still accounted)."""
    if lease is None or not report:
        return
    dreq = max(0, int(report.get("requests", 0)) - booked["requests"])
    dbytes = max(0, int(report.get("bytes", 0)) - booked["bytes"])
    if dreq or dbytes:
        p.store.add_proxy_usage(p.name, lease.day, lease.source_id, dreq, dbytes)
        booked["requests"] += dreq
        booked["bytes"] += dbytes


def budget_alert(p, sid: str | None, purpose: str = "run") -> bool:
    """One `proxy_budget` alert per project and day, delivered through the alert hook / webhook / alerts.jsonl."""
    from . import watchdog
    day = today()
    if any(a.get("kind") == "proxy_budget" and str(a.get("created_at", "")).startswith(day) for a in p.store.list_alerts(p.name, 200)):
        return False
    left = remaining(p, day)
    msg = (f"residential-proxy budget for {day} is spent ({left['used_bytes']:,} of {left['daily_bytes']:,} bytes, "
           f"{left['used_requests']} of {left['daily_requests']} requests); the proxy route is stopped until tomorrow ({purpose})")
    watchdog.raise_alerts(p, [{"source_id": sid, "kind": "proxy_budget", "message": msg, "key": day}], dedup=False)
    return True


# ------------------------------------------------------------------ surfaces
def pool_status(check: int = 0) -> dict:
    """Pool presence and size, cooling exits, and optionally a live health probe of `check` exits (ids only)."""
    kind = "url" if os.environ.get("HARVEST_PROXY_URL") else "file" if os.environ.get("HARVEST_PROXY_FILE") else None
    pool = load() if kind else []
    health = _health_read()
    out: dict[str, Any] = {"configured": bool(kind), "kind": kind, "exits": len(pool),
                           "file_env": "HARVEST_PROXY_FILE" if kind == "file" else None,
                           "cooling": sum(1 for v in health.values() if v.get("cool_until", 0) > time.time()),
                           "mode_default": os.environ.get("HARVEST_PROXY_MODE") or "sticky",
                           "country_default": os.environ.get("HARVEST_PROXY_COUNTRY") or "auto"}
    if check and pool:
        res = []
        for ex in random.sample(pool, min(check, len(pool))):
            ok, reason, n = probe(ex)
            if not ok:
                cooldown(exit_id(ex), reason)
            res.append({"exit": exit_id(ex), "ok": ok, "reason": reason, "bytes": n})
        out["checked"] = res
    return out


def summary(p, days: int = 7) -> dict:
    s = settings(p)
    srcs = p.store.list_sources(p.name)
    per_source = []
    for x in srcs:
        d = source_decision(x)
        on, why = switched_on(p, x)
        el, ewhy = eligible(p, x, "run")
        if d is None and not s["enabled"]:
            continue
        per_source.append({"id": x["id"], "switched_on": on, "decision": d, "eligible_for_runs": el, "why": ewhy if not el else why})
    usage = p.store.proxy_usage(p.name, since=(dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days - 1)).isoformat())
    by_source: dict[str, dict] = {}
    for u in usage:
        b = by_source.setdefault(u["source_id"], {"requests": 0, "bytes": 0})
        b["requests"] += int(u["requests"] or 0)
        b["bytes"] += int(u["bytes"] or 0)
    return {"project": p.name, "pool_configured": configured(),
            "decision": {k: s[k] for k in ("enabled", "reason", "at", "by")},
            "settings": {k: s[k] for k in ("daily_bytes", "daily_requests", "mode", "country")},
            "today": remaining(p), "sources": per_source, "usage_by_day": usage, f"usage_{days}d_by_source": by_source,
            "history": s["history"]}


def sandboxed(p, src: dict, job: dict, timeout_s: float, on_event=None, *, purpose: str = "run", pending: Pending | int | None = None,
              enabled: bool = True, stop=None) -> dict:
    """`sandbox.run` with the route armed when gates (a)+(b) allow it: usage is booked as pages arrive (a killed walk
    is still accounted), settled at the end, failing exits cooled down, and a budget stop alerted. The result gains
    `proxy` (counters only) when a route was armed."""
    from . import sandbox
    lease = arm(p, src, purpose, pending) if enabled else None
    if lease is not None:
        job = {**job, "proxy": lease.job()}
    booked = {"requests": 0, "bytes": 0}

    def ev(e: dict) -> None:
        if lease is not None and e.get("proxy"):
            book(p, lease, e["proxy"], booked)
        if on_event:
            on_event(e)
    try:
        res = sandbox.run(job, timeout_s, ev, stop=stop)
    except BaseException:
        if lease is not None:
            release(lease)
        raise
    if lease is not None:
        end = res.get("end") or {}
        last = end.get("proxy") or next((pg.get("proxy") for pg in reversed(res.get("pages") or []) if pg.get("proxy")), None)
        rep = settle(p, lease, last, already=booked) or {}
        res["proxy"] = {"armed": True, "why": lease.why, "requests": rep.get("requests", 0), "bytes": rep.get("bytes", 0),
                        "budget_stop": bool(rep.get("budget_stop")), "share_spent": bool(rep.get("share_spent")),
                        "allowance_bytes": lease.reserved[0], "allowance_requests": lease.reserved[1],
                        "failed_exits": len(rep.get("failed_exits") or {})}
    return res


def detect_http(p, src: dict, *, rate_s: float, enabled: bool = True, ua_mode: str = "own"):
    """(Http, lease) for lane detection of one source: with the route armed when gates (a)+(b) allow it."""
    from .http import Http
    lease = arm(p, src, "detect") if enabled else None
    return Http(rate_s=rate_s, route=lease.route if lease else None, ua_mode=ua_mode), lease
