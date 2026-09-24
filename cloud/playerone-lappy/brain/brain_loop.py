#!/usr/bin/env python3
"""brain_loop — PlayerOne plays: eyes -> jevlike brain -> hands, at reflex rate.

The playtest lane's brain mode. One booted game page (the collect_evidence
shape: 480x800 phone context, chromium headless; a persistent profile by
default — selfplay's wasm-cache law — so only the first run pays the
download; --profile-dir fresh reverts to a throwaway context), then per tick:

    CDP screenshot -> cv2 -> perception.perceive() (the fused state dict)
    -> brain.reflex(state, game, profile) -> [input_bridge.Gesture]
    -> Bridge.perform() (CDP touch / keyboard) -> log

Rate and spam are bounded by law (brain.py): DECISION_HZ ticks a second at
most, GESTURE_BUDGET_PER_SECOND gestures land a second at most. The wait
class, a sub-CONFIDENCE_FLOOR score, an unencoded game and an exhausted
budget all land as "no gesture" — silence is logged, never guessed through.

Flags (all read from the environment, forwarded by worker.lane_env):
    P1_BRAIN=jevlike   this lane refuses to run without it (the worker gates
                       the lane on the same flag; belt and braces)
    P1_EYES=fusion     perception's YOLO fill pass (a venv without ultralytics
                       degrades to the template lane — never a crash)
    P1_INPUT=bridge    the worker's actuator flag; this lane always acts
                       through input_bridge.Bridge, flag or no flag

Honesty notes, up front:
  * score is NOT readable by these eyes (no HUD OCR in the sprite bank) — the
    report carries survival/deaths/decisions, not points.
  * the sonar lane supplements the state dict with the training recorder's own
    HUD pixel probes (air bar, ping ring, echo flash — brain.aux_sonar); the
    star-visitor lane gets nothing extra (its brain is a documented transfer).
  * legacy playtest (collect_evidence) sends NO input: its survival_seconds is
    the whole window by construction. The numbers compare a lane that plays
    against one that watches.

Output: progress on stderr, ONE json report on stdout (the worker json.loads
it, exec-style), latest.json under <out-root>/<game>/ in collect_evidence's
vocabulary plus the brain fields.

    P1_BRAIN=jevlike python3 brain_loop.py --game sonar --seconds 60
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")   # the GPU law: CPU only —
# perception's YOLO fill runs on the CPU here; the 3060 belongs to the arbiter

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("PLAYERONE_ROOT", HERE.parent))
for cand in (str(HERE), str(ROOT), str(ROOT / "scripts")):
    if cand not in sys.path:
        sys.path.append(cand)

import cv2                                  # noqa: E402
import numpy as np                          # noqa: E402

import brain                                # noqa: E402  (the spinal cord)
import perception                           # noqa: E402  (the eyes)

DEFAULT_URL = "https://retromonkey.com.au/games/{game}/"
VIEWPORT = (480, 800)          # the phone frame the critic + the eyes read
GOTO_TIMEOUT = 60_000
BOOT_SECONDS = 180.0           # loader-overlay wait (cold wasm cache law)
BOOT_SETTLE = 3.0              # engine paints after the overlay hides
SHOT_CADENCE = 1.5             # the legacy evidence ring's cadence
KEEP_SHOTS = 12
START_BUDGET = 90.0            # wall seconds for the start routine
START_BEAT = 2.5               # seconds between start gestures
RESTART_BUDGET = 12            # bounded death restarts per session
DEAD_FRAMES = 8                # field not in play this many ticks -> dead
MAX_EVENTS = 200


def say(msg: str) -> None:
    print(f"[brain_loop] {msg}", file=sys.stderr, flush=True)


def note(events: list, t0: float, text: str) -> None:
    if len(events) < MAX_EVENTS:
        events.append({"t": round(time.monotonic() - t0, 2), "note": text})


# ------------------------------------------------------------------- the page --
def alive(state: dict) -> bool:
    """The pixel truth of "the game is up and something is on the field":
    the player, or any threat/item the eyes found. (star-visitor's hero
    template is partial on walk frames — a sighted agent counts too.)"""
    return bool(state.get("player") or state.get("threats")
                or state.get("items"))


def in_play(state: dict, game: str, aux: dict | None) -> bool:
    """The pixel truth that the game is actually BEING PLAYED. Sonar needs
    more than alive(): the title logo reads as a player to the eyes and the
    death overlay keeps its background decorations, so the recorder's own
    hull law (brain.aux_sonar's scene verdict) is the referee there."""
    if not alive(state):
        return False
    if game == "sonar" and aux is not None:
        return aux.get("scene") == "playing"
    return True


def boot_page(pw, url: str, boot_seconds: float, events: list, t0: float,
              profile_dir: str | None = None):
    """One booted game page, touch on, bridge in.

    profile_dir set -> a persistent context (selfplay's wasm-cache law): the
    FIRST run pays the full wasm download, later runs boot from cache — the
    difference on a throttled site is minutes per session. None -> a fresh
    throwaway context (collect_evidence's law).

    The loader probe wants the #status overlay gone, but never on a
    technicality: an overlay that was never SEEN does not count as cleared
    (found live — the Godot boot splash paints over it and the probe passed
    on a frame that was still splashing), and after it clears the engine
    still needs BOOT_SETTLE before the canvas is playable."""
    from input_bridge import Bridge

    kwargs = dict(viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
                  is_mobile=True, has_touch=True)
    if profile_dir:
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        browser = None
        context = pw.chromium.launch_persistent_context(
            profile_dir, headless=True, **kwargs)
        page = context.pages[0] if context.pages else context.new_page()
    else:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(**kwargs)
        page = context.new_page()
    net_broken = {"seen": False}

    def _on_pageerror(exc) -> None:
        text = str(exc)[:200]
        note(events, t0, f"pageerror: {text}")
        if "network" in text.lower():
            net_broken["seen"] = True    # Godot's loader throws on a dead fetch

    page.on("console", lambda msg: note(events, t0, f"console {msg.type}: "
             f"{msg.text[:200]}") if msg.type == "error" else None)
    page.on("pageerror", _on_pageerror)
    page.goto(url, wait_until="load", timeout=GOTO_TIMEOUT)
    note(events, t0, f"load complete in {round(time.monotonic() - t0, 2)}s")
    bridge = Bridge(page).install()
    STATUS_HIDDEN = ("() => { const s = document.getElementById('status');"
                     " return !s || getComputedStyle(s).visibility === 'hidden'"
                     " || s.style.visibility === 'hidden'; }")
    start = time.monotonic()
    seen_visible = False
    reloads = 0
    while time.monotonic() - start < boot_seconds:
        if net_broken["seen"] and reloads < 3:
            reloads += 1
            net_broken["seen"] = False
            note(events, t0, f"network error during boot — reload {reloads}")
            try:
                page.goto(url, wait_until="load", timeout=GOTO_TIMEOUT)
            except Exception as exc:
                note(events, t0, "reload failed: "
                     + " ".join(str(exc).split())[:120])
            start = time.monotonic()
            seen_visible = False
            continue
        try:
            if not page.evaluate(STATUS_HIDDEN):
                seen_visible = True
            elif seen_visible or time.monotonic() - start > 10.0:
                note(events, t0, f"loader cleared in "
                     f"{round(time.monotonic() - start, 1)}s "
                     f"(seen_visible={seen_visible})")
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    else:
        note(events, t0, f"loader overlay never cleared in "
             f"{boot_seconds:.0f}s — pressing on")
    page.wait_for_timeout(int(BOOT_SETTLE * 1000))
    return browser, context, page, bridge


def shoot(page) -> np.ndarray | None:
    """One frame as BGR — CDP first (the fast path), page.screenshot as the
    fallback, exactly collect_evidence's order."""
    try:
        cdp = getattr(page, "_p1_cdp", None)
        if cdp is None:
            cdp = page.context.new_cdp_session(page)
            page._p1_cdp = cdp
        png = base64.b64decode(cdp.send("Page.captureScreenshot",
                                        {"format": "png"})["data"])
    except Exception:
        try:
            png = page.screenshot(timeout=8000)
        except Exception:
            return None
    return cv2.imdecode(np.frombuffer(png, np.uint8, len(png)),
                        cv2.IMREAD_COLOR)


# ------------------------------------------------------------------- session --
def run(game: str, seconds: float, url: str, out_root: Path,
        temperature: float, boot_seconds: float, tag: str,
        profile_dir: str | None = None) -> dict:
    from input_bridge import load_profile
    profile = load_profile(game)
    brain_npz, encoding_note = brain.brain_for(game)
    report: dict = {
        "game": game, "url": url, "lane": "jevlike",
        "brain": brain_npz.npz_name if brain_npz else None,
        "encoding": encoding_note, "temperature": temperature,
        "decision_hz": brain.DECISION_HZ,
        "gesture_budget_per_second": brain.GESTURE_BUDGET_PER_SECOND,
        "tag": tag,
    }
    if brain_npz is None:
        say(f"{game}: no encoding — the lane will hold its fire (do-nothing)")

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events: list = []
    t0 = time.monotonic()
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    error = None
    try:
        browser, context, page, bridge = boot_page(pw, url, boot_seconds,
                                                   events, t0, profile_dir)
    except Exception as exc:
        pw.stop()
        report.update({"error": f"boot failed: {type(exc).__name__}: {exc}",
                       "started": started, "seconds": seconds, "shots": 0,
                       "survival_seconds": 0, "actions_taken": 0,
                       "note": "jevlike brain lane", "events": events})
        _write_latest(out_root, game, report)
        print(json.dumps(report, default=str))
        return report

    shots = decisions = gestures_landed = 0
    deaths = restarts = budget_drops = waits = 0
    playing_ticks = 0
    alive_wall = 0.0               # measured, not assumed: the dt between
    last_play_ts = None            # consecutive in-play ticks, per life
    play_started = None            # when the field first came into play
    perceive_ms: list = []
    intents: dict = {}
    monologues: list = []
    last_shot = 0.0
    dead_streak = 0
    rode_out = False
    last_state: dict = {}
    budget = brain.Budgeter()
    was_playing = False

    def start_attempt(label: str, aux: dict | None = None,
                      restart: bool = False) -> None:
        """The training recorder's own start law where one exists: sonar's
        title screen starts on a centre tap (240, 430) and its death overlay
        restarts on Space (taps do nothing there) — sonar_playthrough
        .start_dive verbatim, on the bridge's hands. Space is only for a
        restart we EARNED (the field was in play and left it): a dark splash
        and a game-over look the same to the pixel law. Every other game
        falls back to its profile intents."""
        try:
            if game == "sonar":
                if restart and aux is not None and aux.get("scene") == "over":
                    bridge.key("Space", 100)
                    note(events, t0, f"{label}: Space (game-over restart)")
                else:
                    bridge.tap(240, 430)
                    note(events, t0, f"{label}: tap (240, 430)")
            else:
                intents = ("retry", "start", "confirm") if restart \
                    else ("start", "retry", "confirm")
                for intent in intents:
                    try:
                        bridge.perform_intent(intent,
                                              {"viewport": list(VIEWPORT)})
                        note(events, t0, f"{label}: {intent}")
                        break
                    except Exception:
                        continue
            page.wait_for_timeout(1200)
        except Exception as exc:
            note(events, t0, f"{label} failed: {type(exc).__name__}")

    try:
        note(events, t0, "start routine (perception-gated, "
             f"{START_BUDGET:.0f}s wall budget)")
        start_deadline = time.monotonic() + START_BUDGET
        next_beat = 0.0
        while time.monotonic() < start_deadline:
            frame = shoot(page)
            state = perception.perceive(frame, game) if frame is not None \
                else {}
            aux = brain.aux_sonar(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)) \
                if (game == "sonar" and frame is not None) else None
            if in_play(state, game, aux):
                note(events, t0, "field in play — the game is running"
                     + (f" (scene={aux.get('scene')}, air={aux.get('air')})"
                        if aux else ""))
                play_started = time.monotonic()
                break
            if time.monotonic() >= next_beat:
                next_beat = time.monotonic() + START_BEAT
                start_attempt(f"start attempt "
                              f"{int((time.monotonic() - t0) / START_BEAT)}",
                              aux)
        tick = 1.0 / brain.DECISION_HZ
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            t_tick = time.monotonic()
            frame = shoot(page)
            if frame is None:
                note(events, t0, "frame gone — ending the session")
                break
            state = perception.perceive(frame, game)
            perceive_ms.append(state["ms"])
            if shots == 0 or t_tick - last_shot >= SHOT_CADENCE:
                _keep_shot(out_root, game, shots, frame)
                shots += 1
                last_shot = t_tick

            aux = brain.aux_sonar(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)) \
                if game == "sonar" else None
            playing = in_play(state, game, aux)
            if playing:
                playing_ticks += 1
                now = time.monotonic()
                if last_play_ts is not None and now - last_play_ts < 1.5:
                    alive_wall += now - last_play_ts   # same life: wall dt
                last_play_ts = now
                dead_streak = 0
                rode_out = False
                was_playing = True
            else:
                dead_streak += 1
                if dead_streak >= DEAD_FRAMES:
                    if was_playing:
                        deaths += 1
                        was_playing = False
                        last_play_ts = None     # a new life starts the clock
                    if restarts >= RESTART_BUDGET:
                        if not rode_out:
                            note(events, t0,
                                 "restart budget spent — riding it out")
                            rode_out = True
                    else:
                        restarts += 1
                        note(events, t0, f"field left play x{DEAD_FRAMES} "
                             f"(scene={aux.get('scene') if aux else 'n/a'}) — "
                             f"restart {restarts}")
                        start_attempt("restart", aux, restart=True)
                        dead_streak = 0
                time.sleep(max(0.0, tick - (time.monotonic() - t_tick)))
                continue

            gesture_list, info = brain.reflex(
                state, game, profile, aux=aux, temperature=temperature,
                budget=budget)
            if info["context"] is not None and len(monologues) < 240:
                monologues.append({k: info[k] for k in
                                   ("context", "action", "intent", "p")})
            if info["intent"] and not gesture_list and info["p"] >= \
                    brain.CONFIDENCE_FLOOR:
                budget_drops += 1
            for gesture in gesture_list:
                try:
                    bridge.perform(gesture)
                    gestures_landed += 1
                except Exception as exc:
                    note(events, t0, f"gesture {gesture.kind} failed: "
                         f"{type(exc).__name__}")
            if info["intent"]:
                intents[info["intent"]] = intents.get(info["intent"], 0) + 1
            else:
                waits += 1
            decisions += 1
            last_state = {
                "player": state.get("player"),
                "threats": len(state.get("threats") or []),
                "items": len(state.get("items") or []),
                "yolo_ms": state.get("yolo_ms"),
                "scene": aux.get("scene") if aux else None,
                "air": aux.get("air") if aux else None,
                "context": info["context"],
            }
            time.sleep(max(0.0, tick - (time.monotonic() - t_tick)))
    except KeyboardInterrupt:
        error = "interrupted by operator"
    except Exception as exc:                       # noqa: BLE001 — report, don't die
        error = f"{type(exc).__name__}: {exc}"
        note(events, t0, "lane error: " + " ".join(str(error).split())[:200])
    finally:
        try:
            bridge.release()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
            context.close()
        except Exception:
            pass
        pw.stop()

    wall = round(time.monotonic() - t0, 1)
    play_window = round(time.monotonic() - play_started, 1) \
        if play_started is not None else 0.0
    perceive_ms.sort()
    report.update({
        "started": started, "seconds": seconds,
        "shots": shots,
        "survival_seconds": seconds,     # judge()'s vocabulary: the window
        "actions_taken": gestures_landed,
        "note": "jevlike brain lane (eyes -> brain -> hands)",
        "decisions": decisions,          # reflex passes on playing frames
        "waits": waits,                  # the do-nothing class honoured
        "gestures": gestures_landed,
        "gestures_per_second": round(gestures_landed / max(wall, 0.1), 2),
        "intents": intents,
        "budget_drops": budget_drops,
        "deaths": deaths,
        "restarts": restarts,
        "playing_ticks": playing_ticks,
        "alive_seconds": round(alive_wall, 1),   # measured wall time in play
        "play_window": play_window,              # wall seconds from first play
        "ticks": len(perceive_ms),
        "mean_perception_ms": round(sum(perceive_ms) / len(perceive_ms), 2)
        if perceive_ms else 0.0,
        "p95_perception_ms": perceive_ms[int(len(perceive_ms) * 0.95)]
        if perceive_ms else 0.0,
        "decision_rate_hz": round(len(perceive_ms) / max(play_window, 0.1), 2)
        if play_window else 0.0,
        "final_state": last_state,
        "monologues": monologues[-12:],
        "events": events,
    })
    if error is not None:
        report["error"] = error
    path = _write_latest(out_root, game, report)
    say(f"wrote {path} (decisions={decisions}, gestures={gestures_landed}, "
        f"deaths={deaths}, shots={shots})")
    print(json.dumps(report, default=str))
    return report


def _keep_shot(out_root: Path, game: str, index: int, frame: np.ndarray) -> None:
    """The legacy evidence ring: last KEEP_SHOTS frames as NNN.png, so the
    critic's vision lane reads a brain session exactly like a legacy one."""
    directory = out_root / game
    directory.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(directory / f"{index:03d}.png"), frame)
    stale = sorted(directory.glob("*.png"))
    if len(stale) > KEEP_SHOTS:
        stale[0].unlink(missing_ok=True)


def _write_latest(out_root: Path, game: str, report: dict) -> Path:
    directory = out_root / game
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "latest.json"
    path.write_text(json.dumps(report, indent=2, default=str),
                    encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PlayerOne's brain lane: eyes -> jevlike brain -> hands")
    parser.add_argument("--game", required=True)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--url", default=None)
    parser.add_argument("--out-root", default=str(ROOT / "evidence"))
    parser.add_argument("--temperature", type=float, default=0.0,
                        help="0 = greedy (the play law); >0 samples p**(1/T)")
    parser.add_argument("--boot-seconds", type=float, default=BOOT_SECONDS)
    parser.add_argument("--profile-dir", default=None,
                        help="persistent browser profile (selfplay's "
                             "wasm-cache law; default "
                             "<root>/data/pw-profiles/brain-<game>; "
                             "'fresh' = a throwaway context per run)")
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    if os.environ.get("P1_BRAIN", "").strip().lower() != "jevlike":
        say("P1_BRAIN=jevlike is not set — refusing to steer (fail closed)")
        return 2
    if os.environ.get("P1_EYES", "").strip().lower() == "fusion":
        perception.configure(True)     # the YOLO fill pass; degrades cleanly
        say("eyes: fusion (templates primary, YOLO fills the holes)")
    else:
        say("eyes: templates")
    url = args.url or DEFAULT_URL.format(game=args.game)
    if args.profile_dir == "fresh":
        profile_dir = None               # collect_evidence's throwaway law
    else:
        profile_dir = args.profile_dir or str(
            ROOT / "data" / "pw-profiles" / f"brain-{args.game}")
    run(args.game, args.seconds, url, Path(args.out_root), args.temperature,
        args.boot_seconds, args.tag, profile_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
