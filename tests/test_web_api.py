import csv
import io

import pytest
from localsite import CAR_MODULE

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from harvest_ai import project  # noqa: E402
from harvest_ai.web.app import create_app  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client():
    return TestClient(create_app())


def test_auth_and_static(client):
    assert client.get("/api/health").json()["ok"]
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/me", headers=AUTH).json()["role"] == "admin"
    assert client.get("/api/projects", headers={"X-Harvest-Token": "test-token"}).status_code == 200
    page = client.get("/")
    assert page.status_code == 200 and "/static/app.js" in page.text
    assert client.get("/static/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/static/secret.py").status_code == 404
    csp = page.headers["content-security-policy"]  # scraped text is rendered here: no inline/foreign script, no framing
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and page.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in client.get("/docs").headers  # Swagger UI loads its own CDN assets


def test_generated_token_when_unset(monkeypatch, home):
    monkeypatch.delenv("HARVEST_ADMIN_TOKEN")
    app = create_app()
    tok = (home / "admin_token").read_text().strip()
    assert tok == app.state.admin_token and len(tok) > 20 and oct((home / "admin_token").stat().st_mode)[-3:] == "600"


def test_full_flow_over_the_api(client, cars):
    r = client.post("/api/projects", headers=AUTH, json={"name": "cars", "target": "used cars", "regions": ["KZ", "GE"], "record_type": "vehicles",
                                                          "rate_s": 0.01, "time_budget_s": 60})
    assert r.status_code == 201 and r.json()["project"]["region_codes"] == ["KZ", "GE"]
    assert client.post("/api/projects", headers=AUTH, json={"name": "bad", "target": "x", "regions": ["Mars"], "record_type": "vehicles"}).status_code == 400
    nope = client.get("/api/projects/nope", headers=AUTH)
    assert nope.status_code == 404 and "nope" in nope.json()["message"] and str(project.home()) not in nope.json()["message"]
    assert [x["name"] for x in client.get("/api/projects", headers=AUTH).json()["projects"]] == ["cars"]
    assert client.get("/api/projects/cars/census/plan", headers=AUTH).json()["regions"][1]["currency"] == "GEL"
    url = cars.url + "/cars"
    add = client.post("/api/projects/cars/census/candidates", headers=AUTH, json={"candidates": [
        {"url": url, "name": "Demo cars", "regions": ["KZ"], "angle": "classifieds", "evidence": [{"url": url, "observation": "10 car ads"}]}]}).json()
    sid = add["added"][0]["id"]
    assert client.get("/api/projects/cars/census/gaps", headers=AUTH).json()["sources"] == 1
    job = client.post(f"/api/projects/cars/sources/{sid}/detect?wait=true", headers=AUTH).json()
    assert job["status"] == "done" and job["result"]["lane"] == "html" and job["result"]["terms"]["status"] == "no_clause"
    sc = client.post(f"/api/projects/cars/sources/{sid}/scaffold", headers=AUTH).json()
    assert sc["written"] and "def fetch" in sc["code"]
    project.load("cars").module_path(sid).write_text(CAR_MODULE, encoding="utf-8")  # what the build agent would write
    rv = client.post(f"/api/projects/cars/sources/{sid}/review?wait=true", headers=AUTH).json()
    assert rv["status"] == "done" and rv["result"]["verdict"] == "pass"
    ap = client.post(f"/api/projects/cars/sources/{sid}/approve", headers=AUTH)
    assert ap.status_code == 200 and ap.json()["enabled"]
    tune = client.patch(f"/api/projects/cars/sources/{sid}", headers=AUTH, json={"max_pages": 10, "cadence_hours": "12h"}).json()
    assert tune["source"]["max_pages"] == 10
    run = client.post("/api/projects/cars/run", headers=AUTH, json={"wait": True}).json()
    assert run["status"] == "done" and run["result"]["ran"][0]["rows_stored"] == 25
    st = client.get("/api/projects/cars", headers=AUTH).json()
    assert st["records"] == 25 and st["enabled"] == 1 and st["by_lane"] == {"html": 1}
    recs = client.get("/api/projects/cars/records", headers=AUTH, params={"filters": '{"make": "Toyota"}', "limit": 3}).json()
    assert recs["total"] == 6 and len(recs["rows"]) == 3
    assert client.get("/api/projects/cars/records", headers=AUTH, params={"filters": "{bad"}).status_code == 400
    exp = client.get("/api/projects/cars/export", headers=AUTH, params={"format": "csv"})
    assert exp.status_code == 200 and len(list(csv.DictReader(io.StringIO(exp.text)))) == 25
    assert client.get("/api/projects/cars/runs", headers=AUTH).json()["runs"][0]["status"] == "ok"
    assert client.get("/api/projects/cars/quarantine", headers=AUTH).json()["quarantine"] == []
    assert len(client.get("/api/projects/cars/jobs", headers=AUTH).json()["jobs"]) == 3
    sched = client.post("/api/projects/cars/schedule", headers=AUTH, json={"kind": "cron"}).json()
    assert "--due" in sched["files"]["cron"]["content"]
    assert client.post("/api/projects/cars/schedule", headers=AUTH, json={"kind": "crontab"}).status_code == 400  # not a silent no-op
    assert client.post("/api/projects/cars/watchdog", headers=AUTH).json()["findings"] == []
    assert client.post("/api/projects/cars/fx", headers=AUTH, json={"rates": {"KZT": 500}}).json()["rates"]["KZT"] == 500
    assert client.post(f"/api/projects/cars/sources/{sid}/disable", headers=AUTH).json()["enabled"] is False
    rej = client.post(f"/api/projects/cars/sources/{sid}/reject", headers=AUTH, json={"reason": "duplicate"}).json()
    assert rej["status"] == "rejected"
    refused = client.post(f"/api/projects/cars/sources/{sid}/approve", headers=AUTH)
    assert refused.status_code in (200, 409)
    assert client.post("/api/projects/cars/census/run", headers=AUTH, json={"kind": "delete-everything"}).status_code == 400
