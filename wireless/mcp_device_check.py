#!/usr/bin/env python3
"""Prove the Hermes-facing MCP path can drive the real phone.

Spawns the ARTEMIS MCP server the way Hermes' native MCP client does, then calls
`mobile_diagnose` and `mobile_get_device_state` against the paired device — the
same calls a new Hermes session would make. Saves any returned screenshot to
mcp-screenshot.png so it can be inspected directly.

Usage: ~/projects/artemis/.venv/bin/python mcp_device_check.py [serial]
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path.home() / "projects" / "artemis"
HERE = Path.home() / "projects" / "artemis-wireless"
SERIAL = sys.argv[1] if len(sys.argv) > 1 else "adb-XXXXXXX-XXXXXX._adb-tls-connect._tcp"


def _extract_screenshot(blocks: list) -> bytes | None:
    """Find and decode a screenshot in whatever shape the tool returns it."""
    for block in blocks:
        # 1. an actual image content block
        data = getattr(block, "data", None)
        if data and getattr(block, "type", "") == "image":
            try:
                return base64.b64decode(data)
            except (binascii.Error, ValueError):
                pass
        text = getattr(block, "text", "") or ""
        # 2. a path to a saved image
        m = re.search(r"(/[^\s\"']+\.(?:png|jpe?g))", text)
        if m and Path(m.group(1)).exists():
            return Path(m.group(1)).read_bytes()
        # 3. inline base64 inside JSON
        m = re.search(r'"(?:screenshot|image)_?(?:base64|png)?"\s*:\s*"([A-Za-z0-9+/=]{200,})"', text)
        if m:
            try:
                return base64.b64decode(m.group(1))
            except (binascii.Error, ValueError):
                pass
    return None


async def main() -> int:
    env = {
        "PATH": f"{Path.home() / '.local' / 'bin'}:{os.environ['PATH']}",
        "HOME": os.environ["HOME"],
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(REPO),
    }
    params = StdioServerParameters(
        command=str(REPO / ".venv" / "bin" / "python"),
        args=["-m", "mcp_server"],
        cwd=str(REPO),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"tools exposed to Hermes: {[t.name for t in tools.tools]}")

            print("\n=== mobile_diagnose ===")
            res = await session.call_tool("mobile_diagnose", {"probe_device": False})
            raw = "".join(getattr(b, "text", "") for b in res.content)
            try:
                start = raw.index("{")
                diag = json.loads(raw[start:])
                print("verdict:", diag.get("verdict"))
                for c in diag.get("checks", []):
                    print(f"  {c['status']:5} {c['id']:20} {c['summary']}")
            except (ValueError, KeyError):
                print(raw[:1200])

            print("\n=== mobile_get_device_state (screenshot) ===")
            res = await session.call_tool(
                "mobile_get_device_state",
                {"view_type": "screenshot", "device_serial": SERIAL},
            )
            print("returned blocks:", [getattr(b, "type", "?") for b in res.content])
            for b in res.content:
                text = getattr(b, "text", "")
                if text:
                    print(text[:700])
            image = _extract_screenshot(res.content)
            if image:
                out = HERE / "mcp-screenshot.png"
                out.write_bytes(image)
                print(f"\nscreenshot saved: {out} ({len(image):,} bytes)")
                return 0
            print("\nno screenshot found in the response")
            return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
