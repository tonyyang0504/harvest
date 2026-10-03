"""Installed-package smoke: spawn the harvest MCP console script over stdio, initialise, list the
tools and call two of them. Usage: python tools/smoke_stdio.py /path/to/harvest-mcp"""
import asyncio
import json
import sys
import tempfile

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main(cmd: str) -> None:
    home = tempfile.mkdtemp(prefix="harvest-smoke-")
    params = StdioServerParameters(command=cmd, args=[], env={"PATH": "/usr/bin:/bin", "HARVEST_HOME": home})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            assert "harvest_new_project" in tools and "harvest_run" in tools, tools
            res = await s.call_tool("harvest_templates", {})
            assert not res.is_error and "vehicles" in res.content[0].text
            res = await s.call_tool("harvest_new_project", {"name": "smoke", "target": "used cars", "regions": ["KZ", "GE"], "record_type": "vehicles"})
            assert not res.is_error, res.content[0].text
            plan = await s.call_tool("harvest_census_plan", {"project": "smoke"})
            assert json.loads(plan.content[0].text)["regions"][0]["region"] == "KZ"
            print(f"ok {cmd}: {init.server_info.name} proto={init.protocol_version} tools={len(tools)}")


asyncio.run(main(sys.argv[1]))
