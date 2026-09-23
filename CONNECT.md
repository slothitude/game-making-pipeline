# CONNECTING AN AGENT TO THE GAME MAKING PIPELINE

*The front door. Read this once, operate the factory forever.*
*Status: live 2026-09-23 · API gateway + MCP server + queue operational on retromonkey*

---

## What you're connecting to

An autonomous game factory. Games are designed, built, gated, shipped, and
critiqued by agents. You become one of them. The loop you're joining:

```
INPUTS (you, Telegram, itch, cron, critiques)
  → BRAIN (big-NVIDIA ladder plans; openrouter/free workers draft)
  → THE QUEUE (Postgres, one job at a time, offloads to Google free tiers)
  → BUILDER → 2× GATES → DEPLOY (retromonkey + itch)
  → CRITIC (PlayerOne: plays, sees, judges) → LEDGER (diary)
  → work-orders back to the queue. The loop closes.
```

## The two doors

| Door | Address | For |
|---|---|---|
| **MCP** | stdio (local) or `http://retromonkey.com.au/mcp` → :8911 (remote) | Claude Code, Colab MCP, any MCP client |
| **REST** | `https://retromonkey.com.au/api/*` (token-authed) | curl, scripts, bots, anything that speaks HTTP |

Both front the same 14 operations. Reads answer directly; **anything that does
work enqueues a job** and returns `{"id": N}` — you poll it. The gateway never
runs work itself (that's the law that keeps the 956MB server alive).

## Quick starts

### Claude Code (already registered on Rog, user scope)
```bash
claude mcp add pipeline --scope user \
  -e GATEWAY_URL=https://retromonkey.com.au \
  -e GATEWAY_TOKEN=<token from cloud/mcp/.token> \
  -- python C:/Users/aaron/game-making-pipeline/cloud/mcp/server.py
```
Then just ask: *"check pipeline_status"* — the tools appear as `pipeline:*`.

### Any MCP client (remote, streamable HTTP)
```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client(
    "http://retromonkey.com.au/mcp",
    headers={"Authorization": "Bearer <GATEWAY_TOKEN>"},
) as (r, w, _):
    async with ClientSession(r, w) as s:
        await s.initialize()
        result = await s.call_tool("pipeline_status", {})
        print(result.content[0].text)
```

### Plain REST
```bash
# status
curl -s https://retromonkey.com.au/api/status -H "X-Token: $GATEWAY_TOKEN"

# file a new game (async — returns a job id)
curl -s -X POST https://retromonkey.com.au/api/games \
  -H "X-Token: $GATEWAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"MOON LADDER","template":"arcade",
       "pitch":"climb words like rungs before the tide","requester":"my-agent"}'

# watch it
curl -s "https://retromonkey.com.au/api/jobs?limit=5" -H "X-Token: $GATEWAY_TOKEN"
curl -s https://retromonkey.com.au/api/jobs/42 -H "X-Token: $GATEWAY_TOKEN"
```

### Colab / GPU worker
Connect as above (MCP recipe), or pull GPU jobs directly through
`cloud/colab/colab_worker.py` on the queue host (Linux only — `run_job(job)`).

## The 14 operations

| Operation | Inputs | What it does |
|---|---|---|
| `pipeline_status` | — | Queue depth, games roster, ladder, version |
| `pipeline_health` | — | Liveness of gateway + queue |
| `new_game` | name, template, pitch, requester | Enqueues a game build (templates: word-game, arcade, rpg-fork, gyro-tilt, 3d-study, remake-backlog) |
| `read_diary` | slug | Last 40 lines of the game's DIARY.md — the ground truth of what happened |
| `tune_tunable` | game, tunable, new_value | T1 lane: rewrites the const, runs the gate wall, auto-reverts on red |
| `order_art` | game, prompt, asset_id | T2 lane: Flux generation + keying + acceptance bounds |
| `run_critique` | game, mode: llm\|vision\|full | PlayerOne judges: jevlike plays, llama-3.2 sees, ladder writes |
| `run_playtest` | game, seconds | The emulator/Playwright play lane, evidence + screenshots collected |
| `run_gpu` | kind: train\|mesh\|render, staging_dir, return_dir | Offloads to Colab T4 (provision → run → retrieve → teardown) |
| `device_test` | game, apk | Firebase Test Lab robo run on real devices |
| `deploy` | game, target: retromonkey\|itch | Ships a green build |
| `list_jobs` | status?, limit? | Queue listing |
| `job_status` | id | One job: status, result, error, attempts |
| `refresh_ladder` | — | Re-curates the LLM ladder from the live NVIDIA catalog |

REST paths mirror 1:1: `GET /api/status`, `POST /api/games`,
`GET /api/games/{slug}/diary`, `POST /api/orders/tunable`, `/api/orders/art`,
`/api/critique`, `/api/playtest`, `/api/gpu`, `/api/device_test`,
`/api/deploy`, `GET /api/jobs`, `GET /api/jobs/{id}`, `POST /api/ladder/refresh`,
`GET /api/health`.

## Auth

- One secret: **GATEWAY_TOKEN** (lives at `cloud/mcp/.token`, 600).
- REST: `X-Token: <token>` header. MCP HTTP: `Authorization: Bearer <token>`.
- The MCP server forwards the same token to the gateway as `X-Token`.
- The public site lane `POST /api/jobs` (from `make.html`) has the token
  injected by Caddy server-side — browsers never see it.
- Never commit tokens. Never log them.

## Queue semantics (read this or you'll write a bad agent)

- **Everything async returns `{"id": N}` immediately.** Poll `job_status`.
- Statuses: `queued → running → done | failed`.
- Priority 1 (urgent) … 9 (background). The router claims ONE job at a time,
  highest priority first, oldest within a priority.
- **Retry law**: first error requeues the job; second error fails it
  permanently. Check `attempts` and `error` before retrying yourself.
- Be patient between polls (≥5s). The box is small; serialization is the design.

## The laws your agent inherits

1. **2× CLEAR** — nothing is done until its suite passes twice. Never claim green from one run.
2. **NO DEPLOY PAST RED** — a failing gate means no ship. Not your call to override.
3. **DIARY LAW** — green milestones get written to the game's DIARY.md. If you finish work, it gets recorded.
4. **BRAINS = BIG NVIDIA · WORKERS = OPENROUTER/FREE** — never promote a worker model into a brain role; the ladder refresh (every 7 days) curates brain roles from the big NVIDIA catalog only.
5. **EVERYTHING THROUGH THE QUEUE** — don't ssh somewhere and run work directly; enqueue it. Offloads (Test Lab / Colab / Cloud Run) are queue-executed.
6. Report honestly: red is red, stubs are stubs, unverified is unverified.

## Executor wiring status

| Job type | State |
|---|---|
| `llm` | wired (ladder stub logging) |
| `new_game`, `tune_tunable`, `generate_art`, `critique`, `emulator`, `deploy`, `gate`, `device_test`, `gpu.*`, `ladder_refresh` | queued → executor wiring in progress (the handlers exist as CLI rails: `daily/`, `~/pipeline/colab/`, PlayerOne stack) — jobs queue safely until wired |

## Environment reference

| Thing | Where |
|---|---|
| Gateway | retromonkey `:8902` (loopback) → Caddy `/api/*` public |
| MCP server | `cloud/mcp/server.py` (stdio) · `:8911/mcp` (HTTP, remote via Caddy) |
| Queue API | retromonkey `:8901` loopback · public lane `POST /api/jobs` |
| Queue DB | Postgres `gmp.jobs` (user pipeline, host-local) |
| Site / games | https://retromonkey.com.au/ · `/games/<slug>/` · `/make.html` |
| Diary roots | `C:\Users\aaron\<slug>\DIARY.md` (Rog) — game roots configurable via `GAME_ROOTS` |
| Ladder | `daily/ladder.json` — brain roles from big NVIDIA, worker pinned openrouter/free |
| Emulator | AVD `gmp-atd` (aosp_atd x86_64) on retromonkey |
| Colab lane | `~/pipeline/colab/` (Linux-only CLI, T4) |
| Telegram front | bot 8563469356:AAF… · owner chat 5597932516 |

## A full walkthrough (copy this pattern)

```
1. pipeline_status            → see the board
2. new_game(name, template, pitch, requester="my-agent")
                              → {"id": 42}
3. job_status(42)             → queued → running → done
4. read_diary("moon-ladder")  → what the crew actually built
5. run_critique("moon-ladder", mode="full")
                              → {"id": 43} → scores, loves, issues
6. tune_tunable("moon-ladder", "FALL_SPEED", 420.0)
                              → gate wall runs; auto-revert if red
7. deploy("moon-ladder", "retromonkey")
                              → live at /games/moon-ladder/
```

Welcome to the factory. File work, read diaries, respect the gates.
