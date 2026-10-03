"""Real-browser end-to-end tests of the web app (`harvest web`), driven like a user with Playwright.

One `harvest web --inline-jobs` server runs per module on a free port with a temporary HARVEST_HOME; fixture
websites (tests/uisite.py) stand in for the internet and a stub agent CLI (tests/stub_agent.py) for the agents.
The tests run in file order as one user journey (sign in -> project -> census -> lanes -> build/review/approve ->
proxy/UA decisions -> crawl -> quarantine -> data -> export -> schedule -> watchdog -> census resume), then the
cross-cutting checks (reload mid-job, two tabs, keyboard only, 390 px viewport, XSS, error states).

Every page fails its test on any console error, uncaught exception or unexpected HTTP error response.
Skipped unless Playwright and Chromium are installed (`pip install -e '.[dev,browser]' && playwright install chromium`).
Set HUIE2E_SHOTS=<dir> to save screenshots of key screens.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")
pytest.importorskip("fastapi")
from playwright.sync_api import expect  # noqa: E402
from uisite import ARABIC, CJK, LONG, LONG_TITLE, XSS_TITLE, candidate, cars_board, challenge_wall, denied, spare, url  # noqa: E402

TOKEN = "huie2e-admin-token-0123456789"
PROJECT = "cars-e2e"
HERE = Path(__file__).parent
SHOTS = os.environ.get("HUIE2E_SHOTS")


def _browser_ok() -> bool:
    try:
        with pw.sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


def _localhost_subdomains_resolve() -> bool:
    try:
        return socket.getaddrinfo("cars.localhost", 80)[0][4][0] in ("127.0.0.1", "::1")
    except OSError:
        return False


_missing = [why for ok, why in ((_browser_ok(), "no Playwright browser installed"),
                                (_localhost_subdomains_resolve(), "*.localhost does not resolve to loopback here")) if not ok]
if _missing and os.environ.get("HUIE2E_REQUIRE") == "1":  # CI: a suite that silently skips is not a passing suite
    raise RuntimeError("UI e2e prerequisites missing: " + "; ".join(_missing))
pytestmark = pytest.mark.skipif(bool(_missing), reason="; ".join(_missing))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    """The server under test, the fixture sites and the shared journey state."""

    def __init__(self, tmp: Path):
        self.home = tmp / "home"
        self.cars, self.wall, self.denied = cars_board(), challenge_wall(), denied()
        self.spares = [spare() for _ in range(3)]
        self.u = {"cars": url(self.cars, "cars", "/cars"), "wall": url(self.wall, "wall", "/ads"), "denied": url(self.denied, "denied", "/list"),
                  **{f"spare{i}": url(s, f"spare{i}", "/ads") for i, s in enumerate(self.spares, 1)}}
        self.census_file = tmp / "census.json"
        # what the stub census agent records: round 1 fills four of the five budget slots; round 2 (after resume) adds nothing new
        self.census_file.write_text(json.dumps([
            [candidate(self.u["cars"], "Cars board", ["KZ"]), candidate(self.u["wall"], "Challenge wall", ["AE"]),
             candidate(self.u["denied"], "Denied 403", ["JP"], "marketplaces"), candidate(self.u["spare1"], "Spare one", ["KZ"])],
            []]), encoding="utf-8")
        agent = tmp / "agent.sh"
        agent.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "stub_agent.py"}" "$@"\n', encoding="utf-8")
        agent.chmod(0o755)
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("HARVEST_")}
        env.update(HARVEST_HOME=str(self.home), HARVEST_ADMIN_TOKEN=TOKEN, HARVEST_ALLOW_PRIVATE="1", HARVEST_AGENT_BIN=str(agent),
                   HUIE2E_CENSUS_FILE=str(self.census_file), HARVEST_BACKOFF_S="0.05", HARVEST_URL_BUDGET_S="5", HARVEST_HTTP_TIMEOUT="5",
                   HARVEST_REVIEW_TIMEOUT_S="60", PYTHONPATH=os.pathsep.join([str(HERE.parent / "src"), str(HERE)]))
        self.log_path = tmp / "server.log"
        self.log = self.log_path.open("w")
        boot = ("import faulthandler, signal, sys; faulthandler.register(signal.SIGUSR1, all_threads=True); "
                "from harvest_ai.cli import entry; sys.argv = ['harvest'] + sys.argv[1:]; entry()")
        self.proc = subprocess.Popen([sys.executable, "-c", boot, "web", "--inline-jobs", "--port", str(self.port)],
                                     env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.base + "/api/health", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("harvest web did not start: " + (tmp / "server.log").read_text())
        self.sids: dict[str, str] = {}

    def api(self, method: str, path: str, body: dict | None = None, token: str | None = TOKEN) -> tuple[int, dict | str]:
        req = urllib.request.Request(self.base + "/api" + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw, status, ctype = r.read(), r.status, r.headers.get("content-type", "")
        except urllib.error.HTTPError as e:
            raw, status, ctype = e.read(), e.code, e.headers.get("content-type", "")
        return status, json.loads(raw) if "json" in ctype else raw.decode("utf-8", "replace")

    def log_tail(self, n: int = 120) -> str:
        self.log.flush()
        return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-n:])

    def dump_threads(self):
        """Ask the server for every thread's stack (faulthandler on SIGUSR1) to diagnose a hang."""
        if hasattr(signal, "SIGUSR1") and self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGUSR1)
            time.sleep(1)

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        for s in (self.cars, self.wall, self.denied, *self.spares):
            s.server.shutdown()


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("huie2e"))
    yield s
    s.close()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


class Watched:
    """A page whose console errors, uncaught exceptions and HTTP error responses are recorded. Error responses
    must be declared with `allow(status)`; everything else fails the test."""

    def __init__(self, page, base: str):
        self.page, self.base = page, base
        self.errors: list[str] = []
        self.allowed: set[int] = set()
        self.allowed_console: list[str] = []  # console errors a test causes on purpose (a simulated network failure)
        self.dialogs: list[str | None] = []  # answers for prompt()/confirm(), in order; None dismisses
        self.seen_dialogs: list[str] = []
        page.on("console", self._console)
        page.on("pageerror", lambda exc: self.errors.append(f"pageerror: {exc}"))
        page.on("response", self._response)
        page.on("dialog", self._dialog)

    def _console(self, msg):
        if msg.type != "error":
            return
        m = re.search(r"status of (\d{3})", msg.text)
        if m and int(m.group(1)) in self.allowed:
            return
        if any(re.search(p, msg.text) for p in self.allowed_console):
            return
        self.errors.append(f"console: {msg.text} @ {msg.location.get('url')}")

    def _response(self, r):
        if r.status >= 400 and r.status not in self.allowed:
            self.errors.append(f"http {r.status}: {r.request.method} {r.url}")

    def _dialog(self, d):
        self.seen_dialogs.append(d.message)
        ans = self.dialogs.pop(0) if self.dialogs else None
        if ans is None:
            d.dismiss()
        else:
            d.accept(ans)

    def allow(self, *statuses: int):
        self.allowed.update(statuses)

    def goto(self, hash_: str = ""):
        self.page.goto(self.base + "/" + hash_)

    def shot(self, name: str, full: bool = True):
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=full)

    def toast(self, pattern: str):
        expect(self.page.locator("#toast")).to_contain_text(re.compile(pattern, re.I), timeout=10000)

    def no_horizontal_scroll(self):
        over = self.page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert over <= 1, f"page scrolls horizontally by {over}px"


@pytest.fixture(scope="module")
def ctx(browser):
    c = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
    yield c
    c.close()


@pytest.fixture
def ui(ctx, stack):
    page = ctx.new_page()
    w = Watched(page, stack.base)
    yield w
    try:
        assert not w.errors, "browser errors:\n" + "\n".join(w.errors)
        assert page.evaluate("window.__xss") is None, "an XSS payload executed"
    finally:
        page.close()


def signed_in(ui: Watched, hash_: str = ""):
    ui.goto(hash_)
    if ui.page.locator("#login").count():
        ui.page.fill("#tok", TOKEN)
        ui.page.press("#tok", "Enter")
        expect(ui.page.locator("#logout")).to_be_visible()
        if hash_:
            ui.goto(hash_)


def tab(ui: Watched, name: str):
    ui.page.locator(".tabs a", has_text=re.compile(f"^{name}$")).click()
    expect(ui.page.locator(".tabs a.on")).to_have_text(name)


def source_row(ui: Watched, name: str):
    return ui.page.locator("tbody tr", has=ui.page.locator("a", has_text=name)).first


def assert_source_actions_reachable(ui: "Watched"):
    """Every action button of every sources row is fully on screen (after a vertical scroll at most), not clipped
    by the table's scroll box, and shows its whole label."""
    vw = ui.page.viewport_size["width"]
    rows = ui.page.locator("tbody tr[data-source]")
    assert rows.count(), "no sources rows"
    for i in range(rows.count()):
        for b in rows.nth(i).locator("td.act button").all():
            b.scroll_into_view_if_needed()
            box = b.bounding_box()
            wrap = b.evaluate("e => { const r = e.closest('.tablewrap').getBoundingClientRect(); return {l: r.left, r: r.right}; }")
            label = b.inner_text()
            assert box and box["x"] >= 0 and box["x"] + box["width"] <= vw + 0.5, f"{label!r} at {box} outside 0..{vw}"
            assert box["x"] >= wrap["l"] - 0.5 and box["x"] + box["width"] <= wrap["r"] + 0.5, f"{label!r} clipped by the table box {wrap}"
            assert b.evaluate("e => e.scrollWidth <= e.clientWidth + 1"), f"{label!r} label truncated"
            assert ui.page.evaluate("([x, y]) => document.elementFromPoint(x, y)?.closest('button')?.textContent",
                                    [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2]) == label, f"{label!r} is covered"


def wait_job_done(stack: Stack, kind: str, n: int = 1, timeout: float = 180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = [j for j in stack.api("GET", f"/projects/{PROJECT}/jobs")[1]["jobs"] if j["kind"] == kind]
        if len(jobs) >= n and all(j["status"] not in ("queued", "running") for j in jobs):
            return jobs
        time.sleep(0.5)
    stack.dump_threads()
    raise AssertionError(f"{kind} jobs not finished: {jobs}\n--- server log tail ---\n{stack.log_tail()}")


# ======================================================================== the journey
def test_01_sign_in_refuses_missing_and_wrong_tokens(ui, stack):
    assert stack.api("GET", "/projects", token=None)[0] == 401
    assert stack.api("GET", "/projects", token="wrong")[0] == 401
    ui.goto()
    expect(ui.page.get_by_role("heading", name="Sign in")).to_be_visible()
    expect(ui.page.locator("#logout")).to_be_hidden()
    ui.shot("01-sign-in")
    # missing: the form will not submit an empty token
    ui.page.get_by_role("button", name="Sign in").click()
    assert ui.page.evaluate("document.querySelector('#tok').validity.valueMissing")
    # wrong: refused with a clear message, nothing is kept
    ui.allow(401)
    ui.page.fill("#tok", "not-the-token")
    ui.page.get_by_role("button", name="Sign in").click()
    ui.toast("wrong|invalid|refused")
    expect(ui.page.get_by_role("heading", name="Sign in")).to_be_visible()
    assert ui.page.evaluate("localStorage.getItem('harvest_token')") is None
    # right
    ui.page.fill("#tok", TOKEN)
    ui.page.get_by_role("button", name="Sign in").click()
    expect(ui.page.get_by_role("heading", name="Projects")).to_be_visible()
    expect(ui.page.locator("#logout")).to_be_visible()


def test_02_empty_home_and_create_errors(ui, stack):
    signed_in(ui)
    expect(ui.page.get_by_role("heading", name="Projects")).to_be_visible()
    expect(ui.page.locator("main")).to_contain_text(re.compile("no projects yet", re.I))
    f = ui.page.locator("#newp")
    f.locator("[name=name]").fill("bad-regions")
    f.locator("[name=target]").fill("used cars")
    f.locator("[name=regions]").fill("Mars, Atlantis")
    ui.allow(400)
    f.get_by_role("button", name="Create").click()
    ui.toast("region")
    expect(ui.page.get_by_role("heading", name="Projects")).to_be_visible()
    assert stack.api("GET", "/projects")[1]["projects"] == []
    # the browser refuses a bad name before anything is sent
    f.locator("[name=name]").fill("Bad Name!")
    f.locator("[name=regions]").fill("KZ")
    f.get_by_role("button", name="Create").click()
    assert ui.page.evaluate("document.querySelector('#newp [name=name]').validity.patternMismatch")


def test_03_create_project_with_regions_type_and_fields(ui, stack):
    signed_in(ui)
    f = ui.page.locator("#newp")
    f.locator("[name=name]").fill(PROJECT)
    f.locator("[name=target]").fill("used cars")
    f.locator("[name=regions]").fill("KZ, United Arab Emirates; JP")
    f.locator("[name=record_type]").select_option("vehicles")
    f.locator("[name=fields]").fill("warranty, seller_phone")
    f.locator("[name=max_sources]").fill("5")
    f.locator("[name=cadence]").select_option("12h")
    ui.shot("03-new-project")
    f.get_by_role("button", name="Create").click()
    expect(ui.page).to_have_url(re.compile(f"#/p/{PROJECT}"))
    expect(ui.page.get_by_role("heading", name="used cars")).to_be_visible()
    head = ui.page.locator("main > p.muted").first
    expect(head).to_contain_text("vehicles")
    expect(head).to_contain_text("KZ, AE, JP")
    expect(head).to_contain_text("12h")
    spec = stack.api("GET", f"/projects/{PROJECT}")[1]["project"]
    assert spec["region_codes"] == ["KZ", "AE", "JP"] and spec["max_sources"] == 5 and spec["cadence"] == "12h"
    assert {"warranty", "seller_phone"} <= set(spec["fields"] if isinstance(spec["fields"], list) else spec["fields"].keys())
    # duplicate name is refused, the existing project untouched
    ui.goto()
    ui.allow(400)
    f = ui.page.locator("#newp")
    f.locator("[name=name]").fill(PROJECT)
    f.locator("[name=target]").fill("other")
    f.locator("[name=regions]").fill("PT")
    f.get_by_role("button", name="Create").click()
    ui.toast("already exists")
    assert stack.api("GET", f"/projects/{PROJECT}")[1]["project"]["target"] == "used cars"
    expect(ui.page.locator("tbody tr")).to_have_count(1)


def test_04_empty_project_states(ui, stack):
    signed_in(ui, f"#/p/{PROJECT}")
    expect(ui.page.locator(".stat").first).to_contain_text("0")
    for t in ("sources", "data", "runs", "quarantine", "jobs"):
        tab(ui, t)
        expect(ui.page.locator("main")).to_contain_text(re.compile(r"Nothing|No ", re.I))
    ui.shot("04-empty-project")


def test_05_census_plan(ui, stack):
    signed_in(ui, f"#/p/{PROJECT}")
    tab(ui, "census")
    main = ui.page.locator("main")
    for code, cur in (("KZ", "KZT"), ("AE", "AED"), ("JP", "JPY")):
        expect(main).to_contain_text(code)
        expect(main).to_contain_text(cur)
    expect(main).to_contain_text("classifieds")
    expect(main).to_contain_text("ar, en")  # the regions' languages drive the local-language queries
    expect(main).to_contain_text("<translate")  # and the plan asks the agent to translate them
    ui.shot("05-census-plan")


def test_06_census_agent_and_api_candidates_fill_the_budget(ui, stack):
    signed_in(ui, f"#/p/{PROJECT}")
    ui.page.get_by_role("button", name="Run census agent").click()
    ui.toast("census started")
    wait_job_done(stack, "agent:census")
    # agents stand-in through the API: one more source (with a hostile name), one past the budget, one without evidence
    xss_name = "<img src=x onerror=\"window.__xss=1\">Spare two"
    st, res = stack.api("POST", f"/projects/{PROJECT}/census/candidates", {"candidates": [
        candidate(stack.u["spare2"], xss_name, ["AE"]), candidate(stack.u["spare3"], "Spare three", ["JP"]),
        {"url": "https://no-evidence.example/", "name": "No evidence", "regions": ["KZ"], "angle": "classifieds", "evidence": []}]})
    assert st == 200 and len(res["added"]) == 1, res
    assert [r.get("deferred") for r in res["rejected"]] == [True, None], res
    tab(ui, "sources")
    rows = ui.page.locator("tbody tr")
    expect(rows).to_have_count(5)
    expect(ui.page.locator("main")).to_contain_text(xss_name)  # shown as text, not markup
    for r in stack.api("GET", f"/projects/{PROJECT}/sources")[1]["sources"]:
        stack.sids[r["name"]] = r["id"]
    tab(ui, "jobs")
    expect(ui.page.locator("tbody tr", has_text="agent:census")).to_contain_text("done")
    tab(ui, "overview")
    expect(ui.page.locator(".budget")).to_contain_text("Census budget reached")
    tab(ui, "census")
    expect(ui.page.locator("main")).to_contain_text("Deferred by the budget")
    expect(ui.page.locator("main")).to_contain_text("spare3.localhost")


def test_07_lane_detection(ui, stack):
    signed_in(ui, f"#/p/{PROJECT}")
    ui.page.get_by_role("button", name="Detect lanes").click()
    ui.toast("detect started")
    wait_job_done(stack, "detect_lanes", timeout=240)
    tab(ui, "sources")
    cars, wall, den = source_row(ui, "Cars board"), source_row(ui, "Challenge wall"), source_row(ui, "Denied 403")
    expect(cars).to_contain_text("html")
    expect(cars).to_contain_text("no_clause")
    expect(wall.locator("td.lane")).to_contain_text("none")
    expect(den.locator("td.lane")).to_contain_text("none")
    # nothing can be approved before a review; a lane-none source cannot even be built or scaffolded
    expect(cars.get_by_role("button", name="Approve")).to_be_disabled()
    expect(cars.get_by_role("button", name="Review")).to_be_disabled()
    for r in (wall, den):
        expect(r.get_by_role("button", name="Build (agent)")).to_be_disabled()
        expect(r.get_by_role("button", name="Scaffold")).to_be_disabled()
        expect(r.get_by_role("button", name="Approve")).to_be_disabled()
    ui.shot("07-sources-after-detection")


def test_08_scaffold_review_build_approve_reject(ui, stack):
    signed_in(ui, f"#/p/{PROJECT}/sources")
    cars = source_row(ui, "Cars board")
    cars.get_by_role("button", name="Scaffold").click()
    ui.toast("scaffold written")
    expect(cars.get_by_role("button", name="Scaffold")).to_be_disabled()
    expect(cars.get_by_role("button", name="Review")).to_be_enabled()
    # the generic template is a start, not a scraper: its review does not pass and approval stays closed
    cars.get_by_role("button", name="Review").click()
    ui.toast("review")
    wait_job_done(stack, "review")
    ui.page.reload()
    cars = source_row(ui, "Cars board")
    expect(cars.locator(".review")).not_to_contain_text("pass")
    expect(cars.get_by_role("button", name="Approve")).to_be_disabled()
    # the API refuses too, with reasons (and the UI shows them)
    st, body = stack.api("POST", f"/projects/{PROJECT}/sources/{stack.sids['Cars board']}/approve")
    assert st == 409 and body["refused"], body
    # the build agent writes the module; the review passes; approve
    cars.get_by_role("button", name="Build (agent)").click()
    ui.toast("build")
    wait_job_done(stack, "agent:build")
    cars.get_by_role("button", name="Review").click()
    wait_job_done(stack, "review", 2)
    ui.page.reload()
    cars = source_row(ui, "Cars board")
    expect(cars.locator(".review")).to_contain_text("pass")
    cars.get_by_role("button", name="Approve").click()
    ui.toast("approve")
    expect(cars.get_by_role("button", name="Disable")).to_be_visible()
    expect(cars.locator("td.status")).to_contain_text("enabled")
    # reject: cancelling the prompt changes nothing; a reason rejects
    spare2 = source_row(ui, "Spare two")
    ui.dialogs = [None]
    spare2.get_by_role("button", name="Reject").click()
    expect(spare2.locator("td.status")).not_to_contain_text("rejected")
    ui.dialogs = ["   "]
    spare2.get_by_role("button", name="Reject").click()
    expect(spare2.locator("td.status")).not_to_contain_text("rejected")
    ui.dialogs = ["not a used-car site <b>really</b>"]
    spare2.get_by_role("button", name="Reject").click()
    ui.toast("reject")
    expect(spare2.locator("td.status")).to_contain_text("rejected")
    expect(spare2.get_by_role("button", name="Reject")).to_be_disabled()
    ui.shot("08-sources-approved")


def test_09_proxy_and_ua_decisions_and_gates(ui, stack):
    signed_in(ui, f"#/p/{PROJECT}")
    panel = ui.page.locator("#proxy-panel")
    # dismissed or blank: nothing is recorded
    ui.dialogs = [None]
    panel.get_by_role("button", name="Switch on…").click()
    ui.dialogs = [""]
    panel.get_by_role("button", name="Switch on…").click()
    assert stack.api("GET", f"/projects/{PROJECT}/proxy")[1]["project"]["decision"].get("enabled") in (None, False)
    # too short: the API refuses and the UI says why
    ui.allow(400)
    ui.dialogs = ["ok"]
    panel.get_by_role("button", name="Switch on…").click()
    ui.toast("needs a reason")
    # a real reason is recorded with the date and who decided; markup in it stays text
    ui.dialogs = ["Denied 403 is an IP-level block <i>from the DC</i>"]
    panel.get_by_role("button", name="Switch on…").click()
    ui.toast("proxy on")
    expect(panel).to_contain_text("enabled")
    expect(panel.locator(".decision")).to_contain_text("Denied 403 is an IP-level block <i>from the DC</i>")
    expect(panel.locator(".decision")).to_contain_text("web:admin")
    d = stack.api("GET", f"/projects/{PROJECT}/proxy")[1]["project"]["decision"]
    assert d["enabled"] and d["by"] == "web:admin"
    ui.dialogs = [None]  # cancelling the prompt keeps it on
    panel.get_by_role("button", name="Switch off").click()
    expect(panel.get_by_role("button", name="Switch off")).to_be_visible()
    ui.dialogs = [""]  # switching off needs no reason
    panel.get_by_role("button", name="Switch off").click()
    ui.toast("proxy off")
    expect(panel).to_contain_text("off")
    # per source: the challenge wall can be switched on, but the gate says it will not be used
    tab(ui, "sources")
    wall = source_row(ui, "Challenge wall")
    ui.dialogs = ["operator wants to try the browser UA here"]
    wall.get_by_role("button", name="Browser UA…").click()
    ui.toast("browser UA on")
    expect(wall.locator(".gate")).to_contain_text("not used")
    ui.dialogs = [""]
    wall.get_by_role("button", name="Own UA").click()
    ui.toast("browser UA off")
    expect(wall.get_by_role("button", name="Browser UA…")).to_be_visible()
    den = source_row(ui, "Denied 403")
    ui.dialogs = ["plain 403 from the datacenter range"]
    den.get_by_role("button", name="On…").click()
    ui.toast("proxy on")
    expect(den.locator(".gate")).to_contain_text("not used")  # no pool configured here, and the terms were never read
    assert_source_actions_reachable(ui)
    ui.shot("09-sources-gates")
    ui.dialogs = ["no longer needed"]
    den.get_by_role("button", name="Off").click()
    ui.toast("proxy off")


def test_10_crawl_with_reload_mid_job_and_a_second_tab(ui, stack, ctx):
    signed_in(ui, f"#/p/{PROJECT}")
    other = Watched(ctx.new_page(), stack.base)  # a second tab, already signed in (shared storage), watching the runs
    other.goto(f"#/p/{PROJECT}/runs")
    expect(other.page.locator("main")).to_contain_text("No runs yet")
    ui.page.get_by_role("button", name="Run collection now").click()
    ui.toast("run started")
    expect(ui.page.locator("main")).to_contain_text(re.compile(r"Working: run \((running|queued)\)"), timeout=10000)
    ui.page.reload()  # mid-job: still signed in, the job still visible and progressing
    expect(ui.page.locator("#logout")).to_be_visible()
    expect(ui.page.get_by_role("heading", name="used cars")).to_be_visible()
    expect(ui.page.locator("main")).to_contain_text(re.compile(r"Working: run|records stored"))
    # the other tab picks the run up by itself, then its final state
    expect(other.page.locator("tbody tr").first).to_contain_text(re.compile("running|partial|ok"), timeout=20000)
    jobs = wait_job_done(stack, "run")
    assert jobs[0]["status"] == "done", jobs
    expect(other.page.locator("tbody tr").first).to_contain_text("partial", timeout=15000)
    first = other.page.locator("tbody tr").first.locator("td")
    expect(first.nth(4)).to_have_text("3")      # pages walked
    expect(first.nth(5)).to_have_text("13")     # stored: pages 1-2 minus three quarantined rows
    expect(first.nth(6)).to_have_text("3")      # quarantined
    expect(first.nth(7)).to_have_text("no")     # page 3 broke: the walk is incomplete
    expect(first.nth(0)).to_have_text(re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d UTC$"))  # readable time, not raw ISO
    expect(ui.page.locator(".stat").nth(2)).to_contain_text("13", timeout=10000)
    other.shot("10-runs-partial")
    # the site is repaired; the next run completes and the second tab sees it without a reload
    stack.cars.broken = False
    ui.page.get_by_role("button", name="Run collection now").click()
    wait_job_done(stack, "run", 2)
    expect(other.page.locator("tbody tr").first).to_contain_text("ok", timeout=15000)
    expect(other.page.locator("tbody tr").first.locator("td").nth(7)).to_have_text("yes")
    expect(ui.page.locator(".stat").nth(2)).to_contain_text("21", timeout=10000)
    assert not other.errors, other.errors
    other.page.close()


def test_11_quarantine(ui, stack):
    signed_in(ui, f"#/p/{PROJECT}/quarantine")
    rows = ui.page.locator("tbody tr")
    expect(rows).to_have_count(3)
    text = ui.page.locator("tbody").inner_text()
    assert "missing required field price" in text and "year=1066" in text and "missing required field url" in text, text
    assert "javascript:window.__xss=1" in text  # the hostile raw row is shown as text
    assert ui.page.locator("tbody a, tbody img, tbody script").count() == 0
    ui.shot("11-quarantine")


def test_12_browse_filter_records_with_hostile_and_non_ascii_data(ui, stack):
    signed_in(ui, f"#/p/{PROJECT}/data")
    expect(ui.page.locator("#qcount")).to_have_text("21 records")
    body = ui.page.locator("tbody")
    expect(body).to_contain_text(ARABIC)
    expect(body).to_contain_text(CJK)
    expect(body).to_contain_text(XSS_TITLE)  # the stored markup is shown as text
    heads = ui.page.locator("thead th").all_inner_texts()
    assert heads[0] == "title" and heads[-1] == "url" and "price" in heads and "year" in heads, heads
    years = body.locator(f"tr td:nth-child({heads.index('year') + 1})").all_inner_texts()
    assert all(re.fullmatch(r"(19|20)\d\d", y) for y in years), years  # a year is not "2,015"
    # numbers and words are not broken mid-way: a year cell renders on one line
    heights = body.locator(f"tr td:nth-child({heads.index('year') + 1})").evaluate_all("els => els.map(e => e.getClientRects()[0].height)")
    line = ui.page.evaluate("parseFloat(getComputedStyle(document.querySelector('tbody td')).lineHeight) || 20")
    assert all(h < 2.5 * line for h in heights[:3]), (heights[:3], line)
    assert body.locator("img, script").count() == 0
    hrefs = body.locator("a").evaluate_all("els => els.map(e => e.getAttribute('href'))")
    assert hrefs and all(h.startswith("http") for h in hrefs), hrefs
    # the very long title is clipped in its cell (full text in the tooltip) and the page does not scroll sideways
    long_cell = body.locator("td span[title]", has_text="Lada Vesta ЖЖЖ").first
    assert len(long_cell.inner_text()) < 200 and long_cell.get_attribute("title").startswith(LONG_TITLE[:100])
    ui.no_horizontal_scroll()
    ui.shot("12-data")
    # filter: a JSON filter, an Arabic contains-filter, and errors
    q = ui.page.locator("#qfilters")
    q.fill('{"make": "Toyota"}')
    q.press("Enter")
    expect(ui.page.locator("#qcount")).not_to_have_text("21 records")
    n_toyota = int(ui.page.locator("#qcount").inner_text().split()[0])
    assert 0 < n_toyota < 21
    q.fill('{"title": {"contains": "تويوتا"}}')
    ui.page.get_by_role("button", name="Query").click()
    expect(ui.page.locator("#qcount")).to_have_text("1 records")
    expect(body).to_contain_text(ARABIC)
    q.fill("{not json")
    ui.page.get_by_role("button", name="Query").click()
    ui.toast("filters:")
    expect(ui.page.locator("#qcount")).to_have_text("1 records")  # nothing was sent
    ui.allow(400)
    q.fill('{"no_such_field": {"gte": 1}}')
    ui.page.get_by_role("button", name="Query").click()
    expect(ui.page.locator("[role=alert]")).to_be_visible()
    expect(ui.page.locator("main")).to_contain_text("No records: the query failed.")
    q.fill('{"title": {"contains": "nothing-matches-this"}}')
    ui.page.get_by_role("button", name="Query").click()
    expect(ui.page.locator("main")).to_contain_text("No records match these filters.")
    expect(ui.page.get_by_role("button", name="Export CSV")).to_be_disabled()
    q.fill("")
    ui.page.get_by_role("button", name="Query").click()
    expect(ui.page.locator("#qcount")).to_have_text("21 records")
    stack.n_toyota = n_toyota


def _download(ui: Watched, button: str) -> bytes:
    with ui.page.expect_download() as d:
        ui.page.get_by_role("button", name=button).click()
    return Path(d.value.path()).read_bytes()


def test_13_export_csv_jsonl_parquet(ui, stack, tmp_path):
    signed_in(ui, f"#/p/{PROJECT}/data")
    expect(ui.page.locator("#qcount")).to_have_text("21 records")
    rows = list(csv.DictReader(io.StringIO(_download(ui, "Export CSV").decode("utf-8"))))
    assert len(rows) == 21
    titles = {r["title"] for r in rows}
    assert {ARABIC, CJK, XSS_TITLE} <= titles  # exact and unescaped
    assert LONG_TITLE[:1000] in titles  # string fields are bounded at 1,000 characters by design ...
    assert LONG in {r["description"] for r in rows}  # ... text fields keep the whole 10k+ characters
    lines = _download(ui, "JSONL").decode("utf-8").splitlines()
    recs = [json.loads(x) for x in lines if x.strip()]
    assert len(recs) == 21 and {ARABIC, CJK} <= {r["title"] for r in recs}
    assert all(r["currency"] == "KZT" and isinstance(r["price"], (int, float)) for r in recs)
    import pyarrow.parquet as pq
    pqf = tmp_path / "x.parquet"
    pqf.write_bytes(_download(ui, "Parquet"))
    table = pq.read_table(pqf)
    assert table.num_rows == 21 and ARABIC in table.column("title").to_pylist()
    # a filtered export holds only the filtered rows
    ui.page.locator("#qfilters").fill('{"make": "Toyota"}')
    ui.page.locator("#qfilters").press("Enter")
    expect(ui.page.locator("#qcount")).to_have_text(f"{stack.n_toyota} records")
    rows = list(csv.DictReader(io.StringIO(_download(ui, "Export CSV").decode("utf-8"))))
    assert len(rows) == stack.n_toyota and {r["make"] for r in rows} == {"Toyota"}
    # bad requests come back as clear errors, not 500s
    assert stack.api("GET", f"/projects/{PROJECT}/export?format=xlsx")[0] == 400
    assert stack.api("GET", f"/projects/{PROJECT}/export?format=csv&filters=%7Bbad")[0] == 400


def test_14_schedule(ui, stack):
    signed_in(ui, f"#/p/{PROJECT}")
    ui.page.get_by_role("button", name="Write schedule").click()
    ui.toast("schedule snippets written")
    panel = ui.page.locator("#schedule-result")
    expect(panel).to_contain_text("harvest.cron")
    expect(panel).to_contain_text(f"run {PROJECT} --due")
    expect(panel).to_contain_text(stack.sids["Cars board"])  # its next due time
    deploy = stack.home / "projects" / PROJECT / "deploy"
    assert (deploy / "harvest.cron").is_file() and (deploy / f"harvest-{PROJECT}.timer").is_file()
    ui.page.wait_for_timeout(4500)  # survives the background refresh
    expect(panel).to_be_visible()
    ui.shot("14-schedule")
    assert stack.api("POST", f"/projects/{PROJECT}/schedule", {"kind": "crontab"})[0] == 400


def test_15_watchdog(ui, stack):
    signed_in(ui, f"#/p/{PROJECT}")
    ui.page.get_by_role("button", name="Watchdog").click()
    ui.toast("watchdog: 0 finding")
    expect(ui.page.locator("#watchdog-result")).to_contain_text("All sources healthy")
    # someone edits the reviewed module: the watchdog flags it, collection pauses until a new review passes
    sid = stack.sids["Cars board"]
    mod = stack.home / "projects" / PROJECT / "sources" / f"{sid}.py"
    mod.write_text(mod.read_text(encoding="utf-8") + "\n# edited after review\n", encoding="utf-8")
    ui.page.get_by_role("button", name="Watchdog").click()
    ui.toast("watchdog: 1 finding")
    res = ui.page.locator("#watchdog-result")
    expect(res).to_contain_text("review_stale")
    expect(res).to_contain_text(sid)
    expect(ui.page.locator("main")).to_contain_text("module changed after")  # also in the alerts table
    ui.shot("15-watchdog")
    tab(ui, "sources")
    cars = source_row(ui, "Cars board")
    cars.get_by_role("button", name="Review").click()
    wait_job_done(stack, "review", 3)
    ui.page.reload()
    cars = source_row(ui, "Cars board")
    expect(cars.locator(".review")).to_contain_text("pass")
    expect(cars.locator("td.status")).not_to_contain_text("review_stale")


def test_16_census_resume_after_the_budget(ui, stack):
    signed_in(ui, f"#/p/{PROJECT}")
    block = ui.page.locator(".budget")
    expect(block).to_contain_text("1 verified candidates deferred")
    resume = block.get_by_role("button", name="Raise budget & resume")
    ui.dialogs = [None]
    resume.click()
    ui.dialogs = ["lots"]
    resume.click()
    ui.toast("whole number above 5")
    ui.dialogs = ["4"]
    resume.click()
    ui.toast("whole number above 5")
    assert stack.api("GET", f"/projects/{PROJECT}")[1]["project"]["max_sources"] == 5
    ui.dialogs = ["8"]
    resume.click()
    ui.toast("budget 8: 1 deferred re-added, census agent queued")
    wait_job_done(stack, "agent:census", 2)
    expect(ui.page.locator(".budget")).to_have_count(0, timeout=10000)
    tab(ui, "sources")
    expect(source_row(ui, "Spare three")).to_be_visible()
    tab(ui, "census")
    expect(ui.page.locator("main")).not_to_contain_text("Deferred by the budget")
    assert stack.api("GET", f"/projects/{PROJECT}")[1]["project"]["max_sources"] == 8


# ======================================================================== cross-cutting
def test_20_error_states(ui, stack):
    ui.allow(404)
    signed_in(ui, "#/p/no-such-project")
    alert = ui.page.locator(".panel.error")
    expect(alert).to_contain_text("Could not open this project")
    expect(alert).to_contain_text("no-such-project")
    assert str(stack.home) not in alert.inner_text()  # no server paths in messages
    ui.shot("20-error-unknown-project")
    alert.get_by_role("link", name="Back to projects").click()
    expect(ui.page.get_by_role("heading", name="Projects")).to_be_visible()
    # an unknown tab falls back to the overview
    ui.goto(f"#/p/{PROJECT}/nonsense")
    expect(ui.page.locator(".tabs a.on")).to_have_text("overview")
    # the server goes away during background refresh: one notice, the page stays, and it recovers by itself
    ui.goto(f"#/p/{PROJECT}/jobs")
    expect(ui.page.locator(".tabs a.on")).to_have_text("jobs")
    expect(ui.page.locator("tbody tr", has_text="detect_lanes")).to_be_visible()
    ui.allowed_console.append("ERR_FAILED|ERR_CONNECTION")
    ui.page.route("**/api/**", lambda r: r.abort())
    ui.toast("connection problem: cannot reach the harvest server")
    expect(ui.page.locator("tbody tr", has_text="detect_lanes")).to_be_visible()
    ui.page.unroute("**/api/**")
    expect(ui.page.locator("#toast")).to_contain_text("connection restored", timeout=15000)
    expect(ui.page.locator(".tabs a.on")).to_have_text("jobs")
    # a token that stops working mid-session sends the user back to sign in, with a reason
    ui.allow(401)
    ui.page.evaluate("localStorage.setItem('harvest_token', 'rotated-away')")
    ui.page.locator(".tabs a", has_text="runs").click()
    expect(ui.page.get_by_role("heading", name="Sign in")).to_be_visible()
    ui.toast("wrong or expired admin token")


def test_21_sign_out_in_one_tab_signs_out_the_other(ui, stack, ctx):
    signed_in(ui, f"#/p/{PROJECT}")
    other = Watched(ctx.new_page(), stack.base)
    other.goto(f"#/p/{PROJECT}/sources")
    expect(other.page.locator("#logout")).to_be_visible()
    ui.page.locator("#logout").click()
    expect(ui.page.get_by_role("heading", name="Sign in")).to_be_visible()
    expect(other.page.get_by_role("heading", name="Sign in")).to_be_visible(timeout=5000)
    assert not other.errors, other.errors
    other.page.close()


def test_22_non_ascii_and_hostile_project_text(ui, stack):
    target = "سيارات مستعملة <img src=x onerror=\"window.__xss=1\"> 中古車"
    st, _ = stack.api("POST", "/projects", {"name": "arabic-cjk", "target": target, "regions": ["AE", "JP"], "record_type": "vehicles"})
    assert st == 201
    signed_in(ui)
    row = ui.page.locator("tbody tr", has_text="arabic-cjk")
    expect(row).to_contain_text(target)
    row.get_by_role("link", name="arabic-cjk").click()
    expect(ui.page.locator("h1")).to_have_text(target)
    assert ui.page.locator("main img").count() == 0


def test_23_keyboard_only(browser, stack):
    c = browser.new_context(viewport={"width": 1280, "height": 900})
    ui = Watched(c.new_page(), stack.base)
    try:
        ui.goto()
        expect(ui.page.locator("#tok")).to_be_focused()  # the token field takes focus
        ui.page.keyboard.type(TOKEN)
        ui.page.keyboard.press("Enter")
        expect(ui.page.get_by_role("heading", name="Projects")).to_be_visible()
        # tab to the project link and open it
        for _ in range(40):
            ui.page.keyboard.press("Tab")
            if ui.page.evaluate("document.activeElement.textContent") == PROJECT:
                break
        else:
            raise AssertionError("the project link is not reachable with Tab")
        outline = ui.page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
        assert outline != "none", "no visible focus indicator"
        ui.page.keyboard.press("Enter")
        expect(ui.page.get_by_role("heading", name="used cars")).to_be_visible()
        # tab to the "sources" section and open it
        for _ in range(40):
            ui.page.keyboard.press("Tab")
            if ui.page.evaluate("document.activeElement.textContent") == "sources":
                break
        else:
            raise AssertionError("the sources tab is not reachable with Tab")
        ui.page.keyboard.press("Enter")
        expect(ui.page.locator(".tabs a.on")).to_have_text("sources")
        # focus a button: the background refresh (every 4 s) must not steal it
        ui.page.locator("[data-src=detect]").first.focus()
        ui.page.wait_for_timeout(9000)
        assert ui.page.evaluate("document.activeElement.dataset.src") == "detect"
        # the first Tab on a fresh page reaches the skip link
        ui.page.reload()
        ui.page.keyboard.press("Tab")
        expect(ui.page.locator(".skip")).to_be_focused()
        assert not ui.errors, ui.errors
    finally:
        c.close()


@pytest.mark.parametrize("where", ["", "overview", "census", "sources", "data", "runs", "quarantine", "jobs"])
def test_24_mobile_390(browser, stack, where):
    c = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    ui = Watched(c.new_page(), stack.base)
    try:
        signed_in(ui, f"#/p/{PROJECT}/{where}" if where else "")
        expect(ui.page.locator("main h1")).to_be_visible()
        ui.page.wait_for_timeout(300)
        ui.no_horizontal_scroll()
        if where in ("", "overview", "sources", "data"):
            ui.shot(f"24-mobile-{where or 'home'}")
        assert not ui.errors, ui.errors
    finally:
        c.close()


@pytest.mark.parametrize("width", [1280, 1024, 390])
def test_25_source_actions_stay_reachable(browser, stack, width):
    c = browser.new_context(viewport={"width": width, "height": 900 if width > 600 else 844})
    ui = Watched(c.new_page(), stack.base)
    try:
        signed_in(ui, f"#/p/{PROJECT}/sources")
        expect(ui.page.locator("tbody tr[data-source]").first).to_be_visible()
        assert_source_actions_reachable(ui)
        ui.no_horizontal_scroll()  # only the table box scrolls sideways, never the page
        if width > 600:  # long reasons are folded: rows stay compact
            heights = ui.page.locator("tbody tr[data-source]").evaluate_all("rs => rs.map(r => r.getBoundingClientRect().height)")
            assert max(heights) < 200, heights
        # a folded reason opens from the keyboard and shows the full text
        wall = source_row(ui, "Challenge wall")
        summary = wall.locator("td.lane details.why > summary")
        summary.focus()
        ui.page.keyboard.press("Enter")
        expect(wall.locator("td.lane details.why")).to_have_attribute("open", "")
        expect(wall.locator("td.lane details.why > div")).to_be_visible()
        expect(wall.locator("td.lane details.why > div")).to_contain_text("challenge")
        expect(source_row(ui, "Spare one").locator("td.lane summary")).to_have_text("JavaScript shell…")  # a whole clause, not a stub
        ui.shot(f"25-sources-{width}", full=False)
        assert not ui.errors, ui.errors
    finally:
        c.close()
