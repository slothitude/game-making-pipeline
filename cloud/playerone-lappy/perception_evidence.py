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
import numpy as np
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from perception import perceive, annotate  # noqa: E402

URL = "https://retromonkey.com.au/games/{game}/"
OUT_ROOT = HERE / "evidence" / "perception"
BROWSER_CACHE = HERE / ".p1_browser_cache"   # persistent chromium profile
GAME_CACHE = HERE / "game_cache"             # curl'd copies of the export
                                             # files (resumable, reliable)
VIEWPORT = {"width": 480, "height": 800}   # the phone frame the critic reads
SHOT_CADENCE = 1.5
TAP_EVERY = 6              # a mid-screen tap every N shots — keeps tap-to-act
TAP_XY = (240, 400)        # games alive through the capture window instead of
                           # idling into their game-over screen
BOOT_SETTLE = 2.0
GOTO_TIMEOUT = 60_000
BOOT_TIMEOUT = 1_800_000
CONTENT_TIMEOUT = 900_000  # after the engine is up, the pck still downloads
                           # inside Godot's boot scene — gate on the canvas
                           # actually showing art (the loader is near-black)
CONTENT_LUMA = 28.0        # mean grayscale above the loader's ~14    # wasm+pck crawl over lappy's ~37KB/s link (the
                          # splash bar sits near zero for minutes); #status
                          # flipping to hidden IS the engine-up signal
ANNOTATED = 3


def say(msg: str) -> None:
    print(f"[perception_evidence] {msg}", file=sys.stderr, flush=True)


def capture(game: str, seconds: float, url: str,
            frames_dir: Path) -> tuple[int, str]:
    """Live headless chromium -> JPEG frames on disk. Returns (shots, served
    mode) — the served mode goes into the summary as provenance."""
    from playwright.sync_api import sync_playwright

    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("*.jpg"):
        old.unlink()

    pw = sync_playwright().start()
    # lappy runs eth+wifi with two v6 default routes; the site has an AAAA
    # record and the v6 flow dies on every interface flap (ERR_NETWORK_CHANGED
    # forever at 5%) — pin the host to its IPv4
    args = ["--host-resolver-rules=MAP retromonkey.com.au 168.138.8.0"]
    browser = pw.chromium.launch_persistent_context(
        str(BROWSER_CACHE), headless=True, viewport=VIEWPORT,
        is_mobile=True, has_touch=True, args=args)
    shots = 0
    try:
        page = browser.new_page()
        # the 39.5MB wasm stalls mid-transfer on lappy's WAN flow more often
        # than not; when a curl'd copy exists, serve the export files from
        # disk (byte-identical art from the same URL — just a reliable pipe)
        cache_dir = GAME_CACHE / game
        wasm = cache_dir / "index.wasm"
        served = "network"
        if wasm.is_file() and wasm.stat().st_size > 1_000_000:
            ctypes = {".html": "text/html", ".js": "text/javascript",
                      ".wasm": "application/wasm",
                      ".pck": "application/octet-stream",
                      ".png": "image/png"}

            def _serve(route):
                name = os.path.basename(urlparse(route.request.url).path)
                f = cache_dir / name if name else None
                if f and f.is_file():
                    route.fulfill(status=200, body=f.read_bytes(),
                                  content_type=ctypes.get(f.suffix,
                                                          "application/octet-stream"))
                else:
                    route.continue_()

            page.route("**/*", _serve)
            served = f"local-cache ({cache_dir})"
        page.goto(url, wait_until="load", timeout=GOTO_TIMEOUT)
        say(f"{game}: loaded {url} [{served}]")
        # the engine-up signal: the export shell hides #status when the
        # runtime takes over (canvas dims flip early — not a boot signal)
        try:
            page.wait_for_selector("#status", state="hidden",
                                   timeout=BOOT_TIMEOUT)
            say(f"{game}: engine boot complete")
        except Exception as exc:  # noqa: BLE001 — capture what is there
            say(f"{game}: boot wait gave up ({type(exc).__name__})")
        page.wait_for_timeout(int(BOOT_SETTLE * 1000))
        say(f"{game}: waiting for the canvas to show art "
            f"(luma > {CONTENT_LUMA:.0f})")
        content_deadline = time.monotonic() + CONTENT_TIMEOUT / 1000.0
        lit = False
        while time.monotonic() < content_deadline:
            try:
                jpeg = page.screenshot(type="jpeg", quality=40, timeout=8000)
                gray = cv2.imdecode(np.frombuffer(jpeg, np.uint8),
                                    cv2.IMREAD_GRAYSCALE)
                luma = float(gray.mean()) if gray is not None else 0.0
            except Exception:  # noqa: BLE001 — page hiccup, keep polling
                luma = 0.0
            if luma > CONTENT_LUMA:
                lit = True
                break
            page.wait_for_timeout(5000)
        say(f"{game}: game content " + ("detected" if lit else "TIMEOUT"))
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
            if shots % TAP_EVERY == 0:
                try:
                    page.touchscreen.tap(*TAP_XY)
                except Exception:  # noqa: BLE001 — taps are best-effort
                    pass
    finally:
        browser.close()   # persistent context: cache survives for next run
        pw.stop()
    say(f"{game}: captured {shots} frames -> {frames_dir}")
    return shots, served


def analyse(game: str, frames_dir: Path, out_dir: Path,
            served: str = "network") -> dict:
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
        "served_from": served,
        "frames": len(states),
        "player_found_share": round(player_hits / n, 3),
        "mean_ms": round(ms_total / n, 2),
        "class_rates": {cls: round(hits / n, 3)
                        for cls, hits in sorted(class_hits.items())},
        "per_frame": per_frame,
    }

    # the annotated set should SHOW the matcher working: spread over the frames
    # that drew blood (detections), falling back to even spacing on a blank run
    hit_idx = [i for i, (_, st) in enumerate(states) if st["all"]]
    pool = hit_idx or list(range(len(states)))
    picks = [states[i] for i in sorted({pool[0], pool[len(pool) * 2 // 3],
                                        pool[-1]})][:ANNOTATED]
    annotated = []
    for stale in out_dir.glob("annotated_*.jpg"):
        stale.unlink()   # re-analyses must not leave stale picks behind
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
    served = "network"
    if not args.no_capture:
        shots, served = capture(args.game, args.seconds, url, frames_dir)
    summary = analyse(args.game, frames_dir, out_dir, served)
    if summary.get("error"):
        say(f"{args.game}: {summary['error']}")
        return 1
    print(json.dumps({k: v for k, v in summary.items() if k != "per_frame"},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
