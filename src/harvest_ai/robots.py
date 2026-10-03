"""robots.txt per RFC 9309: groups by user-agent, longest-match allow/disallow with `*` and `$`,
Crawl-delay and Sitemap lines. Fetch outcomes: 2xx parsed, 4xx = no restrictions, 5xx or network
failure = everything disallowed (the RFC's "unreachable")."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[str, bool]] = field(default_factory=list)  # (path pattern, allow)
    crawl_delay: float | None = None


@dataclass
class Robots:
    groups: list[Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    status: str = "parsed"  # parsed | missing (4xx) | unreachable (5xx/network)
    fetched_url: str | None = None

    def _group(self, ua: str) -> Group | None:
        """The rules that apply to `ua` (RFC 9309 2.2.1): every group naming its product token, merged; else
        every `*` group, merged. A site often repeats `User-agent: *` further down the file, and the rules of
        all those groups apply. The token must match exactly (case-insensitive): a group for another crawler
        whose name merely contains ours ("harvest" vs "harvest-bot", or "bot") is not ours."""
        token = ua.split("/")[0].strip().lower()
        own, default = [], []
        for g in self.groups:
            names = {a.split("/")[0].strip().lower() for a in g.agents}
            if token and token in names:
                own.append(g)
            elif "*" in names:
                default.append(g)
        picked = own or default
        if not picked:
            return None
        if len(picked) == 1:
            return picked[0]
        delays = [g.crawl_delay for g in picked if g.crawl_delay is not None]
        return Group(agents=[a for g in picked for a in g.agents], rules=[r for g in picked for r in g.rules],
                     crawl_delay=max(delays) if delays else None)

    def can_fetch(self, ua: str, url: str) -> bool:
        if self.status == "missing":
            return True
        if self.status == "unreachable":
            return False
        u = urlsplit(url)
        path = u.path or "/"
        if path == "/robots.txt":
            return True
        target = unquote(path + ("?" + u.query if u.query else ""))
        g = self._group(ua)
        if g is None:
            return True
        best: tuple[int, bool] | None = None
        for pattern, allow in g.rules:
            if not pattern:
                continue
            if _match(pattern, target):
                rank = (len(pattern), allow)
                if best is None or rank > best:
                    best = rank
        return True if best is None else best[1]

    def crawl_delay(self, ua: str) -> float | None:
        g = self._group(ua)
        return g.crawl_delay if g else None

    def summary(self, ua: str, paths: list[str] | None = None) -> dict:
        g = self._group(ua)
        return {"status": self.status, "url": self.fetched_url, "group": (g.agents if g else None),
                "crawl_delay": self.crawl_delay(ua), "sitemaps": self.sitemaps[:20],
                "disallow": [p for p, a in (g.rules if g else []) if not a][:30],
                "checked": {p: self.can_fetch(ua, p) for p in (paths or [])}}


def _match(pattern: str, target: str) -> bool:
    anchored = pattern.endswith("$")
    body = ".*".join(re.escape(part) for part in unquote(pattern[:-1] if anchored else pattern).split("*"))
    return re.match(body + ("$" if anchored else ""), target) is not None


def parse(text: str) -> Robots:
    r = Robots()
    cur: Group | None = None
    last_was_agent = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            if cur is None or not last_was_agent:
                cur = Group()
                r.groups.append(cur)
            cur.agents.append(val)
            last_was_agent = True
            continue
        last_was_agent = False
        if key == "sitemap":
            r.sitemaps.append(val)
        elif cur is None:
            continue
        elif key == "disallow":
            if val:
                cur.rules.append((val, False))
        elif key == "allow":
            cur.rules.append((val, True))
        elif key == "crawl-delay":
            try:
                cur.crawl_delay = float(val)
            except ValueError:
                pass
    return r


def from_status(status: int | None, text: str = "", url: str | None = None) -> Robots:
    if status is None or status >= 500:
        r = Robots(status="unreachable")
    elif 400 <= status < 500:
        r = Robots(status="missing")
    else:
        r = parse(text)
    r.fetched_url = url
    return r
