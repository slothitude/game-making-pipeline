#!/usr/bin/env python3
"""cloud/mp — Cloud Run multiplayer skeleton (Pipeline todo #88, free-google-plan G3).

Single-process asyncio server:
  GET  /health   open healthcheck (Cloud Run probes this; no auth by design)
  WS   /signal   peer registry + room relay (offer/answer/ice) + presence broadcast
  GET  /lobby    list in-memory rooms
  POST /lobby    create / join rooms (seats sized for checkers: max 2)

Auth is a STUB: X-Token header (or ?token= on the WS handshake) checked against
env MP_TOKEN. Guests get 401. Real Telegram HMAC tokens land later per
cloud/multiplayer-plan.md §7 — this file deliberately does NOT implement them.

Design laws inherited from cloud/multiplayer-plan.md:
  - the server is dumb about WebRTC — it shuttles opaque envelopes, never parses SDP
  - relay `to` is validated against the sender's OWN room roster (MP-B7 isolation)
  - per-peer outbound queue cap; slow peers get `error peer_slow`, never OOM
  - law constants live in ONE named block; tokens are never logged
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

# --- laws (all tunables in one block, per multiplayer-plan.md §5) -------------
LAW_ENVELOPE_MAX = 64 * 1024        # bytes, one WS JSON message (aiohttp enforces)
LAW_MAX_ROOMS = 32
LAW_MAX_PEERS = 4                   # hard ceiling per room; checkers uses 2
LAW_DEFAULT_MAX = 2                 # checkers-sized default
LAW_ROOM_TTL_S = 2 * 3600           # rooms reaped at 2h regardless
LAW_EMPTY_REAP_S = 300              # empty rooms reaped after 5 min
LAW_REAP_TICK_S = 60
LAW_WS_HEARTBEAT_S = 30             # aiohttp protocol-level ping (zombie reap)
LAW_RELAY_QUEUE = 16                # max buffered outbound envelopes per peer
LAW_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/L
LAW_CODE_LEN = 6
LAW_MAX_NAME = 32
LAW_RELAY_TYPES = ("offer", "answer", "ice")
# ------------------------------------------------------------------------------

BOOT = time.time()
MP_TOKEN = ""          # set at boot from env; empty = open mode (dev only)
QUIET = False


def log(msg: str) -> None:
    if not QUIET:
        print(time.strftime("%H:%M:%S"), msg, flush=True)


def err(code: str, msg: str) -> dict:
    return {"type": "error", "code": code, "msg": msg}


def clean_name(value: object) -> str:
    name = str(value or "").strip()[:LAW_MAX_NAME]
    return name or "guest"


def new_code(rooms: dict) -> str:
    while True:
        code = "".join(secrets.choice(LAW_CODE_ALPHABET) for _ in range(LAW_CODE_LEN))
        if code not in rooms:
            return code


@dataclass
class Peer:
    """One live /signal socket. Room-scoped numeric id assigned on create/join."""
    ws: web.WebSocketResponse
    q: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=LAW_RELAY_QUEUE))
    id: int | None = None
    name: str = "guest"
    room: "Room | None" = None
    dropped: int = 0
    sender: asyncio.Task | None = None
    last_id: int | None = None          # for logs after leave() clears .id


@dataclass
class Room:
    id: str
    game: str
    max: int
    created: float
    players: list[str] = field(default_factory=list)   # REST lobby roster (names)
    peers: dict[int, Peer] = field(default_factory=dict)  # live WS peers

    def roster(self) -> list[dict]:
        return [{"id": p.id, "name": p.name} for _, p in sorted(self.peers.items())]

    def view(self) -> dict:
        return {
            "id": self.id,
            "game": self.game,
            "max": self.max,
            "players": list(self.players),
            "peers": len(self.peers),
            "open": len(self.players) < self.max and len(self.peers) < self.max,
            "age_s": round(time.time() - self.created),
        }


def send(peer: Peer, msg: dict) -> None:
    """Queue an envelope. Never blocks, never grows unbounded (LAW_RELAY_QUEUE)."""
    try:
        peer.q.put_nowait(msg)
    except asyncio.QueueFull:
        peer.dropped += 1


async def _sender(peer: Peer) -> None:
    """Drain the peer's outbound queue onto the socket. One task per peer."""
    try:
        while True:
            msg = await peer.q.get()
            if msg is None:
                return
            await peer.ws.send_str(json.dumps(msg, separators=(",", ":")))
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # socket died mid-send; drop_peer handles teardown
        log(f"peer#{peer.id} sender stopped: {type(exc).__name__}")


class Hub:
    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}
        self.live: dict[web.WebSocketResponse, Peer] = {}

    # ---- connection lifecycle -------------------------------------------------
    def connect(self, ws: web.WebSocketResponse) -> Peer:
        peer = Peer(ws=ws)
        peer.sender = asyncio.get_running_loop().create_task(_sender(peer))
        self.live[ws] = peer
        return peer

    async def drop(self, peer: Peer) -> None:
        self.leave(peer, quiet_ack=True)
        send(peer, None)  # sentinel: stop the sender task
        if peer.sender is not None:
            peer.sender.cancel()
        self.live.pop(peer.ws, None)
        log(f"peer#{peer.last_id} disconnected (dropped={peer.dropped})")

    # ---- room ops -------------------------------------------------------------
    def create(self, peer: Peer, msg: dict) -> None:
        if peer.room is not None:
            send(peer, err("already_in_room", "leave your current room first"))
            return
        if len(self.rooms) >= LAW_MAX_ROOMS:
            send(peer, err("too_many_rooms", f"server at LAW_MAX_ROOMS={LAW_MAX_ROOMS}"))
            return
        try:
            room_max = int(msg.get("max", LAW_DEFAULT_MAX))
        except (TypeError, ValueError):
            room_max = LAW_DEFAULT_MAX
        room_max = max(2, min(room_max, LAW_MAX_PEERS))
        game = str(msg.get("game") or "unknown").strip()[:LAW_MAX_NAME] or "unknown"
        peer.name = clean_name(msg.get("name"))
        room = Room(id=new_code(self.rooms), game=game, max=room_max, created=time.time())
        self.rooms[room.id] = room
        peer.room = room
        peer.id = 1                       # creator is peer id 1 (plan §5)
        peer.last_id = peer.id
        room.peers[peer.id] = peer
        send(peer, {"type": "room_created", "room": room.id, "you": 1,
                    "game": game, "max": room_max,
                    "peers": room.roster()})
        log(f"room {room.id} created by peer#1 ({peer.name}) game={game} max={room_max}")

    def join(self, peer: Peer, msg: dict) -> None:
        if peer.room is not None:
            send(peer, err("already_in_room", "leave your current room first"))
            return
        code = str(msg.get("room") or "").strip().upper()
        room = self.rooms.get(code)
        if room is None:
            send(peer, err("room_not_found", f"no room {code!r}"))
            return
        if len(room.peers) >= room.max:
            send(peer, err("room_full", f"room {code} is full ({room.max})"))
            return
        peer.name = clean_name(msg.get("name"))
        peer.room = room
        peer.id = (max(room.peers) + 1) if room.peers else 1
        peer.last_id = peer.id
        room.peers[peer.id] = peer
        send(peer, {"type": "room_joined", "room": room.id, "you": peer.id,
                    "game": room.game, "peers": room.roster()})
        self._presence(room, "join", peer)
        log(f"peer#{peer.id} ({peer.name}) joined room {room.id}")

    def leave(self, peer: Peer, quiet_ack: bool = False) -> None:
        room = peer.room
        if room is None:
            return
        pid = peer.id
        room.peers.pop(pid, None)
        peer.room = None
        peer.id = None
        if not quiet_ack:
            send(peer, {"type": "left", "room": room.id})
        self._presence(room, "leave", peer, gone_id=pid)
        log(f"peer#{pid} left room {room.id}")

    def _presence(self, room: Room, event: str, subject: Peer | None = None,
                  gone_id: int | None = None) -> None:
        """Presence list broadcast to room members (except the departing socket)."""
        payload = {"type": "presence", "event": event, "room": room.id,
                   "peers": room.roster()}
        if subject is not None:
            # on leave, subject.id is already cleared — gone_id carries the real id
            payload["peer"] = {"id": gone_id if gone_id is not None else subject.id,
                               "name": subject.name}
        if gone_id is not None:
            payload["peer_id"] = gone_id
        for p in room.peers.values():
            if p is not subject:
                send(p, payload)

    # ---- relay ----------------------------------------------------------------
    def relay(self, peer: Peer, msg: dict) -> None:
        room = peer.room
        if room is None:
            send(peer, err("not_in_room", "join a room before relaying"))
            return
        to = msg.get("to")
        target = room.peers.get(to) if isinstance(to, int) else None
        if target is None:
            send(peer, err("not_in_room",
                           f"peer {to!r} is not in room {room.id}"))
            return
        out = {k: v for k, v in msg.items() if k != "to"}
        out["from"] = peer.id             # opaque payload passes through untouched
        try:
            target.q.put_nowait(out)
        except asyncio.QueueFull:
            send(peer, err("peer_slow", f"peer {to} outbound queue full; envelope dropped"))
            log(f"relay drop #{peer.id}->#{to} in {room.id} (peer_slow)")
            return
        log(f"relay {msg.get('type')} #{peer.id}->#{to} in {room.id}")

    # ---- inbound dispatch -------------------------------------------------------
    async def on_message(self, peer: Peer, raw: str) -> None:
        if len(raw) > LAW_ENVELOPE_MAX:
            send(peer, err("too_big", f"envelope > {LAW_ENVELOPE_MAX} bytes"))
            return
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                raise ValueError("not an object")
        except ValueError:
            send(peer, err("bad_json", "message must be one JSON object"))
            return
        mtype = msg.get("type")
        if mtype == "ping":
            send(peer, {"type": "pong"})
        elif mtype == "create":
            self.create(peer, msg)
        elif mtype == "join":
            self.join(peer, msg)
        elif mtype == "leave":
            self.leave(peer)
        elif mtype in LAW_RELAY_TYPES:
            self.relay(peer, msg)
        else:
            send(peer, err("bad_type", f"unknown type {mtype!r}"))

    # ---- reaper -----------------------------------------------------------------
    async def reap(self) -> None:
        while True:
            await asyncio.sleep(LAW_REAP_TICK_S)
            now = time.time()
            dead = [code for code, r in self.rooms.items()
                    if now - r.created > LAW_ROOM_TTL_S
                    or (not r.peers and not r.players
                        and now - r.created > LAW_EMPTY_REAP_S)]
            for code in dead:
                self.rooms.pop(code, None)
                log(f"reaped room {code}")


# --- auth stub -----------------------------------------------------------------
def token_ok(request: web.Request) -> bool:
    """STUB auth. Real Telegram HMAC token verify lands per plan §7 (bridge.py)."""
    if not MP_TOKEN:
        return True                        # open mode — local dev only, logged at boot
    got = request.headers.get("X-Token") or request.query.get("token", "")
    return bool(got) and secrets.compare_digest(got.encode(), MP_TOKEN.encode())


def unauthorized() -> web.Response:
    return web.json_response(
        {"error": "unauthorized",
         "detail": "missing or bad X-Token (auth stub: set MP_TOKEN; Telegram auth later)"},
        status=401)


# --- handlers ------------------------------------------------------------------
async def h_health(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    return web.json_response({
        "ok": True,
        "rooms": len(hub.rooms),
        "peers": sum(len(r.peers) for r in hub.rooms.values()),
        "uptime_s": round(time.time() - BOOT, 1),
    })


async def h_lobby_get(request: web.Request) -> web.Response:
    if not token_ok(request):
        return unauthorized()
    hub: Hub = request.app["hub"]
    rooms = sorted(hub.rooms.values(), key=lambda r: r.created)
    return web.json_response({"rooms": [r.view() for r in rooms]})


async def h_lobby_post(request: web.Request) -> web.Response:
    if not token_ok(request):
        return unauthorized()
    hub: Hub = request.app["hub"]
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("not an object")
    except Exception:
        return web.json_response(err("bad_json", "body must be one JSON object"), status=400)

    action = body.get("action")
    if action == "create":
        if len(hub.rooms) >= LAW_MAX_ROOMS:
            return web.json_response(err("too_many_rooms", "server full"), status=503)
        try:
            room_max = int(body.get("max", LAW_DEFAULT_MAX))
        except (TypeError, ValueError):
            room_max = LAW_DEFAULT_MAX
        room_max = max(2, min(room_max, LAW_MAX_PEERS))
        game = str(body.get("game") or "unknown").strip()[:LAW_MAX_NAME] or "unknown"
        room = Room(id=new_code(hub.rooms), game=game, max=room_max, created=time.time())
        player = clean_name(body.get("player"))
        room.players.append(player)
        hub.rooms[room.id] = room
        log(f"rest room {room.id} created game={game} max={room_max} player={player}")
        return web.json_response({"room": room.view()}, status=201)

    if action == "join":
        code = str(body.get("room") or "").strip().upper()
        room = hub.rooms.get(code)
        if room is None:
            return web.json_response(err("room_not_found", f"no room {code!r}"), status=404)
        if len(room.players) >= room.max:
            return web.json_response(err("room_full", f"room {code} is full"), status=409)
        player = clean_name(body.get("player"))
        room.players.append(player)
        log(f"rest player {player} joined room {code}")
        return web.json_response({"room": room.view()})

    return web.json_response(err("bad_action", "action must be 'create' or 'join'"),
                             status=400)


async def h_signal(request: web.Request) -> web.StreamResponse:
    if not token_ok(request):
        return unauthorized()
    ws = web.WebSocketResponse(heartbeat=LAW_WS_HEARTBEAT_S,
                               max_msg_size=LAW_ENVELOPE_MAX)
    await ws.prepare(request)
    hub: Hub = request.app["hub"]
    peer = hub.connect(ws)
    log("peer connected (no room yet)")
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await hub.on_message(peer, msg.data)
            elif msg.type == WSMsgType.ERROR:
                # oversize envelope: aiohttp enforced LAW_ENVELOPE_MAX, closed 1009
                log("ws error (envelope over LAW_ENVELOPE_MAX?)")
                break
    finally:
        await hub.drop(peer)
    return ws


def build_app() -> web.Application:
    app = web.Application()
    app["hub"] = Hub()
    app.router.add_get("/health", h_health)
    app.router.add_get("/lobby", h_lobby_get)
    app.router.add_post("/lobby", h_lobby_post)
    app.router.add_get("/signal", h_signal)
    return app


async def run(host: str, port: int) -> None:
    app = build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    hub = app["hub"]
    reaper = asyncio.get_running_loop().create_task(hub.reap())
    log(f"mp-skeleton listening on http://{host}:{port}  "
        f"(pid {os.getpid()})")
    log("auth: OPEN — MP_TOKEN unset, all guests allowed (dev mode only!)"
        if not MP_TOKEN else
        "auth: token required — X-Token header or ?token= (stub; Telegram later)")
    try:
        await asyncio.Event().wait()       # run until cancelled
    finally:
        reaper.cancel()
        await runner.cleanup()


def main() -> None:
    global MP_TOKEN
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):   # Windows UTF-8 law
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="cloud/mp multiplayer skeleton (G3)")
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address (Cloud Run law: 0.0.0.0)")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8080")),
                    help="port (default $PORT or 8080)")
    ap.add_argument("--local", action="store_true",
                    help="dev mode: bind 127.0.0.1 (auth still on if MP_TOKEN set)")
    args = ap.parse_args()
    MP_TOKEN = os.environ.get("MP_TOKEN", "").strip()
    host = "127.0.0.1" if args.local else args.host
    try:
        asyncio.run(run(host, args.port))
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
