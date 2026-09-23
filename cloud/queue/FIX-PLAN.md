# The Fix Loop Plan — making the pipeline actually improve the live library

*Written 2026-09-24. The three wires that turn "machinery exists" into "games change."*

## The goal
A player leaves a review → the critic judges → a fix lands → the wall greens it → **the live game on retromonkey updates** — all unattended.

Current state: critique→directive→commit→wall all work; zero fixes shipped. Three wires missing.

---

## WIRE 1: Export → Deploy (the ship lane)

**Problem**: green tunes/milestones change source code only. Nobody re-exports
the game and ships the new `.pck` to the live site.

**Parts:**
1. Export templates on retromonkey (~500MB download, one-time):
   `~/Godot_v4.7.1-stable_linux.x86_64` exists; templates go to
   `~/.local/share/godot/export_templates/4.7.1.stable/`
2. Upgrade `exec_deploy.py`: on `{game, target: retromonkey}` with a Web
   export preset in the repo → run the native godot export headless →
   rsync the fresh build to `/home/ubuntu/site/games/<game>/`
3. Chain hook: `exec_milestone` on `verdict == "green"` already enqueues
   `deploy` — that fires the new lane automatically.
4. `exec_tune` gains the same: after a green wall on a tune push → enqueue
   deploy. (Check the wall verdict with the same `await_wall` helper.)

**Verification**: enqueue a sonar deploy job → the pck on the live site has a
new timestamp → `curl` the game URL → it loads.

## WIRE 2: Commission the art lane

**Problem**: every `generate_art` job dies at `art_work_order exit 2`. The
Flux pipeline works on Rog; the server invocation has a path/env mismatch.

**Diagnosis step** (one command): run art_work_order.py on the server with
stderr visible on a tiny prompt. Read the actual error.

**Likely culprits** (from the exit-2 pattern):
- The game's `data/tunables.json` doesn't carry the asset registry the order
  references (`wo-art-*.json` work-order files land in a `runtime/orders/`
  that may not exist on the server)
- NVAPI_KEY present (it is, in the router env) but the acceptance/gate steps
  reference Godot paths that differ
- The export preset check fails because the server repos' export_presets.cfg
  wasn't synced

**Fix**: correct the invocation, then one real asset order (a sonar sprite)
lands as a committed PNG in the game repo → deploy ships it.

## WIRE 3: Land one green milestone

**Problem**: pi's best milestone scored 27/1 — one test short. The learner
(red-wall loop) ran its 3 attempts without green.

**Parts:**
1. Read the specific failing check from the wall log (the learner's food —
   verify `fail_logs()` actually retrieves it through the Forgejo API)
2. If `fail_logs` returns empty (the API route may 404 — it did in testing),
   fix the route: `/api/v1/repos/<org>/<game>/actions/jobs/<id>/logs`
3. One supervised iteration: fire the milestone, watch the RED WALL prompt
   land in pi's invocation (grep the recorded prompt for "RED WALL ATTEMPT"),
   confirm the fix targets the failing check
4. On green: the chain hook fires deploy → Wire 1 ships it → **first
   pipeline-built-and-shipped game update**

---

## The order
1 → 2 → 3. Wire 1 first because it's the payoff for every future green;
Wire 3 last because it depends on 1 to mean anything.

## Time estimate
Wire 1: ~30 min (template download + executor edit + one test deploy)
Wire 2: ~15 min (diagnose + fix + one asset order)
Wire 3: ~30-60 min (the learner's iteration cycle, watched)
