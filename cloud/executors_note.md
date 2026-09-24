# executors_note.md — wiring the Forgejo gate wall into the new_game scaffold

The audit gap: pi writes engine code and pushes to Forgejo, and the runner
executes echo-CI — code that never met a wall. Rog builds are gated by the
7-suite wall in `.github/workflows/games-ci.yml`; pi's code must be gated the
same way. The wall itself is `cloud/forgejo/gates.yml` in this repo.

## Where the integration lands

**Not in this repo.** There is no `new_game.sh` under `cloud/` (checked:
`cloud/queue/executors/` holds only `exec_art/exec_critique/exec_deploy/
exec_emulator/exec_gpu.py` + README; `cloud/executors/` does not exist). The
new_game server executor lives on pi at `~/pipeline/executors/new_game.sh`
(per CONNECT.md's executor rails, `~/pipeline/`), and it is the pi **prompt**
inside that script which scaffolds a new game repo. That file is outside this
repo's scope, so the integration is recorded here verbatim-ready instead of
being applied.

The repo-side convention to keep in step: the canonical wall is
`cloud/forgejo/gates.yml`; every game repo carries a byte-identical copy at
`.forgejo/workflows/gates.yml`.

## The verbatim pi-prompt addition (paste into the new_game pi prompt)

Also create `.forgejo/workflows/gates.yml` with exactly this content (byte-for-byte, no edits — it is copied from game-making-pipeline cloud/forgejo/gates.yml):

```yaml
name: Gate wall (2x)

# The Forgejo Actions gate wall. Audit gap this closes: pi writes engine code and
# pushes to Forgejo, and the runner was executing echo-CI — code that never met
# the wall. This gates pi's pushes exactly like Rog builds are gated on GitHub
# Actions (same image, same import pass, same headless suite runs).
#
# Source of truth: GMP cloud/forgejo/gates.yml. Each game repo carries its own
# copy at .forgejo/workflows/gates.yml (pi scaffolds it in at new_game time —
# see cloud/executors_note.md). Keep the copies in step.
#
# 2x CLEAR LAW: the wall runs TWICE, as two explicit steps. wall-1 pays the
# one-time costs (import cache warm-up, autoload/scene-load state); wall-2
# re-proves every suite against that warm project. A suite that is only green
# once is red — the log shows both walls.

on:
  push:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  gates:
    name: Gate wall x2 (every tests/*_tests.gd + tests/*_replay.gd)
    # The runner's label as registered on the Forgejo instance.
    runs-on: ubuntu-latest
    container:
      # The reproducible headless gate runner: the server's pre-pulled
      # gmp-godot:4.7.1 (Godot 4.7.1 + git + curl + unzip + fontconfig baked in,
      # so no apt bootstrap step and no Hub pull per run). NO docker:// scheme —
      # in a job's container.image that scheme is runner-label syntax, not a
      # docker reference, and act_runner dies on it with "invalid reference
      # format" before the job even starts.
      image: gmp-godot:4.7.1
    steps:
      # The game repo itself — this workflow lives IN the repo it gates.
      - name: Checkout
        uses: actions/checkout@v4

      # Discover the wall before paying for an import: a repo with no suites
      # under tests/ fails here — an empty wall proves nothing.
      # Flat tests/ only (res://tests/<suite>.gd is the contract, same as
      # games-ci); sorted so the log is a stable, comparable list.
      - name: Discover suites (tests/*_tests.gd + tests/*_replay.gd)
        run: |
          set -e
          find tests -maxdepth 1 -type f \( -name '*_tests.gd' -o -name '*_replay.gd' \) -printf '%f\n' \
            | sed 's/\.gd$//' | sort > /tmp/gate_suites.txt
          if [ ! -s /tmp/gate_suites.txt ]; then
            echo "no suites found under tests/ — nothing to gate, failing"
            exit 1
          fi
          echo "suites discovered:"
          cat /tmp/gate_suites.txt

      # Import once so .godot/imported exists before any suite runs.
      - name: Import project (once, before the wall)
        run: godot --headless --path . --import

      # THE GATE WALL, pass 1. set -e + a plain loop: the first non-zero suite
      # exit stops the job here. wall-2 below only ever runs on a green wall-1.
      - name: wall-1
        run: |
          set -e
          while IFS= read -r suite; do
            echo "::group::wall-1: ${suite}"
            godot --headless --path . --script "res://tests/${suite}.gd"
            echo "::endgroup::"
          done < /tmp/gate_suites.txt

      # THE GATE WALL, pass 2 — the 2x clear law, visible in the log: every
      # suite must be green twice, cold and warm.
      - name: wall-2
        run: |
          set -e
          while IFS= read -r suite; do
            echo "::group::wall-2: ${suite}"
            godot --headless --path . --script "res://tests/${suite}.gd"
            echo "::endgroup::"
          done < /tmp/gate_suites.txt
```

## Applying it on pi (when you're ready, one edit)

In `~/pipeline/executors/new_game.sh`, find the new_game prompt text and append
the sentence + yaml block above. The one-line summary of the addition:

> Also create `.forgejo/workflows/gates.yml` with exactly this content: <cloud/forgejo/gates.yml pasted verbatim>

## What the wall does (per run)

1. Container start on the server's pre-pulled `gmp-godot:4.7.1`
   (git/curl/unzip/fontconfig baked in — no apt step; the image ref carries NO
   `docker://` scheme, which is runner-label syntax and fails the job).
2. Checkout of the repo the workflow lives in.
3. Suite discovery: every flat `tests/*_tests.gd` + `tests/*_replay.gd`, sorted;
   zero suites = red (an empty wall proves nothing).
4. `godot --headless --path . --import` (one pass).
5. `wall-1` then `wall-2` — each discovered suite as
   `godot --headless --path . --script res://tests/<suite>.gd`; the first
   non-zero exit fails the job, and wall-2 only ever runs on a green wall-1
   (the 2x clear law, visible in the log as `wall-1: <suite>` / `wall-2: <suite>`).
