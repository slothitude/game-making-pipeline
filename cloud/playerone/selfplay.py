"""PlayerOne self-play recorder — fresh play rows from the current brain.

The evolve cycle needs FRESH data every week: PlayerOne opening the live game
with its CURRENT brain and playing it. This module is the recorder half of
that loop:

    boot page -> start routine -> observe (view_of -> context+options)
    -> scorer.probabilities(context, options) -> sample at temperature 0.35
    -> execute -> {context, options, action} row -> settle -> repeat

It runs the DEPLOYED playthrough modules unchanged (scripts/ on sys.path) —
same view_of pixel law, same contexts, same option vocabulary — by wrapping
the live page in the CanvasSession-compatible surface those modules already
expect. Row routing is the recorder's job: rows go straight into
out-root/selfplay-<stamp>.jsonl, one line per decision (no-silent law).

Merge law: after recording, the run's rows are appended to
<out-root>/train.jsonl, seeding <out-root>/validation.jsonl 90/10 the first
time only (fresh play never leaks into the held-out split), and the same
merge runs into --merge-to (default <root>/data/<game>) — where the weekly
evolve.sh reads.

    cd /home/ubuntu/playerone && venv/bin/python cloud/playerone/selfplay.py \
        --game sonar --sessions 3 --brain runs/sonar-evolve-<stamp>.npz

INPUT: with P1_INPUT=bridge in the environment (worker.py forwards the flag)
every action this recorder sends goes through input_bridge.py — CDP touch
events, a deviceorientation override, a synthetic gamepad — so Godot web
builds see real InputEventScreenTouch/Drag instead of synthesized mouse. The
context is also built with has_touch. Flag absent: the legacy mouse/keyboard
path, byte-identical.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import random
import re
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path("/home/ubuntu/playerone")
DECISIONS = 80          # decision cap per session
TEMPERATURE = 0.35      # p**(1/T) sampling: argmax-leaning, tail alive
SETTLE_SECONDS = 1.0
VIEWPORT = (480, 800)
BOOT_SECONDS = 420.0    # loader-overlay wait (cold wasm cache law)
RESTART_BUDGET = 40     # bounded start-routine re-entries per session

# P1_INPUT=bridge -> act through input_bridge.py (see the header note). The
# legacy mouse/keyboard lane stays when the flag is absent.
INPUT_BRIDGE = os.environ.get("P1_INPUT", "").strip().lower() == "bridge"

GAMES = {
    "sonar":  {"module": "sonar_playthrough",
               "url": "https://retromonkey.com.au/games/sonar/"},
    "slime":  {"module": "slime_playthrough",
               "url": "https://retromonkey.com.au/games/slime-line/"},
    "arcade": {"module": "arcade_playthrough",
               "url": "https://retromonkey.com.au/games/octogram-arcade/"},
}

# Start routines are named per game (start_dive / start_run / enter_playing).
START_NAMES = ("start", "start_dive", "start_run", "enter_playing")

# The same loader-overlay probe canvas_recorder boots with: the Godot games
# hide #status once the wasm bundle is live.
STATUS_HIDDEN = """
() => {
  const status = document.getElementById('status');
  return !status || getComputedStyle(status).visibility === 'hidden' ||
         status.style.visibility === 'hidden';
}
"""

DRAG_RE = re.compile(r"^drag from \((\d+), (\d+)\) to \((\d+), (\d+)\)$")
TAP_RE = re.compile(r"^tap \((\d+), (\d+)\)$")


def log(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


# -- session surface ----------------------------------------------------------

def _bridge_surface(session) -> None:
    """Instance-level input override for the CanvasSession path (P1_INPUT=bridge
    only): same tap/drag/press signatures the deployed playthrough modules
    already call, the transport underneath becomes input_bridge. The class is
    deployed code and stays untouched — duck typing, not a rewrite."""
    from input_bridge import Bridge
    bridge = Bridge(session.page).install()
    session._input_bridge = bridge
    session.tap = lambda x, y: bridge.tap(x, y)
    session.drag = lambda x1, y1, x2, y2, steps=10, hold_ms=120: bridge.swipe(
        x1, y1, x2, y2, dur_ms=max(200, int(hold_ms) * 2), steps=steps)
    session.press = lambda key: bridge.key(key, 60)

class MinimalSession:
    """The CanvasSession duck type view_of / start_* / run need, over one
    already-open page: settle/pause/tap/drag/press/execute/screenshot_png/
    screenshot_b64, the rows counter, .page, the loader-overlay boot, and the
    context-manager close. Deliberately NO record()/split writers — routing
    rows is selfplay's job, and selfplay writes them itself."""

    def __init__(self, pw, context, page, game: str, url: str,
                 viewport: tuple[int, int]) -> None:
        self._pw = pw
        self._context = context
        self.page = page
        self.game = game
        self.url = url
        self.viewport = viewport
        self.rows = 0
        self.boot_seconds = 0.0

    def _boot(self, boot_seconds: float) -> float:
        self.page.goto(self.url, wait_until="load", timeout=60000)
        start = time.time()
        while time.time() - start < boot_seconds:
            try:
                if self.page.evaluate(STATUS_HIDDEN):
                    return time.time() - start
            except Exception:
                pass
            self.page.wait_for_timeout(500)
        raise RuntimeError(
            f"{self.game}: loader overlay never cleared in {boot_seconds}s")

    def settle(self, seconds: float = 1.0) -> None:
        self.page.wait_for_timeout(int(seconds * 1000))

    def pause(self, ms: int = 500) -> None:
        self.page.wait_for_timeout(ms)

    def _bridge(self):
        """The input_bridge actuator, lazily (P1_INPUT=bridge only). None means
        "legacy lane": the caller falls through to page.mouse/page.keyboard."""
        if not INPUT_BRIDGE:
            return None
        bridge = getattr(self, "_input_bridge", None)
        if bridge is None:
            from input_bridge import Bridge
            bridge = Bridge(self.page).install()
            self._input_bridge = bridge
        return bridge

    def tap(self, x: int, y: int) -> None:
        bridge = self._bridge()
        if bridge is not None:
            bridge.tap(x, y)
            return
        self.page.mouse.click(x, y)

    def drag(self, x1: int, y1: int, x2: int, y2: int,
             steps: int = 10, hold_ms: int = 120) -> None:
        bridge = self._bridge()
        if bridge is not None:
            # a real touch drag: touchStart -> interpolated touchMove -> touchEnd
            bridge.swipe(x1, y1, x2, y2, dur_ms=max(200, int(hold_ms) * 2),
                         steps=steps)
            return
        self.page.mouse.move(x1, y1)
        self.page.mouse.down()
        self.page.wait_for_timeout(hold_ms)
        self.page.mouse.move(x2, y2, steps=steps)
        self.page.wait_for_timeout(hold_ms)
        self.page.mouse.up()

    def press(self, key: str) -> None:
        bridge = self._bridge()
        if bridge is not None:
            bridge.key(key, 60)
            return
        self.page.keyboard.press(key)

    def execute(self, action: str, centre: tuple[int, int] = (240, 430),
                reach: int = 170) -> bool:
        """Run one option string from the canvas vocabulary (CanvasSession
        law: wait / press ENTER|ESC / drag / tap)."""
        del centre, reach  # signature parity with the deployed harness
        action = action.strip()
        if action == "wait":
            self.pause()
            return True
        if action in ("press ENTER", "press ESC"):
            self.press("Enter" if action.endswith("ENTER") else "Escape")
            return True
        if match := DRAG_RE.match(action):
            x1, y1, x2, y2 = (int(value) for value in match.groups())
            self.drag(x1, y1, x2, y2)
            return True
        if match := TAP_RE.match(action):
            self.tap(int(match[1]), int(match[2]))
            return True
        return False

    def screenshot_png(self) -> bytes:
        return self.page.screenshot(type="png")

    def screenshot_b64(self) -> str:
        import base64
        return base64.b64encode(self.screenshot_png()).decode("ascii")

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._pw.stop()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def open_session(game: str, url: str, profile: Path,
                 viewport: tuple[int, int], boot_seconds: float,
                 headless: bool):
    """One booted game page in the CanvasSession shape.

    Prefers the deployed CanvasSession constructed against our browser
    handle (same persistent-profile wasm cache law, same _boot overlay
    probe) with EMPTY split writers — selfplay routes rows itself and never
    calls record(). Falls back to MinimalSession (identical duck type) when
    canvas_recorder cannot be imported: fixture runs and minimal deploys.
    """
    from playwright.sync_api import sync_playwright

    profile.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    try:
        # has_touch only under the bridge flag: a context without it never
        # emits real touch events, and Godot's web port then sees nothing.
        touch_kwargs = {"has_touch": True, "is_mobile": True} if INPUT_BRIDGE else {}
        context = pw.chromium.launch_persistent_context(
            str(profile), headless=headless,
            viewport={"width": viewport[0], "height": viewport[1]},
            **touch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
    except Exception:
        pw.stop()
        raise

    try:
        from canvas_recorder import CanvasSession
    except Exception as exc:
        log({"surface": "MinimalSession",
             "why": f"canvas_recorder unavailable: {type(exc).__name__}: {exc}"})
        session = MinimalSession(pw, context, page, game, url, viewport)
    else:
        session = CanvasSession.__new__(CanvasSession)
        session.game = game
        session.url = url
        session.viewport = viewport
        session.rows = 0
        session.writers = {}  # never record()-ed; close() iterates it harmlessly
        session._pw = pw
        session._context = context
        session.page = page
        if INPUT_BRIDGE:
            _bridge_surface(session)

    try:
        session.boot_seconds = session._boot(boot_seconds)
    except Exception:
        session.close()
        raise
    return session


# -- module loading ------------------------------------------------------------

def load_module(spec: str):
    """--module as a .py file path (fixture runs) or an importable name
    (deployed playthrough modules, found via --scripts-dir on sys.path)."""
    if spec.endswith(".py"):
        path = Path(spec)
        if not path.is_file():
            raise FileNotFoundError(f"no such module file: {path}")
        module_spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[path.stem] = module
        module_spec.loader.exec_module(module)
        return module
    return importlib.import_module(spec)


def start_routine(module, game: str):
    """The game's start callable, signature (session, debug_dir). Sonar calls
    it start_dive, slime start_run, the arcade enter_playing; accept any."""
    for name in START_NAMES:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return candidate, name
    for name in dir(module):
        if re.match(r"^(start|enter)_", name) and callable(getattr(module, name)):
            return getattr(module, name), name
    return (lambda session, debug_dir: None), "none"


# -- scorer --------------------------------------------------------------------

def _ensure_numpy_scorer(scripts_dir: Path) -> None:
    """numpy_scorer lives beside the playthrough modules in the repo layout
    (examples/pipeline/numpy_scorer.py) — find it from the scripts dir."""
    try:
        import numpy_scorer  # noqa: F401
        return
    except ImportError:
        pass
    for candidate in (scripts_dir, scripts_dir.parent):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        try:
            import numpy_scorer  # noqa: F401
            return
        except ImportError:
            continue
    raise ImportError("numpy_scorer not importable — looked in "
                      f"{scripts_dir} and {scripts_dir.parent}")


def build_scorer(args, scripts_dir: Path):
    """-> (probabilities(context, options) -> list[float], label)."""
    if args.scorer_module:
        module = load_module(args.scorer_module)
        if not callable(getattr(module, "probabilities", None)):
            raise TypeError(f"{args.scorer_module}: scorer modules must expose "
                            "probabilities(context, options) -> list[float]")
        return module.probabilities, f"module:{Path(args.scorer_module).name}"
    _ensure_numpy_scorer(scripts_dir)
    from numpy_scorer import NumpyScorer
    brain = Path(args.brain)
    if not brain.is_file():
        raise FileNotFoundError(f"brain not found: {brain}")
    return NumpyScorer(brain).probabilities, f"NumpyScorer:{brain.name}"


def choose(options: list[str], probs: list[float], temperature: float,
           rng: random.Random) -> tuple[str, float, float]:
    """Sample the brain's distribution at temperature: p**(1/T) renormalised.
    T=0.35 leans hard on the argmax while the tail keeps landing — pure
    argmax would just re-record the brain's existing biases and exploration
    would die. Returns (action, brain prob, sampling share)."""
    if not options:
        raise ValueError("observe returned no options")
    if len(probs) != len(options):
        log({"sample": "uniform",
             "why": f"scorer returned {len(probs)} probabilities "
                    f"for {len(options)} options"})
        probs = [1.0 / len(options)] * len(options)
    scaled = [max(float(p), 1e-9) ** (1.0 / temperature) for p in probs]
    total = sum(scaled)
    point, acc = rng.random() * total, 0.0
    chosen, index = options[-1], len(options) - 1
    for position, (option, weight) in enumerate(zip(options, scaled)):
        acc += weight
        if point <= acc:
            chosen, index = option, position
            break
    return chosen, probs[index], scaled[index] / total


# -- play ----------------------------------------------------------------------

def run_action(module, session, action: str) -> None:
    """Module-provided run() when the game has one (slime/arcade parse their
    own vocabulary); otherwise the session's canvas-vocabulary executor
    (sonar has no run — its actions are plain execute() material)."""
    runner = getattr(module, "run", None)
    if runner is not None:
        runner(session, action)
        return
    if not session.execute(action):
        raise ValueError(f"unrunnable action: {action}")


def play_session(module, start, session, probabilities, rng, args,
                 rows_path: Path, session_index: int,
                 debug_dir: Path) -> tuple[int, int, str | None]:
    """One sitting: start routine, then observe -> score -> sample -> execute
    -> row, settled between, capped at --decisions. A frame whose measured
    state left 'playing' (death overlay, hub screen) routes back through the
    start routine instead of being recorded as garbage rows.

    Returns (rows recorded, restarts, error-or-None)."""
    recorded = 0
    restarts = 0
    start(session, debug_dir)
    while recorded < args.decisions and restarts < RESTART_BUDGET:
        try:
            view, png = module.view_of(session)
            state = view.get("state")
            if state is not None and state != "playing":
                restarts += 1
                debug_dir.mkdir(parents=True, exist_ok=True)
                debug_dir.joinpath(
                    f"s{session_index}-restart-{restarts}.png").write_bytes(png)
                log({"session": session_index, "restart": restarts,
                     "state": state})
                start(session, debug_dir)
                continue
            context, options = module.observe(view)
            probs = probabilities(context, options)
            action, chosen_p, share = choose(options, probs,
                                             args.temperature, rng)
            run_action(module, session, action)
            row = {"context": context, "options": options, "action": action}
            with rows_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            recorded += 1
            session.rows = recorded  # keep the CanvasSession counter honest
            log({"session": session_index, "step": recorded,
                 "action": action, "p": round(float(chosen_p), 3),
                 "w": round(float(share), 3),
                 "top": options[max(range(len(probs)),
                                   key=lambda i: probs[i])],
                 "top_p": round(max(float(p) for p in probs), 3),
                 "rows_file": rows_path.name})
            session.settle(args.settle)
        except Exception as exc:
            return recorded, restarts, f"{type(exc).__name__}: {exc}"
    return recorded, restarts, None


# -- merge law -----------------------------------------------------------------

def append_rows(path: Path, rows: list[dict]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def merge_rows(rows: list[dict], base: Path, label: str) -> None:
    """Merge law: append everything to train.jsonl; seed validation.jsonl
    90/10 the first time only — the held-out split never sees fresh rows, so
    weekly merges cannot leak new play into its own eval set. This is what
    makes evolve.sh (which reads data/<game>/train.jsonl) find the week."""
    train = base / "train.jsonl"
    validation = base / "validation.jsonl"
    cut = (len(rows) * 9 + 9) // 10  # >=90% to train even for tiny runs
    written = append_rows(train, rows[:cut])
    validation_rows = 0
    if not validation.exists():
        validation_rows = append_rows(validation, rows[cut:])
    log({"merge": str(label), "train": str(train), "train_rows": written,
         "validation": str(validation),
         "validation_rows": validation_rows if validation_rows else "kept"})


# -- cli -----------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="PlayerOne self-play recorder — fresh play rows from the "
                    "current brain, merged where evolve.sh reads",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", required=True,
                        help="sonar | slime | arcade | <custom> (custom "
                             "resolves module <game>_playthrough)")
    parser.add_argument("--sessions", type=int, default=3,
                        help="one booted game page per session (default 3)")
    parser.add_argument("--brain",
                        help="exported .npz brain, e.g. "
                             "runs/sonar-evolve-<stamp>.npz — required unless "
                             "--scorer-module")
    parser.add_argument("--scorer-module",
                        help="duck-typed scorer .py exposing "
                             "probabilities(context, options) — fixture runs")
    parser.add_argument("--module",
                        help="playthrough module: importable name or .py path "
                             "(default: the game's deployed module)")
    parser.add_argument("--url", help="live game URL (default: the game's "
                                      "retromonkey URL)")
    parser.add_argument("--root", default=str(ROOT),
                        help=f"playerone repo root for derived defaults "
                             f"(default {ROOT})")
    parser.add_argument("--scripts-dir",
                        help="deployed playthrough scripts dir (default "
                             "<root>/examples/pipeline/scripts)")
    parser.add_argument("--out-root",
                        help="where rows land (default "
                             "<root>/data/<game>-evolve)")
    parser.add_argument("--merge-to",
                        help="second merge target, what evolve.sh reads "
                             "(default <root>/data/<game>; 'none' disables)")
    parser.add_argument("--decisions", type=int, default=DECISIONS,
                        help=f"decision cap per session (default {DECISIONS})")
    parser.add_argument("--temperature", type=float, default=TEMPERATURE,
                        help=f"sampling sharpness (default {TEMPERATURE})")
    parser.add_argument("--settle", type=float, default=SETTLE_SECONDS,
                        help="seconds between decisions "
                             f"(default {SETTLE_SECONDS})")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--viewport", default="480x800")
    parser.add_argument("--boot-seconds", type=float, default=BOOT_SECONDS,
                        help="loader-overlay wait; raise on cold wasm cache "
                             f"(default {BOOT_SECONDS})")
    parser.add_argument("--profile",
                        help="persistent browser profile (default "
                             "<root>/data/pw-profiles/<game>-selfplay)")
    parser.add_argument("--debug-dir",
                        help="restart-frame dumps (default <out-root>/debug)")
    parser.add_argument("--headed", action="store_true",
                        help="show the browser (default headless)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = Path(args.root).expanduser()
    scripts_dir = (Path(args.scripts_dir).expanduser() if args.scripts_dir
                   else root / "examples" / "pipeline" / "scripts")
    registry = GAMES.get(args.game, {})
    module_spec = args.module or registry.get("module") \
        or f"{args.game}_playthrough"
    url = args.url or registry.get("url")
    if not url:
        print(f"selfplay: unknown game {args.game!r} and no --url",
              file=sys.stderr)
        return 2
    if bool(args.brain) == bool(args.scorer_module):
        print("selfplay: give exactly one of --brain / --scorer-module",
              file=sys.stderr)
        return 2

    out_root = Path(args.out_root) if args.out_root \
        else root / "data" / f"{args.game}-evolve"
    if args.merge_to:
        merge_base = None if args.merge_to.lower() == "none" \
            else Path(args.merge_to)
    else:
        merge_base = root / "data" / args.game
    profile = Path(args.profile) if args.profile \
        else root / "data" / "pw-profiles" / f"{args.game}-selfplay"
    debug_dir = Path(args.debug_dir) if args.debug_dir else out_root / "debug"
    try:
        width, height = (int(part) for part in args.viewport.lower().split("x"))
    except ValueError:
        print(f"selfplay: --viewport wants WxH, got {args.viewport!r}",
              file=sys.stderr)
        return 2
    viewport = (width, height)

    if scripts_dir.is_dir():
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
    else:
        log({"scripts_dir": str(scripts_dir), "present": False,
             "note": "deployed playthrough harness not found — MinimalSession "
                     "surface (fixture runs expect this)"})

    rows_path = out_root / f"selfplay-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    rows_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        module = load_module(module_spec)
        start, start_name = start_routine(module, args.game)
        probabilities, scorer_label = build_scorer(args, scripts_dir)
    except Exception as exc:
        log({"error": f"{type(exc).__name__}: {exc}"})
        return 1

    log({"selfplay": args.game, "url": url, "scorer": scorer_label,
         "module": module_spec, "start": start_name,
         "input": "input_bridge" if INPUT_BRIDGE else "mouse+keyboard",
         "sessions": args.sessions, "decisions_cap": args.decisions,
         "temperature": args.temperature, "rows_file": str(rows_path),
         "merge_to": str(merge_base) if merge_base else "none",
         "seed": args.seed, "viewport": args.viewport,
         "headless": not args.headed})

    rng = random.Random(args.seed)
    all_rows = 0
    sessions_ok = 0
    try:
        for index in range(1, args.sessions + 1):
            opened = time.time()
            try:
                session = open_session(args.game, url, profile, viewport,
                                       args.boot_seconds, not args.headed)
            except Exception as exc:
                log({"session": index, "status": "boot-failed",
                     "error": f"{type(exc).__name__}: {exc}"})
                continue
            with session:
                session.settle(1.0)
                recorded, restarts, error = play_session(
                    module, start, session, probabilities, rng, args,
                    rows_path, index, debug_dir)
            all_rows += recorded
            sessions_ok += int(error is None)
            log({"session": index,
                 "status": "ok" if error is None else "error",
                 "rows": recorded, "restarts": restarts,
                 "elapsed_s": round(time.time() - opened, 1),
                 **({"error": error} if error else {})})
    finally:
        # merge whatever the run recorded, even a partial one
        rows = []
        if rows_path.exists():
            rows = [json.loads(line)
                    for line in rows_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        if rows:
            merge_rows(rows, out_root, out_root)
            if merge_base is not None:
                merge_rows(rows, merge_base, merge_base)
        else:
            log({"merge": "skipped", "why": "no rows recorded"})

    log({"done": args.game, "sessions_ok": sessions_ok,
         "sessions": args.sessions, "rows": all_rows,
         "rows_file": str(rows_path)})
    return 0 if all_rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
