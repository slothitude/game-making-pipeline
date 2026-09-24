#!/usr/bin/env python3
"""brain — PlayerOne's jevlike spinal cord: perception state dict -> gestures.

The last rung of the PlayerOne 2.0 wiring. The eyes (perception.py v2) hand
over a fused state dict; this module turns it into input_bridge gestures by
way of the trained jevlike brains — numpy only, no torch (the worker venv has
none; numpy_scorer.py is the parity-proven forward pass, retromonkey's own).

    decide(state, game, profile) -> [Gesture]     # the whole reflex arc

THE INPUT ENCODING (measured, not guessed — this is the load-bearing finding):
the jevlike lineage is NOT a float-vector policy. It is a byte-level OPTION
SCORER (jevlike/model.py TinyScorer): one context string + N option strings ->
N logits through embedding(257,W) + position(W) + one attention head. The
synthetic root checkpoint (runs/synthetic.pt, 99.75% top-1 on its held-out
badge task, shuffled-context control 22.75%) was trained on

    "Choose the exact badge {colour animal}. Notes: {8 compass words}.
     Badge: {colour animal}."          options: "{colour animal}" pairs

and the game brains on recorded playthrough rows whose context/options come
from state_format.py + the deployed playthrough modules. Byte-exact training
formats (read off the rows themselves, data/*/train.jsonl):

    sonar   (playerone-sonar.npz, 303 rows, width 64 rank 64):
        context  "SONAR air {air%}; jelly {jelly}; sub {lane}; echo {yes|no}; ping {ready|cool}"
                 jelly = "{BEARING} {range}" (nearest creature, bearing FROM it
                         TO the sub) or "none"; lane = far-left|left|mid|right|far-right
        options  8 drag strings from (240, 430) to the HEADINGS table below,
                 + "tap (240, 717)" (the sonar button) + "wait"

    slime   (playerone-slime.npz, 301 rows, width 128 rank 128):
        context  "SLIME gap {gap}; lead {BEARING}; tilt {left|mid|right}"
                 gap = touch|near|mid|far at 18/60/150 px; lead = 8-wind bearing
                 of the thing steered TOWARD. THREE fields — the deployed
                 slime_playthrough.py now writes four (added "speed"); the
                 training rows have three, so the four-field form is OFF
                 distribution and is never emitted here.
        options  the 8 drag strings + "wait"

    star-visitor: no checkpoint in the lineage was trained on it. The lane
        maps it onto the slime brain (both are dodge-the-incoming-thing
        games with 8-way steering): gap = nearest-threat range on the slime
        bands, lead = the ESCAPE bearing (away from the threat — slime's
        "steer toward lead" semantic then steers away from danger), tilt =
        the player lane. Documented transfer, benchmarked as such.

Anything else is not encoded: decide() returns [] (the do-nothing class) with
a reason in the meta — an untrained game is a missing sense, never a guess.

DECODING: the scorer picks an option STRING; decode() parses it into an intent
("drag from ..." -> dodge_{left,right,up,down} by dominant axis, sonar's
"tap (240, 717)" -> ping, "wait" -> none) and input_bridge.from_intent() turns
the intent into real gestures through the game's own profile. Option strings
never become coordinates directly — the profile owns the pixels.

KNOBS: TEMPERATURE (p**(1/T) sampling, selfplay's law; 0 = argmax) and
CONFIDENCE_FLOOR (below it the brain does nothing rather than act on a guess).
The gesture stream is budgeted (GESTURE_BUDGET_PER_SECOND) — no input spam.

Selftest (offline, no browser, no network; green x2 by law):

    python3 brain.py --selftest
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:          # numpy_scorer lives beside this file
    sys.path.insert(0, str(HERE))
_ROOT = Path(os.environ.get("PLAYERONE_ROOT", HERE.parent))
for _cand in (str(_ROOT), str(_ROOT / "scripts")):
    if os.path.isdir(_cand) and _cand not in sys.path:
        sys.path.append(_cand)          # input_bridge + the playthrough twins

BRAIN_DIR = Path(os.environ.get("P1_BRAIN_DIR", str(HERE)))

# ------------------------------------------------------------------ constants --
DECISION_HZ = 12.0              # ceiling: the fused frame costs 33.6 ms (~15 Hz)
GESTURE_BUDGET_PER_SECOND = 5.0 # the anti-spam law: <=5 gestures land per second
TEMPERATURE = 0.35              # selfplay's sampling sharpness; 0 = greedy
CONFIDENCE_FLOOR = 0.34         # under this the brain holds its fire
DEFAULT_PROB_FLOOR = 1e-9       # sampling guard, selfplay's law

# the 8-way steering anchor table — byte-identical to the training rows
STEER_CENTRE = (240, 430)
HEADINGS = {
    "N": (240, 260), "NE": (360, 310), "E": (410, 430), "SE": (360, 550),
    "S": (240, 600), "SW": (120, 550), "W": (70, 430), "NW": (120, 310),
}
SONAR_BUTTON = (240, 717)
WINDS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

# the lineage: game -> deployed npz. Transfer entries are commented as such.
GAME_BRAINS = {
    "sonar": "playerone-sonar.npz",         # exact: trained on data/sonar
    "slime": "playerone-slime.npz",         # exact: trained on data/slime
    "slime-line": "playerone-slime.npz",
    "star-visitor": "playerone-slime.npz",  # TRANSFER (see the docstring)
}
DEFAULT_BRAIN = "synthetic.npz"             # the 99.75% badge root — no game
                                            # encoding, so it never steers

# sonar HUD probes (numpy twins of sonar_playthrough's pixel laws — the eyes
# are blind to the air bar, the ping ring and the echo flash, so the lane
# measures them the way the training recorder did; see aux_sonar())
AIR_BOX = (8, 12, 170, 24)
RING_BOX = (185, 660, 295, 775)
PLAY_BOX = (4, 40, 476, 640)
PING_READY_RING = 9.5
ECHO_PIXELS = 150
HULL_TITLE = 3000        # hull pixels at/above which the title logo is up
HULL_OVER = 120          # hull pixels under which the sub is gone

DRAG_RE = re.compile(r"^drag from \((\d+), (\d+)\) to \((\d+), (\d+)\)$")
TAP_RE = re.compile(r"^tap \((\d+), (\d+)\)$")


def say(msg: str) -> None:
    print(f"[brain] {msg}", flush=True)


# ------------------------------------------------------------ geometry twins --
def bearing(fx: float, fy: float) -> str:
    """8-wind compass bearing of an offset — vision_state.bearing verbatim
    (0 = up, clockwise; offsets under 0.04 of a unit read as C). math on
    purpose: the twin must agree to the bit, not to a numpy promotion."""
    reach = math.hypot(fx, fy)
    if reach < 0.04:
        return "C"
    angle = math.degrees(math.atan2(fx, -fy))  # 0 = up, clockwise
    return WINDS[int(((angle + 22.5) % 360) // 45)]


def range_of(reach: float) -> str:
    """vision_state.range_of verbatim."""
    return "near" if reach < 0.2 else "mid" if reach < 0.42 else "far"


def sonar_lane(x: float, frame_w: int) -> str:
    """sonar_playthrough.lane_of on the 480-wide training frame, scaled to
    whatever width the eyes captured at."""
    x = x * 480.0 / max(1, frame_w)
    if x < 160:
        return "far-left"
    if x < 215:
        return "left"
    if x > 320:
        return "far-right"
    if x > 265:
        return "right"
    return "mid"


def slime_lane(x: float, frame_w: int) -> str:
    """slime_playthrough.lane_of (200/280 on the 480-wide frame), scaled."""
    x = x * 480.0 / max(1, frame_w)
    return "left" if x < 200 else "right" if x > 280 else "mid"


def _centre(det: tuple) -> tuple[float, float]:
    """A perception detection (cls, x, y, w, h, conf) -> its centre."""
    return (det[1] + det[3] / 2.0, det[2] + det[4] / 2.0)


def nearest_threat(state: dict) -> tuple | None:
    """The perception threat nearest the player, or None."""
    player = state.get("player")
    threats = state.get("threats") or []
    if player is None or not threats:
        return None
    px, py = player[0], player[1]
    return min(threats, key=lambda d: (_centre(d)[0] - px) ** 2
               + (_centre(d)[1] - py) ** 2)


# ------------------------------------------------------------- HUD aux twins --
def _region_bright(rgb: np.ndarray, box: tuple, floor: float = 110.0) -> float:
    """sonar_playthrough/vision_state.region_bright, vectorised: the fraction
    of a box's pixels whose luma clears `floor`."""
    x0, y0, x1, y1 = box
    h, w = rgb.shape[:2]
    region = rgb[max(0, y0):min(h, y1), max(0, x0):min(w, x1)].astype(np.float32)
    if region.size == 0:
        return 0.0
    luma = 0.2126 * region[:, :, 0] + 0.7152 * region[:, :, 1] \
        + 0.0722 * region[:, :, 2]
    return float((luma > floor).mean())


def aux_sonar(frame_rgb: np.ndarray) -> dict:
    """The sonar fields perception cannot see: air %, ping readiness, echo,
    and the recorder's own scene verdict (title / playing / over).

    The exact thresholds of the training recorder (sonar_playthrough.measure):
    the air bar's bright fraction as a percent, the sonar button ring's bright
    per-mille against PING_READY_RING, the white echo arcs against
    ECHO_PIXELS, and the yellow-hull pixel count against the recorder's title
    law (> HULL_TITLE = the title logo, which the eyes otherwise read as a
    player; < HULL_OVER = the sub is gone). scene is the recorder's verdict:
    "title" | "over" (hull low AND air < 6) | "unknown" (hull low, air up) |
    "playing". These are the ONLY pixels this module reads itself.
    """
    air = round(_region_bright(frame_rgb, AIR_BOX) * 100)
    ring = round(_region_bright(frame_rgb, RING_BOX, floor=90.0) * 1000) / 10
    x0, y0, x1, y1 = PLAY_BOX
    h, w = frame_rgb.shape[:2]
    play = frame_rgb[max(0, y0):min(h, y1), max(0, x0):min(w, x1)].astype(np.int16)
    r, g, b = play[:, :, 0], play[:, :, 1], play[:, :, 2]
    echo = int(((r > 190) & (g > 210) & (b > 210)).sum())
    hull = int(((r >= 95) & (g >= 90) & (np.abs(r - g) < 45)
                & (r > b + 30) & (g > b + 25)).sum())
    if hull >= HULL_TITLE:
        scene = "title"
    elif hull < HULL_OVER:
        scene = "over" if air < 6 else "unknown"
    else:
        scene = "playing"
    return {"air": air, "ping": "ready" if ring >= PING_READY_RING else "cool",
            "echo": "yes" if echo >= ECHO_PIXELS else "no", "ring": ring,
            "hull": hull, "scene": scene}


# ----------------------------------------------------------------- encoding --
def _drag_options() -> list[str]:
    return [f"drag from ({STEER_CENTRE[0]}, {STEER_CENTRE[1]}) to ({x}, {y})"
            for x, y in HEADINGS.values()]


SONAR_OPTIONS = _drag_options() + [f"tap ({SONAR_BUTTON[0]}, {SONAR_BUTTON[1]})",
                                   "wait"]
SLIME_OPTIONS = _drag_options() + ["wait"]


def encode(state: dict, game: str, aux: dict | None = None) -> tuple | None:
    """Perception state dict -> (context, options) in the TRAINING byte format.

    None means "this game has no encoding" — the caller does nothing. aux
    carries the HUD fields the eyes are blind to (sonar: air/ping/echo); every
    aux field has a neutral default so a blind lane still produces valid,
    in-distribution text.
    """
    aux = aux or {}
    frame_w = (state.get("frame") or [480, 800])[0]
    player = state.get("player")
    if game == "sonar":
        air = int(aux.get("air", 100))
        ping = aux.get("ping", "ready")
        echo = aux.get("echo", "no")
        lane = sonar_lane(player[0], frame_w) if player else "mid"
        threat = nearest_threat(state)
        if threat is not None:
            tx, ty = _centre(threat)
            px, py = float(player[0]), float(player[1])
            reach = ((tx - px) ** 2 + (ty - py) ** 2) ** 0.5
            # the training law, verbatim: the bearing FROM the creature TO the
            # sub (the escape direction), the range on a 240x400-normalised diag
            away = bearing((px - tx) / 240.0, (py - ty) / 400.0)
            jelly = f"{away} {range_of(reach / 400.0)}"
        else:
            jelly = "none"
        return (f"SONAR air {air}; jelly {jelly}; sub {lane}; "
                f"echo {echo}; ping {ping}", list(SONAR_OPTIONS))
    if game in ("star-visitor", "slime", "slime-line"):
        lane = slime_lane(player[0], frame_w) if player else "mid"
        threat = nearest_threat(state)
        if threat is not None:
            tx, ty = _centre(threat)
            px, py = float(player[0]), float(player[1])
            dx, dy = tx - px, ty - py
            reach = (dx * dx + dy * dy) ** 0.5
            # the training bands (18/60/150 px on the 480-wide frame); the
            # touch band is unreachable — the transfer offers no eat-tap
            gap = ("near" if reach < 60 else "mid" if reach < 150 else "far")
            # TRANSFER LAW: slime's lead is the thing steered TOWARD (food);
            # star-visitor's nearest sprite is a threat, so lead carries the
            # ESCAPE bearing (away from it) and the slime brain's "steer
            # toward lead" then steers out of danger.
            lead = bearing((px - tx), (py - ty))
        else:
            gap, lead = "none", "none"
        return (f"SLIME gap {gap}; lead {lead}; tilt {lane}",
                list(SLIME_OPTIONS))
    return None


def decode(action: str, game: str) -> str | None:
    """Option string -> input_bridge intent. "wait" and anything unparsed are
    the do-nothing class (None), never a guessed gesture."""
    action = (action or "").strip()
    if action == "wait":
        return None
    if action in ("press ENTER", "press ESC"):
        return "start"
    if match := DRAG_RE.match(action):
        x1, y1, x2, y2 = match.groups()
        dx, dy = int(x2) - int(x1), int(y2) - int(y1)
        if abs(dx) >= abs(dy):
            return "dodge_left" if dx < 0 else "dodge_right"
        return "dodge_up" if dy < 0 else "dodge_down"
    if TAP_RE.match(action):
        return "ping" if game == "sonar" else "confirm"
    return None


# ------------------------------------------------------------------- budget --
class Budgeter:
    """The anti-spam law: at most GESTURE_BUDGET_PER_SECOND gestures land. A
    sliding 1-second window; combos count as their children."""

    def __init__(self, per_second: float = GESTURE_BUDGET_PER_SECOND) -> None:
        self.per_second = float(per_second)
        self._sent: deque = deque()

    def _weight(self, gestures: list) -> int:
        total = 0
        for gesture in gestures:
            total += len(gesture.args.get("children", [])) \
                if getattr(gesture, "kind", "") == "combo" else 1
        return max(1, total)

    def allow(self, gestures: list, now: float | None = None) -> list:
        """The prefix of `gestures` that fits this second's budget."""
        now = time.monotonic() if now is None else now
        while self._sent and now - self._sent[0] >= 1.0:
            self._sent.popleft()
        room = int(self.per_second) - len(self._sent)
        allowed: list = []
        for gesture in gestures:
            weight = self._weight([gesture])
            if weight > room:
                break
            allowed.append(gesture)
            room -= weight
        if allowed:
            self._sent.extend([now] * self._weight(allowed))
        return allowed


# -------------------------------------------------------------------- brain --
class JevlikeBrain:
    """One npz lineage checkpoint wrapped as a state-dict -> gestures policy."""

    def __init__(self, npz_name: str, brain_dir: Path | None = None) -> None:
        from numpy_scorer import NumpyScorer
        path = Path(brain_dir or BRAIN_DIR) / npz_name
        if not path.is_file():
            raise FileNotFoundError(f"brain checkpoint not found: {path}")
        self.npz_name = npz_name
        self.scorer = NumpyScorer(path)

    def probabilities(self, context: str, options: list[str]) -> list[float]:
        if len(options) < 2:                      # the scorer needs >= 2 options
            options = [*options, "wait"]
        return self.scorer.probabilities(context, options)

    def choose(self, options: list[str], probs: list[float],
               temperature: float = TEMPERATURE,
               rng: random.Random | None = None) -> tuple[int, float]:
        """selfplay's choose law: p**(1/T) renormalised, T=0 greedy. Returns
        (index, chosen prob)."""
        rng = rng or random.Random()
        if temperature <= 0:
            index = max(range(len(probs)), key=lambda i: probs[i])
            return index, float(probs[index])
        scaled = [max(float(p), DEFAULT_PROB_FLOOR) ** (1.0 / temperature)
                  for p in probs]
        total = sum(scaled)
        point, acc = rng.random() * total, 0.0
        for index, weight in enumerate(scaled):
            acc += weight
            if point <= acc:
                return index, float(probs[index])
        return len(options) - 1, float(probs[-1])


_BRAIN_CACHE: dict[str, JevlikeBrain] = {}


def brain_for(game: str) -> tuple[JevlikeBrain | None, str]:
    """(brain, encoding note) for a game — the lineage mapping, cached."""
    npz = GAME_BRAINS.get(game)
    if npz is None:
        return None, f"no encoding for {game!r} — do-nothing lane"
    if npz not in _BRAIN_CACHE:
        _BRAIN_CACHE[npz] = JevlikeBrain(npz)
    note = "exact training encoding"
    if game == "star-visitor":
        note = ("TRANSFER: slime brain on star-visitor perception "
                "(no star-visitor checkpoint exists in the lineage)")
    return _BRAIN_CACHE[npz], note


def reflex(state: dict, game: str, profile: dict, aux: dict | None = None,
           temperature: float = TEMPERATURE, rng: random.Random | None = None,
           confidence_floor: float = CONFIDENCE_FLOOR,
           budget: Budgeter | None = None) -> tuple[list, dict]:
    """decide() with its inner monologue attached: ONE scoring pass returns
    both the gestures and why. The loop lane logs info; it never re-scores."""
    from input_bridge import from_intent      # the hands, imported late
    brain, note = brain_for(game)
    info = {"game": game, "brain": brain.npz_name if brain else None,
            "note": note, "context": None, "action": "wait", "intent": None,
            "p": 0.0, "gestures": 0}
    if brain is None:
        return [], info
    encoded = encode(state, game, aux)
    if encoded is None:
        return [], info
    context, options = encoded
    info["context"] = context
    probs = brain.probabilities(context, options)
    index, top = brain.choose(options, probs, temperature, rng)
    info.update({"action": options[index], "p": round(top, 4)})
    intent = decode(options[index], game)
    info["intent"] = intent
    if intent is None or top < confidence_floor:
        return [], info
    gestures = from_intent(profile, intent, {"viewport": state.get("frame")})
    if budget is not None:
        gestures = budget.allow(gestures)
    info["gestures"] = len(gestures)
    return gestures, info


def decide(state: dict, game: str, profile: dict, aux: dict | None = None,
           temperature: float = TEMPERATURE, rng: random.Random | None = None,
           confidence_floor: float = CONFIDENCE_FLOOR,
           budget: Budgeter | None = None) -> list:
    """The reflex arc: perception state dict -> [input_bridge.Gesture].

    Empty list = do nothing (the wait class, a sub-floor confidence, an
    unencoded game, or an exhausted budget) — silence is always a decision
    the caller can log, never an exception.
    """
    return reflex(state, game, profile, aux, temperature, rng,
                  confidence_floor, budget)[0]


def explain(state: dict, game: str, aux: dict | None = None,
            temperature: float = TEMPERATURE) -> dict:
    """decide()'s inner monologue, for the lane log: context, the scored
    options with probabilities, the chosen intent, the gestures it would be."""
    from input_bridge import from_intent
    brain, note = brain_for(game)
    encoded = encode(state, game, aux) if brain else None
    if brain is None or encoded is None:
        return {"game": game, "brain": None, "note": note,
                "context": None, "action": "wait", "intent": None,
                "probs": []}
    context, options = encoded
    probs = brain.probabilities(context, options)
    index, top = brain.choose(options, probs, temperature)
    intent = decode(options[index], game)
    return {"game": game, "brain": brain.npz_name, "note": note,
            "context": context, "action": options[index], "intent": intent,
            "p": round(top, 4),
            "probs": [round(float(p), 4) for p in probs],
            "top_prob": round(max(float(p) for p in probs), 4),
            "options": options}


# ----------------------------------------------------------------- selftest --
def _fake_state(player, threats=(), items=(), frame=(480, 800)) -> dict:
    return {"t": time.time(), "frame": list(frame), "player": player,
            "threats": list(threats), "items": list(items), "all": [],
            "source": {}, "ms": 0.0, "yolo_ms": 0.0}


def _selftest() -> int:
    """Offline: the encoding round-trips, the numpy pass matches the torch
    fixture, every emitted gesture is valid bridge grammar, the budget holds
    under a flood, and the do-nothing class is honoured. Green x2 by law."""
    checks = 0

    # -- 1. the lineage loads, the parity fixture holds (<1e-6 against torch)
    fixture = HERE / "parity_fixture.json"
    assert fixture.is_file(), f"parity fixture missing: {fixture}"
    data = json.loads(fixture.read_text(encoding="utf-8"))["fixtures"]
    worst = 0.0
    rows = 0
    for block in data:
        brain = JevlikeBrain(f"{block['checkpoint']}.npz")
        for row in block["rows"]:
            got = brain.probabilities(row["context"], row["options"])
            want = row["torch_probs"]
            assert len(got) == len(want), (len(got), len(want))
            worst = max(worst, max(abs(a - b) for a, b in zip(got, want)))
            rows += 1
    assert worst < 1e-6, f"numpy/torch parity broken: max delta {worst:g}"
    checks += 1
    say(f"selftest: numpy forward pass matches the torch fixture on {rows} "
        f"rows across {len(data)} checkpoints — max |dp| = {worst:.2e} (<1e-6)")

    # -- 2. the sonar encoding round-trips BYTE-EXACT against the training rows
    state = _fake_state(player=(240, 430), threats=[("threats:creature", 300,
                                                     430, 20, 20, 0.9)])
    aux = {"air": 54, "ping": "ready", "echo": "yes"}
    context, options = encode(state, "sonar", aux)
    assert context == ("SONAR air 54; jelly W near; sub mid; echo yes; "
                       "ping ready"), context
    assert options == SONAR_OPTIONS and options[-1] == "wait", options[-3:]
    assert options[0] == "drag from (240, 430) to (240, 260)", options[0]
    assert options[-2] == "tap (240, 717)", options[-2]
    assert len(context.encode()) <= 192, "context over the 192-byte window"
    checks += 1
    say(f"selftest: sonar state dict -> training bytes verbatim: {context!r}")

    # -- 3. the slime/star-visitor transfer encoding: THREE fields, escape lead
    sv = _fake_state(player=(240, 400), threats=[("threats:walker", 300, 400,
                                                  20, 28, 0.8)],
                     frame=(480, 800))
    context, options = encode(sv, "star-visitor")
    parts = context.split("; ")
    assert len(parts) == 3 and parts[0].startswith("SLIME gap "), context
    assert parts[1] == "lead W", context       # threat is east -> escape west
    assert parts[2] == "tilt mid", context
    assert options == SLIME_OPTIONS, "transfer options must be the 8 drags+wait"
    checks += 1
    say(f"selftest: star-visitor -> slime-format transfer bytes: {context!r}")

    # -- 4. unencoded games are the do-nothing class, loudly
    assert encode(state, "word-poker") is None
    assert decide(_fake_state(None), "word-poker", {}) == []
    checks += 1
    say("selftest: unencoded game -> encode None, decide [] (never a guess)")

    # -- 5. synthetic state sequences -> valid bridge grammar, real intents
    from input_bridge import Gesture, compile_steps, from_intent
    import input_bridge
    profile = input_bridge.load_profile("sonar")
    rng = random.Random(7)
    emitted, kinds = [], set()
    for step in range(40):                     # a 40-frame synthetic dive
        threats = [("threats:creature", 100 + step * 6, 200 + step * 4,
                    24, 24, 0.9)]
        state = _fake_state(player=(200 + step * 2, 430), threats=threats)
        info = explain(state, "sonar", aux_sonar_probe(step), temperature=0.0)
        gestures = decide(state, "sonar", profile, aux_sonar_probe(step),
                          temperature=0.0, rng=rng)
        # the round-trip law: decide() emits EXACTLY what from_intent builds
        # for the intent explain() named — or nothing at all
        want = from_intent(profile, info["intent"],
                           {"viewport": [480, 800]}) \
            if info["intent"] and info["p"] >= CONFIDENCE_FLOOR else []
        assert [repr(g) for g in gestures] == [repr(g) for g in want], \
            (info["action"], info["intent"], gestures, want)
        for gesture in gestures:
            assert isinstance(gesture, Gesture), gesture
            assert gesture.kind in ("tap", "swipe", "hold", "key", "combo",
                                    "tilt", "pad", "stick"), gesture.kind
            assert compile_steps(gesture), f"uncompilable {gesture}"
            emitted.append(gesture)
            kinds.add(gesture.kind)
    assert emitted, "a moving-threat dive must emit something"
    checks += 1
    say(f"selftest: 40-frame dive -> {len(emitted)} gestures ({sorted(kinds)}), "
        f"every one compiles and round-trips through from_intent")

    # -- 6. the budget law: a decision flood never exceeds 5 gestures/second
    budget = Budgeter()
    now = 1000.0
    landed = 0
    for _ in range(60):                        # 60 decisions in one instant
        landed += len(budget.allow([Gesture("key", {"name": "z",
                                                    "down_ms": 10})], now=now))
    assert landed == int(GESTURE_BUDGET_PER_SECOND), landed
    landed += len(budget.allow([Gesture("key", {"name": "z", "down_ms": 10})],
                               now=now + 0.5))
    assert landed == int(GESTURE_BUDGET_PER_SECOND), "mid-window top-up must drop"
    landed += len(budget.allow([Gesture("key", {"name": "z", "down_ms": 10})],
                               now=now + 1.5))
    assert landed == int(GESTURE_BUDGET_PER_SECOND) + 1, "window must slide"
    checks += 1
    say(f"selftest: 60-decision flood -> {GESTURE_BUDGET_PER_SECOND:g} "
        f"gestures in window 1, top-up dropped, window slides at +1.5s")

    # -- 7. the knobs: confidence floor and the wait class both mean silence
    quiet = decide(state, "sonar", profile, aux_sonar_probe(0),
                   temperature=0.0, confidence_floor=1.1)   # impossible floor
    assert quiet == [], "above the floor nothing may fire"
    wait_ctx, wait_opts = encode(_fake_state(None), "sonar", {"air": 100})
    assert "; jelly none;" in wait_ctx, wait_ctx
    assert decode("wait", "sonar") is None and decode("nonsense", "sonar") is None
    assert decode("drag from (240, 430) to (70, 430)", "sonar") == "dodge_left"
    assert decode("drag from (240, 430) to (410, 430)", "sonar") == "dodge_right"
    assert decode("drag from (240, 430) to (240, 260)", "sonar") == "dodge_up"
    assert decode("drag from (240, 430) to (240, 600)", "sonar") == "dodge_down"
    assert decode("tap (240, 717)", "sonar") == "ping"
    assert decode("tap (240, 717)", "star-visitor") == "confirm"
    checks += 1
    say("selftest: floor=1.1 -> []; wait/unparsed -> None; drag->dodge and "
        "sonar tap->ping decode on the training vocabulary")

    # -- 8. the geometry twins match the deployed modules when they are present
    try:
        from vision_state import bearing as vb, range_of as vr
    except ImportError:
        say("selftest: vision_state not importable here — twin parity checked "
            "on the deploy box")
    else:
        rng2 = random.Random(11)
        for _ in range(200):
            fx, fy = rng2.uniform(-2, 2), rng2.uniform(-2, 2)
            assert bearing(fx, fy) == vb(fx, fy), (fx, fy)
            reach = rng2.uniform(0, 2)
            assert range_of(reach) == vr(reach), reach
        checks += 1
        say("selftest: bearing/range_of twins match vision_state on 200 "
            "random offsets")

    # -- 9. aux probes stay in the training band on flat + title frames
    flat = np.full((800, 480, 3), 40, np.uint8)
    aux = aux_sonar(flat)
    assert set(aux) == {"air", "ping", "echo", "ring", "hull", "scene"}, aux
    assert (aux["air"] == 0 and aux["ping"] == "cool" and aux["echo"] == "no"
            and aux["scene"] == "over"), aux
    bright = np.full((800, 480, 3), 255, np.uint8)
    aux = aux_sonar(bright)
    assert (aux["air"] == 100 and aux["ping"] == "ready" and aux["echo"] == "yes"
            and aux["scene"] == "unknown"), aux
    title = np.full((800, 480, 3), 40, np.uint8)
    title[100:300, 100:400] = (200, 180, 60)      # a huge yellow logo blob
    aux = aux_sonar(title)
    assert aux["scene"] == "title" and aux["hull"] >= HULL_TITLE, aux
    checks += 1
    say("selftest: sonar HUD probes — dark frame scene=over, white frame "
        "air=100/scene=unknown, yellow-blob frame scene=title (the recorder's "
        "hull law)")

    say(f"selftest: PASS — {checks} checks, offline (numpy + fake-free: the "
        "only wire touched was the fixture on disk)")
    return 0


def aux_sonar_probe(step: int) -> dict:
    """A deterministic aux sequence for the selftest dive (above 45 air the
    training policy pings/waits — this keeps the synthetic dive in-distribution)."""
    return {"air": 60 - step, "ping": "ready" if step % 3 == 0 else "cool",
            "echo": "yes" if step % 2 else "no"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PlayerOne's jevlike spinal cord — state dict -> gestures")
    parser.add_argument("--selftest", action="store_true",
                        help="offline: parity fixture, encoding round-trip, "
                             "grammar, budget, do-nothing class")
    parser.add_argument("--explain", action="store_true",
                        help="score one synthetic state and print the monologue")
    parser.add_argument("--game", default="sonar")
    args = parser.parse_args()
    if args.selftest:
        return _selftest()
    if args.explain:
        state = _fake_state(player=(240, 430),
                            threats=[("threats:creature", 330, 430, 20, 20, 0.9)])
        say(json.dumps(explain(state, args.game,
                               {"air": 54, "ping": "ready", "echo": "yes"}),
                       indent=2))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
