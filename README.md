# Game Making Pipeline (GMP)

The factory that turns a player's tap on FEEDBACK into a shipped, tested, live
game change — autonomous, zero budget, with a human Telegram thread watching.

The loop: **feedback in -> AI triage/implement -> gate wall -> web deploy ->
confirmation back**, typically inside 15 minutes. Nothing ships without the wall;
every deploy is a git commit, so every deploy is revertible.

Full plan and roadmap: [`PLAN.md`](PLAN.md). Runbook for bad days:
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## The stack

| Piece | Role |
|---|---|
| Telegram bot | Intake (from games + humans), review thread, notifications, `/stop /approve /revert` overrides |
| glm-5.3 (NVIDIA free endpoint) | The brain: triage, plan, implement (JSON / art / code tiers) |
| Flux.1-dev (NVIDIA genai) | Art generation (logos, textures, characters), PIL magic-wand post-processing |
| Google Colab CLI | Heavy lifting: batch art, mesh generation, long renders |
| GitHub Actions | The cloud runner: `godot:latest` container -> 7-suite gate wall -> web export -> Pages push; later, blender jobs |
| GitHub Pages | Delivery — every deploy is a commit, instantly live |
| godot:latest (container) | Reproducible headless gates + exports, no Rog required |
| Blender | 3D front (phase 5) |
| Rog / Lappy | Rog = orchestrator (the cloud_editor loop); Lappy = optional local GPU |

Architecture law (inherited from the sculmm 3D pipeline): **work-orders over
code** (the LLM targets JSON manifests like the ones in [`schemas/`](schemas/)),
**named law constants** (every tunable a named key, LLM-verb-modulatable),
**factory fronts** (each stage is its own front with its own green battery),
**fallback chains** (Colab -> local NVIDIA -> queued, never blocked),
**gates always**.

Autonomy tiers — how much the LLM may edit without a human:

| Tier | LLM may edit | Review |
|---|---|---|
| T0 conversational | nothing | — |
| T1 tunables JSON | per-game `data/tunables.json` (timers, scores, colors, taunts) | gates only |
| T2 art work-orders | art spec JSON -> Flux/PIL -> `assets/generated/` | gates + pixel acceptance checks |
| T3 code diffs | GDScript on a scratch branch | gates + green battery x3 attempts, else human |

## Repo layout

```
game-making-pipeline/
├── PLAN.md                     # the full plan (owner-authored) — read this first
├── README.md                   # this file
├── .github/workflows/
│   ├── games-ci.yml            # dispatch-only: gate wall (7 suites) + optional Web deploy
│   └── pipeline-poll.yml       # every 30 min: one cloud_editor pass over pending feedback
├── docs/
│   └── OPERATIONS.md           # token rotation, quotas/allowlist, reverting a deploy
├── schemas/                    # work-order contracts (.md per schema + .json examples)
│   ├── README.md
│   ├── feedback_work_order.{md,json}   # one player request, intake -> done/failed/human
│   ├── art_work_order.{md,json}        # T2 art request + pixel acceptance gate
│   └── new_game_work_order.{md,json}   # hub NEW-GAME request (P8 instantiation)
├── hub/                        # (other front) the Pages front door: Telegram login,
│   └── ...                     #   MY GAMES, NEW GAME picker — do not edit casually
└── instantiate/                # (other front) P8 template-instantiation front
    └── ...
```

## The customers

- **Octogram Arcade** — the flagship (Word Poker + RPG Campaign + Eight Letters in
  one menu). Gate wall today: **7 suites, 654 checks**: `run_tests`, `run_rpg_tests`,
  `smoke_battle`, `smoke_menu`, `run_e2e`, `run8_tests`, `run8_e2e`.
- Standalones stay live: `octogram`, `octogram-rpg`, `eight-letters`.
- v2: player-created games under the hub at `games/<player-id>-<slug>/`.

## Running the Rog loop (local orchestrator)

Rog is the development loop — same script the Actions poller runs, but long-lived:

```
cd C:\Users\aaron\octogram-arcade
python cloud_editor\cloud_editor.py
```

It polls Telegram `getUpdates`, triages each feedback with glm-5.3, applies the
produced diff on a scratch branch, runs the same 7-suite gate wall headlessly,
exports web + commits to `web_deploy` on green (or retries, then escalates on
red), and reports back in the Telegram thread. Local config lives in
`cloud_editor/config.json` (bot token, chat id, NVIDIA key, repo path) with state
in `cloud_editor/state.json` and one audit JSON per request under
`cloud_editor/feedback/`. Stop it with `/stop` in the thread.

The Actions `Pipeline poll` job (below) is the same harness in one-shot mode:
`--once` processes whatever feedback is pending, then exits — that is the whole
difference from the Rog loop.

## The Actions jobs

### `Games CI` (`.github/workflows/games-ci.yml`) — dispatch-only

Run by hand (or from a bot work-order): `gh workflow run games-ci.yml -f
game=octogram-arcade -f deploy=true`. There is no push trigger on purpose — this
repo holds the pipeline, not game code, so path filtering is N/A.

Inputs:

- `game` (choice: `octogram-arcade`) — which game repo to gate.
- `deploy` (boolean, default false) — after a green wall, export Web and push it.

Job flow (single job, `ubuntu-latest`, `container: docker://godot:latest`):

1. Install `git`/`unzip` into the minimal godot image (checkout needs git; node is
   mounted in from the runner).
2. Checkout this repo (full, no sparse paths) and the game repo into `./arcade`
   using `GAMES_TOKEN` (cross-repo read), submodules off.
3. If deploying: bootstrap the matching Godot **export templates** — `godot:latest`
   ships the editor only, and `--export-release` fails without templates.
4. `godot --headless --path arcade --import` once, then the gate wall: for each of
   the 7 suites, `godot --headless --path arcade --script res://tests/<suite>.gd`.
   Any non-zero exit fails the job — nothing exports past a red wall.
5. If `deploy`: `godot --headless --path arcade --export-release "Web"` (uses the
   game's `Web` preset -> `arcade/build/web/`), copy the build into `deploy/`, then
   clone the game repo over HTTPS with `GAMES_TOKEN` into `./arcade_deploy`,
   replace its tree with `deploy/`, commit **"CI deploy from GMP"** to
   `web_deploy`, push. Pages takes it from there.

Note: `GAMES_TOKEN` is used because the deploy pushes to a DIFFERENT repo than the
one the workflow lives in — the built-in `GITHUB_TOKEN` is scoped to this repo and
cannot do that. **`GAMES_TOKEN` needs repo scope for now** (contents: read for the
game repo + contents: read/write for `web_deploy`); tighten to fine-grained
contents-only when convenient. A per-repo **deploy key** would not work here
without juggling key-per-repo and `known_hosts`; one PAT keeps the clone+push
pattern in a single credential.

### `Pipeline poll` (`.github/workflows/pipeline-poll.yml`) — every 30 min + dispatch

`cron: */30 * * * *` (UTC; GitHub schedules can be delayed a few minutes) plus
manual dispatch. It checks out this repo and runs
`python3 cloud_editor/cloud_editor.py --once` with `CLOUD=1` and `TG_TOKEN`,
`NVAPI_KEY`, `CHAT_ID` from repo secrets — one pass over pending feedback, then
exit. The Rog loop runs the same script without `--once`.

Pending sync: `cloud_editor/` currently lives in `octogram-arcade` and is being
upgraded (v2: `--once` + multi-tenant routing + env-var config). Until it is
synced into this repo, the poll job fails loudly by design — that is the reminder,
not a bug.

## Secret setup

All of these live in GitHub -> Settings -> Secrets and variables -> Actions (repo
secrets), and locally on Rog only in `cloud_editor/config.json`. Never commit
them; never paste them into work-orders, schemas, or logs (mask as `<set>`).

| Secret | What | Get it |
|---|---|---|
| `TG_TOKEN` | Telegram bot token for intake + the human review thread | `@BotFather` -> `/mybots` -> API Token. Rotate per [`docs/OPERATIONS.md`](docs/OPERATIONS.md) if leaked. |
| `NVAPI_KEY` | NVIDIA free endpoint key — glm-5.3 triage/implementation (and Flux.1-dev art) | build.nvidia.com -> account -> API key (`nvapi-...`) |
| `CHAT_ID` | Telegram chat/thread the bot posts the review thread into | message the bot, then `getUpdates` and read `result[].message.chat.id` |
| `GAMES_TOKEN` | GitHub PAT for cross-repo game checkout + `web_deploy` push | GitHub -> Settings -> Developer settings -> Personal access tokens. Needs repo scope for now (see note above). |

Deploy-key note: a GitHub deploy key (per-repo SSH key) would only cover one repo
per key and needs SSH `known_hosts` handling in the container; the workflow uses
the HTTPS `x-access-token:<PAT>@github.com/...` pattern with one PAT instead.
Fine-grained PAT (contents read/write on the game repos only) is the tightening
step once the fleet is stable.

## Quotas / who can talk to the bot

v1 runs invite-mode: `cloud_editor/allowlist.txt`, one Telegram id per line —
anyone not listed is refused. Quota law and the revert/token runbooks:
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Known caveats

- `godot:latest` is not an official-blessed CI image and does not bundle export
  templates; the workflow bootstraps them from Godot's GitHub releases on deploy
  (community images like `barichello/godot-ci` bake them in, if we ever switch).
- GitHub Pages serves `web_deploy` as-is; whether `.nojekyll` is needed for the
  underscore-prefixed Godot web files follows whatever the existing
  `cloud_editor` deploy flow already does — confirm when the first GMP-driven
  deploy lands.
- The 30-minute poll can be delayed by GitHub's scheduler; the Rog loop is the
  low-latency path.
