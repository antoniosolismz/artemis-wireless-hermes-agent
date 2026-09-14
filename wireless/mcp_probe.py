"""Probe the ARTEMIS MCP server exactly the way Hermes' native MCP client does."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path.home() / "projects" / "artemis"


async def main() -> int:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(REPO),
        # Pin the adb binary we installed; artemis resolves adb via its toolchain.
        "ARTEMIS_ADB_PATH": str(Path.home() / ".local" / "bin" / "adb"),
    }
    params = StdioServerParameters(
        command=str(REPO / ".venv" / "bin" / "python"),
        args=["-m", "mcp_server"],
        cwd=str(REPO),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("=== initialize ===")
            print("server:", init.serverInfo.name, init.serverInfo.version)
            tools = await session.list_tools()
            print(f"\n=== {len(tools.tools)} tools discovered ===")
            for t in tools.tools:
                print(f"  - {t.name}")
            print("\n=== mobile_get_device_state schema (device_serial targeting) ===")
            for t in tools.tools:
                if t.name == "mobile_get_device_state":
                    print(json.dumps(t.inputSchema, indent=2)[:1500])
            print("\n=== calling mobile_diagnose (real execution) ===")
            res = await session.call_tool("mobile_diagnose", {"probe_device": False})
            for block in res.content:
                text = getattr(block, "text", "")
                try:
                    parsed = json.loads(text)
                    print(json.dumps(parsed, indent=2)[:4000])
                except Exception:
                    print(text[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
