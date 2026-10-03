"""Browser user-agent mode: an opt-in, per operator decision, for sites that refuse non-browser user agents.

When on for a project or source, its requests send a current stable desktop Chrome user agent with a consistent
Accept / Accept-Language set (`http.BROWSER_HEADERS`) instead of harvest's own UA; the browser lane uses the
launched Chromium build's own Chrome UA. That is all it changes. It never spoofs fingerprints, hides
navigator.webdriver, solves captchas or challenges, or works around logins and paywalls: a challenge page still
stops the source (lane none). robots.txt is evaluated for harvest's own token AND for `*`, the stricter wins.

Gates, like the proxy route (proxy.py) and decided separately from it:
  (a) a recorded operator decision (reason + date) on the project (`project.json` -> `ua`) or the source
      (`lane_detail.ua`); a source-level decision wins;
  (b) robots allow and the terms verdict is allowed/no_clause (lane detection may also run with an unread verdict
      so the terms can be read; `forbids` stops it), and the source is not lane none for a login, paywall,
      captcha or challenge.
Agents have no tool to switch it on; `tune` / `update_source` refuse it.
"""

from __future__ import annotations

from .proxy import _now, redact


def settings(p) -> dict:
    cfg = dict(getattr(p.spec, "ua", None) or {})
    return {"enabled": bool(cfg.get("enabled")), "reason": cfg.get("reason"), "at": cfg.get("at"), "by": cfg.get("by"),
            "history": list(cfg.get("history") or [])[-20:]}


def source_decision(src: dict) -> dict | None:
    return ((src or {}).get("lane_detail") or {}).get("ua")


def switched_on(p, src: dict) -> tuple[bool, str]:
    d = source_decision(src)
    if d is not None:
        return (True, "source decision") if d.get("enabled") and d.get("reason") and d.get("at") else (False, "switched off for this source")
    s = settings(p)
    if s["enabled"] and s["reason"] and s["at"]:
        return True, "project decision"
    return False, "no operator decision (harvest ua enable <project> [--source] --reason ...)"


def eligible(p, src: dict, purpose: str = "run") -> tuple[bool, str]:
    ok, why = switched_on(p, src)
    if not ok:
        return False, why
    if src.get("robots_status") == "disallowed":
        return False, "robots.txt disallows the source"
    terms = src.get("terms_status")
    if terms == "forbids":
        return False, "the terms forbid automated collection"
    if purpose != "detect" and terms not in ("allowed", "no_clause"):
        return False, f"terms verdict is {terms}; record allowed/no_clause first"
    if purpose != "detect" and src.get("robots_status") != "allowed":
        return False, f"robots status is {src.get('robots_status')}"
    reason = ((src.get("lane_detail") or {}).get("reason") or "").lower()
    if any(w in reason for w in ("login", "paywall", "captcha", "challenge")):
        return False, f"lane none for a reason no user agent changes ({reason})"
    return True, why


def mode_for(p, src: dict, purpose: str = "run", enabled: bool = True) -> str:
    return "browser" if enabled and eligible(p, src, purpose)[0] else "own"


def _record(p, entry: dict) -> None:
    cfg = dict(getattr(p.spec, "ua", None) or {})
    cfg["history"] = (list(cfg.get("history") or []) + [entry])[-50:]
    p.spec.ua = cfg
    p.save()


def enable(p, *, reason: str, source_id: str | None = None, by: str = "operator") -> dict:
    reason = (reason or "").strip()
    if len(reason) < 8:
        raise ValueError("an operator decision needs a reason (at least a short sentence): --reason '...'")
    at = _now()
    if source_id:
        src = p.store.get_source(p.name, source_id)
        if not src:
            raise LookupError(f"no source {source_id}")
        detail = dict(src.get("lane_detail") or {})
        detail["ua"] = {"enabled": True, "mode": "browser", "reason": redact(reason), "at": at, "by": by}
        p.store.upsert_source(p.name, source_id, {"lane_detail": detail})
    else:
        cfg = dict(getattr(p.spec, "ua", None) or {})
        cfg.update(enabled=True, mode="browser", reason=redact(reason), at=at, by=by)
        p.spec.ua = cfg
    _record(p, {"action": "enable", "source_id": source_id, "reason": redact(reason), "at": at, "by": by})
    return summary(p)


def disable(p, *, source_id: str | None = None, reason: str | None = None, by: str = "operator") -> dict:
    at = _now()
    if source_id:
        src = p.store.get_source(p.name, source_id)
        if not src:
            raise LookupError(f"no source {source_id}")
        detail = dict(src.get("lane_detail") or {})
        detail["ua"] = {"enabled": False, "reason": redact(reason or "switched off"), "at": at, "by": by}
        p.store.upsert_source(p.name, source_id, {"lane_detail": detail})
    else:
        cfg = dict(getattr(p.spec, "ua", None) or {})
        cfg.update(enabled=False, reason=redact(reason or "switched off"), at=at, by=by)
        p.spec.ua = cfg
    _record(p, {"action": "disable", "source_id": source_id, "reason": redact(reason or ""), "at": at, "by": by})
    return summary(p)


def summary(p) -> dict:
    from .http import BROWSER_HEADERS, browser_ua
    s = settings(p)
    per_source = []
    for x in p.store.list_sources(p.name):
        d = source_decision(x)
        if d is None and not s["enabled"]:
            continue
        on, why = switched_on(p, x)
        el, ewhy = eligible(p, x, "run")
        per_source.append({"id": x["id"], "switched_on": on, "decision": d, "ua_mode_for_runs": "browser" if el else "own",
                           "why": why if el else ewhy})
    return {"project": p.name, "decision": {k: s[k] for k in ("enabled", "reason", "at", "by")}, "sources": per_source,
            "browser_headers": {"User-Agent": browser_ua(), **BROWSER_HEADERS}, "history": s["history"]}
