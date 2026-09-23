#!/usr/bin/env python3
"""Local smoke battery for cloud/mp (G3 skeleton).

Boots main.py as a subprocess in --local mode on a free port with MP_TOKEN set,
then exercises:
  - /health open (Cloud Run probes need it), /lobby 401 for guests/bad tokens
  - REST lobby create / list / join / room_full / room_not_found
  - WS: create, join, presence broadcast, offer/ice relay both ways,
    not_in_room isolation, room_full, ping/pong, leave

Exit code 0 = all green. Server logs stream to this console (interleaved).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")   # Windows law
sys.stderr.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "main.py")
TOKEN = "smoke-secret-1234"

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((ok, label))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f"  -- {detail}" if detail else ""), flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, path: str, body: dict | None = None,
         token: str | None = TOKEN) -> tuple[int, dict | str]:
    url = f"http://127.0.0.1:{PORT}{path}"
    req = urllib.request.Request(url, method=method)
    if token is not None:
        req.add_header("X-Token", token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


PORT = free_port()


async def ws_connect(session, token: str | None = TOKEN):
    url = f"http://127.0.0.1:{PORT}/signal" + (f"?token={token}" if token else "")
    ws = await session.ws_connect(url)
    return ws


async def ws_send(ws, msg: dict) -> None:
    await ws.send_str(json.dumps(msg))


async def ws_recv(ws, timeout: float = 5.0) -> dict:
    from aiohttp import WSMsgType
    msg = await asyncio.wait_for(ws.receive(), timeout)
    assert msg.type == WSMsgType.TEXT, f"unexpected ws frame: {msg}"
    return json.loads(msg.data)


async def battery() -> None:
    import aiohttp

    # --- REST ---------------------------------------------------------------
    st, body = http("GET", "/health", token=None)
    check(st == 200 and body.get("ok") is True, "B1 GET /health open -> 200 ok",
          json.dumps(body))

    st, body = http("GET", "/lobby", token=None)
    check(st == 401, "B2 GET /lobby guest (no token) -> 401", str(body))

    st, body = http("GET", "/lobby", token="wrong-token")
    check(st == 401, "B3 GET /lobby bad token -> 401", str(body))

    st, body = http("POST", "/lobby", {"action": "create", "game": "checkers",
                                       "player": "Aaron"})
    check(st == 201 and len(body["room"]["id"]) == 6,
          "B4 POST /lobby create checkers -> 201", json.dumps(body))
    room_a = body["room"]["id"]

    st, body = http("POST", "/lobby", {"action": "create", "game": "octogram",
                                       "player": "Bo"})
    check(st == 201, "B5 POST /lobby create octogram -> 201", json.dumps(body))

    st, body = http("GET", "/lobby")
    ids = [r["id"] for r in body.get("rooms", [])]
    check(st == 200 and room_a in ids and len(ids) == 2,
          "B6 GET /lobby lists both rooms", json.dumps(body))

    st, body = http("POST", "/lobby", {"action": "join", "room": room_a,
                                       "player": "Tash"})
    check(st == 200 and body["room"]["players"] == ["Aaron", "Tash"],
          f"B7 POST /lobby join {room_a} -> 200, 2 players", json.dumps(body))

    st, body = http("POST", "/lobby", {"action": "join", "room": room_a,
                                       "player": "Third"})
    check(st == 409 and body.get("code") == "room_full",
          "B8 POST /lobby join full room -> 409 room_full", str(body))

    st, body = http("POST", "/lobby", {"action": "join", "room": "ZZZZZZ",
                                       "player": "Nobody"})
    check(st == 404 and body.get("code") == "room_not_found",
          "B9 POST /lobby join bogus code -> 404 room_not_found", str(body))

    # --- WS -----------------------------------------------------------------
    async with aiohttp.ClientSession() as session:
        c1 = await ws_connect(session)
        await ws_send(c1, {"type": "create", "game": "checkers", "name": "Aaron"})
        m = await ws_recv(c1)
        check(m.get("type") == "room_created" and m.get("you") == 1,
              "B10 WS create -> room_created you=1", json.dumps(m))
        room = m["room"]

        c2 = await ws_connect(session)
        await ws_send(c2, {"type": "join", "room": room, "name": "Tash"})
        m = await ws_recv(c2)
        check(m.get("type") == "room_joined" and m.get("you") == 2
              and [p["id"] for p in m.get("peers", [])] == [1, 2],
              "B11 WS join -> room_joined you=2, full roster [1,2]", json.dumps(m))

        m = await ws_recv(c1)
        check(m.get("type") == "presence" and m.get("event") == "join"
              and m.get("peer", {}).get("id") == 2,
              "B12 peer1 sees presence join (id 2)", json.dumps(m))

        await ws_send(c1, {"type": "offer", "to": 2, "sdp": "v=0 fake-offer"})
        m = await ws_recv(c2)
        check(m.get("type") == "offer" and m.get("from") == 1
              and m.get("sdp") == "v=0 fake-offer",
              "B13 offer relayed 1->2, payload untouched, to->from", json.dumps(m))

        await ws_send(c2, {"type": "ice", "to": 1,
                           "candidate": "candidate:8421 typ srflx"})
        m = await ws_recv(c1)
        check(m.get("type") == "ice" and m.get("from") == 2
              and "srflx" in m.get("candidate", ""),
              "B14 ice relayed 2->1", json.dumps(m))

        await ws_send(c1, {"type": "offer", "to": 99, "sdp": "x"})
        m = await ws_recv(c1)
        check(m.get("type") == "error" and m.get("code") == "not_in_room",
              "B15 relay to unknown peer -> error not_in_room (MP-B7 style)", json.dumps(m))

        await ws_send(c1, {"type": "ping"})
        m = await ws_recv(c1)
        check(m.get("type") == "pong", "B16 ping -> pong", json.dumps(m))

        c3 = await ws_connect(session)
        await ws_send(c3, {"type": "join", "room": room, "name": "Crowder"})
        m = await ws_recv(c3)
        check(m.get("type") == "error" and m.get("code") == "room_full",
              "B17 third WS peer -> error room_full", json.dumps(m))

        await ws_send(c1, {"type": "leave"})
        m = await ws_recv(c1)
        check(m.get("type") == "left", "B18 WS leave acked", json.dumps(m))
        m = await ws_recv(c2)
        check(m.get("type") == "presence" and m.get("event") == "leave"
              and m.get("peer_id") == 1,
              "B19 peer2 sees presence leave (id 1)", json.dumps(m))

        st, body = http("GET", "/lobby")
        check(st == 200 and body.get("rooms"),
              "B20 GET /lobby still lists rooms after WS churn", json.dumps(body))

        await c1.close()
        await c2.close()
        await c3.close()

    # --- guest WS handshake rejected with 401 --------------------------------
    async with aiohttp.ClientSession() as s2:
        try:
            bad = await ws_connect(s2, token=None)
            await bad.close()
            check(False, "B21 guest WS handshake -> 401", "handshake was allowed")
        except Exception as exc:
            check(True, "B21 guest WS handshake refused -> 401",
                  type(exc).__name__)


def main() -> None:
    env = {**os.environ, "MP_TOKEN": TOKEN, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, MAIN, "--local", "--port", str(PORT)],
        env=env, cwd=HERE)
    print(f"== server booting on 127.0.0.1:{PORT} (MP_TOKEN set, --local) ==", flush=True)
    deadline = time.time() + 10
    up = False
    while time.time() < deadline:
        try:
            st, body = http("GET", "/health", token=None)
            up = st == 200
            break
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.2)
    if not up:
        proc.terminate()
        print("server never came up; aborting")
        sys.exit(2)
    try:
        asyncio.run(battery())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    fails = [label for ok, label in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} green ==")
    if fails:
        for label in fails:
            print("FAILED:", label)
        sys.exit(1)


if __name__ == "__main__":
    main()
