"""A local HTTP test server whose routes tests define in Python."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlsplit

RESET = "__reset__"


@dataclass
class Req:
    method: str
    path: str
    query: dict
    headers: dict
    body: bytes = b""
    peer: tuple = ()  # the client's (host, port): a test can tell a proxied connection from a direct one


@dataclass
class Site:
    routes: dict[str, Callable[[Req], tuple] | tuple | str] = field(default_factory=dict)
    hits: list[tuple[float, str, str]] = field(default_factory=list)
    active: int = 0
    max_active: int = 0
    server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def route(self, path: str, resp) -> None:
        self.routes[path] = resp

    def paths(self) -> list[str]:
        return [h[2] for h in self.hits]

    def count(self, path: str) -> int:
        return sum(1 for h in self.hits if h[2] == path)


def html_page(body: str, title: str = "t", head: str = "") -> str:
    return f"<!doctype html><html lang='en'><head><title>{title}</title>{head}</head><body>{body}</body></html>"


def make_handler(site: Site):
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _serve(self, method: str):
            u = urlsplit(self.path)
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            req = Req(method, u.path, {k: v[0] for k, v in parse_qs(u.query).items()}, dict(self.headers), body, tuple(self.client_address))
            with lock:
                site.active += 1
                site.max_active = max(site.max_active, site.active)
                site.hits.append((time.monotonic(), method, u.path + ("?" + u.query if u.query else "")))
            try:
                r = site.routes.get(u.path)
                if r is None:
                    status, headers, payload = 404, {"content-type": "text/plain"}, "not found"
                else:
                    res = r(req) if callable(r) else r
                    if res == RESET:  # drop the connection with a TCP RST, as a network-level block does
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                        self.close_connection = True
                        return
                    if isinstance(res, str):
                        res = (200, {"content-type": "text/html; charset=utf-8"}, res)
                    elif isinstance(res, (dict, list)):
                        res = (200, {"content-type": "application/json"}, json.dumps(res))
                    status, headers, payload = res
                data = payload.encode("utf-8") if isinstance(payload, str) else payload
                time.sleep(float(headers.pop("x-delay", 0)) if "x-delay" in headers else 0)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            finally:
                with lock:
                    site.active -= 1

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

    return H


def start(site: Site) -> Site:
    site.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(site))
    site.server.daemon_threads = True
    threading.Thread(target=site.server.serve_forever, daemon=True).start()
    return site


# ---------------------------------------------------------------- a demo classifieds site
CARS = [
    {"id": f"a{i}", "make": ["Toyota", "Lada", "Hyundai", "Kia"][i % 4], "model": ["Camry", "Vesta", "Tucson", "Rio"][i % 4],
     "year": 2010 + i % 12, "price": f"{(5 + i) * 1_000_000:,}".replace(",", " ") + " ₸", "mileage": f"{50 + i * 3} тыс. км",
     "fuel": ["Бензин", "Дизель", "Гибрид", "Бензин"][i % 4], "gear": ["Автомат", "Механика"][i % 2]}
    for i in range(1, 26)
]


def cars_site(pages: int = 3, per_page: int = 10, terms_text: str | None = None, extra_robots: str = "") -> Site:
    s = Site()
    s.route("/robots.txt", (200, {"content-type": "text/plain"}, f"User-agent: *\nDisallow: /private\n{extra_robots}\nSitemap: /nothing.xml\n"))
    s.route("/terms", html_page("<h1>Terms</h1><p>" + (terms_text or "Use this site kindly. Listings are published by sellers.") + "</p>"))

    def listing(req: Req):
        page = int(req.query.get("page", "1"))
        rows = CARS[(page - 1) * per_page: page * per_page] if page <= pages else []
        cards = "".join(
            f"<article class='ad' data-id='{c['id']}'><a href='/cars/{c['id']}'><h2>{c['make']} {c['model']}</h2></a>"
            f"<span class='year'>{c['year']}</span><span class='price'>{c['price']}</span><span class='km'>{c['mileage']}</span>"
            f"<span class='fuel'>{c['fuel']}</span><span class='gear'>{c['gear']}</span></article>" for c in rows)
        filler = "<p>" + ("Used cars for sale in Kazakhstan. " * 60) + "</p>" + "".join(f"<a href='/c/{i}'>c{i}</a>" for i in range(12))
        return html_page(f"<main>{cards}</main>{filler}<footer><a href='/terms'>Terms of use</a></footer>", "Cars")
    s.route("/cars", listing)
    return s


CAR_MODULE = '''"""Demo scraper for the local cars site."""
from harvest_ai import extract


def fetch(page, *, http, ctx):
    base = ctx["source"]["url"]
    html = http.get_text(base + ("" if page == 1 else f"?page={page}"))
    if not html:
        return []
    doc = extract.parse_html(html)
    rows = []
    for card in doc.find_all("article", cls="ad"):
        a = card.find("a")
        make, _, model = card.find("h2").text.partition(" ")
        rows.append({
            "source_id": card.get("data-id"),
            "url": a.get("href"),
            "title": card.find("h2").text,
            "make": make,
            "model": model,
            "year": card.find("span", cls="year").text,
            "price": card.find("span", cls="price").text,
            "mileage": card.find("span", cls="km").text,
            "fuel_type": card.find("span", cls="fuel").text,
            "transmission": card.find("span", cls="gear").text,
        })
    return rows
'''
