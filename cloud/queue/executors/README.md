# executors — the missing wires

The audit's unwired job types, as one importable package. Each module exposes
`run(job) -> dict`, raising on failure — exactly the contract the router's
`EXECUTORS` table already uses. stdlib + subprocess only; UTF-8 reconfigure;
one narration line per action.

| Module | Job type(s) | What it actually runs |
|---|---|---|
| `exec_critique.py` | `critique` | PlayerOne critic stack in `/home/ubuntu/playerone`'s venv via a /tmp driver script (LLMCritic ladder + vision_critic), then **converts issues into queue jobs** (`issues_to_jobs`, max 3, priority 7) and POSTs them (`enqueue`) — the wire that makes critiques get actioned |
| `exec_deploy.py` | `deploy` | rsync `/home/ubuntu/games-src/<game>/{build,export}/web` -> `/home/ubuntu/site/games/<game>/`, returns the live URL; honest RuntimeError listing the exact paths checked when there is no build; itch = NotImplementedError (butler TODO) |
| `exec_art.py` | `generate_art` | `python3 /home/ubuntu/pipeline/daily/art_work_order.py --game … --prompt … [--asset-id …]`, cwd `/home/ubuntu/pipeline/daily`, `GAME_ROOTS=/home/ubuntu/games-src` |
| `exec_gpu.py` | `gpu.train` `gpu.mesh` `gpu.render` | `sys.path` + `/home/ubuntu/pipeline/colab`, `from colab_worker import run_job`; ImportError -> "colab lane not deployed" |
| `exec_emulator.py` | `emulator` | `/home/ubuntu/playerone/venv/bin/python /home/ubuntu/playerone/emu_play.py --game … --seconds …`; non-zero exit -> RuntimeError with the stderr tail. ATD is parked by default — works when the emulator is booted |

## The one router patch

In `cloud/queue/router.py`, right after the `EXECUTORS = { ... }` dict
(line ~62), add:

```python
# --- the missing wires: executors package (cloud/queue/executors) ----------
from executors import REGISTRY  # noqa: E402
EXECUTORS.update(REGISTRY)
```

`EXECUTORS.update` keeps the existing `llm` stub and leaves `device_test` /
`gate` as `exec_not_wired` — only the seven keys in `REGISTRY` are replaced
(`critique`, `deploy`, `generate_art`, `gpu.train`, `gpu.mesh`, `gpu.render`,
`emulator`).

### One more thing the patch should include (same edit, same file)

Router line 20 (`ALL_TYPES`) does not contain the new type names, and the
queue API's default claim list (`queue_api.py` `POST /jobs/claim`) matches it —
jobs filed as `generate_art` / `gpu.*` / `tune_tunable` would sit unclaimed.
Either add them to `ALL_TYPES`:

```python
ALL_TYPES = ["llm", "device_test", "gpu", "gate", "deploy", "emulator",
             "critique", "generate_art", "tune_tunable",
             "gpu.train", "gpu.mesh", "gpu.render"]
```

…or keep filing gpu work as `type: "gpu"` with `payload.kind` (the old
convention — `exec_gpu` reads `payload.kind` either way, and the router's
existing `"gpu"` key is then overwritten by the registry's `gpu.*` entries,
which is fine). Pick one convention and keep the gateway's filings in step
with it.

## Deploy notes (Rog -> retromonkey)

```bash
# 1. ship the package (the human applies it; server copies live at the same
#    relative path under /home/ubuntu/pipeline/)
scp -r C:/Users/aaron/game-making-pipeline/cloud/queue/executors \
    retromonkey:/home/ubuntu/pipeline/cloud/queue/

# 2. sanity-compile in place, then selftest the two pure-python wires
ssh retromonkey 'cd /home/ubuntu/pipeline/cloud/queue && \
    python3 -m py_compile executors/*.py && \
    python3 executors/exec_critique.py --selftest && \
    python3 executors/exec_deploy.py --selftest'

# 3. dry-run a real deploy plan against real server paths (no rsync fired)
ssh retromonkey 'cd /home/ubuntu/pipeline/cloud/queue && \
    python3 executors/exec_deploy.py --dry-run sonar'

# 4. restart the router so the patch loads
ssh retromonkey 'systemctl --user restart gmp-router'   # or the unit's real name
journalctl --user -u gmp-router -n 20                    # watch one job drain
```

Environment the executors expect on the queue host (the router's systemd unit
already carries `QUEUE_TOKEN`; the rest matter for the critique lane):

- `QUEUE_TOKEN` — needed for exec_critique to enqueue issue jobs; without it
  the critique still returns, unactioned (narrated, not fatal).
- `QUEUE_API` (optional, default `http://127.0.0.1:8901`).
- `NVAPI_KEY` / `OPENROUTER_KEY` / `GEMINI_API_KEY` (or `~/.nvapi`,
  `~/.gemini_key`) — the LLMCritic ladder and the vision tier read these from
  the inherited environment.
- `GMP_ROOT` — exec_critique sets it to `/home/ubuntu/pipeline` for its driver
  subprocess unless already set (so `daily/ladder.json` is found server-side).
- Paths baked in as constants: `/home/ubuntu/playerone`,
  `/home/ubuntu/playerone/venv/bin/python`, `/home/ubuntu/pipeline`,
  `/home/ubuntu/games-src`, `/home/ubuntu/site/games`. Every one is overridable
  via env where a selftest needs it (`GMP_GAMES_SRC`, `GMP_SITE_GAMES`).

## Selftests (no server, no subprocess, no network)

```bash
python3 cloud/queue/executors/exec_critique.py --selftest   # issues_to_jobs on a canned critique
python3 cloud/queue/executors/exec_deploy.py  --selftest    # temp build dir -> dry-run plan -> honest miss
```
