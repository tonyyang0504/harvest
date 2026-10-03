import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from localsite import CAR_MODULE, Site, cars_site, start  # noqa: E402

from harvest_ai import project  # noqa: E402

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("HARVEST_HOME", str(h))
    monkeypatch.setenv("HARVEST_ALLOW_PRIVATE", "1")
    monkeypatch.setenv("HARVEST_BACKOFF_S", "0.05")
    monkeypatch.setenv("HARVEST_URL_BUDGET_S", "5")
    monkeypatch.setenv("HARVEST_HTTP_TIMEOUT", "5")
    monkeypatch.setenv("HARVEST_ADMIN_TOKEN", "test-token")
    for k in ("HARVEST_REPAIR_DISPATCH", "HARVEST_ALERT_HOOK", "HARVEST_ALERT_WEBHOOK", "HARVEST_FX_PROVIDER", "HARVEST_FX_FILE", "HARVEST_AGENT_BIN",
              "HARVEST_AGENT_CLI", "HARVEST_PROXY_FILE", "HARVEST_PROXY_URL", "HARVEST_PROXY_MODE", "HARVEST_PROXY_COUNTRY",
              "HARVEST_PROXY_DAILY_BYTES", "HARVEST_PROXY_DAILY_REQUESTS", "HARVEST_PROXY_PROBE_URL", "HARVEST_PROXY_HEALTHCHECK", "HARVEST_STORE_DSN", "HARVEST_MCP_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    pg = os.environ.get("HARVEST_TEST_PG_DSN")
    if pg:  # run the whole suite against Postgres (CI job `postgres`)
        monkeypatch.setenv("HARVEST_STORE_DSN", pg)
        from harvest_ai.db import Store
        st = Store(pg)
        for t in ("sources", "reviews", "runs", "records", "quarantine", "alerts", "jobs", "census_rounds", "census_deferred", "repairs", "proxy_usage", "source_locks"):
            st.execute(f"DELETE FROM {t}")
        st.close()
    project.forget_cache()
    yield h
    project.forget_cache()


@pytest.fixture
def site():
    s = start(Site())
    yield s
    s.server.shutdown()


@pytest.fixture
def cars():
    s = start(cars_site())
    yield s
    s.server.shutdown()


def make_project(name="cars", record_type="vehicles", regions=("KZ", "GE"), **kw):
    from harvest_ai import service
    kw.setdefault("rate_s", 0.01)
    kw.setdefault("time_budget_s", 60)
    service.new_project(name, kw.pop("target", "used cars"), list(regions), record_type, **kw)
    return project.load(name)


def add_source(p, url, regions=("KZ",), angle="classifieds", name=None):
    from harvest_ai import census
    res = census.add(p, [{"url": url, "name": name or url, "regions": list(regions), "angle": angle,
                          "evidence": [{"url": url, "observation": "listing page with items"}]}])
    assert res["added"] or res["merged"], res
    return (res["added"] or res["merged"])[0]["id"]


def ready_source(p, url, module=CAR_MODULE, terms="no_clause"):
    """Register a source, write its module, record lane/robots/terms as detection would, review and enable."""
    from harvest_ai import review
    sid = add_source(p, url)
    p.store.upsert_source(p.name, sid, {"lane": "html", "robots_status": "allowed", "terms_status": terms, "terms_url": url + "/terms", "status": "lane_detected"})
    p.module_path(sid).write_text(module, encoding="utf-8")
    rep = review.review(p, sid, timeout_s=60)
    assert rep["verdict"] == "pass", rep
    assert review.enable(p, sid)["enabled"]
    return sid


@pytest.fixture
def env_ok():
    return os.environ
