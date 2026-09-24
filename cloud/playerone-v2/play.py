#!/usr/bin/env python3
"""playerone-v2 play — the game loop: v2 eyes + the ladder, one page.

The v1 loop (generic_player.py) decided from a stdlib PNG brightness grid
with the brain alone. v2 runs the full stack every tick on Lappy's 3060:

    CDP screenshot (1.5s cadence)
      -> perceive.perceive(frame, prev)          OpenCV + YOLO + templates
      -> perceive.to_context(...)                "motion:strong-left | ..."
      -> numpy_scorer probabilities (jevlike)    the reflex brain picks
      -> choose() at temperature                 the selfplay sampling law
      -> execute on the real page                zone taps/swipes
      -> every STRATEGIC_EVERY-th decision:
             brain.think(context, strategic_prompt, tier="strategic")
             — the ladder (boss -> backup -> gemini -> openrouter/free)
      -> row {context, options, action, strategic_advice?} -> data/<game>-evolve/

DEATH TELEMETRY: when perception raises the game_over template flag, the
loop asks the ladder "I died. What went wrong?", records the answer as
row["death_note"], and taps past the menu (start_attempts) to restart.

The zone vocabulary, the sampling law, the merge law and the CDP screenshot
law are generic_player.py's, byte-for-byte in spirit — one source of truth
per law, credited where copied. numpy_scorer missing -> uniform fallback
(the choose() law already handles a wrong-length probability list, and a
uniform session still produces rows).

Selftest (offline — fake page/screen/brain, no playwright, no network):

    python play.py --selftest
"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import random
import sys
import time
from pathlib import Path

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import perceive as perceive_module                              # noqa: E402
from perceive import perceive, to_context, yolo_state           # noqa: E402
import brain                                                    # noqa: E402

ROOT = Path("/home/aaron/playerone-v2")   # the lappy deploy root
VIEWPORT = (480, 800)                     # the phone frame every lane reads
TICK_SECONDS = 1.5                        # the spec'd screenshot cadence
SETTLE_RANGE = (0.4, 0.9)                 # shorter than v1: perception is fast
TEMPERATURE = 0.4
MAX_ROWS = 120
WAIT_STREAK_LIMIT = 5
SWIPE_PX = 220
FAIL_LIMIT = 5
GOTO_TIMEOUT_MS = 60_000
SHOT_TIMEOUT_MS = 8000
BOOT_SETTLE_SECONDS = 2.0
START_BEAT_MS = 1200
STRATEGIC_EVERY = 5                       # every 5th decision asks the ladder
STRATEGIC_PROMPT = ("Given this game state: {context}, my action history: "
                    "{actions}, should I change my approach? Answer in "
                    "<=15 words.")
DEATH_PROMPT = "I died. What went wrong?"

ZONE_ACTIONS = ("tap_center", "tap_left", "tap_right", "tap_top", "tap_bottom",
                "swipe_up", "swipe_down", "swipe_left", "swipe_right", "wait")


def log(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


# ------------------------------------------------- the zone vocabulary (v1 law) --
def zone_geometry(viewport: tuple[int, int]) -> dict[str, tuple]:
    """The 10 option strings -> real-page coordinates. generic_player.py's law."""
    width, height = viewport
    cx, cy = width // 2, height // 2
    return {
        "tap_center": ("tap", cx, cy),
        "tap_left": ("tap", width // 6, cy),
        "tap_right": ("tap", (5 * width) // 6, cy),
        "tap_top": ("tap", cx, height // 6),
        "tap_bottom": ("tap", cx, (5 * height) // 6),
        "swipe_up": ("drag", cx, cy, cx, cy - SWIPE_PX),
        "swipe_down": ("drag", cx, cy, cx, cy + SWIPE_PX),
        "swipe_left": ("drag", cx, cy, cx - SWIPE_PX, cy),
        "swipe_right": ("drag", cx, cy, cx + SWIPE_PX, cy),
    }


def execute(page, action: str, geometry: dict) -> bool:
    """One zone action on the real page — generic_player.execute verbatim."""
    spec = geometry.get(action)
    if spec is None:
        return False
    if spec[0] == "tap":
        _, x, y = spec
        try:
            page.touchscreen.tap(x, y)
        except Exception:
            page.mouse.click(x, y)
        return True
    _, x1, y1, x2, y2 = spec
    page.mouse.move(x1, y1)
    page.mouse.down()
    page.wait_for_timeout(120)
    page.mouse.move(x2, y2, steps=10)
    page.wait_for_timeout(120)
    page.mouse.up()
    return True


class Screen:
    """The CDP screenshot law from collect_evidence/generic_player."""

    def __init__(self, page):
        self._page = page
        self._cdp = None

    def png(self) -> bytes:
        try:
            if self._cdp is None:
                self._cdp = self._page.context.new_cdp_session(self._page)
            return base64.b64decode(
                self._cdp.send("Page.captureScreenshot",
                               {"format": "png"})["data"])
        except Exception:
            self._cdp = None
            return self._page.screenshot(timeout=SHOT_TIMEOUT_MS)


def png_to_bgr(png: bytes) -> np.ndarray:
    """PNG bytes -> BGR ndarray (the one place cv2.imdecode enters the loop)."""
    import cv2
    buf = np.frombuffer(png, dtype=np.uint8)
    frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("screenshot did not decode")
    return frame


def start_attempts(page) -> None:
    """Tap past a title screen with zero game knowledge — the proven opener."""
    for label, action in (
            ("enter", lambda: page.keyboard.press("Enter")),
            ("tap1", lambda: page.touchscreen.tap(
                page.viewport_size["width"] // 2,
                page.viewport_size["height"] // 2)),
            ("tap2", lambda: page.touchscreen.tap(
                page.viewport_size["width"] // 2,
                page.viewport_size["height"] // 2 + 30))):
        try:
            action()
            log({"start_attempt": label})
        except Exception as exc:  # noqa: BLE001 — never kill the run here
            log({"start_attempt": label,
                 "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
        page.wait_for_timeout(START_BEAT_MS)


def choose(options: list[str], probs: list[float], temperature: float,
           rng: random.Random) -> tuple[str, float]:
    """Sample at temperature p**(1/T) renormalised — the selfplay law."""
    if not options:
        raise ValueError("no options")
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
    return chosen, probs[index]


# ------------------------------------------------------------- the jevlike brain --
def build_scorer(brain_path: str | None):
    """NumpyScorer on the exported .npz — or a uniform stand-in that logs once
    (a missing numpy_scorer.py must not stop rows from flowing)."""
    path = Path(brain_path) if brain_path else None
    if path and path.is_file():
        try:
            from numpy_scorer import NumpyScorer  # noqa: PLC0415 — lazy
            return NumpyScorer(str(path)), f"NumpyScorer:{path.name}"
        except Exception as exc:  # noqa: BLE001
            log({"scorer": "fallback-uniform",
                 "why": f"numpy_scorer: {type(exc).__name__}: {str(exc)[:100]}"})
    elif brain_path:
        log({"scorer": "fallback-uniform", "why": f"no brain at {brain_path}"})
    else:
        log({"scorer": "fallback-uniform", "why": "no --brain given"})

    def uniform(_context, options):
        return [1.0 / len(options)] * len(options)
    return uniform, "uniform"


# ------------------------------------------------------------------ the loop --
def _last_rung() -> dict:
    """The final rung record of the last think() — {} when the brain is
    stubbed (selftest) or the tier never touched the ladder."""
    trace = brain.last_trace()
    return trace[-1] if trace else {}


def session_loop(page, screen, scorer, geometry, rng, args, rows_path: Path) \
        -> dict:
    """Decide until --seconds / --max-rows. perception -> context -> action ->
    ladder every STRATEGIC_EVERY-th tick; death raises the ladder's
    "what went wrong" and restarts. Returns the session summary."""
    histogram: collections.Counter = collections.Counter()
    rows = deaths = strategic_calls = 0
    wait_streak = failures = 0
    actions: list[str] = []
    last_advice: str | None = None
    prev_frame: np.ndarray | None = None
    started = time.monotonic()
    deadline = started + args.seconds

    while rows < args.max_rows and time.monotonic() < deadline:
        try:
            frame = png_to_bgr(screen.png())
            perception = perceive(frame, prev_frame, frame=rows)
            prev_frame = frame
            context = to_context(perception)
            failures = 0
        except Exception as exc:  # noqa: BLE001 — a bad frame is not a death
            failures += 1
            log({"frame_error": f"{type(exc).__name__}: {str(exc)[:140]}",
                 "failures": failures})
            if failures >= FAIL_LIMIT:
                log({"bail": f"{failures} consecutive failed frames"})
                break
            page.wait_for_timeout(1000)
            continue

        row: dict = {"context": context, "options": list(ZONE_ACTIONS)}

        # -- the death telemetry: game_over on screen -> ask the ladder why --
        if "game_over" in (perception.get("flags") or []):
            deaths += 1
            note = brain.think(context, DEATH_PROMPT, tier="strategic")
            row["death_note"] = note or "ladder silent"
            strategic_calls += 1
            log({"death": deaths, "note": (note or "ladder silent")[:120],
                 "trace": _last_rung()})
            prev_frame = None                      # new scene: motion resets
            try:
                start_attempts(page)               # tap past menu -> restart
            except Exception as exc:  # noqa: BLE001
                log({"restart_error": f"{type(exc).__name__}: {str(exc)[:120]}"})
            _append_row(rows_path, row)
            rows += 1
            continue

        # -- the strategic adjustment: every 5th decision asks the ladder --
        if rows % STRATEGIC_EVERY == 0:
            prompt = STRATEGIC_PROMPT.format(
                context=context,
                actions=", ".join(actions[-8:]) or "none yet")
            if last_advice:
                prompt += f" Previous advice: {last_advice}"
            advice = brain.think(context, prompt, tier="strategic")
            strategic_calls += 1
            if advice:
                last_advice = advice
                row["strategic_advice"] = advice
            log({"strategic": rows, "advice": (advice or "ladder silent")[:120],
                 "trace": _last_rung()})

        probs = scorer(context, list(ZONE_ACTIONS))
        action, chosen_p = choose(ZONE_ACTIONS, probs, args.temperature, rng)
        if action == "wait":
            wait_streak += 1
            if wait_streak >= WAIT_STREAK_LIMIT:
                action = rng.choice([a for a in ZONE_ACTIONS
                                     if a.startswith("tap_")])
                wait_streak = 0
                row["forced"] = True              # the v1 anti-stuck law
        else:
            wait_streak = 0

        if action != "wait":
            try:
                execute(page, action, geometry)
            except Exception as exc:  # noqa: BLE001 — input errors happen
                log({"action_error": f"{type(exc).__name__}: {str(exc)[:120]}"})
        else:
            page.wait_for_timeout(500)

        actions.append(action)
        histogram[action] += 1
        row["action"] = action
        _append_row(rows_path, row)
        log({"step": rows, "action": action, "p": round(float(chosen_p), 3),
             "context": context,
             **({"advice": row["strategic_advice"][:60]}
                if "strategic_advice" in row else {})})
        rows += 1
        page.wait_for_timeout(int(rng.uniform(*SETTLE_RANGE) * 1000))

    return {"decisions": rows, "deaths": deaths,
            "strategic_calls": strategic_calls,
            "action_histogram": dict(sorted(histogram.items(),
                                            key=lambda kv: -kv[1])),
            "elapsed_s": round(time.monotonic() - started, 1)}


def _append_row(rows_path: Path, row: dict) -> None:
    rows_path.parent.mkdir(parents=True, exist_ok=True)
    with rows_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def merge_rows(rows: list[dict], base: Path, label) -> None:
    """The merge law (selfplay verbatim): append to train.jsonl, seed
    validation.jsonl 90/10 first time only — evolve.sh reads this."""
    train, validation = base / "train.jsonl", base / "validation.jsonl"
    cut = (len(rows) * 9 + 9) // 10
    base.mkdir(parents=True, exist_ok=True)
    with train.open("a", encoding="utf-8") as handle:
        for row in rows[:cut]:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    validation_rows = 0
    if not validation.exists():
        with validation.open("a", encoding="utf-8") as handle:
            for row in rows[cut:]:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        validation_rows = len(rows[cut:])
    log({"merge": str(label), "train_rows": cut,
         "validation_rows": validation_rows if validation_rows else "kept"})


def open_page(url: str, viewport: tuple[int, int], headless: bool = True):
    """One phone-frame page — generic_player.open_page verbatim."""
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]},
            is_mobile=True, has_touch=True)
        page = context.new_page()
        page.goto(url, wait_until="load", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        pw.stop()
        raise
    return pw, browser, page


# ------------------------------------------------------------------ cli/entry --
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="playerone-v2 play — v2 eyes (OpenCV+YOLO+templates) + "
                    "the ladder (reflex/strategic/vision), one game loop")
    parser.add_argument("--url", help="game url (default the retromonkey slug)")
    parser.add_argument("--game", help="slug — names data dirs")
    parser.add_argument("--seconds", type=float, default=90.0)
    parser.add_argument("--brain", help="exported .npz (jevlike) — absent -> "
                                        "uniform fallback")
    parser.add_argument("--record", action=argparse.BooleanOptionalAction,
                        default=True)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--out-root", help="default <root>/data/<game>-evolve")
    parser.add_argument("--merge-to", help="default <root>/data/<game>; "
                                           "'none' disables")
    parser.add_argument("--max-rows", type=int, default=MAX_ROWS)
    parser.add_argument("--temperature", type=float, default=TEMPERATURE)
    parser.add_argument("--strategic-every", type=int, default=STRATEGIC_EVERY)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--viewport", default="480x800")
    parser.add_argument("--selftest", action="store_true",
                        help="offline loop: fake page/screen/brain, prove "
                             "rows, the every-5th ladder call and the death path")
    args = parser.parse_args(argv)
    if not args.selftest:
        if not args.game:
            parser.error("--game is required (or run --selftest)")
        if not args.url:
            parser.error("--url is required (or run --selftest)")
    return args


def _selftest() -> int:
    """Offline: a fake page serves synthetic frames (a mover + a shrinking
    health bar + a game_over card late in the run); the brain is stubbed to a
    canned advice string. Proves the loop, the every-5th strategic call, the
    death restart, and the rows file — no playwright, no network."""
    import cv2
    import tempfile
    rows_path = HERE / "verify-root" / "selftest-rows.jsonl"
    if rows_path.exists():
        rows_path.unlink()

    CARD = (300, 420, 140, 340)   # y0,y1,x0,x1 — the game_over card rectangle

    def draw_card(frame_bgr: np.ndarray) -> np.ndarray:
        """The game_over card: solid panel + darker bars (a pattern, so
        matchTemplate has variance to bite on — a constant sprite vs a
        constant region is undefined correlation)."""
        y0, y1, x0, x1 = CARD
        frame_bgr[y0:y1, x0:x1] = (90, 90, 230)
        frame_bgr[y0 + 12:y0 + 42, x0 + 20:x1 - 20] = (30, 30, 60)
        frame_bgr[y0 + 62:y0 + 92, x0 + 20:x1 - 20] = (30, 30, 60)
        return frame_bgr

    state = {"tick": 0, "x": 60}

    def fake_frame() -> np.ndarray:
        rng = np.random.default_rng(state["tick"] + 11)
        frame_bgr = rng.integers(0, 64, size=(800, 480, 3), dtype=np.uint8)
        state["x"] = 60 + (state["tick"] * 20) % 300
        frame_bgr[380:420, state["x"]:state["x"] + 60] = (255, 255, 255)
        health = max(30 - state["tick"] * 4, 0)
        frame_bgr[:120, :int(480 * health / 100)] = (40, 40, 220)
        if state["tick"] >= 12:                     # the game_over card
            draw_card(frame_bgr)
        state["tick"] += 1
        return frame_bgr

    class FakeScreen:
        @staticmethod
        def png() -> bytes:
            ok, buf = cv2.imencode(".png", fake_frame())
            assert ok
            return buf.tobytes()

    class FakeTouchscreen:
        @staticmethod
        def tap(x, y):
            FakePage.actions.append(f"tap({x},{y})")

    class FakeMouse:
        @staticmethod
        def move(x, y, steps=1):
            pass

        @staticmethod
        def down():
            pass

        @staticmethod
        def up():
            pass

    class FakeKeyboard:
        @staticmethod
        def press(key):
            pass

    class FakePage:
        actions: list[str] = []
        touchscreen = FakeTouchscreen()
        mouse = FakeMouse()
        keyboard = FakeKeyboard()
        viewport_size = {"width": 480, "height": 800}

        @staticmethod
        def wait_for_timeout(ms):
            pass

    class Args:
        seconds = 1e9          # the row cap ends the loop, not the clock
        max_rows = 14
        temperature = 0.4

    def stub_brain(context, question, tier="strategic", **_kw):
        return "stay near cover and keep the bar above half"
    brain.think = stub_brain

    # the game_over sprite: cut from a synthetic card frame into a temp
    # templates/ dir so the death path can actually fire offline
    tmpl = tempfile.TemporaryDirectory(prefix="p1v2_play_tmpl_")
    sprite_gray = cv2.cvtColor(
        draw_card(np.random.default_rng(99).integers(
            0, 64, size=(800, 480, 3), dtype=np.uint8))[CARD[0]:CARD[1],
                                                        CARD[2]:CARD[3]],
        cv2.COLOR_BGR2GRAY)
    cv2.imwrite(str(Path(tmpl.name) / "game_over.png"), sprite_gray)
    real_tmpl_dir = perceive_module.TEMPLATE_DIR
    perceive_module.TEMPLATE_DIR = Path(tmpl.name)

    def uniform(_context, options):
        return [1.0 / len(options)] * len(options)

    try:
        summary = session_loop(FakePage(), FakeScreen(), uniform,
                               zone_geometry(VIEWPORT), random.Random(7),
                               Args(), rows_path)
    finally:
        perceive_module.TEMPLATE_DIR = real_tmpl_dir
        tmpl.cleanup()
    rows = [json.loads(line) for line in
            rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    print(f"loop        : {summary['decisions']} rows, "
          f"{summary['deaths']} death(s), {summary['strategic_calls']} "
          f"ladder call(s), {summary['elapsed_s']:.1f}s")
    print(f"histogram   : {summary['action_histogram']}")
    with_advice = [r for r in rows if "strategic_advice" in r]
    with_death = [r for r in rows if "death_note" in r]
    print(f"rows        : {len(with_advice)} with strategic_advice, "
          f"{len(with_death)} with death_note "
          f"(rows {[rows.index(r) for r in with_advice]})")
    assert len(rows) == summary["decisions"] == 14, len(rows)
    assert all(r["context"].startswith("motion:") for r in rows), rows[0]
    assert all("menu:no" in r["context"] or "menu:yes" in r["context"]
               for r in rows)
    # every 5th decision (0, 5, 10) asked the ladder for advice
    assert [rows.index(r) for r in with_advice] == [0, 5, 10], \
        [rows.index(r) for r in with_advice]
    assert all(r["strategic_advice"] == "stay near cover and keep the bar "
               "above half" for r in with_advice)
    # the game_over card showed up -> at least one death row with a note
    assert with_death and "game_over:yes" in with_death[0]["context"], \
        with_death and with_death[0]["context"]
    assert all("death_note" in r for r in with_death)
    print(f"sample row  : {json.dumps(with_advice[0], ensure_ascii=False)[:160]}")
    print(f"death row   : {json.dumps(with_death[0], ensure_ascii=False)[:160]}")
    print("selftest: PASS — loop, every-5th strategic ladder call, death "
          "telemetry + restart, and the rows file verified offline "
          f"({rows_path})")
    return 0


def main() -> int:
    args = parse_args()
    if args.selftest:
        return _selftest()
    root = Path(args.root).expanduser()
    try:
        width, height = (int(part) for part in args.viewport.lower().split("x"))
    except ValueError:
        print(f"play: --viewport wants WxH, got {args.viewport!r}", file=sys.stderr)
        return 2
    viewport = (width, height)
    url = args.url or f"https://retromonkey.com.au/games/{args.game}/"
    out_root = Path(args.out_root) if args.out_root \
        else root / "data" / f"{args.game}-evolve"
    merge_base = None if (args.merge_to or "").lower() == "none" \
        else Path(args.merge_to) if args.merge_to else root / "data" / args.game
    if not args.strategic_every or args.strategic_every < 1:
        print("play: --strategic-every wants >= 1", file=sys.stderr)
        return 2
    global STRATEGIC_EVERY
    STRATEGIC_EVERY = args.strategic_every

    stamp = time.strftime("%Y%m%d-%H%M%S")
    rows_path = out_root / f"selfplay-{stamp}.jsonl"
    out_root.mkdir(parents=True, exist_ok=True)
    geometry = zone_geometry(viewport)
    scorer, scorer_label = build_scorer(args.brain)
    log({"play": args.game, "url": url, "scorer": scorer_label,
         "yolo": yolo_state(), "tick_seconds": TICK_SECONDS,
         "strategic_every": STRATEGIC_EVERY, "seconds": args.seconds,
         "max_rows": args.max_rows, "record": args.record,
         "rows_file": rows_path.as_posix(), "seed": args.seed})

    pw = browser = page = None
    try:
        pw, browser, page = open_page(url, viewport, headless=not args.headed)
        page.wait_for_timeout(int(BOOT_SETTLE_SECONDS * 1000))
        start_attempts(page)
        summary = session_loop(page, Screen(page), scorer, geometry,
                               rng=random.Random(args.seed), args=args,
                               rows_path=rows_path if args.record else
                               out_root / f"selfplay-{stamp}.dryrun.jsonl")
    except KeyboardInterrupt:
        log({"interrupted": True})
        return 0
    except Exception as exc:  # noqa: BLE001 — one bad session reports, honestly
        log({"error": f"{type(exc).__name__}: {exc}"})
        return 1
    finally:
        if browser is not None:
            browser.close()
        if pw is not None:
            pw.stop()

    if args.record:
        rows = [json.loads(line) for line in
                rows_path.read_text(encoding="utf-8").splitlines()
                if line.strip()] if rows_path.exists() else []
        trainable = [row for row in rows if row.get("action")]
        if trainable:
            merge_rows(trainable, out_root, out_root)
            if merge_base is not None:
                merge_rows(trainable, merge_base, merge_base)
            log({"merge_note": f"{len(rows) - len(trainable)} death-row(s) "
                               f"kept out of training (no action token)"})
        else:
            log({"merge": "skipped", "why": "no trainable rows recorded"})
    log({"summary": summary})
    return 0 if summary["decisions"] else 1


if __name__ == "__main__":
    sys.exit(main())
