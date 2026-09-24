# playerone-lappy — PlayerOne as a service on Lappy

The move: PlayerOne's **play/see lane** is starved on retromonkey (956MB RAM —
the AVD is parked, chromium was a stranger there, every Playwright run fought
the swap). Lappy has 32GB and an RTX 3060 on the same LAN. So Lappy becomes a
**queue worker**: it claims the PlayerOne job types off the retromonkey queue,
runs them locally with proper resources, and reports back over the same wire
every other worker uses. retromonkey stays the **brain/build lane** — queue,
gateway, ladder, deploys — and never runs a browser again.

```
                     retromonkey (brain/build lane)                lappy (play/see lane)
   ┌───────────────────────────────────────────┐        ┌─────────────────────────────────┐
   │  queue (Postgres :8901, Caddy /api)       │ claim  │  playerone.service (systemd)    │
   │  gateway :8902   ladder refresh   deploys │ ─────▶ │  worker.py — poll 10s, one job  │
   │        ▲ enqueue work-orders (prio 7)     │ ◀───── │  at a time, result POST back    │
   └───────────────────────────────────────────┘ result └─────────────────────────────────┘
                                                                      │ venv subprocesses
                                                   playwright chromium · llm_critic ladder ·
                                                   vision_critic · selfplay (numpy brains)
```

## What moves off the server

| Was on retromonkey | Now on Lappy | Why |
|---|---|---|
| `collect_evidence` playwright runs | `critique` + `playtest` + `emulator` jobs claimed and run locally | chromium + 32GB beats chromium + swap |
| `exec_emulator` (AVD lane, parked) | `emulator` jobs → the chromium feed (same report shape) | the AVD stays parked forever; chromium IS the phone |
| `exec_critique` venv driver | same driver, lappy paths (`~/playerone`) | mode=full no longer needs an emulator booted first |
| `selfplay` sessions | claimed as `selfplay` jobs, newest brain in `runs/` | a boot + 3 sessions no longer throttles the box |
| `runs/*.npz` + playthrough modules + critic stack | copied to `~/playerone/` (read the copies) | the box that RUNS the brain needs the brain |

Stays on retromonkey: the queue, the gateway, the LLM-ladder refresh
(`daily/ladder.json`), the games-src builds, deploys, the site.

## The dispatch table (`worker.py`)

| type | lane | payload | result |
|---|---|---|---|
| `critique` | evidence feed (fresh, unless `payload.evidence` is inline) → venv driver (`llm_critic` ladder + `vision_critic` on the newest frame) → critique JSON written to `~/playerone/critiques/` → **issues become queue work-orders at priority 7** (exec_critique's `issues_to_jobs`, byte-identical) | `{game, mode?=full, seconds?=60, url?, evidence?}` | critique dict + verdict + `enqueued_job_ids` |
| `playtest` | evidence feed for N seconds | `{game, seconds?=60, url?}` | shots / survival_seconds / events / evidence dir |
| `emulator` | same chromium feed — no AVD on lappy, ever | `{game, seconds?=90, url?}` | same, marked as the emulator lane's stand-in |
| `selfplay` | `selfplay.py` under the venv python, `--brain` = payload brain or newest `runs/<game>-evolve-*.npz` | `{game, sessions?=3, decisions?=80, brain?, url?, boot_seconds?}` | rows, sessions_ok, rows_file, merge lines |

Failure law: a runner that dies POSTs `{"error": ...}` — the queue's retry
law requeues it once, then fails it honestly. An unknown job type fails
**permanently** (retrying can't wire it). The daemon itself never dies with a
job: everything heavy is a subprocess under `~/playerone/venv/bin/python`,
and `worker.py` is stdlib-only. SIGTERM stops the current lane child
gracefully, POSTs its error (so the job requeues instead of hanging
`running` forever), then exits.

## Deploy (the human — Lappy is not reachable from here)

**1. On Rog — pull the brain-side files off retromonkey** (check the server
layout first: `ssh retromonkey 'ls /home/ubuntu/playerone'` — the critic
modules live under `examples/pipeline/` in the repo layout, flat in the
deploy layout):

```bash
cd C:/Users/aaron/game-making-pipeline
mkdir -p /tmp/p1-retro/runs /tmp/p1-retro/scripts
scp retromonkey:/home/ubuntu/playerone/runs/*.npz /tmp/p1-retro/runs/
scp "retromonkey:/home/ubuntu/playerone/examples/pipeline/{llm_critic,vision_critic,numpy_scorer,canvas_recorder}.py" /tmp/p1-retro/ \
  || scp "retromonkey:/home/ubuntu/playerone/{llm_critic,vision_critic,numpy_scorer,canvas_recorder}.py" /tmp/p1-retro/
scp "retromonkey:/home/ubuntu/playerone/examples/pipeline/scripts/*.py" /tmp/p1-retro/scripts/
scp retromonkey:/home/ubuntu/pipeline/daily/ladder.json /tmp/p1-retro/
```

The brain inventory to expect (one `.npz` per game, exported weekly by
`evolve.sh`): `sonar-evolve-<stamp>.npz`, `slime-evolve-<stamp>.npz`,
`arcade-evolve-<stamp>.npz` — `ls /tmp/p1-retro/runs/` shows the real stamps.

**2. On Rog — stage the deploy payload + secrets:**

```bash
mkdir -p /tmp/p1-deploy/retro
cp cloud/playerone-lappy/{worker.py,setup.sh,playerone.service,README.md} /tmp/p1-deploy/
cp cloud/evidence/collect_evidence.py  /tmp/p1-deploy/
cp cloud/playerone/selfplay.py         /tmp/p1-deploy/
cp cloud/queue/executors/exec_critique.py /tmp/p1-deploy/
cp -r /tmp/p1-retro/. /tmp/p1-deploy/retro/
printf 'GATEWAY_TOKEN=%s\nNVAPI_KEY=%s\nOPENROUTER_KEY=%s\n' \
  "$(tr -d '\r\n' < cloud/mcp/.token)" \
  "$(ssh retromonkey 'grep ^NVAPI_KEY /home/ubuntu/gmp/cloud/.env | cut -d= -f2')" \
  "$(ssh retromonkey 'grep ^OPENROUTER_KEY /home/ubuntu/gmp/cloud/.env | cut -d= -f2')" \
  > /tmp/p1-deploy/secrets
```

**3. Push to Lappy + install:**

```bash
ssh aaron@192.168.0.33 'mkdir -p ~/playerone-deploy'
scp -r /tmp/p1-deploy/. aaron@192.168.0.33:~/playerone-deploy/
ssh aaron@192.168.0.33 'bash ~/playerone-deploy/setup.sh'
```

`setup.sh` does the whole install: venv (playwright + pillow + numpy —
**not torch**, numpy brains only), `playwright install chromium` +
`install-deps`, the explicit module copies (worker.py, collect_evidence.py,
selfplay.py, exec_critique.py, llm_critic.py, vision_critic.py,
numpy_scorer.py, canvas_recorder.py, `scripts/` playthrough modules,
`runs/*.npz`, `ladder.json` → `~/playerone/daily/`), substitutes the secrets
into the systemd unit, and `enable --now playerone`.

**4. Retire the server-side claimants.** retromonkey's router still has
`critique`/`emulator` wired — with two claimants, SKIP LOCKED just means jobs
randomly land on the starved box again. Stop the server router
(`ssh retromonkey 'sudo systemctl stop gmp-queue-router'`) or drop those
entries from its EXECUTORS dict, and leave the queue API up (Lappy claims
over `https://retromonkey.com.au/api`).

## Ops on Lappy

```bash
journalctl -u playerone -f                       # the no-silent narration
sudo systemctl restart playerone                 # after re-deploying worker.py
sudo systemctl stop playerone                    # graceful: current job requeues
```

Verify end to end: enqueue a cheap job from a box with the token —

```bash
curl -s -X POST https://retromonkey.com.au/api/jobs -H "X-Token: $TOKEN" \
  -d '{"type":"playtest","payload":{"game":"sonar","seconds":20},"priority":1}'
curl -s https://retromonkey.com.au/api/jobs?limit=3 -H "X-Token: $TOKEN"
```

— and watch `journalctl`: claim → `run: ...collect_evidence.py...` →
`reported id=... -> done`. Artifacts land in `~/playerone/evidence/<game>/`
(ring of 12 PNGs + `latest.json`), `~/playerone/critiques/`, and
`~/playerone/data/<game>-evolve/` (selfplay rows + merges).

## Selftest (offline, runs anywhere — no venv, no network)

```bash
python worker.py --selftest
```

Mocks the claim POST (a fake `critique` job, then a fake `playtest` job),
mocks both subprocesses (fake evidence report, fake critique driver), points
`ROOT` at a temp tree with a fake evidence dir + fake brain, and asserts the
whole flow: brain picker → dispatch → evidence → driver → `issues_to_jobs`
(one `tune_tunable` order at prio 7, the vibe-only issue skipped) → result
POSTs. Exit 0 + `selftest: PASS`.

## Files

| File | What |
|---|---|
| `worker.py` | the daemon: claim loop (10s), dispatch table, cooperative-shutdown subprocess runner, work-order wire, `--selftest` |
| `setup.sh` | one-time Lappy install: venv, chromium, explicit module copies, secrets → unit, `enable --now` |
| `playerone.service` | systemd unit (`@MARKER@`-substituted at deploy; `Restart=always`, `TimeoutStopSec=600` so a long critique can POST its error on stop) |
| `README.md` | this file |
