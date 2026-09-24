#!/usr/bin/env python3
"""diag_ticks — the brain lane's pre-flight instrument: eyes + HUD, NO hands.

One booted game page (brain_loop's own boot law), the perception-gated start
routine, then N seconds of one JSON line per tick on stdout:

    {"t": 1.2, "player": [x, y, w, h], "threats": 2, "items": 0,
     "aux": {"air": 84, "ping": "ready", "echo": "no", "ring": 12.3},
     "source": {...}}

It answers "what does the lane actually SEE while the game plays" — the
template hit rate on the player, and whether the sonar air/ping/echo probes
read in their training bands on the live page. It never sends input after the
start (the game runs on its own law) and writes nothing but its lines.

    P1_EYES=fusion python3 diag_ticks.py --game sonar --seconds 25
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")   # the GPU law: CPU only

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cv2                                  # noqa: E402
import brain                                # noqa: E402
import brain_loop                           # noqa: E402  (boot + start law)
import perception                           # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="eyes-only tick diagnostic")
    parser.add_argument("--game", required=True)
    parser.add_argument("--seconds", type=float, default=25.0)
    parser.add_argument("--url", default=None)
    parser.add_argument("--boot-seconds", type=float, default=brain_loop.BOOT_SECONDS)
    parser.add_argument("--frames-dir", default="/tmp/diag_frames")
    args = parser.parse_args()

    if os.environ.get("P1_EYES", "").strip().lower() == "fusion":
        perception.configure(True)
        print("eyes: fusion", file=sys.stderr, flush=True)
    url = args.url or brain_loop.DEFAULT_URL.format(game=args.game)

    from playwright.sync_api import sync_playwright
    events: list = []
    t0 = time.monotonic()
    pw = sync_playwright().start()
    lines = 0
    try:
        browser, context, page, bridge = brain_loop.boot_page(
            pw, url, args.boot_seconds, events, t0)
        deadline = time.monotonic() + args.seconds
        next_beat = 0.0
        started = False
        aux = None
        while time.monotonic() < deadline:
            frame = brain_loop.shoot(page)
            state = perception.perceive(frame, args.game) \
                if frame is not None else {}
            aux = brain.aux_sonar(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)) \
                if (args.game == "sonar" and frame is not None) else None
            if not started:
                if args.game == "sonar":
                    playing_now = (aux is not None
                                   and aux.get("scene") == "playing")
                else:
                    playing_now = brain_loop.in_play(state, args.game, aux)
                if playing_now:
                    started = True
                    print(f"field in play at "
                          f"{time.monotonic() - t0:.1f}s — observing",
                          file=sys.stderr, flush=True)
                elif time.monotonic() >= next_beat:
                    next_beat = time.monotonic() + brain_loop.START_BEAT
                    if args.game == "sonar":
                        scene = aux.get("scene") if aux else None
                        try:
                            if scene == "over":
                                bridge.key("Space", 100)
                            else:
                                bridge.tap(240, 430)
                        except Exception:
                            pass
                    else:
                        for intent in ("start", "retry", "confirm"):
                            try:
                                bridge.perform_intent(
                                    intent,
                                    {"viewport": list(brain_loop.VIEWPORT)})
                                break
                            except Exception:
                                continue
            print(json.dumps({
                "t": round(time.monotonic() - t0, 2),
                "player": list(state["player"]) if state.get("player") else None,
                "threats": len(state.get("threats") or []),
                "items": len(state.get("items") or []),
                "aux": aux,
                "source": state.get("source"),
            }, default=str), flush=True)
            if frame is not None and args.frames_dir \
                    and (lines % 8 == 0 or not started):
                from pathlib import Path
                path = Path(args.frames_dir)
                path.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(path / f"t{int((time.monotonic() - t0) * 10):05d}.png"),
                            frame)
            lines += 1
            page.wait_for_timeout(int(1000 / brain.DECISION_HZ))
    finally:
        try:
            browser.close()
        except Exception:
            pass
        pw.stop()
        print(f"diag: {lines} ticks in {time.monotonic() - t0:.1f}s",
              file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
