# GMP Job Queue — the serialize law, as a service

The 956MB retromonkey box survives by **serializing work**: one job advances at a
time, everything else waits in the `jobs` table with status `queued`. This is
gmp-v2-plan §3.7 as code. Anything too heavy for the box (and anything destined
for free Google capacity — Test Lab, Colab T4, Cloud Run) gets **enqueued**, and
whoever can do the work claims it — on the box today, from anywhere tomorrow.

```
enqueue ──▶ jobs (Postgres) ──claim (one at a time)──▶ router ──▶ executor
   ▲                                                    │
   └────────────── work-orders / critiques / cron ──────┘
```

## Files

| File | What |
|---|---|
| `schema.sql` | the `jobs` table, idempotent (CREATE TABLE IF NOT EXISTS) |
| `queue_api.py` | stdlib HTTP API on `127.0.0.1:8901`, X-Token auth |
| `router.py` | dispatcher loop — claims ONE job, routes by type, reports back |
| `smoke_test.sh` | full semantics proof on a local sqlite file (no Postgres needed) |

Job statuses: `queued → running → done | failed`. Priority: **1 = highest**
(default 5). A job that **errors twice is failed permanently** (first error
requeues it; `attempts` counts). A not-wired executor fails its job immediately —
retrying can't wire it.

## 1. Install the schema (retromonkey)

Postgres creds: host `localhost`, db `gmp`, user `pipeline`, pw `slothitude2026`.

```
scp cloud/queue/schema.sql retromonkey:/home/ubuntu/gmp/cloud/queue/
ssh retromonkey 'PGPASSWORD=slothitude2026 psql -h localhost -U pipeline -d gmp -f /home/ubuntu/gmp/cloud/queue/schema.sql'
```

(If Postgres runs in a container there: `ssh retromonkey 'docker exec -i <pg-container> psql -U pipeline -d gmp' < cloud/queue/schema.sql`.)

Idempotent — safe to re-run. Verify:

```
ssh retromonkey 'PGPASSWORD=slothitude2026 psql -h localhost -U pipeline -d gmp -c "\d jobs"'
```

## 2. Ship the code

```
scp -r cloud/queue retromonkey:/home/ubuntu/gmp/cloud/
```

The API needs `psycopg2` on the box (`python3 -c "import psycopg2"` — if missing:
`pip3 install psycopg2-binary`, or bake it into the pipeline image).
Add one line to `cloud/.env` (gitignored, same file gmp-agent reads):

```
QUEUE_TOKEN=<openssl rand -hex 24>
```

## 3. systemd units (same pattern as gmp-agent)

`/etc/systemd/system/gmp-queue-api.service`:

```ini
[Unit]
Description=GMP job queue API (127.0.0.1:8901)
After=network-online.target

[Service]
WorkingDirectory=/home/ubuntu/gmp/cloud/queue
EnvironmentFile=/home/ubuntu/gmp/cloud/.env
Environment=PGHOST=localhost
Environment=PGDATABASE=gmp
Environment=PGUSER=pipeline
Environment=PGPASSWORD=slothitude2026
ExecStart=/usr/bin/python3 /home/ubuntu/gmp/cloud/queue/queue_api.py --backend pg
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/gmp-queue-router.service` (the always-on loop, 5s poll):

```ini
[Unit]
Description=GMP queue router (serial dispatcher)
After=network-online.target gmp-queue-api.service
Requires=gmp-queue-api.service

[Service]
WorkingDirectory=/home/ubuntu/gmp/cloud/queue
EnvironmentFile=/home/ubuntu/gmp/cloud/.env
ExecStart=/usr/bin/python3 /home/ubuntu/gmp/cloud/queue/router.py --loop --poll 5
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Timer variant (one job per tick, `--once` — for when you want the dispatcher
gated instead of always-on):

```ini
# /etc/systemd/system/gmp-queue-router.timer
[Unit]
Description=Tick the GMP queue router

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```
(the `.timer` drives a oneshot version of the unit: `Type=oneshot`,
`ExecStart=/usr/bin/python3 .../router.py --once`, no `Restart=`.)

Enable:

```
ssh retromonkey 'sudo systemctl daemon-reload && sudo systemctl enable --now gmp-queue-api gmp-queue-router'
ssh retromonkey 'systemctl status gmp-queue-api gmp-queue-router --no-pager'
ssh retromonkey 'journalctl -u gmp-queue-router -n 20 --no-pager'
```

## 4. curl — every endpoint

```bash
TOKEN=$(grep QUEUE_TOKEN cloud/.env | cut -d= -f2)
BASE=http://127.0.0.1:8901

# enqueue (returns {"id": 7})
curl -s -X POST $BASE/jobs -H "X-Token: $TOKEN" \
  -d '{"type":"gate","payload":{"game":"octogram-arcade","suites":7},"priority":1}'

# dashboard feed (filter + cap)
curl -s "$BASE/jobs?status=queued&limit=20" -H "X-Token: $TOKEN"

# a worker pops ONE job (highest priority, oldest first; safe with many workers)
curl -s -X POST $BASE/jobs/claim -H "X-Token: $TOKEN" \
  -d '{"types":["llm","gate","deploy"]}'

# report success (or {"error": "..."} to trigger the retry law; add
# "permanent": true to skip retries)
curl -s -X POST $BASE/jobs/7/result -H "X-Token: $TOKEN" -d '{"result":{"ok":true}}'

# health (no token)
curl -s $BASE/health
```

## 5. Job types → executors

Routed in `router.py` via the `EXECUTORS` dict:

| type | executor this build |
|---|---|
| `llm` | real stub — logs payload shape, reports done (the brain plugs in here) |
| `device_test` `gpu` `gate` `deploy` `emulator` `critique` | stubs — fail the job with "executor not wired yet" |

Wiring a real executor = one function `def exec_gate(job): ...` + one dict entry.

## 6. Smoke test (runs anywhere, no Postgres, no ssh)

```
cd cloud/queue && bash smoke_test.sh
```

Brings up the API on `--backend sqlite` (`test.db`, gitignore it or delete after),
proves auth, mixed-priority claim order (1 → 5 → 9), complete, the twice-and-failed
retry law, the router's llm stub, and the not-wired permanent failure.

Production stays on Postgres (`--backend pg`); sqlite exists only so the smoke
test runs on any machine (e.g. Rog, CPU-only, no Postgres).
