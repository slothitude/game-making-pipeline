#!/usr/bin/env python3
"""playerone-lappy perception_evidence — live proof the eyes v1 work.

Loads a live game in headless chromium (the same phone frame collect_evidence
shoots), grabs periodic JPEGs for N seconds, then runs perception.py OFFLINE
over the captures: a detection-rate summary per class plus annotated frames
(cv2.rectangle on every hit) under evidence/perception/<game>/.

    python perception_evidence.py --game star-visitor [--seconds 45]

Offline pass only (no browser — re-perceive frames already on disk):
    python perception_evidence.py --game star-visitor --no-capture
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from perception import perceive, annotate  # noqa: E402

URL = "https://retromonkey.com.au/games/{game}/"
OUT_ROOT = HERE / "evidence" / "perception"
VIEWPORT = {"width": 480, "height": 800}   # the phone frame the critic reads
SHOT_CADENCE = 1.5
BOOT_SETTLE = 2.0
GOTO_TIMEOUT = 60_000
BOOT_TIMEOUT = 180_000    # the pck download crawls on lappy's uplink — wait
ANNOTATED = 3


def say(msg: str) -> None:
    print(f"[perception_evidence] {msg}", file=sys.stderr, flush=True)


def capture(game: str, seconds: float, url: str, frames_dir: Path) -> int:
    """Live headless chromium -> JPEG frames on disk. Returns the shot count."""
    from playwright.sync_api import sync_playwright

    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("*.jpg"):
        old.unlink()

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    shots = 0
    try:
        context = browser.new_context(viewport=VIEWPORT, is_mobile=True,
                                      has_touch=True)
        page = context.new_page()
        page.goto(url, wait_until="load", timeout=GOTO_TIMEOUT)
        say(f"{game}: loaded {url}")
        # the Godot export keeps a #status splash up until the engine is up;
        # gameplay before it hides means frames of a download bar
        try:
            page.wait_for_selector("#status", state="hidden",
                                   timeout=BOOT_TIMEOUT)
            say(f"{game}: engine boot complete")
        except Exception as exc:  # noqa: BLE001 — capture what is there
            say(f"{game}: boot wait gave up ({type(exc).__name__})")
        page.wait_for_timeout(int(BOOT_SETTLE * 1000))
        # past the tap-to-begin title (best-effort, matches collect_evidence)
        for label, action in (
                ("enter", lambda: page.keyboard.press("Enter")),
                ("tap1", lambda: page.touchscreen.tap(240, 400)),
                ("tap2", lambda: page.touchscreen.tap(240, 430))):
            try:
                action()
                page.wait_for_timeout(1200)
            except Exception:  # noqa: BLE001 — the shot loop still runs
                pass
            say(f"start attempt: {label}")
        deadline = time.monotonic() + seconds
        next_shot = time.monotonic() + SHOT_CADENCE
        while time.monotonic() < deadline:
            time.sleep(max(0.0, next_shot - time.monotonic()))
            next_shot += SHOT_CADENCE
            try:
                jpeg = page.screenshot(type="jpeg", quality=85, timeout=8000)
            except Exception as exc:  # noqa: BLE001 — count it and carry on
                say(f"shot {shots} failed: {type(exc).__name__}")
                continue
            (frames_dir / f"{shots:03d}.jpg").write_bytes(jpeg)
            shots += 1
    finally:
        browser.close()
        pw.stop()
    say(f"{game}: captured {shots} frames -> {frames_dir}")
    return shots


def analyse(game: str, frames_dir: Path, out_dir: Path) -> dict:
    """Offline perceive() over every frame -> summary + annotated evidence."""
    frames = sorted(frames_dir.glob("*.jpg"))
    if not frames:
        return {"error": f"no frames under {frames_dir}"}
    class_hits: dict[str, int] = {}
    player_hits = 0
    ms_total = 0.0
    per_frame = []
    states = []
    for path in frames:
        img = cv2.imread(str(path))
        if img is None:
            continue
        state = perceive(img, game)
        states.append((path, state))
        ms_total += state["ms"]
        if state["player"]:
            player_hits += 1
        seen: set[str] = set()
        for cls, *_ in state["all"]:
            class_hits[cls] = class_hits.get(cls, 0) + 1
            seen.add(cls)
        per_frame.append({"frame": path.name, "player": state["player"],
                          "detections": len(state["all"]),
                          "classes": sorted(seen), "ms": state["ms"]})

    n = max(1, len(states))
    summary = {
        "game": game,
        "frames": len(states),
        "player_found_share": round(player_hits / n, 3),
        "mean_ms": round(ms_total / n, 2),
        "class_rates": {cls: round(hits / n, 3)
                        for cls, hits in sorted(class_hits.items())},
        "per_frame": per_frame,
    }

    picks = [states[i] for i in sorted({
        0, n * 2 // 3, n - 1}) if 0 <= i < len(states)][:ANNOTATED]
    annotated = []
    for path, state in picks:
        img = cv2.imread(str(path))
        out = out_dir / f"annotated_{path.stem}.jpg"
        cv2.imwrite(str(out), annotate(img, state))
        annotated.append(str(out))
    summary["annotated_frames"] = annotated

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True)
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--url", default=None)
    ap.add_argument("--no-capture", action="store_true",
                    help="analyse frames already on disk")
    args = ap.parse_args()

    url = args.url or URL.format(game=args.game)
    out_dir = OUT_ROOT / args.game
    frames_dir = out_dir / "frames"
    if not args.no_capture:
        capture(args.game, args.seconds, url, frames_dir)
    summary = analyse(args.game, frames_dir, out_dir)
    if summary.get("error"):
        say(f"{args.game}: {summary['error']}")
        return 1
    print(json.dumps({k: v for k, v in summary.items() if k != "per_frame"},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
