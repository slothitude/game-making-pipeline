# SELFPLAY — PlayerOne plays its own games, and the brain eats the rows

The critic can judge menus; only play makes the brains better. `selfplay.py`
is the recorder half of the weekly learning loop: PlayerOne opens the live
game with its **current** brain, plays it, and writes fresh
`{context, options, action}` rows — exactly the shape `evolve.sh` trains on.

## The loop (weekly)

    selfplay   the current brain plays the live game   -> fresh rows
    evolve     jevlike.train on train.jsonl            -> new .pt -> exported .npz
    brains improve   the next selfplay is played by a stronger brain
    critiques and coaching improve   the critic sees deeper, longer play

Each week's selfplay is recorded by last week's brain, so the data
distribution tracks real play quality: as the brain improves, the rows cover
the states actual play reaches (longer dives, higher combos, tighter
threats) instead of the narrow band a scripted policy visits. That is what
"increase the learning" means here — the evolve cycle never runs stale.

## Record (server, per game)

    cd /home/ubuntu/playerone
    venv/bin/python cloud/playerone/selfplay.py \
        --game sonar --sessions 3 --brain runs/sonar-evolve-<stamp>.npz

    venv/bin/python cloud/playerone/selfplay.py --game slime --sessions 3 \
        --brain runs/slime-evolve-<stamp>.npz
    venv/bin/python cloud/playerone/selfplay.py --game arcade --sessions 3 \
        --brain runs/arcade-evolve-<stamp>.npz

Then evolve on the merged data:

    bash cloud/playerone/evolve.sh sonar

Where things land (defaults derived from `--root`, default
`/home/ubuntu/playerone`):

    <root>/data/<game>-evolve/selfplay-<runstamp>.jsonl   raw session rows
    <root>/data/<game>-evolve/{train,validation}.jsonl    merged splits
    <root>/data/<game>/{train,validation}.jsonl           what evolve.sh reads

## How a session plays

One session = one booted game page, capped at `--decisions` (80):

    start routine -> view_of (pixel-measured view) -> observe -> context+options
    -> scorer.probabilities(context, options) -> sample at temperature 0.35
    -> execute -> {context, options, action} row -> settle -> repeat

- **Temperature 0.35** — sampling weights are `p**(1/T)` renormalised. The
  run leans on the brain's argmax but the tail keeps landing; pure argmax
  would only re-record the brain's existing biases and exploration dies.
- **Session surface** — the deployed playthrough modules run unchanged. The
  recorder prefers the real `CanvasSession`, constructed against the
  recorder's browser handle with empty split writers, and falls back to a
  `MinimalSession` with the identical duck type (`settle/pause/tap/drag/
  press/execute/screenshot_png/rows/page`) when `canvas_recorder` is not
  importable. Row routing is selfplay's job; `record()` is never called.
- **Restart law** — a frame whose measured state left `playing` (death
  overlay, hub screen) routes back through the game's start routine instead
  of being recorded as garbage rows; restart frames land in the debug dir.
- **Merge law** — rows append to `<out-root>/train.jsonl`;
  `<out-root>/validation.jsonl` is seeded 90/10 the first time only. Fresh
  play never leaks into the held-out split, so weekly merges cannot corrupt
  the eval set. The same merge runs into `--merge-to` (default
  `<root>/data/<game>`), which is the directory `evolve.sh` reads.

## Flags that matter

| flag | default | notes |
|------|---------|-------|
| `--game` | required | `sonar` / `slime` / `arcade`, or a custom slug resolving module `<game>_playthrough` |
| `--sessions` | 3 | one booted page per session |
| `--brain` | — | `runs/<game>-evolve-<stamp>.npz`; required unless `--scorer-module` |
| `--decisions` | 80 | cap per session |
| `--temperature` | 0.35 | sampling sharpness |
| `--out-root` | `<root>/data/<game>-evolve` | raw rows + merged splits |
| `--merge-to` | `<root>/data/<game>` | the evolve.sh target; `none` disables |
| `--boot-seconds` | 420 | loader-overlay wait; raise on cold wasm cache |
| `--headed` | off | watch the play happen |
| `--scorer-module` | — | duck-typed `probabilities(context, options)`; fixture runs without a real npz |
| `--module`, `--url` | per game | point selfplay at a local fixture instead of retromonkey |
| `--seed` | 7 | reproducible sampling |

One line of JSON per decision goes to stdout (no-silent law): the action,
the brain's probability for it, the post-temperature sampling share, and the
brain's top option — a crash or a stuck boot is visible in the log
immediately, and a partial run still merges what it recorded.

## Transcripts vs rows

Rows teach the scorer **what to pick**: `(context, options, action)` triples,
byte-collated by jevlike, cheap to collect by the million. They carry no
judgment.

Transcripts — the reviews corpus — teach the coach and the critic **why**:
`llm_critic.judge(game, evidence, screenshots)` turns a sitting's evidence
into scores, issues, and work orders, and the narrative of a session (what
was tried, what died, what felt good) is what coaching reads. Selfplay feeds
both: the rows go to evolve, and the session evidence (decision logs on
stdout, restart frames in the debug dir) is the raw material a critique pass
scores. Rows scale the skill; critiques aim it.

## Local verification (no server, no brain)

    cd cloud/playerone
    python -m http.server 8123 --directory tests
    python selfplay.py --game fixture \
        --module tests/fixture_playthrough.py \
        --scorer-module tests/fake_scorer.py \
        --url http://127.0.0.1:8123/fixture_game.html \
        --root verify-root --sessions 1 --decisions 12

Expect one line per decision, rows in
`verify-root/data/fixture-evolve/selfplay-*.jsonl`, and the 90/10
train/validation merge under `verify-root/data/fixture*/` — the whole loop
without touching retromonkey. `tests/fixture_game.html` is the animated
one-canvas stand-in (same `#status` loader-overlay contract as the Godot
games); `tests/fixture_playthrough.py` mirrors the deployed playthrough
module contract against `window.fixtureState()`.

## Deploy (human)

    scp cloud/playerone/selfplay.py cloud/playerone/SELFPLAY.md \
        <box>:/home/ubuntu/playerone/cloud/playerone/

Weekly on the box:

    cd /home/ubuntu/playerone \
        && venv/bin/python cloud/playerone/selfplay.py --game sonar \
            --sessions 3 --brain runs/$(ls -t runs | grep '^sonar-evolve-.*\.npz$' | head -1) \
        && bash cloud/playerone/evolve.sh sonar
