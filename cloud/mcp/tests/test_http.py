#!/usr/bin/env python
"""Streamable-HTTP transport test: spawn the stub gateway + the MCP server in
--http mode on 127.0.0.1:8911/mcp, then prove:
  1. a wrong bearer token is rejected with 401
  2. the right bearer (GATEWAY_TOKEN) completes the full handshake
  3. a tools/call round-trips through to the stub gateway

Usage:  python tests/test_http.py
"""

import asyncio
import json
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
MCP_URL = "http://127.0.0.1:8911/mcp"


def wait_for(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status < 500:
                    return
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"server never came up at {url}")


def raw_post(path: str, token: str) -> int:
    """Raw HTTP probe used for the 401 check (no MCP session needed)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    req = urllib.request.Request(
        path if path.startswith("http") else f"http://127.0.0.1:8911{path}",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


async def run_test() -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    # 1. wrong token must be rejected before any MCP session starts
    code = raw_post(MCP_URL, "wrong-token")
    print(f"raw POST /mcp with bad bearer -> HTTP {code} "
          f"({'REJECTED as expected' if code == 401 else 'UNEXPECTED'})")
    assert code == 401, f"expected 401 for bad token, got {code}"

    # 2. right token: full handshake + tool call
    async with streamablehttp_client(
            MCP_URL, headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"}) as (
            read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            info = init.serverInfo
            print(f"initialize -> server '{info.name}' v{info.version} "
                  f"(protocol {init.protocolVersion})")

            tools = await session.list_tools()
            print(f"tools/list -> {len(tools.tools)} tools over HTTP")

            print("\n--- tools/call tune_tunable ---")
            result = await session.call_tool("tune_tunable", {
                "game": "word-poker", "tunable": "round_seconds",
                "new_value": 45.0})
            print(result.content[0].text)

            print("\n--- tools/call job_status ---")
            result = await session.call_tool("job_status", {"id": "j-1007"})
            print(result.content[0].text)


def main() -> int:
    stub = subprocess.Popen(
        [sys.executable, str(STUB)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    httpd = subprocess.Popen(
        [sys.executable, str(SERVER), "--http"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ,
             "GATEWAY_URL": "http://127.0.0.1:8902",
             "GATEWAY_TOKEN": GATEWAY_TOKEN,
             "MCP_PORT": "8911"})
    try:
        wait_for("http://127.0.0.1:8902/api/health")
        wait_for("http://127.0.0.1:8911/mcp")
        print("stub gateway up on :8902, MCP server up on :8911/mcp\n")
        asyncio.run(run_test())
        print("\nstreamable-http transport: PASS")
        return 0
    finally:
        for proc in (httpd, stub):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
