"""Plan-free playthrough surface for the local fixture game.

Mirrors the deployed playthrough-module contract so selfplay runs it
unchanged: ``view_of(session) -> (view, png)``, ``observe(view) ->
(context, options)``, a start routine, and ``run(session, action)``. The
fixture reports its state through ``window.fixtureState()`` — the session
surface under test is the point here, not pixel vision (the deployed games
measure pixels because nothing is in their DOM).

    python selfplay.py --game fixture --module tests/fixture_playthrough.py \
        --scorer-module tests/fake_scorer.py \
        --url http://127.0.0.1:8123/fixture_game.html
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

STEER_CENTRE = (240, 430)
HEADINGS = {"N": (240, 260), "E": (410, 430), "S": (240, 600), "W": (70, 430)}
PING_BUTTON = (240, 430)
WAIT_OPTION = "wait"

STATE_JS = "() => window.fixtureState()"


def view_of(session) -> tuple[dict, bytes]:
    png = session.screenshot_png()
    view = session.page.evaluate(STATE_JS)
    if isinstance(view, str):  # tolerate string-serialised probes
        view = json.loads(view)
    return view, png


def options_for() -> list[str]:
    out = [f"drag from ({STEER_CENTRE[0]}, {STEER_CENTRE[1]}) to ({x}, {y})"
           for x, y in HEADINGS.values()]
    return out + [f"tap ({PING_BUTTON[0]}, {PING_BUTTON[1]})", WAIT_OPTION]


def context_fixture(view: dict) -> str:
    return (f"fixture lane {view['lane']} | bug {view['bearing']} "
            f"| bugs {view['bugs']} | score {view['score']}")


def observe(view: dict) -> tuple[str, list[str]]:
    return context_fixture(view), options_for()


def run(session, action: str) -> None:
    if action == WAIT_OPTION:
        session.pause()
    elif action.startswith("tap ("):
        inner = action[len("tap ("):-1]
        x, y = (int(part.strip()) for part in inner.split(","))
        session.tap(x, y)
    elif action.startswith("drag from ("):
        session.execute(action)
    else:
        raise ValueError(f"unrunnable action: {action}")


def start(session, debug_dir=None) -> None:
    for attempt in range(8):
        view, png = view_of(session)
        if view.get("state") == "playing":
            return
        session.tap(PING_BUTTON[0], PING_BUTTON[1])
        session.settle(0.8)
        if debug_dir is not None and attempt >= 5:
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_dir.joinpath(f"fixture-start-{attempt}.png").write_bytes(png)
    raise RuntimeError("fixture: could not start")
