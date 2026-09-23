#!/usr/bin/env python
"""Stdio transport test: spawn the stub gateway + the MCP server, drive a full
MCP handshake with the official SDK client, call two tools, print the
transcript.

Usage:  python tests/test_stdio.py
"""

import asyncio
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server.py"
STUB = HERE / "stub_gateway.py"

GATEWAY_TOKEN = "test-token-123"


def wait_for(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"stub gateway never came up at {url}")


async def run_test() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        env={**os.environ,
             "GATEWAY_URL": "http://127.0.0.1:8902",
             "GATEWAY_TOKEN": GATEWAY_TOKEN},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            info = init.serverInfo
            print(f"initialize -> server '{info.name}' v{info.version} "
                  f"(protocol {init.protocolVersion})")

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"tools/list -> {len(names)} tools: {', '.join(names)}")

            print("\n--- tools/call pipeline_status ---")
            result = await session.call_tool("pipeline_status", {})
            print(result.content[0].text)

            print("\n--- tools/call new_game ---")
            result = await session.call_tool("new_game", {
                "name": "Cube Fall", "template": "3d",
                "pitch": "3D Kurushi study: tower blocks rush at you, "
                         "smash the matching face.",
                "requester": "stdio-test",
            })
            print(result.content[0].text)

            print("\n--- tools/call list_jobs (query params) ---")
            result = await session.call_tool("list_jobs",
                                             {"status": "queued", "limit": 5})
            print(result.content[0].text)


def main() -> int:
    stub = subprocess.Popen(
        [sys.executable, str(STUB)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for("http://127.0.0.1:8902/api/health")
        print("stub gateway up on 127.0.0.1:8902\n")
        asyncio.run(run_test())
        print("\nstdio transport: PASS")
        return 0
    finally:
        stub.terminate()
        try:
            stub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stub.kill()


if __name__ == "__main__":
    sys.exit(main())
