"""PlayerOne GENERIC player — any game, zero per-game code.

The frontier lane from STUDY.md: the generic brain opens ANY game url and
plays it with a fixed geometry-derived action vocabulary and a context it
derives from the raw pixels (or, in vision mode, from a vision model's
description of them). Nothing about the game is known in advance — no
playthrough module, no start routine, no per-game pixel law. This is the
bottom rung of the ladder in GENERIC.md: the generic brain plays every game
badly, rows accumulate per game, and the weekly evolve fine-tunes a per-game
brain from exactly this corpus. Specialization emerges from the histogram,
not from hand-written modules.

    python cloud/playerone/generic_player.py \\
        --url https://retromonkey.com.au/games/sonar/ --game sonar --seconds 90

ZONE ACTION VOCABULARY (fixed; geometry from viewport thirds of the 480x800
phone law, swipes are 220 px drags from centre — no game knowledge):

    tap_center tap_left tap_right tap_top tap_bottom
    swipe_up   swipe_down swipe_left swipe_right wait

CONTEXT MODES
    pixels (default, free): two CDP screenshots 400 ms apart -> per-third
        brightness + abs-diff motion zone + centre activity + a stable frame
        counter, e.g. "motion left | bright top | calm center | frame 42".
        Pure stdlib PNG decode — cheap, deterministic, game-agnostic.
    vision (--context-mode vision): every 4th decision the screenshot is
        POSTed to NVIDIA llama-3.2-11b-vision-instruct (the vision_critic
        call shape) for a <=12-word player's-eye state description; the text
        is cached between calls; ANY error (no NVAPI_KEY, 5xx, empty reply)
        falls back to the pixels context.

LOOP: screenshot pair -> context -> scorer.probabilities(context, ZONES) ->
sample at temp 0.4 -> execute on the real page -> row {context, options,
action} -> settle 0.8-1.5s. Anti-stuck law: 5 consecutive wait choices force
a random tap, so even a game that ignores the generic vocabulary keeps
generating input rows. Cap 120 rows per session.
"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import random
import re
import struct
import sys
import time
import urllib.request
import zlib
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path("/home/ubuntu/playerone")
VIEWPORT = (480, 800)       # the phone frame every other lane reads
FRAME_GAP_MS = 400          # pixels mode: the shot-pair gap
SHOT_TIMEOUT_MS = 8000      # page.screenshot fallback budget
GOTO_TIMEOUT_MS = 60_000    # live sites on a small box: be patient
BOOT_SETTLE_SECONDS = 2.0   # collect_evidence's settle-for-first-frame law
START_BEAT_MS = 1200        # collect_evidence's tap-past-the-menu cadence
SETTLE_RANGE = (0.8, 1.5)   # seconds between decisions
TEMPERATURE = 0.4
MAX_ROWS = 120              # row cap per session
WAIT_STREAK_LIMIT = 5       # anti-stuck law
SWIPE_PX = 220              # drag reach from centre
FAIL_LIMIT = 5              # consecutive failed frames before bailing
VISION_EVERY = 4            # refresh the vision description every Nth decision
VISION_TIMEOUT = 45
VISION_MODEL = "meta/llama-3.2-11b-vision-instruct"
VISION_API = "https://integrate.api.nvidia.com/v1/chat/completions"
VISION_PROMPT = ("You are a player about to move in a game. Describe the game "
                 "state for a player in <=12 words using zones "
                 "(left/center/right, top/bottom) and any obvious goal/hazard.")
DEFAULT_BRAIN = "brains/generic-v1.npz"  # trained on the generic DOM vocabulary

ZONE_ACTIONS = ("tap_center", "tap_left", "tap_right", "tap_top", "tap_bottom",
                "swipe_up", "swipe_down", "swipe_left", "swipe_right", "wait")
FRAME_RE = re.compile(r" \| frame \d+$")


def log(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


# -- the fixed zone vocabulary -------------------------------------------------

def zone_geometry(viewport: tuple[int, int]) -> dict[str, tuple]:
    """The 10 option strings -> their real-page coordinates. Thirds of the
    viewport and a 220 px swipe reach; the only input is the screen shape."""
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


# -- stdlib PNG decode (the pixels mode's whole dependency) --------------------

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png(png: bytes):
    """8-bit PNG -> (width, height, brightness(x, y) -> 0..255).

    zlib plus the five scanline filters, nothing else — screenshots are
    opaque so alpha is ignored. Raises on anything exotic."""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    pos, width, height, depth, color = 8, 0, 0, 0, 0
    idat, palette = bytearray(), b""
    while pos + 8 <= len(png):
        length, ctype = struct.unpack_from(">I4s", png, pos)
        data = png[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            width, height, depth, color = struct.unpack_from(">IIBB", data)
        elif ctype == b"PLTE":
            palette = data
        elif ctype == b"IDAT":
            idat += data
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if depth != 8:
        raise ValueError(f"unsupported bit depth {depth}")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if channels is None:
        raise ValueError(f"unsupported colour type {color}")
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    rows = bytearray(height * stride)
    for y in range(height):
        src = y * (stride + 1)
        ftype = raw[src]
        line = bytearray(raw[src + 1:src + 1 + stride])
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            above = rows[max(y - 1, 0) * stride:y * stride] if y else bytes(stride)
            for i in range(stride):
                line[i] = (line[i] + above[i]) & 0xFF
        elif ftype == 3:
            above = rows[(y - 1) * stride:y * stride] if y else bytes(stride)
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + above[i]) >> 1)) & 0xFF
        elif ftype == 4:
            above = rows[(y - 1) * stride:y * stride] if y else bytes(stride)
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                upleft = above[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, above[i], upleft)) & 0xFF
        rows[y * stride:(y + 1) * stride] = line

    if color == 3:  # palette
        def brightness(x, y, _rows=rows, _pal=palette):
            o = _rows[y * stride + x] * 3
            return (_pal[o] * 299 + _pal[o + 1] * 587 + _pal[o + 2] * 114) // 1000
    elif color == 2:
        def brightness(x, y, _rows=rows):
            o = y * stride + x * 3
            return (_rows[o] * 299 + _rows[o + 1] * 587 + _rows[o + 2] * 114) // 1000
    elif color == 6:
        def brightness(x, y, _rows=rows):
            o = y * stride + x * 4
            return (_rows[o] * 299 + _rows[o + 1] * 587 + _rows[o + 2] * 114) // 1000
    else:  # 0 gray, 4 gray+alpha
        def brightness(x, y, _rows=rows):
            return _rows[y * stride + x * channels]
    return width, height, brightness


# -- the pixels context law ------------------------------------------------------

GRID_COLS, GRID_ROWS, SAMPLE_STEP = 12, 20, 4
CELL_DIFF_THRESHOLD = 8.0    # mean-brightness delta that marks a cell "moving"
ZONE_ORDER = ("left", "right", "top", "bottom", "center")


def cell_means(brightness, width: int, height: int) -> list[float]:
    """Mean brightness per grid cell, strided sampling (~24k reads a frame)."""
    cell_w, cell_h = width / GRID_COLS, height / GRID_ROWS
    means = []
    for r in range(GRID_ROWS):
        y0, y1 = int(r * cell_h), max(int(r * cell_h) + 1, int((r + 1) * cell_h))
        for c in range(GRID_COLS):
            x0, x1 = int(c * cell_w), max(int(c * cell_w) + 1, int((c + 1) * cell_w))
            total = count = 0
            for y in range(y0, y1, SAMPLE_STEP):
                for x in range(x0, x1, SAMPLE_STEP):
                    total += brightness(x, y)
                    count += 1
            means.append(total / max(count, 1))
    return means


def motion_of(before: list[float], after: list[float]) -> tuple[str, int, int]:
    """abs-diff blob zone: which third the movement lives in. Returns
    (zone, moving cells, moving cells in the centre block). Deterministic —
    ties break in ZONE_ORDER."""
    moving = [i for i, (a, b) in enumerate(zip(before, after))
              if abs(a - b) >= CELL_DIFF_THRESHOLD]
    if not moving:
        return "none", 0, 0
    cols = [i % GRID_COLS for i in moving]
    grid_rows = [i // GRID_COLS for i in moving]
    cw, ch = GRID_COLS // 3, GRID_ROWS // 3
    counts = {
        "left": sum(1 for c in cols if c < cw),
        "right": sum(1 for c in cols if c >= GRID_COLS - cw),
        "top": sum(1 for r in grid_rows if r < ch),
        "bottom": sum(1 for r in grid_rows if r >= GRID_ROWS - ch),
        "center": sum(1 for c, r in zip(cols, grid_rows)
                      if cw <= c < GRID_COLS - cw and ch <= r < GRID_ROWS - ch),
    }
    zone = max(ZONE_ORDER, key=lambda name: counts[name])
    return zone, len(moving), counts["center"]


def bright_word(value: float) -> str:
    return ("dark" if value < 64 else "dim" if value < 128
            else "lit" if value < 192 else "bright")


def activity_word(center_moving: int, moving: int) -> str:
    if moving == 0:
        return "frozen"
    if center_moving == 0:
        return "calm"
    if center_moving <= 4:
        return "stirring"
    if center_moving <= 10:
        return "busy"
    return "wild"


def pixels_context(index: int, png_before: bytes, png_after: bytes) -> str:
    """The generic text context from two screenshots: motion zone, brightest
    third, centre activity, frame counter. No game knowledge anywhere."""
    w1, h1, before = decode_png(png_before)
    _, _, after = decode_png(png_after)
    before_means = cell_means(before, w1, h1)
    after_means = cell_means(after, w1, h1)
    zone, moving, center_moving = motion_of(before_means, after_means)
    third = (GRID_ROWS // 3) * GRID_COLS
    bands = {
        "top": after_means[:third],
        "mid": after_means[third:2 * third],
        "bottom": after_means[2 * third:],
    }
    band_mean = {name: sum(values) / len(values) for name, values in bands.items()}
    best = max(band_mean, key=lambda name: band_mean[name])
    return (f"motion {zone} | {bright_word(band_mean[best])} {best} | "
            f"{activity_word(center_moving, moving)} center | frame {index}")


# -- the vision context law ------------------------------------------------------

def vision_describe(png: bytes, game: str, key: str) -> str:
    """NVIDIA llama-3.2-11b (the vision_critic call shape): one screenshot in,
    a <=12-word player's-eye description out."""
    b64 = base64.b64encode(png).decode("ascii")
    payload = json.dumps({
        "model": VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + b64}},
                {"type": "text", "text": VISION_PROMPT + " Game: " + game},
            ],
        }],
        "max_tokens": 64,
        "temperature": 0.2,
        "top_p": 1,
    }).encode()
    req = urllib.request.Request(VISION_API, data=payload, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=VISION_TIMEOUT) as resp:
        body = json.loads(resp.read().decode())
    text = str(body["choices"][0]["message"].get("content", "")).strip()
    if not text:
        raise RuntimeError("empty vision reply")
    return " ".join(text.split())[:160]


class ContextProvider:
    """Context per decision. vision mode refreshes every VISION_EVERY-th
    decision from the live screenshot and caches between calls; any error
    (missing NVAPI_KEY included) degrades to the pixels law."""

    def __init__(self, mode: str, game: str, width: int, height: int):
        self.mode = mode
        self.game = game
        self.width = width
        self.height = height
        self.key = os.environ.get("NVAPI_KEY", "").strip()
        self.cache: str | None = None
        self.calls = 0
        self.fallbacks = 0

    def context(self, index: int, png_before: bytes, png_after: bytes) -> str:
        if self.mode == "vision" and self.key:
            if index % VISION_EVERY == 0 or self.cache is None:
                try:
                    self.cache = vision_describe(png_after, self.game, self.key)
                    self.calls += 1
                    return self.cache
                except Exception as exc:
                    self.fallbacks += 1
                    log({"vision": "fallback", "step": index,
                         "why": f"{type(exc).__name__}: {str(exc)[:120]}"})
            elif self.cache is not None:
                return self.cache
        return pixels_context(index, png_before, png_after)


# -- session surface -------------------------------------------------------------

class Screen:
    """The CDP screenshot law from collect_evidence.py, as PNG bytes."""

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


def start_attempts(page) -> None:
    """Tap past a title screen with ZERO game knowledge — collect_evidence's
    proven opener (Enter, then two centre taps), best-effort. A game already
    in play ignores it."""
    for label, action in (
            ("enter", lambda: page.keyboard.press("Enter")),
            ("tap1", lambda: page.touchscreen.tap(page.viewport_size["width"] // 2,
                                                  page.viewport_size["height"] // 2)),
            ("tap2", lambda: page.touchscreen.tap(page.viewport_size["width"] // 2,
                                                  page.viewport_size["height"] // 2 + 30))):
        try:
            action()
            log({"start_attempt": label})
        except Exception as exc:  # noqa: BLE001 — never kill the run here
            log({"start_attempt": label,
                 "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
        page.wait_for_timeout(START_BEAT_MS)


def open_page(url: str, viewport: tuple[int, int], headless: bool = True):
    """One phone-frame page at the url — fresh context, collect_evidence's
    no-cookie-carryover law."""
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


def execute(page, action: str, geometry: dict) -> bool:
    """Run one zone action on the real page. Taps ride the touchscreen (phone
    law), swipes drag the mouse — pointer events either way."""
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


# -- scorer ----------------------------------------------------------------------

def build_scorer(args):
    """-> (probabilities(context, options) -> list[float], label). --scorer-module
    is the fixture lane; otherwise the exported brain (default: the generic v1)."""
    if args.scorer_module:
        spec = args.scorer_module
        if spec.endswith(".py"):
            path = Path(spec)
            if not path.is_file():
                raise FileNotFoundError(f"no such scorer module: {path}")
            import importlib.util
            module_spec = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[path.stem] = module
            module_spec.loader.exec_module(module)
        else:
            import importlib
            module = importlib.import_module(spec)
        if not callable(getattr(module, "probabilities", None)):
            raise TypeError(f"{spec}: scorer modules must expose "
                            "probabilities(context, options) -> list[float]")
        return module.probabilities, f"module:{Path(spec).name}"

    brain = Path(args.brain)
    if not brain.is_file():
        raise FileNotFoundError(
            f"brain not found: {brain} — the generic v1 ships with the deploy; "
            "point --brain at an exported .npz or use --scorer-module")
    root = Path(args.root)
    for candidate in (root / "examples" / "pipeline",
                      root / "examples" / "pipeline" / "scripts"):
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    from numpy_scorer import NumpyScorer
    return NumpyScorer(brain).probabilities, f"NumpyScorer:{brain.name}"


def choose(options: list[str], probs: list[float], temperature: float,
           rng: random.Random) -> tuple[str, float, float]:
    """Sample the brain's distribution at temperature: p**(1/T) renormalised —
    the selfplay law verbatim. Returns (action, brain prob, sampling share)."""
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
    return chosen, probs[index], scaled[index] / total


# -- merge law (selfplay verbatim) -------------------------------------------------

def append_rows(path: Path, rows: list[dict]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def merge_rows(rows: list[dict], base: Path, label) -> None:
    """Merge law: append everything to train.jsonl; seed validation.jsonl
    90/10 the first time only — fresh play never leaks into the held-out
    split. This is what makes evolve.sh (which reads data/<game>/train.jsonl)
    find the week."""
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


# -- the loop ----------------------------------------------------------------------

def play(page, screen, provider, probabilities, geometry, rng, args,
         rows_path: Path) -> dict:
    """Decide until --seconds or --max-rows runs out. Returns the session
    summary payload (the difficulty fingerprint lives in action_histogram)."""
    taps = [name for name in ZONE_ACTIONS if name.startswith("tap_")]
    histogram: collections.Counter = collections.Counter()
    scenes: set[str] = set()
    rows = 0
    wait_streak = 0
    forced_taps = 0
    failures = 0
    started = time.monotonic()
    deadline = started + args.seconds

    while rows < args.max_rows and time.monotonic() < deadline:
        try:
            png_before = screen.png()
            page.wait_for_timeout(FRAME_GAP_MS)
            png_after = screen.png()
            context = provider.context(rows, png_before, png_after)
            failures = 0
        except Exception as exc:
            failures += 1
            log({"frame_error": f"{type(exc).__name__}: {str(exc)[:160]}",
                 "failures": failures})
            if failures >= FAIL_LIMIT:
                log({"bail": f"{failures} consecutive failed frames"})
                break
            page.wait_for_timeout(1000)
            continue

        probs = probabilities(context, list(ZONE_ACTIONS))
        action, chosen_p, share = choose(ZONE_ACTIONS, probs,
                                         args.temperature, rng)
        forced = None
        if action == "wait":
            wait_streak += 1
            if wait_streak >= WAIT_STREAK_LIMIT:
                forced = rng.choice(taps)  # anti-stuck law: force a random tap
                action, wait_streak = forced, 0
                forced_taps += 1
        else:
            wait_streak = 0

        if action == "wait":
            page.wait_for_timeout(500)
        else:
            execute(page, action, geometry)

        rows += 1
        histogram[action] += 1
        scenes.add(FRAME_RE.sub("", context))
        row = {"context": context, "options": list(ZONE_ACTIONS),
               "action": action}
        with rows_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        top = ZONE_ACTIONS[max(range(len(probs)), key=lambda i: probs[i])]
        log({"step": rows, "action": action,
             "p": round(float(chosen_p), 3), "w": round(float(share), 3),
             "top": top, "top_p": round(max(float(p) for p in probs), 3),
             "context": context,
             **({"forced": forced} if forced else {}),
             "rows_file": rows_path.name})
        page.wait_for_timeout(int(rng.uniform(*SETTLE_RANGE) * 1000))

    return {
        "decisions": rows,
        "action_histogram": dict(sorted(histogram.items(),
                                        key=lambda kv: -kv[1])),
        "distinct_contexts": len(scenes),
        "forced_taps": forced_taps,
        "vision_calls": provider.calls,
        "vision_fallbacks": provider.fallbacks,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


# -- cli -----------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="PlayerOne GENERIC player — any game, zero per-game code",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="any game url")
    parser.add_argument("--game", required=True,
                        help="slug — names the data dirs (data/<game>-evolve/)")
    parser.add_argument("--seconds", type=float, default=90.0,
                        help="session length in seconds (default 90)")
    parser.add_argument("--brain",
                        help=f"exported .npz brain (default the generic v1: "
                             f"{DEFAULT_BRAIN} under --root)")
    parser.add_argument("--scorer-module",
                        help="duck-typed scorer .py exposing "
                             "probabilities(context, options) — fixture runs")
    parser.add_argument("--context-mode", choices=("pixels", "vision"),
                        default="pixels",
                        help="pixels = CDP shot pair (default, free); "
                             "vision = NVIDIA llama-3.2-11b every 4th "
                             "decision, pixels on any error")
    parser.add_argument("--record", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="write rows + merge (default on)")
    parser.add_argument("--root", default=str(ROOT),
                        help="playerone repo root for derived defaults "
                             f"(default {ROOT.as_posix()})")
    parser.add_argument("--out-root",
                        help="where rows land (default "
                             "<root>/data/<game>-evolve)")
    parser.add_argument("--merge-to",
                        help="second merge target, what evolve.sh reads "
                             "(default <root>/data/<game>; 'none' disables)")
    parser.add_argument("--max-rows", type=int, default=MAX_ROWS,
                        help=f"row cap per session (default {MAX_ROWS})")
    parser.add_argument("--temperature", type=float, default=TEMPERATURE,
                        help=f"sampling sharpness (default {TEMPERATURE})")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--viewport", default="480x800")
    args = parser.parse_args(argv)
    if args.brain and args.scorer_module:
        parser.error("give at most one of --brain / --scorer-module")
    if args.context_mode == "vision" and \
            not os.environ.get("NVAPI_KEY", "").strip():
        # the vision tier needs the key up front; say so before browsing
        log({"vision": "no NVAPI_KEY — every decision falls back to pixels"})
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    root = Path(args.root).expanduser()
    try:
        width, height = (int(part) for part in
                         args.viewport.lower().split("x"))
    except ValueError:
        print(f"generic_player: --viewport wants WxH, got {args.viewport!r}",
              file=sys.stderr)
        return 2
    viewport = (width, height)
    out_root = Path(args.out_root) if args.out_root \
        else root / "data" / f"{args.game}-evolve"
    if args.merge_to:
        merge_base = None if args.merge_to.lower() == "none" \
            else Path(args.merge_to)
    else:
        merge_base = root / "data" / args.game
    brain = args.brain or (root / DEFAULT_BRAIN).as_posix()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    rows_path = out_root / f"selfplay-{stamp}.jsonl"
    summary_path = out_root / f"summary-{stamp}.json"
    out_root.mkdir(parents=True, exist_ok=True)
    geometry = zone_geometry(viewport)

    try:
        probabilities, scorer_label = build_scorer(args)
    except Exception as exc:
        log({"error": f"{type(exc).__name__}: {exc}"})
        return 1
    log({"generic_player": args.game, "url": args.url,
         "scorer": scorer_label, "context_mode": args.context_mode,
         "actions": list(ZONE_ACTIONS), "viewport": args.viewport,
         "seconds": args.seconds, "max_rows": args.max_rows,
         "temperature": args.temperature, "record": args.record,
         "rows_file": rows_path.as_posix(),
         "merge_to": merge_base.as_posix() if merge_base else "none",
         "seed": args.seed})

    pw = browser = page = None
    try:
        pw, browser, page = open_page(args.url, viewport)
        page.wait_for_timeout(int(BOOT_SETTLE_SECONDS * 1000))
        start_attempts(page)
        screen = Screen(page)
        provider = ContextProvider(args.context_mode, args.game,
                                   width, height)
        summary = play(page, screen, provider, probabilities, geometry,
                       rng=random.Random(args.seed), args=args,
                       rows_path=rows_path if args.record else
                       out_root / f"selfplay-{stamp}.dryrun.jsonl")
    except KeyboardInterrupt:
        log({"interrupted": True})
        summary = {"decisions": 0, "action_histogram": {},
                   "distinct_contexts": 0, "forced_taps": 0, "vision_calls": 0,
                   "vision_fallbacks": 0, "elapsed_s": 0.0}
    except Exception as exc:
        log({"error": f"{type(exc).__name__}: {exc}"})
        return 1
    finally:
        if browser is not None:
            browser.close()
        if pw is not None:
            pw.stop()

    summary.update({"game": args.game, "url": args.url,
                    "context_mode": args.context_mode, "scorer": scorer_label,
                    "brain": brain, "record": args.record})
    out_root.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    log({"summary": summary_path.as_posix(), **summary})

    if args.record:
        rows = []
        if rows_path.exists():
            rows = [json.loads(line)
                    for line in
                    rows_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        if rows:
            merge_rows(rows, out_root, out_root)
            if merge_base is not None:
                merge_rows(rows, merge_base, merge_base)
        else:
            log({"merge": "skipped", "why": "no rows recorded"})
    return 0 if summary["decisions"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
