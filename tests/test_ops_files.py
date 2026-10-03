"""Regression checks for the deploy files (found in the 2026-09-25 live pilot)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def _service(name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S|\Z)", COMPOSE, re.S | re.M)
    assert m, name
    return m.group(1)


def test_compose_up_without_postgres_needs_no_postgres_password():
    # compose interpolates every service, including profile-gated ones: a `:?` here broke plain `up`
    assert not re.search(r"POSTGRES_PASSWORD:\?", COMPOSE)
    required = re.findall(r"\$\{(\w+):\?", COMPOSE)
    assert required == ["HARVEST_ADMIN_TOKEN"]


def test_worker_has_its_own_healthcheck():
    # the image HEALTHCHECK probes the web port; the worker serves none and was reported unhealthy
    worker = _service("worker")
    assert "healthcheck:" in worker and "8080" not in worker
    assert "HEALTHCHECK" in (ROOT / "Dockerfile").read_text() and "healthcheck:" not in _service("app")


def test_published_port_is_configurable_and_loopback():
    assert '"127.0.0.1:${HARVEST_PORT:-8080}:8080"' in _service("app")


def test_worker_never_dispatches_repairs_by_default():
    assert 'HARVEST_REPAIR_DISPATCH: "off"' in _service("worker")
