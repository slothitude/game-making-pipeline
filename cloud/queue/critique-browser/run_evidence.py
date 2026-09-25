#!/usr/bin/env python3
"""run_evidence — the browser leg of a GMP critique, run ON LAPPY.

Why here: the queue box (retromonkey) has 956MB RAM and no playwright
browser — chained critique jobs died with `playwright install`. Lappy has
chromium (the playerone worker uses it), so exec_critique ships a JSON job
spec over the tailnet and this script shoots the live game page here.

Usage (from exec_critique.collect_evidence_lappy):
    ~/playerone/venv/bin/python run_evidence.py <job-dir>/spec.json

spec: {"game": "sonar", "url": "https://retromonkey.com.au/games/sonar/",
       "seconds": 60, "shot_cadence": 1.5, "keep_shots": 12}

Writes into the spec's own directory:
    NNN.png           the surviving screenshot ring, 000..NN chronological
    latest.json       {game, url, started, seconds, shots, survival_seconds,
                       actions_taken, note, events: [{t, note}], error?}
                      — `events` carries the console errors / pageerrors
    evidence.tar.gz   the flat tar exec_critique scp's back

stdout is reserved for the report JSON (exec-style wrappers json.loads it);
progress goes to stderr. Exit 0 clean, 1 if the report carries an error,
3 if playwright itself is missing.

Observation only — no input is ever sent to the game (the emulator lane
remains the play lane). Same law as retromonkey's collect_evidence.py.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tarfile
import time
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BOOT_SETTLE = 2.0       # let the canvas paint its first real frame
GOTO_TIMEOUT = 60_000   # ms — live site over the WAN, be patient
MAX_EVENTS = 200        # a page that spams console errors gets truncated
SHOT_FAIL_LIMIT = 5     # consecutive failed shots before we bail out
NOTE = "playwright evidence feed (Lappy browser leg)"


def say(msg: str) -> None:
    print(f"[run_evidence] {msg}", file=sys.stderr, flush=True)


def collect(spec: dict, out_dir: str) -> dict:
    """One run: load url, shoot the loop, return the latest.json payload."""
    from playwright.sync_api import sync_playwright

    game = str(spec.get("game") or "game")
    url = str(spec.get("url"))
    seconds = float(spec.get("seconds") or 60)
    cadence = float(spec.get("shot_cadence") or 1.5)
    keep_n = max(1, int(spec.get("keep_shots") or 12))

    for name in os.listdir(out_dir):        # a rerun in the same dir is pure
        if name.endswith((".png", ".tar.gz", ".json")):
            try:
                os.remove(os.path.join(out_dir, name))
            except OSError:
                pass

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
            viewport={"width": 480, "height": 800}, is_mobile=True,
            has_touch=True)
        page = context.new_page()
        page.on("console", lambda msg: note_event(
            f"console {msg.type}: {msg.text[:200]}")
            if msg.type == "error" else None)
        page.on("pageerror", lambda exc: note_event(
            f"pageerror: {str(exc)[:200]}"))

        # the tailnet path hiccups now and then (net::ERR_NETWORK_CHANGED) —
        # retry the load before declaring the leg dead
        for attempt in range(3):
            try:
                page.goto(url, wait_until="load", timeout=GOTO_TIMEOUT)
                note_event(f"load complete in "
                           f"{round(time.monotonic() - t0, 2)}s"
                           + (f" (attempt {attempt + 1})" if attempt else ""))
                break
            except Exception as exc:  # noqa: BLE001 — the report tells it all
                error = f"{type(exc).__name__}: {exc}"
                note_event(f"load attempt {attempt + 1} failed: "
                           + " ".join(str(exc).split())[:160])
                if attempt < 2:
                    page.wait_for_timeout(3000)

        if error is None:
            page.wait_for_timeout(int(BOOT_SETTLE * 1000))
            note_event(f"settled {BOOT_SETTLE}s for boot")

            # get PAST the menu: our games start on a tap-to-begin title
            # screen. Enter, then two center taps with a beat between.
            for label, action in (
                    ("enter", lambda: page.keyboard.press("Enter")),
                    ("tap1", lambda: page.touchscreen.tap(240, 400)),
                    ("tap2", lambda: page.touchscreen.tap(240, 430))):
                try:
                    action()
                    note_event(f"start attempt: {label}")
                    page.wait_for_timeout(1200)
                except Exception as exc:  # noqa: BLE001 — never kill the run
                    note_event(f"start attempt {label} failed: "
                               f"{type(exc).__name__}")

            kept: list[str] = []
            failures = 0
            deadline = time.monotonic() + seconds
            next_shot = time.monotonic() + cadence
            while time.monotonic() < deadline and error is None:
                time.sleep(max(0.0, next_shot - time.monotonic()))
                next_shot += cadence
                try:
                    try:
                        cdp = getattr(page, "_gmp_cdp", None)
                        if cdp is None:
                            cdp = page.context.new_cdp_session(page)
                            page._gmp_cdp = cdp
                        png = base64.b64decode(cdp.send(
                            "Page.captureScreenshot",
                            {"format": "png"})["data"])
                    except Exception:
                        png = page.screenshot(timeout=8000)
                    failures = 0
                except Exception as exc:
                    failures += 1
                    note_event(f"shot {shots} failed: "
                               f"{type(exc).__name__}: {str(exc)[:160]}")
                    if failures >= SHOT_FAIL_LIMIT:
                        error = (f"gave up after {failures} consecutive "
                                 "failed screenshots (page gone?)")
                    continue
                path = os.path.join(out_dir, f"{shots:03d}.png")
                with open(path, "wb") as f:
                    f.write(png)
                shots += 1
                kept.append(path)
                if len(kept) > keep_n:          # rotate: cap disk mid-run
                    os.remove(kept.pop(0))
            # renumber the surviving ring 000..NN chronological, so the
            # critic's sorted()[:3] vision sample reads oldest-first
            for i, path in enumerate(kept):
                target = os.path.join(out_dir, f"{i:03d}.png")
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
        "survival_seconds": seconds,   # judge()'s evidence vocabulary
        "actions_taken": shots,        # the feed watched the whole window
        "note": NOTE,
        "events": events,
    }
    if error is not None:
        report["error"] = error
    return report


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: run_evidence.py <job-dir>/spec.json", file=sys.stderr)
        return 2
    spec_path = os.path.abspath(sys.argv[1])
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    out_dir = os.path.dirname(spec_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    say(f"game={spec.get('game')} url={spec.get('url')} "
        f"seconds={spec.get('seconds')} -> {out_dir}")

    try:
        report = collect(spec, out_dir)
    except ImportError as exc:
        say(f"playwright missing: {exc}")
        return 3

    latest = os.path.join(out_dir, "latest.json")
    with open(latest, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    tar_path = os.path.join(out_dir, "evidence.tar.gz")
    members = [name for name in sorted(os.listdir(out_dir))
               if name == "latest.json" or
               (name.endswith(".png") and name[:-4].isdigit())]
    with tarfile.open(tar_path, "w:gz") as tar:
        for name in members:            # flat tar — exec_critique refuses
            tar.add(os.path.join(out_dir, name), arcname=name)  # anything else
    say(f"wrote {tar_path} ({len(members)} file(s), "
        f"shots={report.get('shots')}, events={len(report.get('events') or [])}"
        + (f", error={report['error']}" if report.get("error") else "") + ")")

    print(json.dumps(report, indent=2))
    return 1 if report.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
