# GENERIC — the ladder: one brain, any game, specialization for free

*The frontier lane from STUDY.md §4: "generic fallback = fixed action
vocabulary + vision-described context (slower, game-agnostic — the frontier
after selfplay.py proves Lane A on the server)."*

## The law

```
generic brain plays ANY game badly
        -> rows accumulate per game  (data/<game>-evolve/*.jsonl)
        -> merge law seeds data/<game>/train.jsonl 90/10 (selfplay's law)
        -> weekly evolve.sh fine-tunes a PER-GAME brain from exactly this corpus
        -> specialization emerges automatically — from the histogram, not
           from hand-written playthrough modules
```

Zero per-game code anywhere in the chain. No `view_of`, no start routine, no
pixel law. The only game-specific thing is the slug, which names the data
dirs.

## The vocabulary (fixed, geometry-derived)

Ten strings, derived from viewport thirds of the 480x800 phone law. The brain
scores exactly these, on every game:

| action | real page |
|---|---|
| `tap_center` | tap (240, 400) |
| `tap_left` | tap (80, 400) |
| `tap_right` | tap (400, 400) |
| `tap_top` | tap (240, 133) |
| `tap_bottom` | tap (240, 667) |
| `swipe_up` | drag (240, 400) -> (240, 180) |
| `swipe_down` | drag (240, 400) -> (240, 620) |
| `swipe_left` | drag (240, 400) -> (20, 400) |
| `swipe_right` | drag (240, 400) -> (460, 400) |
| `wait` | half a second of nothing |

Taps ride the touchscreen (phone law), swipes drag the mouse — pointer
events either way, which is what every HTML5 canvas listens for.

## The two context modes

**pixels (default, free).** Two CDP screenshots 400 ms apart
(collect_evidence's screenshot law) -> pure-stdlib PNG decode -> a 12x20
brightness grid per frame -> one text context:

```
motion <zone> | <brightest third> | <centre activity> center | frame <n>
motion left | bright top | calm center | frame 42
```

`zone` is where the abs-diff blob lives (left/right/top/bottom/center/none);
the brightest of top/mid/bottom gets its adjective (dark/dim/lit/bright);
centre activity is frozen/calm/stirring/busy/wild. Deterministic, ~50 bytes,
zero game knowledge, zero API spend.

**vision (--context-mode vision).** Every 4th decision the screenshot is
POSTed to NVIDIA `meta/llama-3.2-11b-vision-instruct` (the vision_critic
call shape) with a strict prompt: describe the state for a player in <=12
words using zones and any obvious goal/hazard. The text becomes the context
and is cached between refreshes. ANY error — missing `NVAPI_KEY`, 5xx,
empty reply — falls back to pixels for that decision and carries on.

## The loop

screenshot pair -> context -> `probabilities(context, ZONE_ACTIONS)` ->
sample at temperature 0.4 (the selfplay law: `p**(1/T)` renormalised) ->
execute on the real page -> row `{context, options, action}` -> settle
0.8-1.5 s. Cap 120 rows per session.

**Anti-stuck law:** 5 consecutive wait choices force a random tap. A game
that ignores the generic vocabulary (or sits on a title screen the opener
missed) still generates input rows — and the `forced_taps` count in the
summary is itself a signal: a game the generic player cannot reach is a game
with an unskippable gate.

**Menu opener (zero game knowledge):** before the loop, `Enter` then two
centre taps — collect_evidence's proven tap-past-the-title beat, best-effort.

## Output

```
data/<game>-evolve/selfplay-<stamp>.jsonl    rows, selfplay-compatible
data/<game>-evolve/summary-<stamp>.json      {decisions, action_histogram,
                                              distinct_contexts, forced_taps,
                                              vision_calls, vision_fallbacks}
data/<game>/train.jsonl                      merge law, >=90% of rows
data/<game>/validation.jsonl                 seeded 90/10 first time only
```

`action_histogram` is the **difficulty fingerprint**: which zone actions the
brain's context distribution collapses onto, per game. `distinct_contexts`
counts frame-normalized contexts (the frame counter is stripped) — a game
whose pixels never change scores near 1 and is telling you its load screen
is all you ever see.

## The v1 brain

`--brain` defaults to `brains/generic-v1.npz` under --root. That brain is
trained on the **generic DOM vocabulary** (the game_adapter/state_format
precedent: tap-label / press / wait menus) — the closest thing to a generic
PlayerOne that exists today. It has never seen zone strings or pixels-mode
context, so expect it to play near-uniformly at first. That is fine: the
ladder only needs the rows, and every week of generic play is training data
for every game at once.

## Run

```
python cloud/playerone/generic_player.py \
    --url https://retromonkey.com.au/games/sonar/ --game sonar --seconds 90
python cloud/playerone/generic_player.py --url <any game> --game <slug> \
    --seconds 90 --context-mode vision          # needs NVAPI_KEY
python cloud/playerone/generic_player.py --url http://127.0.0.1:8137/fixture_any.html \
    --game fixtureany --seconds 20 --scorer-module tests/fake_zone_scorer.py \
    --root verify-root
```

## Where Lane A still wins

The generic player is the floor, not the ceiling. Per-game playthrough
modules (sonar's depth/air/jelly context, slime's lane) produce contexts that
actually predict play, and their rows train better brains. The ladder uses
both: generic rows prove a game is reachable and accumulate while it is
unguarded; per-game modules take over once a game earns its own module.
