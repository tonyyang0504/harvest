"""Session headers (`Cookie`, `Authorization`, `Proxy-Authorization`) on scraper requests: refused by default.

harvest never logs in. A module's request that carries one of these headers is refused by the http helper
(`Http(allow_session_headers=False)`, event `session_refused`) unless the operator recorded a decision for that
one source, for example a public API that wants a published key in `Authorization`. The decision lives in the
source's `lane_detail.session_headers` with a reason, a date and who made it; it is per source only, and agents
have no tool for it. A request with such a header still never goes through the residential-proxy route.
"""

from __future__ import annotations

from .proxy import _now, redact

HEADERS = ("cookie", "authorization", "proxy-authorization")


def carries_session(headers: dict | None) -> bool:
    return any(str(k).lower() in HEADERS for k in (headers or {}))


def decision(src: dict) -> dict | None:
    return ((src or {}).get("lane_detail") or {}).get("session_headers")


def allowed(src: dict) -> bool:
    d = decision(src)
    return bool(d and d.get("enabled") and d.get("reason") and d.get("at"))


def _set(p, source_id: str, entry: dict) -> dict:
    src = p.store.get_source(p.name, source_id)
    if not src:
        raise LookupError(f"no source {source_id}")
    detail = dict(src.get("lane_detail") or {})
    hist = list((detail.get("session_headers") or {}).get("history") or [])
    detail["session_headers"] = {**entry, "history": (hist + [entry])[-20:]}
    p.store.upsert_source(p.name, source_id, {"lane_detail": detail})
    return {"source_id": source_id, "session_headers": detail["session_headers"]}


def enable(p, source_id: str, *, reason: str, by: str = "operator") -> dict:
    reason = (reason or "").strip()
    if len(reason) < 8:
        raise ValueError("an operator decision needs a reason (at least a short sentence)")
    return _set(p, source_id, {"enabled": True, "reason": redact(reason), "at": _now(), "by": by})


def disable(p, source_id: str, *, reason: str | None = None, by: str = "operator") -> dict:
    return _set(p, source_id, {"enabled": False, "reason": redact(reason or "switched off"), "at": _now(), "by": by})
