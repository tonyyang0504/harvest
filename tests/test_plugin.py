"""The Claude Code plugin stays consistent with the MCP server.

Claude Code names a plugin's MCP tools `mcp__plugin_<plugin>_<server>__<tool>`; this was verified live on
2026-09-25 with `claude --plugin-dir` (plugin `harvest`, server `harvest`)."""

import asyncio
import json
import re
from pathlib import Path

from harvest_ai import agents
from harvest_ai.mcp_server import server

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "mcp__plugin_harvest_harvest__"
BUILTIN = {"Read", "Edit", "Write", "WebFetch", "WebSearch", "Glob", "Grep"}


def _server_tools() -> set[str]:
    return {t.name for t in asyncio.run(server.list_tools())}


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, path
    out = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def test_manifests_and_mcp_server_name():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    mcp = json.loads((ROOT / ".mcp.json").read_text())
    assert plugin["name"] == "harvest" and list(mcp["mcpServers"]) == ["harvest"]
    assert PREFIX == f"mcp__plugin_{plugin['name']}_{list(mcp['mcpServers'])[0]}__"


def test_every_plugin_agent_has_an_explicit_valid_tools_list():
    names = _server_tools()
    agent_files = sorted((ROOT / "agents").glob("*.md"))
    assert {p.stem for p in agent_files} == {"harvest-census", "harvest-auditor", "harvest-builder", "harvest-repair"}
    for p in agent_files:
        fm = _frontmatter(p)
        assert fm.get("tools"), f"{p.name} must list its tools"
        tools = [t.strip() for t in fm["tools"].split(",")]
        for t in tools:
            if t.startswith("mcp__"):
                assert t.startswith(PREFIX) and t[len(PREFIX):] in names, (p.name, t)
            else:
                assert t in BUILTIN, (p.name, t)
        assert PREFIX + "harvest_agent_brief" in tools, p.name
        assert PREFIX + "harvest_enable_source" not in tools, p.name  # enabling is the operator's decision
        assert "Bash" not in tools


def test_headless_allowlists_name_real_tools():
    names = _server_tools()
    from conftest import make_project
    p = make_project()
    for kind in ("census", "audit", "build", "repair"):
        for t in agents.allowed_tools(kind, p, "src_x"):
            if t.startswith("mcp__"):
                assert t.startswith("mcp__harvest__") and t[len("mcp__harvest__"):] in names, (kind, t)


def test_skill_and_command_reference_real_tools():
    names = _server_tools()
    for f in (ROOT / "skills" / "harvest" / "SKILL.md", ROOT / "commands" / "harvest.md"):
        for t in set(re.findall(r"\bharvest_[a-z_]+", f.read_text(encoding="utf-8"))):
            assert t in names, (f.name, t)
