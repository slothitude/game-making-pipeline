# cloud/evidence — the critic's eyes

`collect_evidence.py` is the screenshot lane for `exec_critique mode=full`:
server-side Playwright (headless chromium) pointed at the LIVE HTML5 games on
retromonkey. No Android emulator, no adb, no ATD — the audit gap was that
mode=full needed screenshots and the only producer was the emulator lane,
which dies when the gmp-atd AVD is parked (and ATD has no Chrome at all).
This lane makes frames a plain CPU-box service.

Observation only — nothing is ever tapped, swiped, or keyed. The emulator lane
(`exec_emulator` → `emu_play.py`) stays the play lane; this lane gives the
critic its eyes when the emulator is parked.

## Install (one-time, human, on the server)

The Playwright *package* rides in `~/playerone/venv`; only the browser binary
needs fetching. On retromonkey:

```bash
~/playerone/venv/bin/pip install -U playwright      # if not already present
~/playerone/venv/bin/playwright install chromium    # ~150MB, one-time
```

Then copy `collect_evidence.py` to the box (server access is human-only):

```bash
# from the machine holding this repo:
scp cloud/evidence/collect_evidence.py retromonkey:~/playerone/collect_evidence.py
```

## Run

```bash
~/playerone/venv/bin/python ~/playerone/collect_evidence.py --game sonar --seconds 60
# → /home/ubuntu/playerone/evidence/sonar/000..011.png + latest.json

# any game, custom page (e.g. a fresh deploy or a ?debug URL):
~/playerone/venv/bin/python ~/playerone/collect_evidence.py \
    --game slime-line --seconds 45 \
    --url https://retromonkey.com.au/games/slime-line/?debug
```

CLI: `--game` (required) · `--seconds` (default 60) · `--url` (default
`https://retromonkey.com.au/games/<game>/`) · `--out-root` (default
`/home/ubuntu/playerone/evidence`).

Each run is a fresh browser context (480x800 phone viewport, mobile, touch,
no cookie carryover), screenshots every ~1.5s for `--seconds`, and keeps the
LAST 12 frames as `<out-root>/<game>/NNN.png` — renamed 000..NN chronological
at the end, older frames deleted as it goes (cap disk). `latest.json` always
lands, even when the page fails to load:

```json
{
  "game": "sonar", "url": "https://retromonkey.com.au/games/sonar/",
  "started": "2026-09-23T04:10:49+00:00", "seconds": 60.0, "shots": 41,
  "survival_seconds": 60.0, "actions_taken": 41,
  "note": "playwright evidence feed",
  "events": [{"t": 2.11, "note": "load complete in 2.11s"},
             {"t": 5.4, "note": "console error: ..."}]
}
```

`survival_seconds` / `actions_taken` speak `llm_critic.judge()`'s evidence
vocabulary (it prints scalar keys straight into the critique context), so the
ladder reads this feed the same way it reads `emu_play`'s trace. Console
errors and page exceptions are captured as events — a game that is spamming
`Uncaught TypeError` shows up in the critique, not just in a screenshot.
Load failure → exit 1, but `latest.json` is still written with `"error"` set,
so a critique job can see WHY instead of guessing. Progress chatter goes to
stderr; stdout is pure JSON (parse it like `exec_emulator` parses
`emu_play`'s report).

## What it feeds

`exec_critique mode=full` samples screenshots into the vision rung
(`llm_critic.judge()` base64s the first 3 `*.png` of the evidence dir —
`000/001/002.png`, i.e. oldest of the final 12, so the critic sees the game
boot through mid-run). With the default `--out-root`, frames land in the same
`/home/ubuntu/playerone/evidence/` tree `emu_play` already writes — but flat
in the game dir (`evidence/<game>/NNN.png`) rather than in a stamp subdir
(`evidence/<game>/<stamp>/NNN.png`). If mode=full's screenshot glob is
recursive over the evidence tree it picks these up unchanged; if it is
stamp-scoped, widen it to `evidence/<game>/*.png` as well. (One thing to
confirm against `exec_critique.py` — this repo doesn't hold a copy.)

## Wire line (exec_emulator-style executor)

Mirror of `exec_emulator.run()`'s command line — an `exec_evidence.py` whose
body is the emulator executor's with this substitution:

```python
cmd = [VENV_PY, "/home/ubuntu/playerone/collect_evidence.py",
       "--game", game, "--seconds", str(seconds)]
# REGISTRY["evidence"] = exec_evidence.run  — critique jobs with mode=full
# can then file {kind: "evidence", payload: {game, seconds}} when the
# emulator is parked; the collector needs no adb and no booted AVD.
```

Fail-lane behavior under the router's retry law: a dead page exits 1 with the
reason in `latest.json` → one requeue, then the job fails honestly, same as
`exec_emulator`.

## Verified locally (fixture, 2026-09-23)

`tests/fixture_game.html` (tiny animated canvas + one deliberate console
error), served with `python -m http.server 8931`, collector run
`--seconds 8 --out-root tests/out`:

- 6 PNGs (000..005), all exactly 480x800, frame counter visibly advancing
  211 → 661 between first and last (animation live, not a blank canvas)
- `latest.json`: `shots: 6`, `survival_seconds: 8.0`, `actions_taken: 6`,
  events captured the load time (2.11s) AND the fixture's intentional console
  error — both lanes the critique consumes
- dead-URL run: exit 1, `latest.json` written with `error:
  ...ERR_UNSAFE_PORT...`, stdout still pure JSON
