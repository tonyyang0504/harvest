"""Headless agent jobs: census, scraper build, source audit and repair, run by an agent CLI.

The CLI is pluggable. `claude` (Claude Code, `claude -p`) is built in; others register an
`AgentCli` subclass through `HARVEST_AGENT_CLI=package.module:ClassName`. Every job gets an explicit
tool allowlist (`--allowedTools`) scoped to what that job needs; permission bypass flags are never
passed, so a tool outside the list is simply denied in headless mode.

Settings: HARVEST_AGENT_CLI (default `claude`), HARVEST_AGENT_BIN (path to the CLI binary),
HARVEST_AGENT_MODEL (e.g. a model alias), HARVEST_AGENT_TIMEOUT_S (default 3600),
HARVEST_AGENT_MAX_TURNS (default 80).
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
from importlib import resources
from pathlib import Path

from .project import Project, home

KINDS = ("census", "build", "audit", "repair")
FORBIDDEN_FLAGS = ("--dangerously-skip-permissions", "--permission-mode=bypassPermissions", "bypassPermissions", "--yolo",
                   "--dangerously-bypass-approvals-and-sandbox")


def brief(kind: str, p: Project, **ctx) -> str:
    """The canonical prompt for a job kind, filled with the project's context. Shared by the plugin
    agents (through the `harvest_agent_brief` MCP tool) and headless jobs."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    text = resources.files("harvest_ai.prompts").joinpath(f"{kind}.md").read_text(encoding="utf-8")
    values = {"project": p.name, "target": p.spec.target, "record_type": p.spec.record_type, "regions": ", ".join(p.spec.region_codes),
              "languages": ", ".join(p.spec.languages), "max_sources": str(p.spec.max_sources), "sources_dir": str(p.sources_dir),
              "source_id": ctx.get("source_id") or "<source_id>", "module_path": str(p.module_path(ctx["source_id"])) if ctx.get("source_id") else "<module>",
              "extra": ctx.get("extra") or ""}
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def mcp_tools(*names: str) -> list[str]:
    return [f"mcp__harvest__{n}" for n in names]


def edit_rule(path) -> str:
    """Claude Code permission rule allowing writes to exactly one file. File tools (Edit and Write) are
    governed by `Edit(...)` rules only, and an absolute path needs a leading `//` (`/x` is relative to the
    project root). Both facts were verified against the live CLI in the 2026-09-25 pilot, where
    `Edit(/srv/...)` + `Write(/srv/...)` silently denied every write."""
    ap = os.path.abspath(str(path))
    return f"Edit(/{ap})" if ap.startswith("/") else f"Edit({ap})"


# The harvest MCP tools each job kind may call. Enforced twice: as the CLI's --allowedTools list, and by the harvest
# MCP server itself (HARVEST_AGENT_KIND in the server's environment), so a permissive user settings file that
# pre-approves every mcp__harvest__* tool cannot hand an agent harvest_enable_source, harvest_proxy_enable & co.
MCP_ALLOWED = {
    "census": ("harvest_census_plan", "harvest_census_add", "harvest_census_gaps", "harvest_list_sources", "harvest_probe_url"),
    "audit": ("harvest_census_gaps", "harvest_census_add", "harvest_list_sources", "harvest_detect_lane", "harvest_record_policy",
              "harvest_probe_url"),
    "build": ("harvest_probe_url", "harvest_review_source", "harvest_list_sources", "harvest_template_scraper", "harvest_detect_lane",
              "harvest_record_policy"),
    "repair": ("harvest_probe_url", "harvest_review_source", "harvest_list_sources", "harvest_template_scraper"),
}
# never passed to an agent's environment (the agent has no use for them; a prompt-injected agent should not hold them)
SECRET_ENV_PREFIXES = ("HARVEST_PROXY", "HARVEST_ADMIN_TOKEN", "HARVEST_ALERT_WEBHOOK", "HARVEST_ALERT_HOOK")


def read_rule(path) -> str:
    """Claude Code permission rule allowing reads under one directory (same `//abs` form as `edit_rule`)."""
    ap = os.path.abspath(str(path))
    return f"Read(/{ap}/**)" if ap.startswith("/") else f"Read({ap}/**)"


def allowed_tools(kind: str, p: Project, source_id: str | None = None) -> list[str]:
    if kind in ("census", "audit"):
        return ["WebSearch", "WebFetch", *mcp_tools(*MCP_ALLOWED[kind])]
    if kind not in MCP_ALLOWED:
        raise ValueError(f"kind must be one of {KINDS}")
    if not source_id:
        raise ValueError(f"{kind} jobs need a source_id")
    # Read is scoped to the sources directory: a page the agent fetched can carry instructions, and an unscoped
    # Read plus WebFetch is a path from those instructions to the admin token, the proxy pool or the CLI's credentials
    return [read_rule(p.sources_dir), edit_rule(p.module_path(source_id)), "WebFetch", *mcp_tools(*MCP_ALLOWED[kind])]


class AgentCli:
    name = "base"

    def binary(self) -> str:
        raise NotImplementedError

    def argv(self, prompt: str, *, allowed: list[str], model: str | None, mcp_config: Path, max_turns: int) -> list[str]:
        raise NotImplementedError


class ClaudeCli(AgentCli):
    name = "claude"

    def binary(self) -> str:
        return os.environ.get("HARVEST_AGENT_BIN") or shutil.which("claude") or "claude"

    def argv(self, prompt: str, *, allowed: list[str], model: str | None, mcp_config: Path, max_turns: int) -> list[str]:
        # --allowedTools only pre-approves; --tools removes every other built-in tool (Bash, Task, Cron ...) from the session
        builtins = sorted({t.split("(")[0] for t in allowed if not t.startswith("mcp__")} | ({"Write"} if any(t.startswith("Edit(") for t in allowed) else set()))
        a = [self.binary(), "-p", prompt, "--tools", ",".join(builtins), "--allowedTools", ",".join(allowed),
             "--mcp-config", str(mcp_config), "--strict-mcp-config",
             "--output-format", "stream-json", "--verbose", "--max-turns", str(max_turns)]
        if model:
            a += ["--model", model]
        return a


CLIS: dict[str, type[AgentCli]] = {"claude": ClaudeCli}


def get_cli(name: str | None = None) -> AgentCli:
    configured = os.environ.get("HARVEST_AGENT_CLI", "claude")
    if name and ":" in name and name != configured:
        # a job parameter (web API, MCP) may pick a built-in CLI; importing an arbitrary module:Class is the operator's
        # environment setting only
        raise ValueError("a custom agent CLI (module:Class) is set through HARVEST_AGENT_CLI, not per job")
    name = name or configured
    if ":" in name:
        mod, _, cls = name.partition(":")
        return getattr(importlib.import_module(mod), cls)()
    if name not in CLIS:
        raise ValueError(f"unknown agent CLI {name!r}; built in: {sorted(CLIS)}; or use module:Class")
    return CLIS[name]()


def mcp_config_for(p: Project, kind: str | None = None) -> Path:
    env = {"HARVEST_HOME": str(home()), **({"HARVEST_ALLOW_PRIVATE": "1"} if os.environ.get("HARVEST_ALLOW_PRIVATE") == "1" else {})}
    if kind:
        env["HARVEST_AGENT_KIND"] = kind  # the server enforces MCP_ALLOWED[kind] and the agent policy rules
    cfg = {"mcpServers": {"harvest": {"command": sys.executable, "args": ["-m", "harvest_ai.mcp_server"], "env": env}}}
    p.agents_dir.mkdir(parents=True, exist_ok=True)
    path = p.agents_dir / (f"mcp-{kind}.json" if kind else "mcp.json")
    path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return path


def parse_output(lines: list[str]) -> dict:
    """The agent's final result from its stdout: the last `{"type": "result"}` event of a stream-json
    transcript, or one JSON document (`--output-format json` CLIs), or the raw tail."""
    tool_calls = 0
    final = None
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            for block in ((ev.get("message") or {}).get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls += 1
        if ev.get("type") == "result" or ("result" in ev and "type" not in ev):
            final = ev
    if final is None:
        text = "".join(lines)
        try:
            final = json.loads(text)
        except ValueError:
            final = {"result": text[-4000:]}
    if not isinstance(final, dict):
        final = {"result": final}
    final["tool_calls"] = tool_calls
    return final


def build_command(kind: str, p: Project, source_id: str | None = None, *, extra: str = "", cli: str | None = None, model: str | None = None) -> list[str]:
    agent = get_cli(cli)
    prompt = brief(kind, p, source_id=source_id, extra=extra)
    argv = agent.argv(prompt, allowed=allowed_tools(kind, p, source_id), model=model or os.environ.get("HARVEST_AGENT_MODEL") or None,
                      mcp_config=mcp_config_for(p, kind), max_turns=int(os.environ.get("HARVEST_AGENT_MAX_TURNS", "80")))
    for tok in argv:
        if any(f in tok for f in FORBIDDEN_FLAGS if tok.startswith("-") or f == tok):
            raise RuntimeError(f"refusing to run an agent with {tok}")
    return argv


def run_agent(kind: str, p: Project, source_id: str | None, *, log_path, extra: str = "", cli: str | None = None, model: str | None = None,
              timeout_s: int | None = None, on_start=None, cancel: threading.Event | None = None) -> tuple[str, dict]:
    """Run one headless agent to completion; stream its transcript into `log_path`. Returns (status, result).
    Pure with respect to the jobs table: the queue worker (or run_job) records the outcome. `cancel`, when set,
    kills the CLI (the worker sets it when it loses its lease)."""
    argv = build_command(kind, p, source_id, extra=extra, cli=cli, model=model)
    timeout = timeout_s or int(os.environ.get("HARVEST_AGENT_TIMEOUT_S", "3600"))
    env = {k: v for k, v in os.environ.items() if not k.startswith(SECRET_ENV_PREFIXES)}
    env["HARVEST_HOME"] = str(home())
    env["HARVEST_AGENT_KIND"] = kind
    # file agents start in the sources directory, so the CLI's implicit working-directory reads cover only modules
    cwd = p.sources_dir if kind in ("build", "repair") else p.root
    Path(cwd).mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    try:
        with open(log_path, "w", encoding="utf-8") as fh:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(cwd),
                                    env=env, text=True, encoding="utf-8", errors="replace")
            if on_start:
                on_start(proc.pid)
            killed = threading.Event()

            def kill():
                killed.set()
                proc.kill()
            timer = threading.Timer(timeout, kill)
            timer.start()
            watcher = None
            if cancel is not None:
                def watch():
                    while proc.poll() is None:
                        if cancel.wait(1.0):
                            proc.kill()
                            return
                watcher = threading.Thread(target=watch, daemon=True)
                watcher.start()
            try:
                for line in proc.stdout:  # the full transcript (stream-json) goes to the log as it happens
                    fh.write(line)
                    fh.flush()
                    lines.append(line)
                    del lines[:-2000]
                proc.wait()
            finally:
                timer.cancel()
        if killed.is_set():
            return "timeout", {"error": f"agent exceeded {timeout}s"}
        if cancel is not None and cancel.is_set():
            return "failed", {"error": "cancelled: the worker lost its lease"}
        parsed = parse_output(lines)
        status = "done" if proc.returncode == 0 and not parsed.get("is_error") else "failed"
        return status, {"returncode": proc.returncode, "result": parsed.get("result"), "cost_usd": parsed.get("total_cost_usd"),
                        "turns": parsed.get("num_turns"), "tool_calls": parsed.get("tool_calls")}
    except OSError as exc:
        return "failed", {"error": f"cannot start the agent CLI: {exc}"}


def run_job(kind: str, p: Project, source_id: str | None = None, *, extra: str = "", cli: str | None = None, model: str | None = None,
            timeout_s: int | None = None, wait: bool = True) -> dict:
    """Run one agent job through the job system (recorded in the jobs table, log under <project>/agents/).
    wait=True runs it here and now; wait=False submits it (queue or in-process thread per HARVEST_JOB_MODE)."""
    from . import jobs
    params = {"source_id": source_id, "extra": extra, "cli": cli, "model": model, "timeout_s": timeout_s}
    return jobs.submit(p, f"agent:{kind}", params, wait=wait)
