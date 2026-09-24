# playerone-lappy brain lane — live benchmark (2026-09-25)

The spinal cord's first measured sessions: 3 playtests per game per lane, 60s
windows, run through worker.py's own `runner_feed` dispatch (foreground; the
daemon's environment untouched). Brain lane = `P1_BRAIN=jevlike
P1_INPUT=bridge P1_EYES=fusion` (yolo-venv python, CPU-pinned); legacy lane =
no flags (collect_evidence, worker venv, observation only by construction).

## sonar (exact training encoding: playerone-sonar.npz)

| run | lane | deaths | alive wall s | decisions | gestures | intents |
|-----|------|--------|--------------|-----------|----------|---------|
| 1 | jevlike | 4 | 39.2 | 76 | 35 | 27 dodge_down, 8 ping |
| 2 | jevlike | 5 | 37.5 | 66 | 38 | 28 dodge_down, 10 ping |
| 3 | jevlike | 5 | 37.7 | 78 | 29 | 24 dodge_down, 5 ping |
| 1-3 | legacy | n/a | n/a (never plays) | 0 | 0 | watch-only, 40 shots |

## star-visitor (TRANSFER: slime brain, escape-bearing lead; no checkpoint
## exists for this game)

| run | lane | deaths | alive wall s | decisions | gestures | intents |
|-----|------|--------|--------------|-----------|----------|---------|
| 1 | jevlike | 0 | 59.4 | 91 | 91 | 78 dodge_left, 12 dodge_right, 1 dodge_down |
| 2 | jevlike | 0 | 59.9 | 90 | 90 | 78 dodge_left, 12 dodge_right |
| 3 | jevlike | 0 | 56.9 | 82 | 82 | 76 dodge_left, 6 dodge_right |
| 1-3 | legacy | n/a | n/a (never plays) | 0 | 0 | watch-only, 40 shots |

## honest reading

* The legacy playtest lane sends NO input: its `survival_seconds: 60` is the
  watch window, not play. The comparison is "a lane that plays vs a lane that
  watches" — there is no legacy play baseline to lose to.
* These eyes have no HUD OCR: SCORE / DEPTH REACHED are not machine-readable.
  Survival, deaths, decisions and gestures are the measured columns. (The
  pre-flight dive reached "DEPTH REACHED 411m" per the death overlay, read by
  eye from a screenshot, not by the lane.)
* sonar: the brain plays the trained dive law — dodge_down dominant (descend
  = depth progress), pings on the ready ring, ~5 lives per 60s window with
  bounded Space restarts between lives.
* star-visitor: the transfer survived all three windows. With no hero
  detection the brain correctly applies the trained no-lead cruise law (SW
  drag) instead of guessing a gap it cannot see.
* Boot law: the lane boots on a persistent profile (selfplay's wasm-cache
  law) and reloads once on a mid-boot network flap — both were needed; two
  earlier sessions died to a stalled 40MB wasm download and a Godot splash
  that the loader probe cannot see behind.
