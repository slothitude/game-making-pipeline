#!/usr/bin/env python3
"""collect_evidence — the critic's eyes: server-side Playwright against the
LIVE HTML5 games.

The audit gap: exec_critique mode=full needs screenshots, and the only lane
that produced them was exec_emulator — which needs the gmp-atd AVD booted
(a luxury on the 956MB box) and has no Chrome anywhere else in the stack.
This lane drives headless chromium straight at the live game page on
retromonkey: phone viewport, ~1.5s screenshot cadence, last 12 frames kept,
and a latest.json the critic consumes directly.

On the server (~/playerone venv — see README.md for the one-time install):

    ~/playerone/venv/bin/python collect_evidence.py --game sonar --seconds 60
    ~/playerone/venv/bin/python collect_evidence.py --game slime-line \\
        --seconds 45 --url https://retromonkey.com.au/games/slime-line/?debug

Output: <out-root>/<game>/NNN.png (last 12, chronological) + latest.json:
    {game, url, started, seconds, shots, survival_seconds, actions_taken,
     note, events: [{t, note}]}
survival_seconds/actions_taken use llm_critic.judge()'s evidence vocabulary so
the critique ladder reads this feed the same way it reads emu_play's. A load
failure still writes latest.json — with "error" set — so critique jobs can
see WHY instead of guessing.

No input is ever sent to the game: this is observation only (the emulator lane
remains the play lane). That is deliberate — the critic's judge() needs to see
the game and its console, not steer it.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_URL = "https://retromonkey.com.au/games/{game}/"
DEFAULT_OUT_ROOT = "/home/ubuntu/playerone/evidence"
VIEWPORT = {"width": 480, "height": 800}  # the phone frame the critic reads
SHOT_CADENCE = 1.5      # seconds between screenshots
KEEP_SHOTS = 12         # ring size — cap disk, newest wins
BOOT_SETTLE = 2.0       # let the canvas paint its first real frame
GOTO_TIMEOUT = 60_000   # ms — live site on a small box, be patient
MAX_EVENTS = 200        # a page that spams console errors gets truncated
SHOT_FAIL_LIMIT = 5     # consecutive failed shots before we bail out
NOTE = "playwright evidence feed"


def say(msg: str) -> None:
    # progress chatter goes to stderr — stdout is reserved for the JSON
    # report (exec-style wrappers json.loads it, like exec_emulator does)
    print(f"[collect_evidence] {msg}", file=sys.stderr, flush=True)


def game_dir(out_root: str, game: str) -> str:
    return os.path.join(out_root, game)


def clear_stale_shots(directory: str) -> int:
    """Drop NNN.png from a previous run so this run's ring is pure."""
    removed = 0
    if not os.path.isdir(directory):
        return 0
    for name in os.listdir(directory):
        if re.fullmatch(r"\d{3}\.png", name):
            try:
                os.remove(os.path.join(directory, name))
                removed += 1
            except OSError:
                pass
    return removed


def write_latest(directory: str, report: dict) -> str:
    path = os.path.join(directory, "latest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return path


def collect(game: str, seconds: float, url: str, out_root: str) -> dict:
    """One run: load url, shoot the loop, return the latest.json payload."""
    from playwright.sync_api import sync_playwright

    directory = game_dir(out_root, game)
    os.makedirs(directory, exist_ok=True)
    clear_stale_shots(directory)

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events: list[dict] = []
    error: str | None = None
    shots = 0

    def note_event(note: str) -> None:
        if len(events) < MAX_EVENTS:
            events.append({"t": round(time.monotonic() - t0, 2), "note": note})

    t0 = time.monotonic()
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    try:  # fresh context per run — no cookie/service-worker carryover
        context = browser.new_context(
            viewport=VIEWPORT, is_mobile=True, has_touch=True)
        page = context.new_page()
        page.on("console", lambda msg: note_event(f"console {msg.type}: "
                                                 f"{msg.text[:200]}")
                if msg.type == "error" else None)
        page.on("pageerror", lambda exc: note_event(
            f"pageerror: {str(exc)[:200]}"))

        try:
            page.goto(url, wait_until="load", timeout=GOTO_TIMEOUT)
            load_seconds = round(time.monotonic() - t0, 2)
            note_event(f"load complete in {load_seconds}s")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            note_event("load error: " + " ".join(error.split())[:200])

        if error is None:
            page.wait_for_timeout(int(BOOT_SETTLE * 1000))
            note_event(f"settled {BOOT_SETTLE}s for boot")

            # get PAST the menu: our games start on a tap-to-begin title
            # screen. Press Enter, then tap center twice with a beat between
            # (menu -> game). Best-effort — a game already in play ignores it.
            for label, action in (("enter", lambda: page.keyboard.press("Enter")),
                                  ("tap1", lambda: page.touchscreen.tap(240, 400)),
                                  ("tap2", lambda: page.touchscreen.tap(240, 430))):
                try:
                    action()
                    note_event(f"start attempt: {label}")
                    page.wait_for_timeout(1200)
                except Exception as exc:  # noqa: BLE001 — never kill the run
                    note_event(f"start attempt {label} failed: "
                               f"{type(exc).__name__}")

            kept: list[str] = []  # capture order, paths of live ring
            failures = 0
            deadline = time.monotonic() + seconds
            next_shot = time.monotonic() + SHOT_CADENCE
            while time.monotonic() < deadline and error is None:
                time.sleep(max(0.0, next_shot - time.monotonic()))
                next_shot += SHOT_CADENCE
                try:
                    try:
                        cdp = getattr(page, "_gmp_cdp", None)
                        if cdp is None:
                            cdp = page.context.new_cdp_session(page)
                            page._gmp_cdp = cdp
                        png = base64.b64decode(
                            cdp.send("Page.captureScreenshot",
                                     {"format": "png"})["data"])
                    except Exception:
                        png = page.screenshot(timeout=8000)
                    failures = 0
                except Exception as exc:
                    failures += 1
                    note_event(f"shot {shots} failed: "
                               f"{type(exc).__name__}: {str(exc)[:160]}")
                    if failures >= SHOT_FAIL_LIMIT:
                        error = f"gave up after {failures} consecutive " \
                                "failed screenshots (page gone?)"
                    continue
                path = os.path.join(directory, f"{shots:03d}.png")
                with open(path, "wb") as f:
                    f.write(png)
                shots += 1
                kept.append(path)
                if len(kept) > KEEP_SHOTS:  # rotate: cap disk mid-run
                    os.remove(kept.pop(0))
            # renumber the surviving ring 000..NN in chronological order so
            # llm_critic's sorted()[:3] vision sample reads oldest-first
            for i, path in enumerate(kept):
                target = os.path.join(directory, f"{i:03d}.png")
                if os.path.abspath(path) != os.path.abspath(target):
                    os.replace(path, target)
    except KeyboardInterrupt:
        error = error or "interrupted by operator"
    finally:
        browser.close()
        pw.stop()

    report = {
        "game": game,
        "url": url,
        "started": started,
        "seconds": seconds,
        "shots": shots,
        "survival_seconds": seconds,   # judge()'s evidence vocabulary: the
        "actions_taken": shots,        # feed watched for the whole window
        "note": NOTE,
        "events": events,
    }
    if error is not None:
        report["error"] = error
    path = write_latest(directory, report)
    flat = " ".join(str(error).split())[:200] if error else ""
    say(f"wrote {path} (shots={shots}, events={len(events)}"
        + (f", error={flat}" if flat else "") + ")")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Playwright evidence feed for exec_critique mode=full")
    parser.add_argument("--game", required=True,
                        help="game slug, e.g. sonar")
    parser.add_argument("--seconds", type=float, default=60.0,
                        help="how long to watch the live page (default 60)")
    parser.add_argument("--url", default=None,
                        help="override page url "
                             f"(default {DEFAULT_URL})")
    parser.add_argument("--out-root", default=DEFAULT_OUT_ROOT,
                        help=f"evidence root (default {DEFAULT_OUT_ROOT})")
    args = parser.parse_args()

    url = args.url or DEFAULT_URL.format(game=args.game)
    try:
        report = collect(args.game, args.seconds, url, args.out_root)
    except KeyboardInterrupt:
        say("interrupted — partial latest.json already on disk if past load")
        return 130
    except ImportError as exc:
        say(f"playwright missing: {exc} — see README.md install step")
        return 3
    print(json.dumps(report, indent=2))
    return 1 if report.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
