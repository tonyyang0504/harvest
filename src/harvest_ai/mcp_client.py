"""Optional "api" lane through any MCP server the user configures.

Config: `$HARVEST_MCP_CONFIG` or `$HARVEST_HOME/mcp_servers.json`:
    {"servers": {"<name>": {"command": "uvx", "args": ["some-mcp-server"], "env": {"TOKEN": "..."},
                             "domains": ["example.com"],                  # sites this server serves
                             "lookup": {"tool": "search", "arg": "query"}  # optional: ask it about a domain
                             }}}
Nothing here is specific to one vendor; with no config the lane is simply skipped.
Scraper modules on an `mcp` lane call `harvest_ai.mcp_client.call_tool(server, tool, args)`.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .http import trusted_caller, trusted_section


def config_path() -> Path:
    from .project import home
    return Path(os.environ.get("HARVEST_MCP_CONFIG") or (home() / "mcp_servers.json"))


_PRELOADED: dict | None = None


def preload() -> None:
    """Read the config now (the sandbox child does this before its file guard goes up: the config holds the
    servers' API keys and is unreadable to scraper code afterwards)."""
    global _PRELOADED
    _PRELOADED = None
    _PRELOADED = load_config()


def load_config() -> dict:
    if _PRELOADED is not None:
        return _PRELOADED
    p = config_path()
    if not p.is_file():
        return {}
    try:
        return (json.loads(p.read_text(encoding="utf-8")) or {}).get("servers") or {}
    except ValueError:
        return {}


async def _session_call(cfg: dict, fn):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **(cfg.get("env") or {})}
    params = StdioServerParameters(command=cfg["command"], args=list(cfg.get("args") or []), env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


@trusted_caller
def _run(coro_factory, timeout: float = 60.0):
    with trusted_section():  # spawning the configured server is harvest's own, trusted action
        return asyncio.run(asyncio.wait_for(coro_factory(), timeout))


def list_tools(server: str, timeout: float = 60.0) -> list[dict]:
    cfg = load_config().get(server)
    if not cfg:
        raise LookupError(f"no MCP server {server!r} in {config_path()}")

    async def go(s):
        return [{"name": t.name, "description": (t.description or "")[:200]} for t in (await s.list_tools()).tools]
    return _run(lambda: _session_call(cfg, go), timeout)


def call_tool(server: str, tool: str, args: dict | None = None, timeout: float = 120.0) -> Any:
    cfg = load_config().get(server)
    if not cfg:
        raise LookupError(f"no MCP server {server!r} in {config_path()}")

    async def go(s):
        res = await s.call_tool(tool, args or {})
        if getattr(res, "structured_content", None):
            return res.structured_content
        texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return {"text": joined, "is_error": bool(res.is_error)}
    return _run(lambda: _session_call(cfg, go), timeout)


def match_domain(domain: str, *, probe: bool = True) -> list[dict]:
    """Configured servers that serve `domain`: declared in `domains`, or (probe=True) whose lookup
    tool mentions the domain in its answer."""
    hits = []
    for name, cfg in load_config().items():
        if domain in (cfg.get("domains") or []):
            hits.append({"server": name, "how": "declared"})
            continue
        lk = cfg.get("lookup")
        if probe and lk and lk.get("tool"):
            try:
                res = call_tool(name, lk["tool"], {lk.get("arg", "query"): domain}, timeout=45)
            except Exception:
                continue
            if domain in json.dumps(res, ensure_ascii=False, default=str):
                hits.append({"server": name, "how": "lookup", "tool": lk["tool"]})
    return hits
