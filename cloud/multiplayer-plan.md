# Retromonkey Multiplayer — P2P WebRTC System Plan

*Two Godot players connect directly (low latency). retromonkey only does signaling,
STUN, and identity. Auth via the existing Telegram bot. Proven end-to-end with a
chat + file-transfer test app.*

Status: plan (nothing implemented yet). Target host: retromonkey
(168.138.8.0 / www.retromonkey.com.au, Oracle Cloud, 956 MB RAM + 4 GB swap,
Ubuntu 22.04, Docker 29.5.3). Builds on the stack in
[`retromonkey.md`](retromonkey.md) (caddy :80/:443, filebrowser loopback :8080)
and [`docker-compose.yml`](docker-compose.yml).

---

## 1. Goals and non-goals

**Goals**
- P2P gameplay between 2+ Godot clients (web export first, native second) over
  WebRTC data channels — the server never touches game traffic.
- Server footprint small enough for a 956 MB box: signaling + STUN ≤ ~80 MB combined.
- Identity: a Telegram user gets a signed token from the bot, the game presents it,
  the signaling server verifies it with no shared database.
- One reusable GDScript class (`MultiplayerClient`) any pipeline game can drop in.
- Test app proves the whole chain: auth → room → P2P → chat → 1 MB file with sha256 match.

**Non-goals (v1)**
- No dedicated/authoritative game server — games are lockstep or host-authoritative
  over the P2P mesh, their own business.
- No TURN TLS (oturn over DTLS UDP only), no voice/video, no spectator slots.
- No lobby browser/matchmaking beyond 6-char room codes.
- No persistent accounts — Telegram id + display name is the whole identity.

---

## 2. Architecture

```
                        retromonkey  168.138.8.0  (www.retromonkey.com.au)
┌──────────────────────────────────────────────────────────────────────────────┐
│                                                                              │
│  Telegram cloud ══ long-poll ══▶ gmp-pipeline (the bot)                      │
│                                     │  /play <game>                          │
│                                     │  mints stateless HMAC token            │
│                                     ▼ (token travels in the chat reply)      │
│  caddy :443 ────── /signal* ──────▶ gmp-signaling :3000 (Node 20 + ws)       │
│   wss://retromonkey.com.au/signal      rooms · relay SDP/ICE · verify token  │
│                                                                              │
│  gmp-stun (coturn) :3478/udp+tcp ◀──── ICE server-reflexive candidates       │
│        (TURN relay config ready, off by default in v1)                       │
└──────────────────────────────────────────────────────────────────────────────┘
          ▲  wss — control only (auth, room, SDP, ICE)   ▲
          │                                              │
 ┌────────┴──────────┐                          ┌────────┴──────────┐
 │  Player 1 (Godot) │ ◀═════ WebRTC P2P ═════▶ │  Player 2 (Godot) │
 │  web / native     │   DTLS data channel      │  web / native     │
 └───────────────────┘   (chat + file chunks)   └───────────────────┘

  Game bytes never touch retromonkey. Once the P2P channel is up, the
  signaling server can even go down without dropping the session.
```

**Traffic classes**
| Path | Carries | Volume |
|---|---|---|
| Bot ↔ Telegram | `/play`, replies | negligible |
| Client ↔ signaling (wss) | auth, room join/leave, SDP, ICE, pings | a few KB per session |
| Client ↔ coturn (udp 3478) | STUN binding requests | negligible |
| Client ↔ client (WebRTC DTLS) | chat, file chunks, gameplay | direct P2P (or TURN relay if enabled and P2P fails) |

---

## 3. Key decisions (and what was rejected)

| Decision | Alternative rejected | Why |
|---|---|---|
| **Node.js 20 + `ws`** for signaling | Python `websockets` (~35 MB vs ~55 MB) | Battle-tested signaling pattern; the owner's draft; RAM delta of 20 MB is noise on this box with 4 GB swap. Node `crypto` covers HMAC verification with zero deps beyond `ws`. |
| **Stateless HMAC-signed tokens** (no shared DB) | Redis, SQLite, or shared JSON file between bot and signaling | The bot (Python) and signaling (Node) would need a cross-language store — a file is race-prone, Redis costs RAM, `node:sqlite` isn't in Node 20. HMAC over the raw token string needs **no canonicalization** and **no I/O** on the hot path. |
| **Same-origin path `wss://retromonkey.com.au/signal`** behind Caddy | Direct port `ws://168.138.8.0:3000`, or new `ws.` subdomain | Godot **web** builds on an `https://` page cannot open plain `ws://` (mixed content) — TLS is mandatory, and Caddy already terminates TLS and proxies WebSocket upgrades transparently. No new DNS record, cert reuses the existing one. Loopback `:3000` stays bound for ssh-tunnel debugging. |
| **`WebRTCMultiplayerPeer.create_mesh()`** | star topology (`create_server`/`create_client` with host id 1) | 2-player MVP is symmetric and simpler as a mesh; host-authoritative games can still treat peer 1 as authority by convention. |
| **coturn with `network_mode: host`** | bridge + `external-ip` + relay port mapping | On a single-NIC VPS with one public IP, host mode removes the whole class of "TURN allocations bind the docker-internal IP" failures. coturn needs nothing on the `web` network. |
| **STUN only in v1, TURN config staged but off** | TURN on day one | Requirement is STUN/ICE. Two players on symmetric NAT (phone hotspot vs hotel wifi) is the failure case TURN covers — config ships in the plan, one env flip to enable, not a v1 gate. |
| **16 KB file chunks over the reliable data channel** | single-packet files, or unreliable chunks | Browser SCTP message size limits + Godot's `add_peer()` channel model make 16 KB reliable chunks trivially correct for <1 MB files; reassembly verified by sha256. |
| **Room codes, cap 2–4 peers** | auto-matchmaking | The test app needs exactly "two people in the same place". Codes are debuggable and isolate rooms by construction. |

---

## 4. Resource budget (the 956 MB question)

Estimates, measured post-deploy:

| Service | Image | RAM est. | `mem_limit` |
|---|---|---|---|
| caddy (running) | `caddy:latest` | ~30 MB | — (existing) |
| filebrowser (running) | `filebrowser/filebrowser:latest` | ~25 MB | — (existing) |
| gmp-pipeline + gmp-godot (sleeping) | python:3.12-slim / godot-ci | ~60–150 MB | (per pipeline plan) |
| **gmp-signaling** | `node:20-slim` + `ws` | ~55 MB | **128m** |
| **gmp-stun** | `coturn/coturn` | ~10 MB | **64m** |
| **Total new** | | **~65 MB** | |

Node gets `--max-old-space-size=96` so V8's heap cap tracks the cgroup limit
(Node sizes its heap from *system* RAM by default and would otherwise let the
OOM killer reap it). Both new containers log with `max-size: 10m` rotation —
a 956 MB box has no room for unbounded json-file logs.

---

## 5. Component A — Signaling server (`gmp-signaling`)

**Role**: authenticate tokens, hand out rooms, relay SDP offers/answers and ICE
candidates between peers of the same room. It is deliberately dumb about WebRTC —
it never parses SDP, it shuttles opaque payloads.

**Files** (deployed under `/home/ubuntu/gmp/signaling/` on retromonkey):

```
signaling/
├── Dockerfile        # build step exists because `ws` must be npm-installed
├── package.json      # { "dependencies": { "ws": "^8" } }
└── server.js         # single file, ~200 lines
```

> The draft compose (`image: node:20-slim`, volume mount, `command: node server.js`)
> has a gap: `ws` wouldn't be installed in the bare image. Building a 3-line
> Dockerfile with `npm install --omit=dev ws` keeps the image self-contained and
> the repo free of vendored `node_modules`.

**Dockerfile**
```dockerfile
FROM node:20-slim
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY server.js ./
ENV NODE_ENV=production
CMD ["node", "--max-old-space-size=96", "server.js"]
```

**Law constants** (named, per house law — all tunables in one block at the top
of `server.js`):

```js
const LAW_PORT          = 3000;
const LAW_ENVELOPE_MAX  = 64 * 1024;   // bytes, one ws JSON message
const LAW_MAX_ROOMS     = 32;
const LAW_MAX_PEERS     = 4;           // test app uses 2
const LAW_ROOM_TTL_MS   = 2 * 3600e3;  // empty rooms reaped; rooms reaped at 2h regardless
const LAW_PING_TIMEOUT  = 75e3;        // drop silent connections
const LAW_RELAY_QUEUE   = 16;          // max buffered outbound envelopes per peer
const LAW_TOKEN_TTL_S   = 600;         // must match bot side
const LAW_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"; // no 0/O/1/I/L
const LAW_CODE_LEN      = 6;
```

**Behavior spec**
- One `ws.Server`; every connection must send `auth` within 10 s or is closed.
- Token verified per **Component C**. On success the connection is bound to
  `{tgId, name}` and gets a room-scoped numeric peer id when it joins a room.
- Rooms keyed by 6-char code; creator = peer id `1`, joiners `2, 3, …`.
  A code colliding with a live room is redrawn (alphabet is 31 chars →
  collision odds negligible at ≤32 rooms).
- `relay` envelopes are only ever delivered to peers **in the same room**; the
  server validates `to` against the room roster. No broadcast primitive is
  exposed (prevents one peer flooding the rest).
- Relay drop policy: if the target socket's buffered amount exceeds
  `LAW_RELAY_QUEUE` messages, the envelope is dropped and the sender gets
  `error {code:"peer_slow"}` — signaling must never become a memory amplifier.
- Heartbeat: client `{type:"ping"}` every 30 s; server closes at
  `LAW_PING_TIMEOUT` of silence. Half-open sockets are the #1 way a tiny box
  accumulates zombie state.
- Health endpoint: plain HTTP `GET /health` → `200 {"ok":true,"rooms":N,"peers":M}`
  for the compose healthcheck and for `ssh retromonkey 'curl -s localhost:3000/health'`.
- Logging: one line per state change (connect, auth ok/fail, create/join/leave,
  relay queue drops) to stdout. **Never log token values.**

---

## 6. Component B — STUN/TURN (`gmp-stun`, coturn)

**Role v1**: STUN only — lets both peers learn `srflx` (public ip:port)
candidates so they can punch through cone/restricted NATs.
**Staged**: TURN relay for the symmetric-NAT failure case (config shipped,
disabled by env in v1).

**Ports**: `3478/udp` + `3478/tcp` (STUN/TURN standard). TURN relay range
`49160–49200/udp` — staged with TURN. TURNS (`5349`) deliberately not opened
(no cert plumbing for coturn in v1).

**`turnserver.conf`**
```conf
listening-port=3478
fingerprint
# --- TURN (staged: flip LAW_TURN_ENABLED on the clients + uncomment these) ---
# lt-cred-mech
# use-auth-secret
# static-auth-secret=<TURN_SECRET from .env>
# REST scheme: user "exp:<rand>", pass = base64(hmac_sha1(secret, user))
realm=retromonkey.com.au
min-port=49160
max-port=49200
# relay abuse guards: never relay into private space
no-multicast-peers
denied-peer-ip=10.0.0.0-10.255.255.255
denied-peer-ip=172.16.0.0-172.31.255.255
denied-peer-ip=192.168.0.0-192.168.255.255
denied-peer-ip=127.0.0.0-127.255.255.255
no-cli
no-tls
no-dtls-tls
```

**Docker**: `network_mode: host`, `mem_limit: 64m`. In host mode coturn sees the
real interface so relay allocations bind the public IP — no `external-ip` juggling.

> **Client cost note**: `iceServers` in Godot maps 1:1 to the browser/native
> implementations. STUN entries are `"urls"` only; TURN entries add
> `"username"`/`"credential"` — minted client-side per the REST scheme above with
> a short `exp`, so no long-lived TURN password ever ships in a build.

---

## 7. Component C — Telegram auth bridge (stateless tokens)

**Flow**

```
1. Player → bot:        /play mp-test
2. Bot (gmp-pipeline poll loop):
   - allowlist check (v1: CHAT_ID only — matches the hub's invite-mode law)
   - mints token for {tg_id, first_name, game:"mp-test", exp: now+600s}
   - replies:  "https://retromonkey.com.au/games/mp-test/?t=<token>"
3. Player opens the URL (web build reads `t` from location.search;
   native builds paste the token into the UI field).
4. Game → wss signaling: {type:"auth", game:"mp-test", token}
5. Signaling verifies HMAC + expiry + game binding → {type:"auth_ok", ...}
6. Room create/join as usual.
```

**Token format (the contract between Python and Node)**

```
body = base64url_nopad( utf8( json({ "v":1, "tg":<int>, "nm":<str ≤32>,
                                     "g":"<game slug>", "exp":<unix>,
                                     "jti":<8 hex rand> }) ) )
sig  = base64url_nopad( hmac_sha256(SIGNING_SECRET, body)[0:16] )
token = body + "." + sig
```

- HMAC is computed over the **raw body string as transmitted** — the verifier
  parses the original string, so no JSON canonicalization is needed. This is the
  whole reason cross-language signing works here.
- **Padding gotcha**: Python's `base64.urlsafe_b64encode` emits `=`, Node's
  `buffer.toString("base64url")` does not. Strip `=` on the Python side;
  Node's `Buffer.from(s, "base64url")` tolerates stripped input. This mismatch
  is the most likely auth-bridge bug — see §14.
- `exp` enforced by the signaling server (not the client). `g` binds a token to
  one game slug so an mp-test token can't join an octogram room.

**Bot side** (new module `cloud/multiplayer/bridge.py`, called from the
`cloud_editor` poll loop's command dispatcher; the poll loop is already the bot):

```python
import base64, hashlib, hmac, json, secrets, time

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

def mint_token(tg_id: int, name: str, game: str, ttl: int = 600) -> str:
    body = {"v": 1, "tg": tg_id, "nm": name[:32], "g": game,
            "exp": int(time.time()) + ttl, "jti": secrets.token_hex(4)}
    raw = _b64(json.dumps(body, separators=(",", ":")).encode())
    sig = _b64(hmac.new(SIGNING_SECRET.encode(), raw.encode(), hashlib.sha256).digest()[:16])
    return f"{raw}.{sig}"
```

**Signaling side** (Node):
```js
const crypto = require("crypto");
const nowSec = () => Math.floor(Date.now() / 1000);

function verifyToken(token, game) {
  const [body, sig] = String(token).split(".");
  if (!body || !sig || sig.includes(".")) return null;
  const want = crypto.createHmac("sha256", process.env.SIGNING_SECRET)
                     .update(body).digest().subarray(0, 16);
  let got; try { got = Buffer.from(sig, "base64url"); } catch { return null; }
  if (got.length !== want.length || !crypto.timingSafeEqual(got, want)) return null;
  let p; try { p = JSON.parse(Buffer.from(body, "base64url").toString("utf8")); }
  catch { return null; }
  if (p.v !== 1 || p.exp < nowSec() || p.g !== game) return null;
  return p;                                  // { tg, nm, g, exp }
}
```

**Env additions to `cloud/.env`** (gitignored — secrets stay out of the repo):
```
SIGNING_SECRET=<openssl rand -hex 32>
TURN_SECRET=<openssl rand -hex 32>        # staged, unused in v1
```

**Fallback**: if the pipeline poll loop isn't the bot's home when this lands,
the same 20-line mint function drops into a standalone `gmp-bridge` container
(python:3.12-slim, stdlib only, same env). Prefer extending the poll loop.

---

## 8. Message protocol (signaling, JSON over wss)

Envelope rules: every message is a single JSON object with `type`. Client→server
messages other than `auth`/`ping` are rejected before auth; `relay`/room ops
rejected outside a room. Server→client `error` carries `{type, code, msg}` with
codes: `bad_token`, `token_expired`, `game_mismatch`, `not_in_room`,
`room_full`, `room_not_found`, `peer_slow`, `too_big`, `rate_limited`.

### Client → Server
| type | fields | notes |
|---|---|---|
| `auth` | `game`, `token` | first message; 10 s window |
| `ping` | — | every 30 s |
| `create_room` | — | peer becomes id 1 |
| `join_room` | `room_id` | assigned next free id |
| `leave_room` | — | also on socket close (server infers) |
| `relay` | `to` (peer id), `payload` (opaque object) | SDP/ICE only, ≤ ~48 KB |

Example `relay` (initiator → peer 2, offer):
```json
{ "type": "relay", "to": 2,
  "payload": { "kind": "sdp", "sdp_type": "offer",
               "sdp": "v=0\r\n..." } }
```
```json
{ "type": "relay", "to": 2,
  "payload": { "kind": "ice", "media": "application", "index": 0,
               "candidate": "candidate:8421... typ srflx ..." } }
```

### Server → Client
| type | fields | notes |
|---|---|---|
| `auth_ok` | `you` `{tg, name}` | token valid |
| `room_created` | `room_id`, `you_are` (1) | |
| `room_joined` | `room_id`, `you_are`, `peers` `[{id, name}]` | roster at join |
| `peer_joined` | `peer_id`, `name` | |
| `peer_left` | `peer_id` | |
| `relay` | `from`, `payload` | unmodified payload |
| `error` | `code`, `msg` | |
| `pong` | — | |

**Peer-id convention**: lower id initiates. When peer N joins, every existing
peer `i < N` opens the offer toward N. For the 2-player test app: creator (1)
offers, joiner (2) answers. Deterministic, no glare.

### File transfer (runs P2P over the data channel — never through signaling)

Godot high-level multiplayer RPCs on top of `WebRTCMultiplayerPeer`, reliable
channel, 16 KB chunks:

| RPC | payload | notes |
|---|---|---|
| `mp_file_start(name: String, size: int, chunk_count: int, sha256: String)` | | header; receiver preallocates `PackedByteArray` |
| `mp_file_chunk(file_id: int, idx: int, data: PackedByteArray)` | 16 KB | `LAW_CHUNK = 16384`, pipelined up to `LAW_WINDOW = 32` in flight |
| `mp_file_done(file_id: int)` | | receiver verifies sha256 → emits `file_received` |
| `mp_chat(text: String)` | ≤ 512 chars | chat itself |

`file_id` (small int, sender-assigned) allows interleaved transfers later; v1
sends one at a time.

---

## 9. Component D — Godot client library (`MultiplayerClient`)

One reusable class, `class_name MultiplayerClient extends Node`, per pipeline
game; the test app is its first consumer.

**Public API**
```gdscript
# lifecycle
func configure(signaling_url: String, ice_urls: Array) -> void
func authenticate(token: String, game: String) -> void       # → auth_ok / auth_failed
func create_room() -> void                                    # → room_created(code)
func join_room(code: String) -> void                          # → room_joined
func leave_room() -> void
func shutdown() -> void

# game data (P2P only)
func send_chat(text: String) -> void
func send_file(file_name: String, data: PackedByteArray) -> void

# signals
signal auth_ok(my_info: Dictionary)
signal auth_failed(code: String)
signal room_created(code: String)
signal room_joined(code: String, peers: Array)
signal peer_joined(peer_id: int, peer_name: String)
signal peer_left(peer_id: int)
signal p2p_ready(ready: bool, path: String)        # path: "host"|"srflx"|"relay"|"unknown"
signal chat_received(from_name: String, text: String)
signal file_progress(file_name: String, got: int, total: int)
signal file_received(file_name: String, data: PackedByteArray)
signal log_line(text: String)                      # test app prints these
```

**Implementation outline** (the parts that must be exactly right; full file
lands with the test app):

```gdscript
const LAW_SIGNALING_URL := "wss://retromonkey.com.au/signal"
const LAW_ICE_URLS := ["stun:retromonkey.com.au:3478"]   # + TURN pair when staged
const LAW_CHUNK := 16384
const LAW_WINDOW := 32
const LAW_PING_SEC := 30.0

var _ws := WebSocketPeer.new()
var _mesh := WebRTCMultiplayerPeer.new()
var _conns := {}        # peer_id -> WebRTCPeerConnection
var _my_id := 0
var _room := ""
var _peers := {}        # peer_id -> name
var _path := "unknown"  # best candidate type seen locally

# ---- THE POLL LAW: everything here is poll-driven. Forgetting this is the
# ---- classic Godot WebRTC failure ("it just never connects").
func _process(delta: float) -> void:
    _ws.poll()
    while _ws.get_ready_state() == WebSocketPeer.STATE_OPEN \
            and _ws.get_available_packet_count() > 0:
        _on_ws_message(_ws.get_packet())
    for id in _conns:
        var pc: WebRTCPeerConnection = _conns[id]
        pc.poll()

func authenticate(token: String, game: String) -> void:
    _ws.connect_to_url(LAW_SIGNALING_URL)
    await _ws_open()                      # helper polling ready_state
    _send({"type": "auth", "game": game, "token": token})

# ---- WebRTC wiring. Order matters:
#  1) pc.initialize({"iceServers": [{"urls": LAW_ICE_URLS}]})
#  2) _mesh.create_mesh(_my_id)                       # ids 1..N allowed in mesh
#  3) _mesh.add_peer(pc, remote_id)  ← MUST happen while pc is STATE_NEW;
#       WebRTCMultiplayerPeer creates its three channels itself — do NOT
#       pre-create data channels by hand when used via the mesh.
#  4) lower peer id calls pc.create_offer()
func _make_pc(remote_id: int, initiator: bool) -> void:
    var pc := WebRTCPeerConnection.new()
    pc.initialize({"iceServers": [{"urls": LAW_ICE_URLS}]})
    pc.session_description_created.connect(_on_sdp.bind(remote_id))
    pc.ice_candidate_created.connect(_on_ice.bind(remote_id))
    _conns[remote_id] = pc
    _mesh.add_peer(pc, remote_id)         # STATE_NEW, before negotiation
    if initiator:
        pc.create_offer()

func _on_sdp(type: String, sdp: String, remote_id: int) -> void:
    var pc: WebRTCPeerConnection = _conns[remote_id]
    pc.set_local_description(type, sdp)
    _relay(remote_id, {"kind": "sdp", "sdp_type": type, "sdp": sdp})

# Answers are implicit: feeding an "offer" into set_remote_description makes
# session_description_created fire with the answer. There is no create_answer().
func _on_relay_sdp(from_id: int, p: Dictionary) -> void:
    var pc: WebRTCPeerConnection = _conns[from_id]
    pc.set_remote_description(p.sdp_type, p.sdp)

func _on_ice(media: String, index: int, cand: String, remote_id: int) -> void:
    _note_path(cand)                      # "typ host"/"srflx"/"relay" → _path
    _relay(remote_id, {"kind": "ice", "media": media,
                       "index": index, "candidate": cand})

func _on_relay_ice(from_id: int, p: Dictionary) -> void:
    _conns[from_id].add_ice_candidate(p.media, p.index, p.candidate)

# p2p_ready fires when every _conns[id] reports STATE_CONNECTED and the mesh
# has all room peers — then multiplayer.multiplayer_peer = _mesh and RPCs work.
```

**Godot-side gotchas this outline encodes**
- `poll()` every frame for the WebSocket **and every** `WebRTCPeerConnection`.
- `add_peer()` in `STATE_NEW`; channels are the mesh's job.
- No `create_answer()`; no `get_channel()` — keep your own `_conns` map.
- ICE config keys are camelCase (`"iceServers"`, `"urls"`, `"credential"`).
- Token from URL on web, pasted on native:
  ```gdscript
  func _read_token() -> String:
      if OS.has_feature("web"):
          return str(JavaScriptBridge.eval(
              "new URLSearchParams(window.location.search).get('t') || ''"))
      for a in OS.get_cmdline_user_args():
          if a.begins_with("--token="):
              return a.substr(8)
      return ""
  ```
- Web builds: official 4.7.1 export templates include the WebRTC module — no
  extra export flag. Native builds use libdatachannel (STUN/TURN both work).
- `p2p_ready`'s `path` is derived from locally seen candidate types — it is an
  honest *approximation* of the selected pair (Godot doesn't expose the
  selected candidate pair); the test app labels it as such.

---

## 10. Component E — Test app: chat + file send

**Purpose**: the green battery for the whole system. Scene:
`games/mp-test/` → deployed to `/home/ubuntu/site/games/mp-test/` like any game.

**Wireframe (1024×600, works at phone width)**

```
┌────────────────────────────────────────────────────────────────────┐
│ mp-test  ·  signaling: ● wss connected   p2p: ● connected (srflx)  │
│            you: Aaron (id 1)             peer: Tash (id 2)         │  ← status bar, 3 LEDs
├────────────────────────────────────────────────────────────────────┤
│  [ token: ______________________________ ] [Connect via Telegram]  │  ← hidden on web (auto)
│  [ Create room ]  room: K7QM2P   [Copy code]                       │
│  [ Join room ]    code: [______]                                   │
├────────────────────────────────────────────────────────────────────┤
│  chat log (RichTextLabel, autoscroll)                              │
│  ─ Aaron: ping                                                     │
│  ─ Tash: pong                                                      │
│  ─ system: Tash joined (id 2)                                      │
│  ─ system: p2p up — path srflx                                     │
├────────────────────────────────────────────────────────────────────┤
│  [ message ______________________________ ] [Send]                 │
│  [Send test image (~0.4 MB res:// icon)]  ▓▓▓▓▓▓▓▓░░ 73%           │
│  received: tash_icon.png  0.4 MB  sha256 OK ✓ (saved to user://)   │
└────────────────────────────────────────────────────────────────────┘
```

- **Status LEDs** map to the real states: `ws ready_state`, mesh/pc states,
  `_path` approximation. "relay mode" shows when the path reads `relay`
  (only observable once TURN is staged).
- **File source**: v1 sends a bundled `res://` image (`Texture2D.get_image()
  .save_png_to_buffer()`) — no file picker needed. Native builds may add an
  `FileDialog`; web user-file picking (HTML5 input via `JavaScriptBridge`) is a
  stretch goal, not a gate.
- Received files are written under `user://mp-test/` and sha256-checked before
  the "OK ✓" line prints.

---

## 11. Docker Compose addition (final draft)

Appended to [`docker-compose.yml`](docker-compose.yml) (changes from the owner's
draft: build step for `ws`, `web` network so Caddy can reach it, memory limits,
healthcheck, log rotation; coturn in host mode).

```yaml
  # === MULTIPLAYER SIGNALING (P2P WebRTC) ===
  # wss://retromonkey.com.au/signal → caddy → this container
  signaling:
    build: ./signaling
    container_name: gmp-signaling
    restart: unless-stopped
    environment:
      - SIGNING_SECRET=${SIGNING_SECRET}
    ports:
      - "127.0.0.1:3000:3000"        # loopback debug/health only; public = via caddy
    mem_limit: 128m
    healthcheck:
      test: ["CMD", "node", "-e",
             "fetch('http://localhost:3000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
      interval: 30s
      timeout: 5s
      retries: 3
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
    networks:
      - web

  # === STUN (coturn) — host networking so relay allocations bind the public IP ===
  # NOTE: network_mode: host ignores `ports:`; 3478/udp+tcp open directly on the host.
  stun:
    image: coturn/coturn:latest       # pin the digest at deploy time
    container_name: gmp-stun
    restart: unless-stopped
    network_mode: host
    command: >
      -n --listening-port=3478 --fingerprint
      --min-port=49160 --max-port=49200
      --no-multicast-peers --no-cli --no-tls --no-dtls-tls
      --denied-peer-ip=10.0.0.0-10.255.255.255
      --denied-peer-ip=172.16.0.0-172.31.255.255
      --denied-peer-ip=192.168.0.0-192.168.255.255
      --denied-peer-ip=127.0.0.0-127.255.255.255
    # TURN staged (v2): add --lt-cred-mech --use-auth-secret
    #   --static-auth-secret=${TURN_SECRET} --realm=retromonkey.com.au
    mem_limit: 64m
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
```

(`coturn` can equally take the conf file from §6 via a bind mount; the flag
form keeps the diff in one file. Pick one at deploy — not both.)

**Caddyfile addition** (`/home/ubuntu/caddy/Caddyfile`, inside the existing
`retromonkey.com.au` site block, before the root/games handler):

```caddy
	handle /signal* {
		reverse_proxy signaling:3000
	}
```

Reload without downtime: `sudo docker exec caddy caddy reload --config /etc/caddy/Caddyfile`.

---

## 12. Deployment steps (ordered)

1. **Firewall — OCI Security List/NSG** (console): ingress `3478/udp` + `3478/tcp`
   from `0.0.0.0/0` (source-restrict later if desired). Staged: `49160–49200/udp`.
2. **Firewall — host iptables** (OCI Ubuntu images ship a REJECT-all INPUT beyond
   22/80/443 — this bites everyone):
   ```bash
   ssh retromonkey
   sudo iptables -I INPUT -p udp --dport 3478 -j ACCEPT
   sudo iptables -I INPUT -p tcp --dport 3478 -j ACCEPT
   sudo netfilter-persistent save
   ```
3. **Files**: create `/home/ubuntu/gmp/signaling/{Dockerfile,package.json,server.js}`
   (scp from `C:\Users\aaron\game-making-pipeline\cloud\signaling\`). Add
   `SIGNING_SECRET` to `/home/ubuntu/gmp/.env`.
4. **Compose**: append the two services from §11 to `/home/ubuntu/gmp/docker-compose.yml`,
   then `sudo docker compose up -d --build signaling stun`.
5. **Verify signaling**: `curl -s localhost:3000/health`; then from Rog,
   `wss` smoke test through Caddy (`curl -si https://retromonkey.com.au/signal -H 'Upgrade: websocket' -H 'Connection: Upgrade' -H 'Sec-WebSocket-Key: x' -H 'Sec-WebSocket-Version: 13' | head -5` → expect `101`).
6. **Verify STUN** (external path, both must pass):
   - from inside the container against the public name:
     `sudo docker exec gmp-stun turnutils_stunclient retromonkey.com.au`
   - browser: webrtc.github.io/samples trickle-ice with `stun:retromonkey.com.au:3478`
     → expect an `srflx` candidate.
7. **Caddyfile**: add the §11 `handle /signal*` block, `caddy reload`.
8. **Bot**: add `/play` handling + `bridge.py` to the poll loop, restart
   `gmp-pipeline`, then in Telegram: `/play mp-test` → expect a URL with `?t=`.
9. **Test app**: build the web export, `scp -r` to `/home/ubuntu/site/games/mp-test/`.
10. **Burn the battery** (§13) — green or it didn't happen.

Rollback: `docker compose down signaling stun`, revert the Caddyfile block,
reload — nothing else on the box is touched.

---

## 13. Acceptance battery (MP-B*, per house law)

| # | Check | Green means |
|---|---|---|
| MP-B1 | `auth` with garbage token | `error bad_token`, socket closed, nothing logged that looks like a token |
| MP-B2 | expired token (mint with `ttl=1`, wait) | `error token_expired` |
| MP-B3 | STUN binding from an external network | `srflx` candidate produced (step 6) |
| MP-B4 | two browsers, same LAN | chat round-trips, path `host` |
| MP-B5 | two peers, different networks (phone hotspot vs home wifi) | chat round-trips, path `srflx` — **the real gate** |
| MP-B6 | 1 MB file, both directions | sizes match, sha256 match, "OK ✓" |
| MP-B7 | room isolation: join room A, send `relay` to a peer id that exists in room B | `error not_in_room`, nothing delivered |
| MP-B8 | signaling container stopped **after** P2P is up | chat keeps flowing (proves P2P independence); on reconnect, room re-join works |
| MP-B9 | token minted for game `octogram` used against `mp-test` | `error game_mismatch` |

---

## 14. Security considerations

- **Tokens**: HMAC-SHA256 (16-byte tag), 10-min expiry, game-bound, timing-safe
  compare, single-use-ish in practice (`jti` present for future replay caching).
  Never logged; never stored; invalid tokens close the socket.
- **Bot allowlist**: `/play` honored only from `CHAT_ID` in v1 (hub invite-mode
  law). Rate-limit per chat id (e.g. 5 tokens/min) before opening it up.
- **Room isolation**: server-side roster check on every `relay`; no broadcast;
  codes from an unambiguous alphabet; empty rooms reaped; 2 h ceiling.
- **Transport**: TLS terminates at Caddy (wss), P2P is DTLS — game traffic is
  encrypted both hops. Signaling binds `127.0.0.1` publicly unreachable; only
  the Caddy proxy path is exposed.
- **Abuse limits**: 64 KB envelope cap, per-socket relay queue cap
  (`peer_slow`), ping timeout reap, `mem_limit` so any leak is contained by the
  cgroup, not the box.
- **TURN (staged)**: shared-secret REST credentials with short `exp`; relay
  denied into RFC1918/loopback ranges so the instance can't be used as an
  internal-network prober. TURN bandwidth counts against Oracle egress — fine
  for a fallback, a reason to keep it off by default.
- **Secrets**: `SIGNING_SECRET`/`TURN_SECRET` in `.env` (gitignored, per the
  repo's `.gitignore`), injected via compose `environment`. Rotating the signing
  secret just invalidates outstanding tokens (≤10 min window).
- **PII**: the only identity data is Telegram numeric id + first name (≤32 chars).
- **Origin header**: browsers send `Origin: https://retromonkey.com.au`, natives
  send none — treat as advisory logging only in v1; token auth is the gate.

---

## 15. Phases

| Phase | Scope | Gate |
|---|---|---|
| MP-P0 | signaling server + compose + Caddyfile + STUN, MP-B1/B2/B3 | health + srflx |
| MP-P1 | `MultiplayerClient` + test app skeleton, auth, rooms, MP-B4 | chat on LAN |
| MP-P2 | file transfer + MP-B5/B6 | **cross-network chat + sha256 OK** |
| MP-P3 | bot `/play` + bridge, MP-B7/B8/B9 | full battery green |
| MP-P4 (staged) | TURN on (`TURN_SECRET`, relay range), TURN creds in client | symmetric-NAT peer reaches `relay` path |
| MP-P5 (later) | lobby listing, >4 rooms tuning, subdomain `ws.` if path proxying ever hurts, native mobile builds | — |

---

## 16. What needs the most careful implementation

1. **The WebRTC wiring in GDScript (§9)** — the entire class of "it never
   connects" bugs lives here: `poll()` every frame for every peer connection;
   `add_peer()` only in `STATE_NEW` (the mesh creates the channels, not you);
   answers come from `set_remote_description("offer", ...)`, there is no
   `create_answer()`; camelCase `"iceServers"`. Get these four wrong and nothing
   works with no error to read.
2. **Cross-language token parity (§7)** — base64url padding (`=`) differs between
   Python and Node; the HMAC must be over the raw body string, never a
   re-serialized object; truncated 16-byte tag on both sides. MP-B1 catches it.
3. **coturn reachability on Oracle Cloud (§12.1–2)** — two firewalls (OCI
   security list + host iptables REJECT-all) must both open 3478/udp, or STUN
   silently returns nothing and everything downstream looks broken. MP-B3
   isolates it.
4. **Signaling memory discipline** — envelope caps, relay queue drops, ping
   reaps, `mem_limit`. On 956 MB the server must stay boring.
5. **Mixed content** — web builds *must* speak `wss` via the Caddy path; any
   stray `ws://` or `:3000` reference in a web build will fail only in
   production (browsers), never in editor testing.
