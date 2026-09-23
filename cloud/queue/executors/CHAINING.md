# CHAINING — the M2→M4 law (the audit gap: "pi stops at scaffolds")

`new_game` scaffolds M1 and stops. `exec_milestone.py` is the deepen wire: one
job = one milestone, built by pi in the game dir, pushed to the Forgejo origin,
and the push is what summons the Actions gate wall. This doc is the law that
drives the chain from scaffold to shipped — the executor itself never enqueues
the next link; the chain advances ONLY off a green Actions wall, never off a
successful push.

## The chain

```
new_job done (scaffold green)
  └─> enqueue milestone M2
        milestone M2 done -> Actions wall GREEN  ─> enqueue M3
          milestone M3 done -> Actions wall GREEN  ─> enqueue M4
            milestone M4 done -> Actions wall GREEN  ─┬> enqueue critique
                                                      └> enqueue deploy
```

- Push ≠ done. A pushed red wall means the next milestone is NOT enqueued;
  the retry law (server-side, two attempts) handles a transient red, and a
  wall red twice is a human look, same as everywhere else.
- One milestone in flight per game, always (the serialize law). The next
  milestone job is only filed after the current one's wall is green.

## The exact queue POSTs

Queue API default `http://127.0.0.1:8901`, token auth via `X-Token`
(`POST /jobs {type, payload, priority}` -> `{id}`). `QUEUE_TOKEN` must be set
in the shell (or swap in the literal token).

```bash
# 1. new_game done -> deepen: the first chained milestone
curl -s -X POST http://127.0.0.1:8901/jobs \
  -H "X-Token: $QUEUE_TOKEN" -H "Content-Type: application/json" \
  -d '{"type":"milestone","payload":{"game":"<slug>","milestone":"M2"},"priority":4}'

# 2. M2's Actions wall GREEN -> deepen again
curl -s -X POST http://127.0.0.1:8901/jobs \
  -H "X-Token: $QUEUE_TOKEN" -H "Content-Type: application/json" \
  -d '{"type":"milestone","payload":{"game":"<slug>","milestone":"M3"},"priority":4}'

# 3. M3's wall GREEN -> the last build milestone
curl -s -X POST http://127.0.0.1:8901/jobs \
  -H "X-Token: $QUEUE_TOKEN" -H "Content-Type: application/json" \
  -d '{"type":"milestone","payload":{"game":"<slug>","milestone":"M4"},"priority":4}'

# 4. M4's wall GREEN -> the chain closes: judge it, then ship it
curl -s -X POST http://127.0.0.1:8901/jobs \
  -H "X-Token: $QUEUE_TOKEN" -H "Content-Type: application/json" \
  -d '{"type":"critique","payload":{"game":"<slug>","mode":"llm"},"priority":7}'
curl -s -X POST http://127.0.0.1:8901/jobs \
  -H "X-Token: $QUEUE_TOKEN" -H "Content-Type: application/json" \
  -d '{"type":"deploy","payload":{"game":"<slug>","target":"retromonkey"},"priority":5}'
```

Priorities: milestones are main-line work (4, ahead of the default 5);
critique stays background-ish (7, the `exec_critique` convention); deploy at
the default 5. Optional payload keys on a milestone job: `directive` (a human
instruction appended to the pi prompt), and the payload inherits the honest
failures — missing `OPENROUTER_API_KEY` raises before pi starts, an unknown
milestone raises listing the ids the spec actually has.

Optional extra instruction mid-chain (e.g. after a critique hands back a
tunable note):

```bash
-d '{"type":"milestone","payload":{"game":"<slug>","milestone":"M3",
    "directive":"keep the jump feel identical to M2"},"priority":4}'
```

## "Actions wall GREEN" — how to actually check

The push went to `HEAD:main` at the Forgejo origin, so the wall's verdict is
the repo's commit status:

```bash
curl -s -u "slothitude:$FORGEJO_TOKEN" \
  "http://127.0.0.1:3001/api/v1/repos/slothitude/<slug>/commits/main/status" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])"
# "success" = green -> the next POST may be filed. Anything else: wait, don't chain.
```

## The registry (one-time edits — human applies; this doc does not edit code)

1. `cloud/queue/executors/__init__.py` — add the import and one REGISTRY line:

```python
from . import exec_art, exec_critique, exec_deploy, exec_emulator, exec_gpu, \
    exec_milestone
```

```python
REGISTRY = {
    "critique": exec_critique.run,
    "deploy": exec_deploy.run,
    "generate_art": exec_art.run,
    "gpu.train": _gpu("train"),
    "gpu.mesh": _gpu("mesh"),
    "gpu.render": _gpu("render"),
    "emulator": exec_emulator.run,
    "milestone": exec_milestone.run,   # <-- the deepen wire
}
```

2. `cloud/queue/router.py` line 20 — `ALL_TYPES` must carry `milestone` (and
   the still-pending generate_art/tune_tunable/gpu.* names from README.md) or
   `POST /jobs/claim` will never hand the router a milestone job:

```python
ALL_TYPES = ["llm", "device_test", "gpu", "gate", "deploy", "emulator",
             "critique", "generate_art", "tune_tunable",
             "gpu.train", "gpu.mesh", "gpu.render", "milestone"]
```

3. Ship + sanity (same dance as the other executors):

```bash
scp -r C:/Users/aaron/game-making-pipeline/cloud/queue/executors \
    retromonkey:/home/ubuntu/pipeline/cloud/queue/
ssh retromonkey 'cd /home/ubuntu/pipeline/cloud/queue && \
    python3 -m py_compile executors/*.py && \
    python3 executors/exec_milestone.py --selftest'
```

Server env: `OPENROUTER_API_KEY` in the router's systemd unit (pi refuses to
start without it); `FORGEJO_TOKEN` optional (defaults to the baked-in
credential; host/org overridable via `FORGEJO_HOST` / `FORGEJO_ORG`).

## Ralph's board — next pass, his file, human edit

Ralph's pass loop (`cloud/ralph/ralph.py`) currently files milestones as
`kind: "manual"` ("a human or coding agent must do the work" — the cubefall-M1
seed row). Next pass his board gains a `chain_task` helper so the loop can
file the chain itself:

- `chain_task(game, milestone)` — checks the Forgejo commit status of the
  game's `main`; `"success"` -> POST the next milestone (the curl above);
  anything else -> leave the board row in-flight, no dispatch.
- After M4's green: file critique + deploy rows the same way.
- The board then needs no `manual` milestone rows at all — the deepen wire is
  a gateway-shaped job like everything else.

That edit is in Ralph's file (`ralph.py` + a seed row update), owned by the
human, and deliberately NOT part of this package.
