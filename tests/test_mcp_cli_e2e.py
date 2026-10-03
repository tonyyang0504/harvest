"""MCP stdio smoke, CLI, and the end-to-end demo against the local test server:
census (stubbed) -> lane -> scraper from the template -> review -> run -> normalise -> store -> export."""

import csv
import json
import os
import subprocess
import sys

import pytest
from localsite import cars_site, start

from harvest_ai import cli, project

REAL_ESTATE_SITE_PAGES = 2


async def test_mcp_stdio_smoke(tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = {"PATH": os.environ.get("PATH", ""), "HARVEST_HOME": str(tmp_path / "h"), "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    params = StdioServerParameters(command=sys.executable, args=["-m", "harvest_ai.mcp_server"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            assert init.server_info.name == "harvest"
            names = [t.name for t in (await s.list_tools()).tools]
            for required in ("harvest_new_project", "harvest_census_add", "harvest_list_sources", "harvest_detect_lane", "harvest_template_scraper",
                             "harvest_review_source", "harvest_enable_source", "harvest_run", "harvest_status", "harvest_query", "harvest_export",
                             "harvest_schedule", "harvest_census_plan", "harvest_census_gaps", "harvest_watchdog"):
                assert required in names
            res = await s.call_tool("harvest_templates", {})
            assert not res.is_error and "vehicles" in res.content[0].text
            res = await s.call_tool("harvest_new_project", {"name": "rent", "target": "rental apartments", "regions": ["PT", "ES"], "record_type": "real_estate_rent"})
            assert not res.is_error and res.structured_content["project"]["region_codes"] == ["PT", "ES"]
            res = await s.call_tool("harvest_status", {"project": "missing"})
            assert res.is_error and "not_found" in res.content[0].text
            res = await s.call_tool("harvest_new_project", {"name": "bad", "target": "x", "regions": ["Atlantis"], "record_type": "vehicles"})
            assert res.is_error and "invalid_input" in res.content[0].text


def _cli(*args) -> tuple[int, object]:
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(list(args))
    out = buf.getvalue()
    try:
        return code, json.loads(out)
    except ValueError:
        return code, out


def test_end_to_end_demo_used_cars(cars, tmp_path):
    """Used cars, KZ + GE, driven through the CLI exactly as an operator or agent would."""
    code, out = _cli("new", "used-cars", "--target", "used cars", "--regions", "KZ,GE", "--record-type", "vehicles", "--rate", "0.01", "--time-budget", "60")
    assert code == 0 and out["project"]["languages"] == ["kk", "ru", "ka"]
    # 1. census (stubbed: what the census agent would add after verifying the site)
    url = cars.url + "/cars"
    cands = tmp_path / "c.json"
    cands.write_text(json.dumps([{"url": url, "name": "Demo Kolesa", "regions": ["KZ"], "angle": "classifieds",
                                  "evidence": [{"url": url, "observation": "page 1 shows 10 used-car ads with prices in KZT"}]}]))
    code, out = _cli("census-add", "used-cars", "@" + str(cands), "--round", "angle:classifieds:KZ")
    sid = out["added"][0]["id"]
    # 2. lane
    code, out = _cli("detect-lane", "used-cars", sid, "--no-mcp")
    assert out["lane"] == "html" and out["robots_status"] == "allowed" and out["terms"]["status"] == "no_clause"
    # 3. scraper from the template, then the agent's edits (select the real cards)
    code, out = _cli("scaffold", "used-cars", sid)
    path = project.load("used-cars").module_path(sid)
    code_text = path.read_text()
    assert out["written"] and 'doc.find_all("article")' in code_text
    code_text = code_text.replace('doc.find_all("article")', 'doc.find_all("article", cls="ad")')
    code_text = code_text.replace('"make": None,', '"make": card.find("h2").text.split(" ")[0],')
    code_text = code_text.replace('"price": None,', '"price": card.find("span", cls="price").text,')
    code_text = code_text.replace('"year": None,', '"year": card.find("span", cls="year").text,')
    code_text = code_text.replace('"mileage": None,', '"mileage": card.find("span", cls="km").text,')
    code_text = code_text.replace('"fuel_type": None,', '"fuel_type": card.find("span", cls="fuel").text,')
    path.write_text(code_text)
    # 4. review gate, then enable
    code, out = _cli("review", "used-cars", sid)
    assert code == 0 and out["verdict"] == "pass", out
    assert out["schema"]["field_coverage"]["mileage"] == 1.0
    code, out = _cli("enable", "used-cars", sid)
    assert code == 0 and out["enabled"]
    # 5. run: walk, normalise, store
    _cli("fx", "used-cars", '{"KZT": 500, "GEL": 2.7}')
    code, out = _cli("run", "used-cars")
    r = out["ran"][0]
    assert r["status"] == "ok" and r["rows_stored"] == 25 and r["complete"]
    code, out = _cli("query", "used-cars", "--filters", '{"fuel_type": "diesel"}', "--order-by", "price", "--asc")
    assert out["total"] == 7 and out["rows"][0]["currency"] == "KZT" and out["rows"][0]["mileage"] > 1000
    assert out["rows"][0]["price_report"] == out["rows"][0]["price"] / 500
    # 6. export
    code, out = _cli("export", "used-cars", "--format", "csv", "--out", str(tmp_path / "cars.csv"))
    rows = list(csv.DictReader(open(out["path"], encoding="utf-8")))
    assert len(rows) == 25 and {"make", "price", "currency", "mileage", "price_report", "source"} <= set(rows[0])
    # 7. autonomy
    code, out = _cli("schedule", "used-cars", "--kind", "cron")
    assert "--due" in out["files"]["cron"]["content"]
    code, out = _cli("run", "used-cars", "--due")
    assert out["ran"] == []
    code, out = _cli("status", "used-cars")
    assert out["records"] == 25 and out["enabled"] == 1
    code, out = _cli("status", "missing")
    assert code == 2


def test_end_to_end_demo_rentals_pt_es(site, tmp_path):
    """Rental apartments, PT + ES: embedded JSON-LD, EUR prices quoted per week and per month, a sale ad quarantined."""
    from localsite import html_page
    ads = [{"id": f"r{i}", "title": f"Apartamento T{i % 3 + 1} arrendamento", "price": f"{700 + i * 10} €" + ("/semana" if i % 5 == 0 else "/mês"),
            "area": f"{50 + i},5 m²", "city": "Lisboa" if i % 2 else "Madrid"} for i in range(1, 13)]
    ads.append({"id": "sale1", "title": "Apartamento T2 para venda", "price": "250 000 €", "area": "70 m²", "city": "Porto"})
    site.route("/robots.txt", (200, {}, "User-agent: *\nAllow: /\n"))
    site.route("/termos", html_page("<p>Condições de utilização do portal.</p>"))

    def listing(req):
        page = int(req.query.get("page", "1"))
        chunk = ads[(page - 1) * 7: page * 7]
        ld = {"@type": "ItemList", "itemListElement": [{"item": {"@type": "Apartment", "identifier": a["id"], "name": a["title"], "url": f"/a/{a['id']}",
                                                                   "price": a["price"], "floorSize": a["area"], "address": a["city"]}} for a in chunk]}
        head = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        return html_page(("<p>" + "arrendamento " * 300 + "</p>") + "<a href='/termos'>Termos e condições</a>", head=head)
    site.route("/arrendar", listing)
    module = '''from harvest_ai import extract

def fetch(page, *, http, ctx):
    html = http.get_text(ctx["source"]["url"] + f"?page={page}")
    if not html:
        return []
    rows = []
    for d in extract.jsonld(html):
        for el in d.get("itemListElement", []):
            it = el["item"]
            rows.append({"source_id": it["identifier"], "url": it["url"], "title": it["name"], "price": it["price"],
                         "area": it["floorSize"], "city": it["address"], "property_type": "apartment"})
    return rows
'''
    code, _ = _cli("new", "rent-iberia", "--target", "rental apartments", "--regions", "PT,ES", "--record-type", "rental_apartments", "--rate", "0.01",
                   "--report-currency", "EUR", "--time-budget", "60")
    url = site.url + "/arrendar"
    code, out = _cli("census-add", "rent-iberia", json.dumps([{"url": url, "regions": ["PT", "ES"], "angle": "vertical_portals",
                                                                 "evidence": [{"url": url, "observation": "7 rental ads per page"}]}]))
    sid = out["added"][0]["id"]
    code, out = _cli("detect-lane", "rent-iberia", sid, "--no-mcp")
    assert out["lane"] == "embedded_json"
    _cli("scaffold", "rent-iberia", sid)
    project.load("rent-iberia").module_path(sid).write_text(module)
    code, out = _cli("review", "rent-iberia", sid)
    assert out["verdict"] == "pass", out
    _cli("enable", "rent-iberia", sid)
    code, out = _cli("run", "rent-iberia")
    r = out["ran"][0]
    assert r["rows_stored"] == 12 and r["rows_quarantined"] == 1 and r["complete"]
    code, out = _cli("query", "rent-iberia", "--filters", '{"source_id": "r5"}')
    row = out["rows"][0]
    assert row["rent_period"] == "week" and row["price_per_month"] == pytest.approx(750 * 52 / 12, rel=1e-3) and row["area"] == 55.5
    code, out = _cli("quarantine", "rent-iberia")
    assert "sale listing in a rent category" in out["quarantine"][0]["reasons"]


def test_cli_console_script_installed():
    for name in ("harvest", "harvest-ai"):  # `harvest-ai` is an alias of the `harvest` command
        exe = os.path.join(os.path.dirname(sys.executable), name)
        if not os.path.exists(exe):
            pytest.skip("console script not installed")
        out = subprocess.run([exe, "templates"], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and "real_estate_rent" in out.stdout, name


def test_distribution_and_import_names():
    from importlib import metadata
    dist = metadata.distribution("harvest-ai")
    assert dist.metadata["Name"] == "harvest-ai" and dist.metadata["License-Expression"] == "Apache-2.0"
    scripts = {e.name: e.value for e in dist.entry_points if e.group == "console_scripts"}
    assert scripts["harvest"] == scripts["harvest-ai"] == "harvest_ai.cli:entry" and scripts["harvest-mcp"] == "harvest_ai.mcp_server:main"


def test_daemon_once(cars, capsys):
    s2 = start(cars_site())
    try:
        from conftest import make_project, ready_source
        p = make_project()
        ready_source(p, s2.url + "/cars")
        cli.daemon(1, once=True)
        line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert line["project"] == "cars" and line["ran"] == 1
    finally:
        s2.server.shutdown()
