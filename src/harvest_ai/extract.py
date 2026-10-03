"""Extraction helpers for scraper modules (no third-party parser needed).

- `parse_html(text)` -> a small DOM: `find(tag, cls=, id=, attrs=)`, `find_all(...)`, `.text`, `.get(attr)`, `.html`
- `jsonld(text)` -> every JSON-LD object (flattened @graph)
- `next_data(text)` -> Next.js `__NEXT_DATA__`; `nuxt_data(text)` -> Nuxt state when it is JSON
- `script_json(text, var)` -> `window.<var> = {...}` assignments
- `feed_items(text)` -> RSS/Atom/JSON Feed items as dicts (raises FeedError when a non-empty document does not parse)
- `sitemap_urls(text)` -> (page URLs, child sitemaps)
- `links(text, base)` -> absolute hrefs; `find_items(obj, keys)` -> dicts in a JSON tree carrying all keys
"""

from __future__ import annotations

import html as _html
import json
import re
from html.parser import HTMLParser
from typing import Any, Iterator
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "_text")

    def __init__(self, tag: str, attrs: dict | None = None, parent: "Node | None" = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list = []
        self.parent = parent

    def get(self, name: str, default: Any = None) -> Any:
        return self.attrs.get(name, default)

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def iter(self) -> Iterator["Node"]:
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.iter()

    def _match(self, tag, cls, id, attrs) -> bool:
        if tag and self.tag != tag:
            return False
        if cls and not all(c in self.classes for c in cls.split()):
            return False
        if id and self.attrs.get("id") != id:
            return False
        for k, v in (attrs or {}).items():
            have = self.attrs.get(k)
            if v is True:
                if have is None:
                    return False
            elif hasattr(v, "search"):
                if have is None or not v.search(have):
                    return False
            elif have != v:
                return False
        return True

    def find_all(self, tag: str | None = None, cls: str | None = None, id: str | None = None, attrs: dict | None = None, limit: int | None = None) -> list["Node"]:
        out = []
        for n in self.iter():
            if n._match(tag, cls, id, attrs):
                out.append(n)
                if limit and len(out) >= limit:
                    break
        return out

    def find(self, tag: str | None = None, cls: str | None = None, id: str | None = None, attrs: dict | None = None) -> "Node | None":
        r = self.find_all(tag, cls, id, attrs, limit=1)
        return r[0] if r else None

    @property
    def text(self) -> str:
        parts: list[str] = []

        def walk(n):
            for c in n.children:
                if isinstance(c, str):
                    parts.append(c)
                elif c.tag not in ("script", "style", "noscript"):
                    walk(c)
        walk(self)
        return re.sub(r"\s+", " ", _html.unescape("".join(parts))).strip()

    @property
    def html(self) -> str:
        def render(n):
            if isinstance(n, str):
                return n
            a = "".join(f' {k}="{_html.escape(str(v))}"' for k, v in n.attrs.items() if v is not None)
            inner = "".join(render(c) for c in n.children)
            return f"<{n.tag}{a}>" + ("" if n.tag in _VOID else f"{inner}</{n.tag}>")
        return render(self)

    def __repr__(self) -> str:
        return f"<Node {self.tag} {self.attrs}>"


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(n)
        if tag not in _VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_html(text: str) -> Node:
    b = _Builder()
    try:
        b.feed(text or "")
        b.close()
    except Exception:
        pass
    return b.root


def _scripts(text: str, type_re: str | None = None, id_: str | None = None) -> list[str]:
    out = []
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", text or "", re.S | re.I):
        attrs = m.group(1)
        if type_re and not re.search(r"type\s*=\s*[\"']?" + type_re, attrs, re.I):
            continue
        if id_ and not re.search(r"id\s*=\s*[\"']?" + re.escape(id_) + r"[\"'\s>]", attrs + ">", re.I):
            continue
        out.append(m.group(2))
    return out


def jsonld(text: str) -> list[dict]:
    out: list[dict] = []
    for body in _scripts(text, r"application/ld\+json"):
        try:
            data = json.loads(body.strip())
        except ValueError:
            try:
                data = json.loads(_html.unescape(body.strip()))
            except ValueError:
                continue
        stack = data if isinstance(data, list) else [data]
        for d in stack:
            if isinstance(d, dict):
                if isinstance(d.get("@graph"), list):
                    out.extend(x for x in d["@graph"] if isinstance(x, dict))
                else:
                    out.append(d)
    return out


def jsonld_types(text: str) -> list[str]:
    types: list[str] = []
    for d in jsonld(text):
        t = d.get("@type")
        for x in (t if isinstance(t, list) else [t]):
            if x and x not in types:
                types.append(str(x))
        if d.get("@type") == "ItemList":
            for el in d.get("itemListElement") or []:
                it = el.get("item") if isinstance(el, dict) else None
                if isinstance(it, dict) and it.get("@type") and it["@type"] not in types:
                    types.append(str(it["@type"]))
    return types


def next_data(text: str) -> Any:
    for body in _scripts(text, None, "__NEXT_DATA__"):
        try:
            return json.loads(body)
        except ValueError:
            return None
    return None


def nuxt_data(text: str) -> Any:
    for body in _scripts(text, None, "__NUXT_DATA__"):
        try:
            return json.loads(body)
        except ValueError:
            pass
    return script_json(text, "__NUXT__")


def script_json(text: str, var: str) -> Any:
    """`window.__INITIAL_STATE__ = {...};` -> the object (JSON only; JS expressions return None)."""
    m = re.search(r"(?:window\.)?" + re.escape(var) + r"\s*=\s*", text or "")
    if not m:
        return None
    start = m.end()
    if start >= len(text) or text[start] not in "{[":
        return None
    depth, in_str, esc = 0, None, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
        elif ch in "\"'":
            in_str = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    return None
    return None


def find_items(obj: Any, keys: tuple | list, limit: int = 5000) -> list[dict]:
    """Every dict in a JSON tree that carries all `keys` (e.g. ('id', 'price'))."""
    out: list[dict] = []
    stack = [obj]
    while stack and len(out) < limit:
        cur = stack.pop()
        if isinstance(cur, dict):
            if all(k in cur for k in keys):
                out.append(cur)
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))
    return out


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1].lower()


class FeedError(ValueError):
    """The text is not a parsable feed (truncated, cut off mid-document, or not a feed at all)."""


def feed_items(text: str) -> list[dict]:
    """Items of an RSS / Atom / JSON Feed document. Empty input (a blocked or failed fetch) gives []. A document that
    does not parse raises FeedError: a feed cut off mid-transfer used to come back as [] and the walk was recorded as a
    complete, empty run (soak 2026-10); in a scraper module the exception makes the page an error instead."""
    t = (text or "").lstrip()
    if not t:
        return []
    if t.startswith("{"):
        try:
            data = json.loads(t)
        except ValueError as exc:
            raise FeedError(f"JSON feed does not parse: {exc}") from None
        return [i for i in data.get("items") or [] if isinstance(i, dict)] if isinstance(data, dict) else []
    try:
        root = ET.fromstring(t.encode("utf-8") if isinstance(t, str) else t)
    except ET.ParseError as exc:
        raise FeedError(f"feed XML does not parse ({len(t)} characters): {exc}") from None
    out = []
    for el in root.iter():
        if _strip_ns(el.tag) not in ("item", "entry"):
            continue
        d: dict[str, Any] = {}
        for c in el:
            name = _strip_ns(c.tag)
            if name == "link" and c.get("href"):
                d.setdefault("link", c.get("href"))
            elif len(c) == 0:
                d.setdefault(name, (c.text or "").strip())
            if c.attrib and name not in ("link",):
                d.setdefault(name + "_attrs", dict(c.attrib))
        out.append(d)
    return out


def sitemap_urls(text: str) -> tuple[list[str], list[str]]:
    try:
        root = ET.fromstring((text or "").strip().encode("utf-8"))
    except ET.ParseError:
        return [], []
    pages, maps = [], []
    kind = _strip_ns(root.tag)
    for el in root.iter():
        if _strip_ns(el.tag) == "loc" and el.text:
            (maps if kind == "sitemapindex" else pages).append(el.text.strip())
    return pages, maps


def links(text: str, base: str | None = None, pattern: str | None = None) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"<a\b[^>]*?href\s*=\s*[\"']([^\"'#]+)", text or "", re.I):
        href = _html.unescape(m.group(1).strip())
        if base:
            href = urljoin(base, href)
        if pattern and not re.search(pattern, href):
            continue
        if href not in out:
            out.append(href)
    return out


def meta(text: str) -> dict:
    out = {}
    for m in re.finditer(r"<meta\b([^>]+)>", text or "", re.I):
        a = dict((k.lower(), v) for k, _, v in re.findall(r"([\w:-]+)\s*=\s*([\"'])(.*?)\2", m.group(1)))
        key = a.get("property") or a.get("name")
        if key and "content" in a:
            out.setdefault(key.lower(), _html.unescape(a["content"]))
    t = re.search(r"<title[^>]*>(.*?)</title>", text or "", re.S | re.I)
    if t:
        out.setdefault("title", _html.unescape(re.sub(r"\s+", " ", t.group(1)).strip()))
    lang = re.search(r"<html\b[^>]*\blang\s*=\s*[\"']?([\w-]+)", text or "", re.I)
    if lang:
        out.setdefault("lang", lang.group(1))
    return out


def visible_text(text: str) -> str:
    t = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", text or "", flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", _html.unescape(t)).strip()
