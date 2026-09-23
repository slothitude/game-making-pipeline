# GMP Pipeline Gateway — the REST door for any agent

User directive: *"a full API so I can connect any agent to the full pipeline."*
One stdlib HTTP server (`gateway.py`, same shape as `cloud/queue/queue_api.py`)
that fronts the whole Pipeline. **Reads answer directly** (roster, diaries,
ladder); **anything that does work is ENQUEUED** into the job queue
(`POST <QUEUE_URL>/jobs`, X-Token from `QUEUE_TOKEN`). The gateway never runs
work itself — the serialize law holds.

```
any agent ──X-Token──▶ gateway :8902 ──▶ reads: games.json / DIARY.md / ladder.json
                                    └──▶ enqueues: POST 127.0.0.1:8901/jobs ──▶ router ──▶ executor
```

## Files

| File | What |
|---|---|
| `gateway.py` | stdlib HTTP API on `127.0.0.1:8902`, X-Token auth, fail closed |
| `games.json` | the real roster: slug, display name, diary path, live URLs |
| `tests/stub_queue.py` | canned queue on :8901 for local testing (no Postgres, no box) |

## Env

| Var | Meaning | Default |
|---|---|---|
| `GATEWAY_TOKEN` | X-Token clients must present. Unset = refuse to start. | — |
| `QUEUE_TOKEN` | X-Token the gateway presents to the queue | — |
| `QUEUE_URL` | queue base URL | `http://127.0.0.1:8901` |
| `GAME_ROOTS` | `os.pathsep` list of roots holding game checkouts; diary = `<root>/<diary from games.json>` | `C:/Users/aaron` |

## Endpoints (all JSON, prefix /api)

Every endpoint requires `X-Token: $GATEWAY_TOKEN` except `/api/health`
(mirrors the queue: health is open, data is not).

| Method | Path | Body | Reads / enqueues |
|---|---|---|---|
| GET | `/api/health` | — | queue reachability + counts |
| GET | `/api/status` | — | version, queue counts, games roster, ladder (no key routing) |
| POST | `/api/games` | `{name, template?, pitch?, requester?}` | enqueue `new_game` |
| GET | `/api/games/{slug}/diary` | — | DIARY.md: `{slug, entries_count, tail: last 40 lines}` |
| POST | `/api/orders/tunable` | `{game, tunable, new_value}` | enqueue `tune_tunable` |
| POST | `/api/orders/art` | `{game, prompt, asset_id?}` | enqueue `generate_art` |
| POST | `/api/critique` | `{game, mode?: llm\|vision\|full}` | enqueue `critique` |
| POST | `/api/playtest` | `{game, seconds? (1..7200, default 60)}` | enqueue `emulator` (the PlayerOne lane) |
| POST | `/api/gpu` | `{kind: train\|mesh\|render, staging_dir, return_dir, payload?}` | enqueue `gpu.<kind>` |
| POST | `/api/device_test` | `{game, apk?}` | enqueue `device_test` |
| POST | `/api/deploy` | `{game, target: retromonkey\|itch}` | enqueue `deploy` |
| GET | `/api/jobs?status=&limit=` | — | proxy → queue `GET /jobs` |
| GET | `/api/jobs/{id}` | — | proxy → queue `GET /jobs?limit=500`, filtered to one id |
| POST | `/api/ladder/refresh` | `{}` | enqueue `ladder_refresh` |

### The /api/jobs collision

The **queue owns public `/api/jobs`** — Caddy routes it to :8901 and stays that
way. The gateway's `/api/jobs` + `/api/jobs/{id}` exist anyway (they proxy the
queue) so an agent that only knows the gateway can still watch its jobs; Caddy
never routes public traffic to them.

### Wiring warning

Every enqueued type — `new_game`, `tune_tunable`, `generate_art`, `critique`,
`emulator`, `gpu.train|gpu.mesh|gpu.render`, `device_test`, `deploy`,
`ladder_refresh` — needs an entry in `cloud/queue/router.py`'s `EXECUTORS`.
An unwired type doesn't wait: the router **fails it permanently** on first
claim ("executor not wired yet"). Wire the executor before advertising the
endpoint.

## Local test (Rog, no Postgres, no ssh)

```
python cloud/api/tests/stub_queue.py --port 8901            # canned queue
GATEWAY_TOKEN=testtoken QUEUE_TOKEN=stubtoken \
GAME_ROOTS=C:/Users/aaron/AppData/Local/Temp/gmp_api_test \
  python cloud/api/gateway.py --port 8902                   # gateway under test

curl -s http://127.0.0.1:8902/api/health
curl -s -H "X-Token: testtoken" http://127.0.0.1:8902/api/status
curl -s -X POST -H "X-Token: testtoken" -H "Content-Type: application/json" \
  -d '{"name":"Moon Ferry","template":"arcade","requester":"claude"}' \
  http://127.0.0.1:8902/api/games                            # -> {"job_id": 99, ...}
curl -s -H "X-Token: testtoken" http://127.0.0.1:8902/api/games/sonar/diary
curl -s -X POST -H "X-Token: testtoken" -H "Content-Type: application/json" \
  -d '{"game":"sonar","tunable":"ping_speed","new_value":1.25}' \
  http://127.0.0.1:8902/api/orders/tunable
```

## Deploy (retromonkey)

### 1. Ship the code + token

```
scp -r cloud/api retromonkey:/home/ubuntu/gmp/cloud/
ssh retromonkey 'grep -q GATEWAY_TOKEN /home/ubuntu/gmp/cloud/.env || \
  echo "GATEWAY_TOKEN=$(openssl rand -hex 24)" >> /home/ubuntu/gmp/cloud/.env'
```

(`QUEUE_TOKEN` is already in `cloud/.env` for the queue; the gateway reads the
same file.)

### 2. systemd unit — mirrors gmp-queue-api

`/etc/systemd/system/gmp-pipeline-gateway.service`:

```ini
[Unit]
Description=GMP pipeline gateway (127.0.0.1:8902)
After=network-online.target gmp-queue-api.service
Requires=gmp-queue-api.service

[Service]
WorkingDirectory=/home/ubuntu/gmp/cloud/api
EnvironmentFile=/home/ubuntu/gmp/cloud/.env
Environment=QUEUE_URL=http://127.0.0.1:8901
Environment=GAME_ROOTS=/home/ubuntu
ExecStart=/usr/bin/python3 /home/ubuntu/gmp/cloud/api/gateway.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

(Only stdlib on this path — no `pip3 install` needed. `GAME_ROOTS=/home/ubuntu`
assumes game checkouts land at `/home/ubuntu/<slug>/`; adjust when the repo
volume story settles. Diaries that don't exist 404 gracefully.)

```
ssh retromonkey 'sudo systemctl daemon-reload && sudo systemctl enable --now gmp-pipeline-gateway'
ssh retromonkey 'systemctl status gmp-pipeline-gateway --no-pager'
ssh retromonkey 'journalctl -u gmp-pipeline-gateway -n 20 --no-pager'
```

### 3. Caddy — one handle pair, ordered

`/home/ubuntu/caddy/Caddyfile`, inside the `retromonkey.com.au` site block,
**above** the other handles (`/api/jobs*` is longer, so it wins; keep it first
anyway so a human reading the file sees the exception before the rule):

```caddy
	handle /api/jobs* {
		reverse_proxy 127.0.0.1:8901   # PUBLIC /api/jobs STAYS ON THE QUEUE
	}
	handle /api/* {
		reverse_proxy 127.0.0.1:8902   # the gateway owns every other /api path
	}
```

Then `ssh retromonkey 'sudo docker restart caddy'`.

**Container-loopback gotcha** (same one the `/drive` proxy hit): if the caddy
container sits on the bridge `web` network, `127.0.0.1` inside the container is
the container, not the host. Mirror whatever mechanism the existing
`/api/jobs → :8901` route already uses. If nothing is in place yet, pick one:

- run caddy with `--network host` (ports 80/443 straight on the host), or
- recreate the caddy container with `--add-host=host.docker.internal:host-gateway`
  and proxy to `host.docker.internal:8901/8902` — then the queue and gateway
  must listen on the bridge gateway address too, not just loopback (`--host`), or
- ship the gateway as a container on the `web` network (the filebrowser pattern)
  and proxy to it by container name.

### 4. Smoke it from outside

```
curl -s https://retromonkey.com.au/api/health
curl -s -H "X-Token: $GATEWAY_TOKEN" https://retromonkey.com.au/api/status
```
