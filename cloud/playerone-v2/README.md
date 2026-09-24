# playerone-v2 — perception + ladder

The v2 stack for Lappy's RTX 3060. The user directive it implements:
*"use opencv yolo and templates, a vision model and jevlike"* +
*"playerone need to use the ladder including openrouter/free"*.

v1 (generic_player.py) decided from a stdlib PNG brightness grid with the
jevlike brain alone. v2 gives the player real eyes and a real opinion:

```
                    Lappy (RTX 3060, ~/playerone-v2/)
 ┌───────────────────────────────────────────────────────────────────┐
 │  play.py — the game loop                                          │
 │                                                                   │
 │   playwright chromium (480x800 phone, headless)                   │
 │        │  CDP screenshot every 1.5 s                              │
 │        ▼                                                          │
 │   perceive.py — the EYES                                          │
 │     OpenCV     absdiff+contours  -> motion:strong-left            │
 │     OpenCV     thirds+hue        -> bright:top / health:30%       │
 │     YOLO       yolov8n (GPU)     -> person@(240,400) conf .9      │
 │     OpenCV     matchTemplate     -> menu_visible / game_over      │
 │        │  to_context() -> "motion:strong-left | health:30% | ..." │
 │        ▼                                                          │
 │   jevlike scorer (numpy_scorer, runs/*.npz)  — picks the action   │
 │        │  probabilities(context, 10 zone actions) -> sample T=0.4 │
 │        ▼                                                          │
 │   execute (tap/swipe on the real page) -> row -> data/<game>-evolve│
 │        │                                                          │
 │        │  every 5th decision + every death                        │
 │        ▼                                                          │
 │   brain.py — the BRAIN (the LADDER)                               │
 └───────────────────────────────────────────────────────────────────┘
```

## The tier table

| tier | what runs | latency | cost | availability |
|------|-----------|---------|------|--------------|
| `reflex` | jevlike only — numpy_scorer scores the answer bank from the .npz brain, locally | ~ms | free | always (no network; no brain -> keyword law) |
| `strategic` | **the ladder**: boss (nemotron-550b) -> backup -> gemini (only if `~/.gemini_key`) -> **openrouter/free** | 6-100s until the floor | free tiers | boss/backup flake (NVIDIA 504s); **openrouter/free never rate-limits — the guaranteed floor** |
| `vision` | ladder.json role `vision` (NVIDIA vision model) on the screenshot + context | ~15-30s | free tier | needs NVAPI_KEY + a PNG; any error falls through to the strategic ladder |

Ladder rungs come from `daily/ladder.json` (the same file the critic and the
site proxy read — one source of truth). Each rung: **1 attempt, 30s timeout,
any error falls through**. `roles.worker` (openrouter/free) is `pinned: true`
and is ALWAYS appended last, so every `think()` either answers at the floor or
returns `""` honestly (with the full story in `brain.last_trace()`).

Keys: `NVAPI_KEY` env or `~/.nvapi` · `OPENROUTER_KEY` (or
`OPENROUTER_API_KEY`) env · `~/.gemini_key` (optional, gates the gemini rung).

## Files

| file | role |
|------|------|
| `perceive.py` | `perceive(frame_bgr, prev) -> dict`, `to_context(perception) -> str`. YOLO/templates degrade independently; `--selftest` runs on synthetic frames |
| `brain.py` | `think(context, question, tier) -> str` — reflex / strategic ladder / vision. `--selftest` mocks the HTTP seam (offline) |
| `play.py` | the loop. `--url --game --seconds 90 --brain <npz> --record`. `--selftest` runs the loop on a fake page (offline) |
| `setup.sh` | venv (opencv-python-headless, ultralytics, numpy, playwright, pillow) + chromium + yolov8n.pt + files + selftest smoke |

## Perception output format

`perceive()` returns one dict; `to_context()` flattens it to the fixed-order
line the scorer eats:

```
motion:strong-left | health:30% | bright:top | person@(240,400) | menu:no | game_over:no | ping_ready:no | frame:42
```

- `motion:` `none` / `weak-{left,center,right}` / `strong-{...}` (absdiff share + biggest contour's centroid third)
- `health:` present only when the red HUD share of the top strip >= 2%
- `bright:` brightest horizontal third; `objects:unknown` when YOLO is unavailable
- `menu/game_over/ping_ready:` from templates/*.png matches (>= 0.80)
- `frame:` the decision counter — stable vocabulary, stable suffix law (v1's)

Rows land in `data/<game>-evolve/selfplay-<stamp>.jsonl`:
`{context, options, action, strategic_advice?, death_note?, forced?}` and
merge 90/10 into `train.jsonl`/`validation.jsonl` (the law evolve.sh reads).
Death rows carry `death_note` instead of an action and are kept out of training.

## How it connects to the queue

This is a CLI driver, not a daemon — the queue worker is
`cloud/playerone-lappy/worker.py`. The natural wiring (not yet wired): a
`selfplay` payload gets `v2: true` and the worker's `runner_selfplay` builds
the same `--url/--game/--seconds/--brain` command against
`~/playerone-v2/venv/bin/python` instead of selfplay.py — same claim/report
wire, same rows destination, so the weekly evolve reads v2 rows unchanged.
The strategic/death telemetry is additive payload fields; nothing on the
retromonkey side needs to change to accept a result.

## Deploy (from Rog)

```bash
# 1. stage
ssh aaron@192.168.0.33 mkdir -p ~/playerone-v2-deploy/runs
scp cloud/playerone-v2/{setup.sh,perceive.py,brain.py,play.py,README.md} aaron@192.168.0.33:~/playerone-v2-deploy/
scp daily/ladder.json                                aaron@192.168.0.33:~/playerone-v2-deploy/
ssh aaron@192.168.0.33 'cp ~/playerone/numpy_scorer.py ~/playerone-v2-deploy/ 2>/dev/null || true'
ssh aaron@192.168.0.33 'cp ~/playerone/runs/*.npz    ~/playerone-v2-deploy/runs/ 2>/dev/null || true'
printf 'NVAPI_KEY=...\nOPENROUTER_KEY=...\n' > secrets
scp secrets aaron@192.168.0.33:~/playerone-v2-deploy/secrets && rm secrets
# 2. install + smoke (runs all three offline selftests)
ssh aaron@192.168.0.33 'bash ~/playerone-v2-deploy/setup.sh'
# 3. play
ssh aaron@192.168.0.33 'source ~/playerone-v2-deploy/secrets; \
  ~/playerone-v2/venv/bin/python ~/playerone-v2/play.py --game sonar --seconds 90 \
  --brain $(ls -t ~/playerone-v2/runs/sonar*.npz | head -1) --record'
```
