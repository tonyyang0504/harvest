"""Fixture websites for the real-browser UI end-to-end suite (tests/test_ui_e2e.py).

Each site is a `localsite.Site` on its own port, addressed as `http://<name>.localhost:<port>` so every site is
its own registrable domain (the census dedups by domain). They model what a real user meets:

- `cars`:   a paginated used-car board with Arabic, CJK, a very long title and description, XSS payloads in the
            scraped fields, three rows that must be quarantined, and a page that breaks (HTTP 500) while `broken` is set;
- `wall`:   a bot-challenge page (403 + `cf-mitigated: challenge`, "Just a moment...");
- `denied`: a plain 403 (an IP-level block);
- `spare*`: small boards used to fill the census budget.
"""

from __future__ import annotations

import html
import json

from localsite import Req, Site, html_page, start

# a listing whose title, as the site shows it, reads `<img src=x onerror=...>`: the scraped text keeps it entity-encoded,
# so after cleaning the stored title holds live markup that every view must render as text
XSS_TITLE_RAW = "&lt;img src=x onerror=\"window.__xss=1\"&gt;<script>window.__xss=1</script>Toyota Camry"
XSS_TITLE = "<img src=x onerror=\"window.__xss=1\"> Toyota Camry"  # what is stored
XSS_URL = "javascript:window.__xss=1"
ARABIC = "تويوتا كامري ٢٠١٨ — دبي"
CJK = "丰田 卡罗拉 東京 中古車 도요타"
LONG_TITLE = "Lada Vesta " + "Ж" * 3000  # one unbroken 3,000-character word
LONG = "Long description " + ("lorem-ipsum-dolor-sit-amet-" * 400) + " END"  # ~11k chars, with a 10k-char unbroken word

PER_PAGE = 8
PAGES = 3  # page 4 is empty: the natural end of the walk


def car_rows() -> list[dict]:
    rows = []
    makes = [("Toyota", "Camry"), ("Lada", "Vesta"), ("Hyundai", "Tucson"), ("Kia", "Rio")]
    for i in range(1, PAGES * PER_PAGE + 1):
        make, model = makes[i % 4]
        rows.append({"id": f"c{i}", "title": f"{make} {model}", "make": make, "model": model, "year": str(2008 + i % 14),
                     "price": f"{(4 + i) * 1_000_000:,}".replace(",", " ") + " ₸", "mileage": f"{40 + i * 5} тыс. км",
                     "fuel": ["Бензин", "Дизель"][i % 2], "gear": ["Автомат", "Механика"][i % 2], "desc": f"Ad number {i}", "href": f"/cars/c{i}"})
    rows[0].update(title=XSS_TITLE_RAW, make="Toyota", model="Camry", trim="<svg onload=window.__xss=1>", desc="<b>bold?</b><script>window.__xss=1</script>")
    rows[1].update(title=ARABIC, make="Toyota", model="Camry", desc="سيارة نظيفة جداً، الفحص كامل")
    rows[2].update(title=CJK, make="Toyota", model="Corolla", desc="走行距離が少ない。程度良好。")
    rows[3].update(title=LONG_TITLE, desc=LONG)
    rows[9].update(price="")            # page 2: a required field missing -> quarantine
    rows[10].update(year="1066")        # page 2: out of the sanity bounds -> quarantine
    rows[11].update(href=XSS_URL, title="<script>window.__xss=1</script>Kia Rio")  # page 2: a javascript: URL is no URL -> quarantine
    return rows


CAR_MODULE = '''"""Scraper for the UI e2e fixture board (written by the stub build agent)."""
import json

from harvest_ai import extract


def fetch(page, *, http, ctx):
    base = ctx["source"]["url"]
    text = http.get_text(base + ("" if page == 1 else "?page=" + str(page)))
    if not text:
        return []
    doc = extract.parse_html(text)
    rows = []
    for card in doc.find_all("article", cls="ad"):
        data = json.loads(card.get("data-json"))
        rows.append({"source_id": data["id"], "url": data["href"], "title": data["title"], "make": data["make"], "model": data["model"],
                     "year": data["year"], "price": data["price"], "mileage": data["mileage"], "fuel_type": data["fuel"],
                     "transmission": data["gear"], "description": data["desc"], "trim": data.get("trim")})
    return rows
'''


def cars_board() -> Site:
    s = Site()
    s.broken = True  # page 3 answers 500 until a test "repairs" the site
    s.route("/robots.txt", (200, {"content-type": "text/plain"}, "User-agent: *\nDisallow: /private\n"))
    s.route("/terms", html_page("<h1>Terms</h1><p>Be kind. Listings are published by their sellers.</p>"))
    rows = car_rows()

    def listing(req: Req):
        page = int(req.query.get("page", "1"))
        if page == 3 and s.broken:
            return (500, {"content-type": "text/html"}, html_page("<h1>Internal Server Error</h1>"))
        chunk = rows[(page - 1) * PER_PAGE: page * PER_PAGE] if page <= PAGES else []
        cards = "".join(f"<article class='ad' data-json=\"{html.escape(json.dumps(r, ensure_ascii=False), quote=True)}\">"
                        f"<h2>{html.escape(r['title'])}</h2><span class='price'>{html.escape(r['price'])}</span></article>" for r in chunk)
        filler = "<p>" + ("Used cars for sale. " * 60) + "</p>" + "".join(f"<a href='/c/{i}'>c{i}</a>" for i in range(12))
        return html_page(f"<main>{cards}</main>{filler}<footer><a href='/terms'>Terms of use</a></footer>", "Cars")
    s.route("/cars", listing)
    return start(s)


def challenge_wall() -> Site:
    s = Site()
    s.route("/robots.txt", (200, {"content-type": "text/plain"}, "User-agent: *\nAllow: /\n"))
    page = html_page("<h1>Just a moment...</h1><p>Checking your browser before accessing.</p><div id='cf-chl-widget'></div>", "Just a moment...")
    s.route("/ads", (403, {"content-type": "text/html", "cf-mitigated": "challenge", "server": "cloudflare"}, page))
    return start(s)


def denied() -> Site:
    s = Site()
    s.route("/robots.txt", (200, {"content-type": "text/plain"}, "User-agent: *\nAllow: /\n"))
    s.route("/list", (403, {"content-type": "text/html"}, html_page("<h1>403 Forbidden</h1><p>Access denied.</p>")))
    return start(s)


def spare() -> Site:
    s = Site()
    s.route("/robots.txt", (200, {"content-type": "text/plain"}, "User-agent: *\nAllow: /\n"))
    # a real JavaScript shell: an empty mount point and a script that renders the ads (so lane detection says
    # "browser"; a short script-free page is plain html since the 2026-10 soak fixes)
    render = "".join(f"<article class='ad'><h2>Car {i}</h2></article>" for i in range(5))
    s.route("/ads", html_page(f"<div id='app'></div><script>document.getElementById('app').innerHTML = \"{render}\";</script>"))
    return start(s)


def url(site: Site, name: str, path: str) -> str:
    return f"http://{name}.localhost:{site.server.server_address[1]}{path}"


def candidate(u: str, name: str, regions: list[str], angle: str = "classifieds") -> dict:
    return {"url": u, "name": name, "regions": regions, "angle": angle, "evidence": [{"url": u, "observation": "page 1: used-car ads with prices"}]}
