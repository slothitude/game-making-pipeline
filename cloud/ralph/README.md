# Ralph Wiggum — the Pipeline's persistence daemon

*Keeps working the task board until everything is done, then idles.*
*The FIRST agent built on the new gateway: Ralph drives the Pipeline
EXCLUSIVELY through its REST API (`http://127.0.0.1:8902`, `X-Token` from
`GATEWAY_TOKEN`) — he never touches services, ssh, or the queue directly.
Read `../../CONNECT.md` for the endpoint contract he speaks.*

## What Ralph is

Ralph is the loop-closer. Given a board of tasks (`ralph_tasks.json`), he
turns the crank one increment at a time: dispatch what the gateway can do,
file what only a human can do as blocked, watch in-flight jobs to done, and
narrate everything. When the board is clear he does not exit — he idles
("I'm learning!") and keeps polling, because new work can arrive any time.

He is fully deterministic by default — **no LLM is required to run Ralph**.

## The memento loop (RALPH.md is the heart)

Canonical Ralph Wiggum pattern (ghuntley.com/loop): the memo file is the
memory. Each pass starts with zero assumptions and three steps:

```
        ┌─────────────────────────────────────────────────────────┐
        │  (a) START: read RALPH.md (the memo) + ralph_tasks.json │
        │      memo says: where I am / in-flight job ids /        │
        │      blockers / next — the pass obeys it                │
        └───────────────────────┬─────────────────────────────────┘
                                ▼
        ┌─────────────────────────────────────────────────────────┐
        │  (b) ONE increment of work:                             │
        │   1. INVENTORY  GET /api/status + GET /api/jobs?limit=50│
        │      → refresh every task that carries a job_id         │
        │        (queued/running stay; done → done;               │
        │         failed → blocked + error; job gone → blocked)   │
        │   2. PICK      first todo task in listed priority order │
        │      → none: "I'm learning!" (idle, memo still updated) │
        │   3. ACT       gateway kind → dispatch via params.op    │
        │                manual kind  → file blocked + human step │
        │      GUARD: never more than --max-inflight jobs at once │
        └───────────────────────┬─────────────────────────────────┘
                                ▼
        ┌─────────────────────────────────────────────────────────┐
        │  (c) REWRITE RALPH.md so the next pass (zero memory)    │
        │      knows exactly where things left off                │
        └───────────────────────┬─────────────────────────────────┘
                                ▼
                     sleep --idle-seconds (default 120) → (a)
```

Fixed memo sections (rewritten every pass):

```
# RALPH - memento (rewritten every pass; a fresh pass reads this first)
## WHERE I AM        board counts, gateway summary, last action
## IN FLIGHT (job ids)   one line per queued/running job:  - #61 - title (queued)
## BLOCKED (reasons)     one line per blocked task and exactly why
## NEXT                  what the next pass will do
## LOG                   append-only, timestamped, capped to last 50 lines
```

Same-prompt philosophy: every pass behaves as if told "keep going, consult
your memo". A pass that crashes loses nothing but its own log lines — the
board and the memo on disk are the truth. The full untrimmed diary is
stdout (under systemd: `journalctl -u gmp-ralph`).

## Files

| File | What |
|---|---|
| `ralph.py` | stdlib-only daemon (urllib, json, http — no pip installs) |
| `ralph_tasks.json` | the board — local state, gitignored-style, never commit |
| `ralph_tasks.seed.json` | the real current board, shipped as the seed; copied to `ralph_tasks.json` on first run |
| `RALPH.md` | the memento — rewritten every pass, gitignored |
| `tests/stub_gateway.py` | canned gateway on :8902 (status + jobs + enqueue echo) for verification without the real gateway |
| `tests/mini_board.json` | 3-task fixture: one gateway task, one manual, one stale job_id |
| `tests/empty_board.json` | empty fixture proving the "I'm learning!" idle |

## The decision table (one pass = one pick)

| Condition | Action |
|---|---|
| gateway unreachable at inventory | skip pass (transient — no fail count, memo says so) |
| task has job_id, job `queued`/`running` | keep in flight |
| task has job_id, job `done` | task → `done` |
| task has job_id, job `failed` | task → `blocked` + queue error + attempts |
| task has job_id, job not in queue (404) | task → `blocked` (stale job_id) |
| no todo tasks | narrate **"I'm learning!"**, idle |
| in-flight count ≥ `--max-inflight` | no pick this pass, wait |
| pick is `kind: manual` | task → `blocked`, notes = the exact human step |
| pick is `kind: gateway`, `params.op` known | POST mapped endpoint → record `job_id` |
| dispatch returns 4xx/5xx once | stays todo, retry next pass (own retry law) |
| dispatch returns 4xx/5xx twice | task → `blocked` + reason |
| `params.op` unknown and `--brain` on | ask the LLM ladder to pick an op |
| `params.op` unknown and `--brain` off | task → `blocked` (deterministic) |

## Dispatch table (`params.op` → endpoint, per CONNECT.md)

| op | Endpoint | Payload keys |
|---|---|---|
| `new_game` | `POST /api/games` | name, template, pitch, requester |
| `tunable` | `POST /api/orders/tunable` | game, tunable, new_value |
| `art` | `POST /api/orders/art` | game, prompt, asset_id |
| `critique` | `POST /api/critique` | game, mode (llm\|vision\|full) |
| `playtest` | `POST /api/playtest` | game, seconds (1..7200) |
| `gpu` | `POST /api/gpu` | kind (train\|mesh\|render), staging_dir, return_dir, payload |
| `device_test` | `POST /api/device_test` | game, apk |
| `deploy` | `POST /api/deploy` | game, target (retromonkey\|itch) |
| `ladder` | `POST /api/ladder/refresh` | — |

Path-like params (`staging_dir`, `return_dir`, `apk`, anything `*_dir`/`*_path`)
are normalized to forward slashes before sending (Windows law).

## Env

| Var | Meaning | Default |
|---|---|---|
| `GATEWAY_TOKEN` | X-Token for the gateway. Unset = refuse to start. | — |
| `GATEWAY_URL` | gateway base URL | `http://127.0.0.1:8902` |
| `TG_TOKEN` / `TG_CHAT_ID` | optional Telegram progress posts (silent-fail) | — |
| `RALPH_HOME` | Ralph's home (board + memo live here) | script's directory |
| `NVAPI_KEY` | enables `--brain` (NVIDIA integrate API) | — |
| `LADDER_PATH` | ladder.json for the boss model | repo `daily/ladder.json` |
| `OPENROUTER_API_KEY` | `--brain` fallback | — |

CLI: `--once` (one pass, timer-friendly) · default loops forever with
`--idle-seconds 120` · `--max-inflight 3` · `--brain` · `--home PATH` ·
`--board PATH`.

## Run it

```bash
# real gateway (retromonkey box or SSH tunnel to :8902)
GATEWAY_TOKEN=<token> python cloud/ralph/ralph.py              # loop forever
GATEWAY_TOKEN=<token> python cloud/ralph/ralph.py --once       # one pass

# verify locally with the stub (no Postgres, no box, no real gateway)
python cloud/ralph/tests/stub_gateway.py --port 8902 &
GATEWAY_TOKEN=stubtoken python cloud/ralph/ralph.py --once --board cloud/ralph/tests/mini_board.json
```

## Adding tasks

Edit `ralph_tasks.json` (the live board — Ralph reads it fresh every pass,
so no restart needed). One entry:

```json
{
  "id": "my-task-id",
  "title": "short human title",
  "kind": "gateway",
  "params": { "op": "deploy", "game": "sonar", "target": "itch" },
  "status": "todo",
  "job_id": null,
  "notes": ""
}
```

- `kind`: `gateway` (Ralph dispatches it) or `manual` (Ralph files it as
  blocked with `notes` = the exact human step — write that step in notes).
- `params.op`: one of the dispatch-table keys; the rest of `params` is the
  payload.
- Order in the file = priority order (first todo wins).
- `status` / `job_id` / `fails` / `updated_at` are Ralph's to manage — leave
  them `todo` / `null` when adding.
- To reset the board to the shipped seed: delete `ralph_tasks.json`.

## systemd unit — gmp-ralph.service

"Until done" means Ralph **idles, never exits** — the unit is a permanent
service with `Restart=always`, not a timer.

`/etc/systemd/system/gmp-ralph.service`:

```ini
[Unit]
Description=GMP Ralph Wiggum (persistence daemon on the gateway API)
After=network-online.target gmp-pipeline-gateway.service
Requires=gmp-pipeline-gateway.service

[Service]
WorkingDirectory=/home/ubuntu/gmp/cloud/ralph
EnvironmentFile=/home/ubuntu/gmp/cloud/.env
Environment=GATEWAY_URL=http://127.0.0.1:8902
Environment=RALPH_HOME=/home/ubuntu/gmp/cloud/ralph
ExecStart=/usr/bin/python3 /home/ubuntu/gmp/cloud/ralph/ralph.py --idle-seconds 120 --max-inflight 3
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

(`GATEWAY_TOKEN` comes from `cloud/.env` via `EnvironmentFile`; add
`TG_TOKEN` / `TG_CHAT_ID` there too if progress posts are wanted. For a
cron/timer style instead, use `ralph.py --once` from a systemd timer — the
memento memo makes every invocation stateless-safe.)

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now gmp-ralph
journalctl -u gmp-ralph -f          # the full diary
cat /home/ubuntu/gmp/cloud/ralph/RALPH.md   # the memento
```

## Laws Ralph inherits (from CONNECT.md)

1. Everything through the API — Ralph never ssh's, never touches the queue
   or Postgres directly.
2. Nothing is "done" until the queue says `done` — Ralph only marks a task
   done from the job's own status, never from optimism.
3. Red is red — a failed job blocks the task with the error in the memo;
   deploys past red are the deploy executor's refusal, not Ralph's call.
4. No silent work — one narrated line per action (stdout + memo LOG +
   optional Telegram).
5. Never log the token.
