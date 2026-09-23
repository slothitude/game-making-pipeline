# cloud/mp — Cloud Run multiplayer skeleton (Pipeline todo #88, G3)

Signaling + lobby + healthcheck for Pipeline P2P games, sized for checkers
(2 seats). One process, one port, one dependency. Runs on Cloud Run free tier;
per [cloud/multiplayer-plan.md](../multiplayer-plan.md) the server is **dumb
about WebRTC** — it shuttles opaque SDP/ICE envelopes and never touches game
traffic.

Design source: `../multiplayer-plan.md` (§5 behavior, §8 protocol, §14 security
laws) adapted to Cloud Run per `../free-google-plan.md` G3. The Node/retromonkey
variant in that plan is a separate deployment target — this is the Python/Cloud
Run one.

## Files

| file | what |
|---|---|
| `main.py` | whole server: WS `/signal`, REST `/lobby`, GET `/health` |
| `requirements.txt` | `aiohttp` (only dep — see the note in the file for why not `websockets`) |
| `Dockerfile` | `python:3.12-slim`, binds `0.0.0.0:$PORT` (Cloud Run law) |
| `smoke_test.py` | local battery (21 checks, boots the server itself) |

## Run locally

```bash
cd C:/Users/aaron/game-making-pipeline/cloud/mp

# dev: loopback only, auth open (loud log line says so)
python main.py --local --port 8080

# dev with the auth stub exercised:
MP_TOKEN=mysecret python main.py --local --port 8080
#   ...then send X-Token: mysecret (REST) or ?token=mysecret (WS handshake)

# full battery (boots its own server, sets MP_TOKEN, exits nonzero on any FAIL)
python smoke_test.py
```

Quick manual curls (server running with `MP_TOKEN=mysecret`):

```bash
curl -s http://127.0.0.1:8080/health
curl -s -H "X-Token: mysecret" http://127.0.0.1:8080/lobby
curl -s -H "X-Token: mysecret" -H "Content-Type: application/json" \
  -d '{"action":"create","game":"checkers","player":"Aaron"}' \
  http://127.0.0.1:8080/lobby
```

Docker (optional sanity check that the image itself runs):

```bash
docker build -t mp-signal .
docker run --rm -p 8080:8080 -e PORT=8080 -e MP_TOKEN=mysecret mp-signal
```

## Auth (STUB — replace, do not extend)

`X-Token` header (or `?token=` query on the WS handshake, because browsers
cannot set WS headers) checked against env `MP_TOKEN`. Mismatch → `401`.
`MP_TOKEN` unset → **open mode** (dev only; server logs a warning at boot).
`/health` is deliberately open — Cloud Run probes it.

Real auth is Telegram HMAC tokens minted by the bot per
`../multiplayer-plan.md` §7 (`bridge.py`, 10-min expiry, game-bound, base64url
padding gotcha). This file intentionally does **not** implement that yet.

## Protocols

### WS `/signal` — client → server (JSON, one object per message)

| type | fields | notes |
|---|---|---|
| `create` | `game`, `name`, `max` (opt) | makes a room; you are peer id `1` |
| `join` | `room`, `name` (opt) | assigned next free peer id |
| `leave` | — | also inferred on socket close |
| `offer` / `answer` / `ice` | `to` (peer id) + payload fields | relayed verbatim; `to` becomes `from` |
| `ping` | — | → `pong` |

Envelope cap 64 KB (over → socket closed, code 1009). Per-peer outbound queue
capped at 16 envelopes; a slow peer gets `error peer_slow`, the server never
becomes a memory amplifier.

### WS `/signal` — server → client

| type | fields | notes |
|---|---|---|
| `room_created` | `room`, `you`=1, `game`, `max`, `peers` | |
| `room_joined` | `room`, `you`, `game`, `peers` | `peers` = **full post-join roster** (incl. you) |
| `presence` | `event` join/leave, `room`, `peer`/`peer_id`, `peers` | broadcast on join/leave |
| `offer`/`answer`/`ice` | `from` + original payload fields | untouched |
| `left` | `room` | ack to leaver |
| `pong` | — | |
| `error` | `code`, `msg` | `bad_json`, `bad_type`, `not_in_room`, `room_full`, `room_not_found`, `already_in_room`, `too_big`, `peer_slow`, `too_many_rooms` |

**Peer-id convention** (plan §8): lower id initiates. For checkers: creator
(`1`) sends `offer`, joiner (`2`) answers.

Relay isolation (MP-B7): `to` is validated against the sender's **own** room
roster; cross-room ids are `error not_in_room`, nothing delivered.

### REST `/lobby`

| call | body | result |
|---|---|---|
| `GET /lobby` | — | `{"rooms":[{id, game, max, players[], peers, open, age_s}]}` |
| `POST /lobby` | `{"action":"create","game":"checkers","player":"Aaron","max":2}` | `201` `{"room":{...}}` |
| `POST /lobby` | `{"action":"join","room":"ABCD12","player":"Tash"}` | `200`, or `404 room_not_found`, `409 room_full` |

All `/lobby` calls require the token; errors are `401` with a JSON body.
Rooms are **in-memory**: a Cloud Run instance restart drops them (fine for a
skeleton; Firestore is the staged upgrade if we ever outgrow this).

Note the two rosters: `players` is the REST lobby view (names), `peers` is the
live WS view (signaling ids). They are independent in this skeleton — binding
them to one identity happens with the Telegram auth work.

## Deploy (Cloud Run)

Prereq: `gcloud auth login` (same one-time step as G1/Test Lab). Then, from
`C:/Users/aaron/game-making-pipeline/cloud/mp` (project + region are
placeholders — set the real project at deploy time):

```bash
gcloud run deploy mp-signal \
  --project <PROJECT_ID> \
  --region australia-southeast1 \
  --source . \
  --allow-unauthenticated \
  --set-env-vars MP_TOKEN=$(openssl rand -hex 16) \
  --cpu 1 --memory 128Mi --concurrency 80 \
  --min-instances 0 --max-instances 1
```

- `--allow-unauthenticated` is required: the app does its own token check
  (Cloud Run IAM invoker auth would break proxied browser/Godot WS handshakes).
  Keep `MP_TOKEN` strong; rotate by redeploying.
- `--max-instances 1` keeps the in-memory rooms coherent (two instances would
  split the room table). Free tier: this idles at zero cost.
- Grab the URL it prints, e.g. `https://mp-signal-<hash>-australia-southeast1.a.run.app`.

Smoke it: `curl -s https://mp-signal-<hash>-australia-southeast1.a.run.app/health`

## Caddy route (NEXT STEP — documented, not done)

On retromonkey (`/home/ubuntu/caddy/Caddyfile`, inside the existing
`retromonkey.com.au` site block), route `/mp*` at the Cloud Run URL, then
reload: `sudo docker exec caddy caddy reload --config /etc/caddy/Caddyfile`.

```caddy
	handle /mp* {
		# Cloud Run routes on Host — rewrite it to the run.app name, or the
		# request dies with 404. WS upgrades proxy transparently.
		reverse_proxy https://mp-signal-<HASH>-australia-southeast1.a.run.app {
			header_up Host {upstream_hostport}
		}
	}
```

Result: `wss://retromonkey.com.au/mp/signal` (wss terminates at Caddy, which is
mandatory for Godot **web** builds — mixed content blocks plain `ws://`).

## What remains for checkers-the-game (out of scope here)

This repo ships the plumbing only. The game itself still needs:

1. **Board rules** — move generation, legal-move validation, captures,
   double-jump, kinging; win/draw detection (40-move / repetition rule if we
   care).
2. **Authority model** — plan non-goal is "no authoritative server", so:
   host-authoritative (peer 1 validates and echoes moves) or lockstep; must be
   decided and enforced client-side.
3. **State sync over the P2P channel** — a small move/undo RPC pair on the
   `WebRTCMultiplayerPeer` reliable channel (16 KB chunks are already the law;
   a checkers move is ~a dozen bytes).
4. **`MultiplayerClient` GDScript class** (plan §9) — the poll-law wiring:
   `poll()` every frame for WS + every peer connection, `add_peer()` in
   `STATE_NEW`, no `create_answer()`, camelCase `iceServers`. Checkers is its
   second consumer (first is the mp-test app).
5. **Real auth** — `bridge.py` in the bot poll loop minting Telegram HMAC
   tokens (plan §7), then `/play checkers` → URL with `?t=`; this server's
   stub verifier gets swapped for the HMAC check.
6. **Caddy route + deploy** — the two NEXT STEP blocks above.
7. **Acceptance battery** — plan §13 checks (MP-B1..B9) re-pointed at this
   server; `smoke_test.py` covers the local subset (B1/B2/B7-style) only.
8. Optional later: STUN/TURN stays a retromonkey/coturn concern — Cloud Run
   cannot serve UDP 3478.
