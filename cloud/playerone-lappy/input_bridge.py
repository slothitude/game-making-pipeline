#!/usr/bin/env python3
"""input_bridge — PlayerOne's universal input actuator (the gesture layer).

The gap: PlayerOne can SEE the games (collect_evidence) and THINK about them
(llm_critic ladder, numpy brains) but cannot PLAY them. The harnesses drive
page.mouse / page.keyboard only, which is the desktop lane of a phone-first
factory — Godot web builds read real touch events, device tilt and the
standard Gamepad API, none of which page.mouse produces (Godot's web port
wires godot_js_input_touch_cb / godot_js_input_gamepad_cb, and never reads
deviceorientation on web — that is why every tilt game here defaults to
MODE_TOUCH). This module is ONE gesture grammar compiling to whichever
transport a game actually reads:

    gesture                        transport                        reaches the game as
    -----------------------------  --------------------------------  ---------------------------
    tap(x,y)                       CDP Input.dispatchTouchEvent      InputEventScreenTouch
    swipe(x1,y1,x2,y2,dur,steps)   CDP Input.dispatchTouchEvent      InputEventScreenTouch+Drag
    hold(x,y,dur_ms)               CDP Input.dispatchTouchEvent      touch held down dur_ms
    key(name,down_ms)              native Playwright keyboard        InputEventKey
    combo([actions])               any of the above, in order        the whole sequence
    tilt(alpha,beta,gamma)         CDP DeviceOrientation override    deviceorientation event
    pad(button=i,pressed=bool)     navigator.getGamepads shim        Gamepad API (EmulatorJS too)
    stick(idx,x,y)                 navigator.getGamepads shim        Gamepad axes

Design law: gestures are DATA first. ActionSpace builds Gesture objects,
compile_steps() turns one into a deterministic list of primitive ops
(cdp / evaluate / key / wait), and Bridge merely walks that list against real
Playwright objects. The offline selftest asserts the compiled op stream with a
fake page — no browser, no network — exactly like worker.py's selftest mocks
the wire:

    python3 input_bridge.py --selftest        (stdlib-only, green x2)

Integration (flag-gated, see worker.py): env P1_INPUT=bridge makes the lane
subprocesses route their actions through here; the legacy mouse/keyboard path
is untouched when the flag is absent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(os.environ.get("PLAYERONE_ROOT", "/home/aaron/playerone"))
PROFILES = ROOT / "profiles"

DEFAULT_URL = "https://retromonkey.com.au/games/{game}/"


def say(msg: str) -> None:
    print(f"[input_bridge] {msg}", flush=True)


def bridge_enabled() -> bool:
    """The flag law: P1_INPUT=bridge turns the actuator on. Anything else is
    the legacy desktop lane, byte-identical to before."""
    return os.environ.get("P1_INPUT", "").strip().lower() == "bridge"


# ------------------------------------------------------------------ gesture IR --
@dataclass(frozen=True)
class Gesture:
    """One input actuation, as data. Never holds a page, a browser or a wire —
    that is what makes the grammar offline-testable and transport-agnostic."""
    kind: str
    args: dict

    def __repr__(self) -> str:  # compact, log-friendly
        inner = ", ".join(f"{k}={v}" for k, v in self.args.items())
        return f"{self.kind}({inner})"


class ActionSpace:
    """The grammar. Every method builds a Gesture; nothing here touches a
    browser. coords are page pixels (the game's canvas space)."""

    def tap(self, x: float, y: float) -> Gesture:
        return Gesture("tap", {"x": x, "y": y})

    def swipe(self, x1: float, y1: float, x2: float, y2: float,
              dur_ms: int = 250, steps: int = 8) -> Gesture:
        return Gesture("swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                 "dur_ms": int(dur_ms), "steps": int(steps)})

    def hold(self, x: float, y: float, dur_ms: int) -> Gesture:
        return Gesture("hold", {"x": x, "y": y, "dur_ms": int(dur_ms)})

    def key(self, name: str, down_ms: int = 120) -> Gesture:
        return Gesture("key", {"name": name, "down_ms": int(down_ms)})

    def combo(self, actions: list) -> Gesture:
        """Sequence of gestures (or (kind, args) tuples) played in order."""
        children = [a if isinstance(a, Gesture) else Gesture(a[0], dict(a[1]))
                    for a in actions]
        return Gesture("combo", {"children": children})

    def tilt(self, alpha: float, beta: float, gamma: float) -> Gesture:
        """Device orientation in degrees — alpha (compass/z), beta (front-back
        x-axis), gamma (left-right y-axis). Beta/gamma drive the tilt games."""
        return Gesture("tilt", {"alpha": alpha, "beta": beta, "gamma": gamma})

    def pad(self, button: int, pressed: bool) -> Gesture:
        """Standard-mapping gamepad button (0=A, 1=B, 2=X, 3=Y, 9=start...)."""
        return Gesture("pad", {"button": int(button), "pressed": bool(pressed)})

    def stick(self, idx: int, x: float, y: float) -> Gesture:
        """Gamepad stick: idx 0 = left (axes 0/1), idx 1 = right (axes 2/3)."""
        return Gesture("stick", {"idx": int(idx), "x": float(x), "y": float(y)})


# ------------------------------------------------------------- profiles ------
# canonical intents every profile should answer (the default profile does);
# per-game profiles may add their own vocabulary on top.
DEFAULT_INTENTS = {
    "start":      {"gesture": "combo", "args": {"children": [
                      {"gesture": "key", "args": {"name": "Enter", "down_ms": 80}},
                      {"gesture": "tap", "args": {"x": 0.5, "y": 0.5, "rel": True}}]}},
    "confirm":    {"gesture": "tap", "args": {"x": 0.5, "y": 0.5, "rel": True}},
    "fire":       {"gesture": "key", "args": {"name": "z", "down_ms": 120}},
    "jump":       {"gesture": "key", "args": {"name": "x", "down_ms": 120}},
    "advance":    {"gesture": "key", "args": {"name": "ArrowRight", "down_ms": 400}},
    "retreat":    {"gesture": "key", "args": {"name": "ArrowLeft", "down_ms": 400}},
    "up":         {"gesture": "key", "args": {"name": "ArrowUp", "down_ms": 200}},
    "down":       {"gesture": "key", "args": {"name": "ArrowDown", "down_ms": 200}},
    "left":       {"gesture": "key", "args": {"name": "ArrowLeft", "down_ms": 200}},
    "right":      {"gesture": "key", "args": {"name": "ArrowRight", "down_ms": 200}},
    "dodge_left":  {"gesture": "swipe", "args": {"x1": 0.62, "y1": 0.5, "x2": 0.2, "y2": 0.5,
                                                 "dur_ms": 220, "steps": 8, "rel": True}},
    "dodge_right": {"gesture": "swipe", "args": {"x1": 0.38, "y1": 0.5, "x2": 0.8, "y2": 0.5,
                                                 "dur_ms": 220, "steps": 8, "rel": True}},
    "dodge_up":    {"gesture": "swipe", "args": {"x1": 0.5, "y1": 0.65, "x2": 0.5, "y2": 0.25,
                                                 "dur_ms": 220, "steps": 8, "rel": True}},
    "dodge_down":  {"gesture": "swipe", "args": {"x1": 0.5, "y1": 0.35, "x2": 0.5, "y2": 0.75,
                                                 "dur_ms": 220, "steps": 8, "rel": True}},
}

# synonyms -> canonical intent (so profiles only need the canonical names)
INTENT_ALIASES = {
    "left": "left", "move_left": "left", "dodge_left": "dodge_left",
    "right": "right", "move_right": "right", "dodge_right": "dodge_right",
    "up": "up", "move_up": "up", "dodge_up": "dodge_up", "rise": "up",
    "down": "down", "move_down": "down", "dodge_down": "dodge_down", "dive": "down",
    "shoot": "fire", "fire": "fire", "ping": "fire", "attack": "fire",
    "jump": "jump", "bomb": "bomb", "super": "super", "boost": "super",
    "start": "start", "begin": "start", "enter": "start", "retry": "retry",
    "restart": "retry", "confirm": "confirm", "ok": "confirm", "tap": "confirm",
    "advance": "advance", "forward": "advance", "retreat": "retreat", "back": "retreat",
    "submit": "submit", "clear": "clear", "shuffle": "shuffle", "remove_last": "remove_last",
}

# which arg keys hold page coordinates (the ones viewport scaling touches)
_COORD_KEYS = ("x", "y", "x1", "y1", "x2", "y2")


def load_profile(game: str, profiles_dir: Path | None = None) -> dict:
    """profiles/<game>.json, else profiles/default.json, else the built-in
    default vocabulary. Unknown games are a downgrade, never a crash."""
    root = Path(profiles_dir) if profiles_dir else PROFILES
    for name in (f"{game}.json", "default.json"):
        path = root / name
        if path.is_file():
            with open(path, "r", encoding="utf-8") as fh:
                profile = json.load(fh)
            profile.setdefault("game", game)
            profile.setdefault("intents", {})
            profile.setdefault("viewport", [540, 960])
            return profile
    return {"game": game, "source": "builtin-default",
            "viewport": [540, 960], "input_classes": ["touch", "key", "pad"],
            "intents": {}}


def _resolve_coords(args: dict, spec: dict, src_vp: list | None,
                    dst_vp: list | None) -> dict:
    out = dict(args)
    # "rel" may live on the spec or inside args — both read the same way
    if spec.get("rel") or args.get("rel"):
        if not dst_vp:
            raise ValueError("rel: true coordinates need ctx viewport")
        for key in _COORD_KEYS:
            if key in out:
                axis = 1 if key in ("y", "y1", "y2") else 0
                out[key] = round(float(out[key]) * dst_vp[axis])
    elif src_vp and dst_vp and tuple(src_vp) != tuple(dst_vp):
        sx = float(dst_vp[0]) / float(src_vp[0])
        sy = float(dst_vp[1]) / float(src_vp[1])
        for key in _COORD_KEYS:
            if key in out:
                scale = sy if key in ("y", "y1", "y2") else sx
                out[key] = round(float(out[key]) * scale)
    for key in _COORD_KEYS:  # drop nothing: ints for CDP
        if key in out:
            out[key] = int(out[key])
    return out


def from_intent(profile: dict, intent: str, ctx: dict | None = None) -> list[Gesture]:
    """Game intent -> gestures, via the per-game profile. ctx carries the live
    viewport ({"viewport": [w, h]}) so profiles written in a game's native
    resolution still land on the right pixels."""
    ctx = ctx or {}
    src_vp = profile.get("viewport")
    dst_vp = ctx.get("viewport") or src_vp
    intents = dict(DEFAULT_INTENTS)
    intents.update(profile.get("intents") or {})
    canonical = INTENT_ALIASES.get(intent, intent)
    spec = intents.get(canonical)
    if spec is None:
        raise KeyError(
            f"no gesture mapping for intent {intent!r} (game "
            f"{profile.get('game')!r}); known: {sorted(intents)}")
    specs = spec["args"]["children"] if spec["gesture"] == "combo" else [spec]
    space = ActionSpace()
    gestures: list[Gesture] = []
    for one in specs:
        args = _resolve_coords(one.get("args") or {}, one, src_vp, dst_vp)
        args.pop("rel", None)
        if one["gesture"] == "combo":
            inner = [Gesture(c["gesture"], dict(c.get("args") or {}))
                     for c in args.pop("children")]
            gestures.append(space.combo(inner))
        else:
            gestures.append(Gesture(one["gesture"], args))
    return gestures


# --------------------------------------------------------------- op compile --
# A compiled step is a plain dict — the wire-agnostic atom the selftest asserts
# and the Bridge executes:
#   {"op": "cdp",  "method": ..., "params": ...}   CDP session send
#   {"op": "evaluate", "script": ..., "arg": ...}  page.evaluate
#   {"op": "init", "script": ...}                  add_init_script (pre-load)
#   {"op": "key",  "action": "down|up", "key": ...} native Playwright keyboard
#   {"op": "wait", "ms": ...}                       settle between primitives

def _touch_point(x: float, y: float, pid: int = 1) -> dict:
    return {"x": round(float(x), 2), "y": round(float(y), 2), "id": pid}


def compile_steps(gesture: Gesture) -> list[dict]:
    """Gesture -> deterministic primitive-op list. Pure: no page, no I/O."""
    kind, a = gesture.kind, gesture.args
    if kind == "tap":
        point = _touch_point(a["x"], a["y"])
        return [{"op": "cdp", "method": "Input.dispatchTouchEvent",
                 "params": {"type": "touchStart", "touchPoints": [point]}},
                {"op": "cdp", "method": "Input.dispatchTouchEvent",
                 "params": {"type": "touchEnd", "touchPoints": []}}]
    if kind == "hold":
        point = _touch_point(a["x"], a["y"])
        return [{"op": "cdp", "method": "Input.dispatchTouchEvent",
                 "params": {"type": "touchStart", "touchPoints": [point]}},
                {"op": "wait", "ms": int(a["dur_ms"])},
                {"op": "cdp", "method": "Input.dispatchTouchEvent",
                 "params": {"type": "touchEnd", "touchPoints": []}}]
    if kind == "swipe":
        steps = max(1, int(a.get("steps", 8)))
        dur = max(0, int(a.get("dur_ms", 250)))
        interval = dur // steps
        x1, y1, x2, y2 = (float(a[k]) for k in ("x1", "y1", "x2", "y2"))
        out = [{"op": "cdp", "method": "Input.dispatchTouchEvent",
                "params": {"type": "touchStart",
                           "touchPoints": [_touch_point(x1, y1)]}}]
        for i in range(1, steps + 1):
            t = i / steps
            out.append({"op": "wait", "ms": interval})
            out.append({"op": "cdp", "method": "Input.dispatchTouchEvent",
                        "params": {"type": "touchMove", "touchPoints": [
                            _touch_point(x1 + (x2 - x1) * t,
                                         y1 + (y2 - y1) * t)]}})
        out.append({"op": "cdp", "method": "Input.dispatchTouchEvent",
                    "params": {"type": "touchEnd", "touchPoints": []}})
        return out
    if kind == "key":
        out = [{"op": "key", "action": "down", "key": a["name"]}]
        if int(a.get("down_ms", 0)) > 0:
            out.append({"op": "wait", "ms": int(a["down_ms"])})
        out.append({"op": "key", "action": "up", "key": a["name"]})
        return out
    if kind == "combo":
        out: list[dict] = []
        for child in a.get("children", []):
            out.extend(compile_steps(child if isinstance(child, Gesture)
                                     else Gesture(child["kind"], child["args"])))
        return out
    if kind == "tilt":
        return [{"op": "cdp", "method": "DeviceOrientation.setDeviceOrientationOverride",
                 "params": {"alpha": float(a["alpha"]), "beta": float(a["beta"]),
                            "gamma": float(a["gamma"])}}]
    if kind == "pad":
        return [{"op": "evaluate", "script": PAD_BUTTON_JS,
                 "arg": {"button": int(a["button"]), "pressed": bool(a["pressed"])}}]
    if kind == "stick":
        return [{"op": "evaluate", "script": PAD_STICK_JS,
                 "arg": {"idx": int(a["idx"]), "x": float(a["x"]),
                         "y": float(a["y"])}}]
    raise ValueError(f"unknown gesture kind {kind!r}")


# ------------------------------------------------------------- gamepad shim --
# Injected via add_init_script (and evaluated once for already-live pages):
# navigator.getGamepads is overridden to return a synthetic pad whose state is
# window.__P1_GAMEPAD__ — the object the bridge mutates over page.evaluate.
# Fires gamepadconnected on first access, so any Gamepad-API consumer works
# (Godot's godot_js_input_gamepad_cb, EmulatorJS, raw JS games).
# FORM LAW: this must stay SELF-INVOKING — playwright's add_init_script only
# *evaluates* the string it is given, so a bare `() => {...}` function
# expression compiles to a function object nobody calls (found live: the shim
# silently did nothing until page.evaluate ran it by hand).
PAD_SHIM_JS = r"""
(() => {
  if (window.__P1_GAMEPAD_SHIM__) { return true; }
  window.__P1_GAMEPAD_SHIM__ = true;
  const state = window.__P1_GAMEPAD__ = {
    id: 'PlayerOne Synthetic Gamepad', index: 0, connected: false,
    buttons: Array.from({length: 17}, () => ({pressed: false, touched: false, value: 0})),
    axes: [0, 0, 0, 0], mapping: 'standard', timestamp: 0
  };
  const snapshot = () => ({
    id: state.id, index: state.index, connected: true, mapping: state.mapping,
    timestamp: performance.now(), vibrationActuator: null,
    buttons: state.buttons.map((b) => ({pressed: !!b.pressed, touched: !!b.touched,
                                        value: b.pressed ? 1 : (b.value || 0)})),
    axes: state.axes.slice()
  });
  let announced = false;
  const announce = () => {
    if (announced) { return; }
    announced = true; state.connected = true;
    // A synthetic pad cannot satisfy GamepadEventInit's interface type check
    // (GamepadEvent throws on a plain object), so fall back to a plain Event
    // carrying .gamepad — listeners read event.gamepad either way.
    let evt;
    try { evt = new GamepadEvent('gamepadconnected', {gamepad: snapshot()}); }
    catch (e) { evt = new Event('gamepadconnected', {bubbles: false}); }
    try { evt.gamepad = snapshot(); } catch (e) { /* real GamepadEvent keeps its own */ }
    window.dispatchEvent(evt);
  };
  navigator.getGamepads = function () { announce(); return [snapshot(), null, null, null]; };
  return true;
})();
"""

PAD_BUTTON_JS = r"""
(arg) => {
  const state = window.__P1_GAMEPAD__;
  if (!state) { return null; }
  const b = state.buttons[arg.button];
  if (!b) { return null; }
  b.pressed = !!arg.pressed;
  b.touched = !!arg.pressed;
  b.value = arg.pressed ? 1 : 0;
  state.timestamp = performance.now();
  return state.buttons.map((x) => !!x.pressed);
}
"""

PAD_STICK_JS = r"""
(arg) => {
  const state = window.__P1_GAMEPAD__;
  if (!state) { return null; }
  state.axes[arg.idx * 2] = arg.x;
  state.axes[arg.idx * 2 + 1] = arg.y;
  state.timestamp = performance.now();
  return state.axes.slice();
}
"""

PAD_RESET_JS = r"""
() => {
  const state = window.__P1_GAMEPAD__;
  if (!state) { return null; }
  state.buttons.forEach((b) => { b.pressed = false; b.touched = false; b.value = 0; });
  state.axes = [0, 0, 0, 0];
  state.timestamp = performance.now();
  return true;
}
"""


# ------------------------------------------------------------------ the bridge --
class Bridge:
    """Walks compiled gestures against a live page. One per page; lazy about
    the CDP session (a fresh context has none). Every perform* returns the
    steps it executed, so lanes can log exactly what was sent."""

    def __init__(self, page, profile: dict | None = None) -> None:
        self.page = page
        self.profile = profile or {}
        self._cdp = None
        self._pad_installed = False
        self.space = ActionSpace()

    # -- transports ---------------------------------------------------------
    def cdp_session(self):
        if self._cdp is None:
            self._cdp = self.page.context.new_cdp_session(self.page)
        return self._cdp

    def install(self, pad: bool = True, gyro: bool = True) -> "Bridge":
        """Idempotent. The gamepad shim wants to exist BEFORE the page runs
        (add_init_script) but a page that is already live needs it evaluated
        now — do both; the shim guards itself."""
        if pad and not self._pad_installed:
            self.page.add_init_script(PAD_SHIM_JS)
            try:
                self.page.evaluate(PAD_SHIM_JS)
            except Exception:
                pass  # a navigated-away page re-runs the init script anyway
            self._pad_installed = True
        return self

    # -- the executor -------------------------------------------------------
    def perform(self, gesture: Gesture) -> list[dict]:
        steps = compile_steps(gesture)
        self.perform_steps(steps)
        return steps

    def perform_steps(self, steps: list[dict]) -> None:
        for step in steps:
            op = step["op"]
            if op == "cdp":
                self.cdp_session().send(step["method"], step["params"])
            elif op == "evaluate":
                self.page.evaluate(step["script"], step.get("arg"))
            elif op == "init":
                self.page.add_init_script(step["script"])
            elif op == "key":
                if step["action"] == "down":
                    self.page.keyboard.down(step["key"])
                else:
                    self.page.keyboard.up(step["key"])
            elif op == "wait":
                self.page.wait_for_timeout(int(step["ms"]))
            else:
                raise ValueError(f"unknown op {op!r}")

    # -- grammar conveniences ----------------------------------------------
    def tap(self, x, y): return self.perform(self.space.tap(x, y))

    def swipe(self, x1, y1, x2, y2, dur_ms=250, steps=8):
        return self.perform(self.space.swipe(x1, y1, x2, y2, dur_ms, steps))

    def hold(self, x, y, dur_ms): return self.perform(self.space.hold(x, y, dur_ms))

    def key(self, name, down_ms=120): return self.perform(self.space.key(name, down_ms))

    def tilt(self, alpha, beta, gamma): return self.perform(self.space.tilt(alpha, beta, gamma))

    def pad(self, button, pressed): return self.perform(self.space.pad(button, pressed))

    def stick(self, idx, x, y): return self.perform(self.space.stick(idx, x, y))

    def perform_intent(self, intent: str, ctx: dict | None = None) -> list[dict]:
        profile = self.profile or load_profile(getattr(self, "game", "") or "default")
        performed: list[dict] = []
        for gesture in from_intent(profile, intent, ctx):
            performed.extend(self.perform(gesture))
        return performed

    # -- reset / teardown ---------------------------------------------------
    def tilt_reset(self):
        return self.cdp_session().send("DeviceOrientation.clearDeviceOrientationOverride", {})

    def pad_reset(self):
        return self.page.evaluate(PAD_RESET_JS)

    def release(self):
        """Release everything a session held down: keys are the caller's
        (short down_ms windows), tilt override cleared, pad zeroed."""
        try:
            self.tilt_reset()
        except Exception:
            pass
        try:
            self.pad_reset()
        except Exception:
            pass

    # -- legacy desktop lane (mouse) ---------------------------------------
    def mouse_tap(self, x, y):
        self.page.mouse.click(x, y)

    def mouse_drag(self, x1, y1, x2, y2, steps=10):
        self.page.mouse.move(x1, y1)
        self.page.mouse.down()
        self.page.mouse.move(x2, y2, steps=steps)
        self.page.mouse.up()


# ------------------------------------------------------- touch-first contexts --
# A context without has_touch never emits real touch events, and Godot's web
# port then sees nothing (mouse events are a different wire). Any context the
# bridge drives must be built with these kwargs.
TOUCH_CONTEXT_KWARGS = {"has_touch": True, "is_mobile": True,
                        "viewport": {"width": 540, "height": 960}}


def open_touch_context(browser, viewport=None, **extra):
    """browser.new_context(...) with touch guaranteed on. Used behind the
    P1_INPUT=bridge flag; the legacy path builds contexts as it always did."""
    kwargs = dict(TOUCH_CONTEXT_KWARGS)
    if viewport:
        kwargs["viewport"] = {"width": int(viewport[0]), "height": int(viewport[1])}
    kwargs.update(extra)
    return browser.new_context(**kwargs)


# ----------------------------------------------------------------- selftest --
class FakeCDP:
    def __init__(self, log: list) -> None:
        self._log = log

    def send(self, method, params=None):
        self._log.append({"op": "cdp", "method": method,
                          "params": params or {}})
        return {"result": {}}


class FakeContext:
    def __init__(self, log: list) -> None:
        self._log = log

    def new_cdp_session(self, page):
        self._log.append({"op": "new_cdp_session"})
        return FakeCDP(self._log)


class FakeKeyboard:
    def __init__(self, log: list) -> None:
        self._log = log

    def down(self, key):
        self._log.append({"op": "key", "action": "down", "key": key})

    def up(self, key):
        self._log.append({"op": "key", "action": "up", "key": key})


class FakeMouse:
    def __init__(self, log: list) -> None:
        self._log = log

    def click(self, x, y):
        self._log.append({"op": "mouse", "action": "click", "x": x, "y": y})

    def move(self, x, y, steps=1):
        self._log.append({"op": "mouse", "action": "move", "x": x, "y": y,
                          "steps": steps})

    def down(self):
        self._log.append({"op": "mouse", "action": "down"})

    def up(self):
        self._log.append({"op": "mouse", "action": "up"})


class FakePage:
    """The offline browser: records every primitive the Bridge sends, in
    order. Stands in for playwright's Page + CDPSession exactly."""

    def __init__(self) -> None:
        self.log: list[dict] = []
        self.context = FakeContext(self.log)
        self.keyboard = FakeKeyboard(self.log)
        self.mouse = FakeMouse(self.log)
        self.waits = 0

    def evaluate(self, script, arg=None):
        self.log.append({"op": "evaluate", "script": script, "arg": arg})
        return True

    def add_init_script(self, script):
        self.log.append({"op": "init", "script": script})

    def wait_for_timeout(self, ms):
        self.waits += int(ms)
        self.log.append({"op": "wait", "ms": int(ms)})


def _ops(log: list, op: str, method: str | None = None) -> list[dict]:
    out = [step for step in log if step["op"] == op
           and (method is None or step.get("method") == method)]
    return out


def _selftest() -> int:
    """Offline: fake CDP/keyboard/mouse/evaluate layers, assert the serialized
    op stream for every gesture plus the profile machinery. No network, no
    browser, no venv."""
    space = ActionSpace()
    checks = 0

    # -- tap: exactly one touchStart + one touchEnd on the CDP touch wire
    page = FakePage()
    Bridge(page).tap(120, 340)
    touches = _ops(page.log, "cdp", "Input.dispatchTouchEvent")
    assert len(touches) == 2, touches
    assert touches[0]["params"] == {"type": "touchStart",
                                    "touchPoints": [{"x": 120, "y": 340, "id": 1}]}, touches[0]
    assert touches[1]["params"] == {"type": "touchEnd", "touchPoints": []}, touches[1]
    checks += 1
    say("selftest: tap -> 1 touchStart + 1 touchEnd, point {x,y,id} intact")

    # -- swipe: 8 interpolated touchMoves between start and end, timed evenly
    page = FakePage()
    steps = Bridge(page).swipe(400, 480, 80, 480, dur_ms=240, steps=8)
    assert len(steps) == 1 + 8 * 2 + 1, len(steps)
    stream = _ops(page.log, "cdp", "Input.dispatchTouchEvent")
    assert [s["params"]["type"] for s in stream] == \
        ["touchStart"] + ["touchMove"] * 8 + ["touchEnd"], stream
    points = [s["params"]["touchPoints"][0] for s in stream[1:-1]]
    assert points[0]["x"] == 360 and points[0]["y"] == 480, points[0]  # 1/8 of 320px
    assert points[-1]["x"] == 80 and points[-1]["y"] == 480, points[-1]  # lands on target
    xs = [p["x"] for p in points]
    assert all(a > b for a, b in zip(xs, xs[1:])), "leftward swipe must advance monotonically"
    waits = [w["ms"] for w in _ops(page.log, "wait")]
    assert waits == [30] * 8, waits  # 240ms / 8 moves
    checks += 1
    say("selftest: swipe(400,480->80,480,240ms,8) -> start+8 moves+end, "
        "monotonic interpolation, 30ms cadence")

    # -- hold: down, dwell, up
    page = FakePage()
    Bridge(page).hold(270, 500, 500)
    stream = [s for s in page.log if s["op"] in ("cdp", "wait")]
    assert [s["params"]["type"] if s["op"] == "cdp" else f"wait:{s['ms']}"
            for s in stream] == ["touchStart", "wait:500", "touchEnd"], stream
    assert page.waits == 500, page.waits
    checks += 1
    say("selftest: hold(270,500,500ms) -> touchStart, 500ms dwell, touchEnd")

    # -- key: down/wait/up, and down_ms=0 collapses to down+up
    page = FakePage()
    Bridge(page).key("ArrowRight", 400)
    keys = _ops(page.log, "key")
    assert [(k["action"], k["key"]) for k in keys] == \
        [("down", "ArrowRight"), ("up", "ArrowRight")], keys
    assert [w["ms"] for w in _ops(page.log, "wait")] == [400]
    page = FakePage()
    Bridge(page).key("z", 0)
    assert _ops(page.log, "wait") == [], "down_ms=0 must not dwell"
    checks += 1
    say("selftest: key(ArrowRight,400ms) -> down/400ms/up; key(z,0) -> down/up")

    # -- combo: order preserved across transports in ONE stream
    page = FakePage()
    Bridge(page).perform(space.combo([space.tap(10, 20), space.key("Space", 50),
                                      space.pad(0, True), space.tilt(0, 30, -12)]))

    def tag(step: dict) -> tuple:
        if step["op"] == "cdp":
            return ("cdp", step["method"])
        if step["op"] == "key":
            return ("key", step["action"])
        if step["op"] == "evaluate":
            return ("evaluate", step["script"])
        return (step["op"], "")

    kinds = [tag(s) for s in page.log if s["op"] in
             ("cdp", "key", "evaluate", "init")]
    assert kinds[:2] == [("cdp", "Input.dispatchTouchEvent")] * 2, kinds
    assert kinds[2:4] == [("key", "down"), ("key", "up")], kinds
    assert kinds[4] == ("evaluate", PAD_BUTTON_JS), kinds[4]
    assert kinds[5] == ("cdp", "DeviceOrientation.setDeviceOrientationOverride"), kinds
    checks += 1
    say("selftest: combo(tap,key,pad,tilt) -> touch, keyboard, evaluate, gyro "
        "in order on one wire")

    # -- tilt: CDP override params verbatim
    page = FakePage()
    Bridge(page).tilt(0.0, 42.5, -12.0)
    gyro = _ops(page.log, "cdp", "DeviceOrientation.setDeviceOrientationOverride")
    assert len(gyro) == 1, gyro
    assert gyro[0]["params"] == {"alpha": 0.0, "beta": 42.5, "gamma": -12.0}, gyro[0]
    Bridge(page).tilt_reset()
    assert _ops(page.log, "cdp", "DeviceOrientation.clearDeviceOrientationOverride"), \
        "tilt_reset must clear the override"
    checks += 1
    say("selftest: tilt(0,42.5,-12) -> DeviceOrientation override; reset clears")

    # -- pad + stick: shim installed, state mutated through page.evaluate
    page = FakePage()
    br = Bridge(page).install()
    br.pad(2, True)
    br.stick(1, 0.5, -0.5)
    inits = _ops(page.log, "init")
    assert len(inits) == 1 and inits[0]["script"] == PAD_SHIM_JS, inits
    evals = _ops(page.log, "evaluate")
    assert evals[0]["script"] == PAD_SHIM_JS, "install() evaluates the shim live too"
    assert evals[1]["arg"] == {"button": 2, "pressed": True}, evals[1]
    assert evals[2]["arg"] == {"idx": 1, "x": 0.5, "y": -0.5}, evals[2]
    br.pad_reset()
    assert _ops(page.log, "evaluate")[-1]["script"] == PAD_RESET_JS
    assert len(_ops(page.log, "init")) == 1, "shim installs exactly once"
    checks += 1
    say("selftest: pad(2,True)/stick(1,.5,-.5) -> __P1_GAMEPAD__ mutations; "
        "shim installed once; pad_reset zeroes")

    # -- profiles: load, default fallback, intent mapping, viewport scaling
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="p1_bridge_selftest_"))
    (tmp / "sonar.json").write_text(json.dumps({
        "game": "sonar", "viewport": [540, 960],
        "input_classes": ["touch", "key"],
        "intents": {"dodge_left": {"gesture": "swipe", "args": {
            "x1": 400, "y1": 480, "x2": 80, "y2": 480,
            "dur_ms": 220, "steps": 8}}}}), encoding="utf-8")
    profile = load_profile("sonar", tmp)
    gestures = from_intent(profile, "dodge_left")
    assert len(gestures) == 1 and gestures[0].kind == "swipe", gestures
    assert gestures[0].args["x1"] == 400, gestures[0]  # same viewport -> untouched
    scaled = from_intent(profile, "dodge_left", {"viewport": [480, 800]})
    assert scaled[0].args["x1"] == round(400 * 480 / 540), scaled[0]
    assert scaled[0].args["y1"] == round(480 * 800 / 960), scaled[0]
    checks += 1
    say("selftest: profile sonar loaded; dodge_left -> swipe, rescaled "
        "540x960 -> 480x800")

    # -- relative coords + the default profile (unknown game -> taps + arrows)
    unknown = load_profile("no-such-game", tmp)  # no file -> built-in default
    assert unknown["source"] == "builtin-default", unknown
    gestures = from_intent(unknown, "dodge_left", {"viewport": [400, 700]})
    assert gestures[0].kind == "swipe", gestures
    assert gestures[0].args["x1"] == round(0.62 * 400), gestures[0]
    assert gestures[0].args["y2"] == round(0.5 * 700), gestures[0]
    assert from_intent(unknown, "advance")[0].args["name"] == "ArrowRight"
    assert from_intent(unknown, "move_up")[0].args["name"] == "ArrowUp"
    assert from_intent(unknown, "shoot")[0].args["name"] == "z"  # alias -> fire
    checks += 1
    say("selftest: unknown game -> default profile (taps + arrows + wasd), "
        "rel coords resolve against the live viewport, aliases work")

    # -- unknown intent is a loud failure, never a silent no-op
    try:
        from_intent(unknown, "sassy_walk")
    except KeyError as exc:
        assert "sassy_walk" in str(exc)
    else:
        raise AssertionError("unknown intent must raise KeyError")
    checks += 1
    say("selftest: unknown intent raises KeyError listing the vocabulary")

    # -- end-to-end: Bridge walks a profiled intent against the fake page
    page = FakePage()
    Bridge(page, profile).perform_intent("dodge_left", {"viewport": [480, 800]})
    stream = _ops(page.log, "cdp", "Input.dispatchTouchEvent")
    assert stream[0]["params"]["type"] == "touchStart", stream[0]
    assert stream[-1]["params"]["type"] == "touchEnd", stream[-1]
    assert len(stream) == 10, len(stream)  # start + 8 moves + end
    checks += 1
    say("selftest: Bridge.perform_intent(dodge_left) end-to-end -> 10 CDP "
        "touch calls on the fake wire")

    # -- touch-first context kwargs
    assert TOUCH_CONTEXT_KWARGS["has_touch"] is True
    checks += 1
    say("selftest: TOUCH_CONTEXT_KWARGS carries has_touch=True")

    # -- injected scripts must be self-invoking: playwright's add_init_script
    #    only *evaluates* the string, so a bare arrow expression is a no-op
    for name, js in (("PAD_SHIM_JS", PAD_SHIM_JS),):
        assert js.lstrip().startswith("(() =>") and js.rstrip().endswith("})();"), \
            f"{name} must be self-invoking (init-script form law)"
    checks += 1
    say("selftest: injected scripts are self-invoking (add_init_script form law)")

    say(f"selftest: PASS — {checks} checks, every gesture compiled and walked "
        "against a fake CDP/keyboard/evaluate wire (offline, no browser)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PlayerOne's universal input actuator — gesture grammar "
                    "+ touch/gyro/gamepad/key transports")
    parser.add_argument("--selftest", action="store_true",
                        help="offline: assert compiled op streams with fake "
                             "CDP/playwright layers")
    parser.add_argument("--profile", help="show the resolved profile for a game")
    parser.add_argument("--intent", help="with --profile: show the gestures")
    args = parser.parse_args()
    if args.selftest:
        return _selftest()
    if args.profile:
        profile = load_profile(args.profile)
        say(json.dumps(profile, indent=2)[:1600])
        if args.intent:
            ctx = {"viewport": profile.get("viewport")}
            say(f"intent {args.intent!r} -> "
                f"{[repr(g) for g in from_intent(profile, args.intent, ctx)]}")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
