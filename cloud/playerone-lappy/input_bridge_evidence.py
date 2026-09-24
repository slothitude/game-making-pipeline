#!/usr/bin/env python3
"""input_bridge_evidence — LIVE proof that the bridge's transports reach the
real games on retromonkey.

The offline selftest proves the grammar compiles to the right op stream. This
lane proves the ops land: a has_touch Playwright context opens the live game
pages, window listeners are installed BEFORE any gesture, then the bridge acts
and we read back what the page actually received.

    (a) star-visitor — DeviceOrientation override -> a deviceorientation event
    (b) star-visitor — a CDP touch swipe -> touchstart/touchmove/touchend
    (c) star-visitor — gamepad shim + pad press -> gamepadconnected + pressed
    (d) sonar        — a keyboard arrow -> keydown

Output: <root>/evidence/input_bridge/<slug>-<probe>.png + probe-<n>.json
(one per probe, with the captured events) + summary.json. Exit 1 if any probe
captured nothing — evidence must be honest in both directions.

    ~/playerone/venv/bin/python input_bridge_evidence.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path("/home/aaron/playerone")
sys.path.insert(0, str(ROOT))          # input_bridge lives beside worker.py
from input_bridge import (  # noqa: E402
    PAD_SHIM_JS, ActionSpace, Bridge, TOUCH_CONTEXT_KWARGS)

STAR_VISITOR = "https://retromonkey.com.au/games/star-visitor/"
SONAR = "https://retromonkey.com.au/games/sonar/"
GOTO_TIMEOUT = 60_000
SETTLE_MS = 4_000      # let the wasm boot paint before acting
EVENT_WAIT_MS = 1_200  # a dispatched event needs a beat to reach the page

# Installed before any page script runs: every transport's DOM shadow, in one
# place. The bridge acts; this is the page's own receipt.
# FORM LAW: self-invoking — playwright's add_init_script only *evaluates* the
# string it is given, so a bare `() => {...}` function expression is never
# called (this exact bug produced a 0-event first run).
LISTENERS_JS = r"""
(() => {
  window.__P1_EVENTS__ = [];
  const push = (row) => window.__P1_EVENTS__.push(row);
  const touch = (kind) => (e) => {
    const t = (e.changedTouches && e.changedTouches[0]) || {};
    push({kind: kind, x: t.clientX, y: t.clientY,
          touches: e.touches ? e.touches.length : 0});
  };
  window.addEventListener('touchstart', touch('touchstart'), {passive: true});
  window.addEventListener('touchmove', touch('touchmove'), {passive: true});
  window.addEventListener('touchend', touch('touchend'), {passive: true});
  window.addEventListener('deviceorientation', (e) => push({
      kind: 'deviceorientation', alpha: e.alpha, beta: e.beta, gamma: e.gamma,
      absolute: e.absolute}));
  window.addEventListener('gamepadconnected', (e) => push({
      kind: 'gamepadconnected', id: e.gamepad.id, index: e.gamepad.index,
      buttons: e.gamepad.buttons.length, axes: e.gamepad.axes.length}));
  window.addEventListener('keydown', (e) => push(
      {kind: 'keydown', key: e.key, code: e.code}));
  return true;
})();
"""


def say(msg: str) -> None:
    print(f"[input_bridge_evidence] {msg}", flush=True)


def drain(page) -> list:
    rows = page.evaluate("() => window.__P1_EVENTS__ || []")
    page.evaluate("() => { window.__P1_EVENTS__ = []; }")
    return rows


def drain_peek(page) -> list:
    """Non-destructive read (polling helpers use this)."""
    return page.evaluate("() => window.__P1_EVENTS__ || []")


def goto_retry(page, url: str, attempts: int = 4) -> None:
    """Lappy is dual-homed (eth + wifi both UP), so a navigation can die with
    net::ERR_NETWORK_CHANGED on an interface flap. Retry — evidence should not
    need three runs."""
    for i in range(attempts):
        try:
            page.goto(url, wait_until="load", timeout=GOTO_TIMEOUT)
            return
        except Exception as exc:
            if i == attempts - 1:
                raise
            say(f"  goto attempt {i + 1} failed ({str(exc).splitlines()[0][:80]})"
                " — retrying")
            page.wait_for_timeout(5000)


def arm(page, name: str) -> None:
    """Install the listeners exactly once per page, post-load, over
    page.evaluate — the proven-delivery configuration. (An init script would
    also install them, but a document-start listener changes when Chromium's
    orientation controller starts pumping, and the tilt probe then goes
    silent; the sensor lane wants the simplest page possible.)"""
    page.evaluate(LISTENERS_JS)
    assert page.evaluate("() => Array.isArray(window.__P1_EVENTS__)")


def wait_boot(page, budget_s: float = 25.0) -> bool:
    """Godot's loader overlay (#status) hides once the wasm is live. Best
    effort — the DOM events land either way, booted or not."""
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        try:
            if page.evaluate(
                    "() => { const s = document.getElementById('status');"
                    " return !s || getComputedStyle(s).visibility === 'hidden'; }"):
                return True
        except Exception:
            pass
        page.wait_for_timeout(1000)
    return False


def shoot(cdp, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    png = cdp.send("Page.captureScreenshot", {"format": "png"})["data"]
    import base64
    path.write_bytes(base64.b64decode(png))
    say(f"  screenshot -> {path.name}")


def probe(page, cdp, name: str, act, out_dir: Path) -> dict:
    """Act, give the event a beat, then read the page's receipt."""
    before = len(drain(page))  # clear
    del before
    steps = act()
    page.wait_for_timeout(EVENT_WAIT_MS)
    events = drain(page)
    shoot(cdp, out_dir / f"{name}.png")
    (out_dir / f"{name}.json").write_text(
        json.dumps({"probe": name, "steps": steps, "events": events},
                   indent=2), encoding="utf-8")
    say(f"  {name}: {len(steps)} step(s) -> page received "
        f"{len(events)} event(s) {[e.get('kind') for e in events][:8]}")
    return {"probe": name, "steps": steps, "events": events,
            "captured": len(events) > 0}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live evidence that the input bridge reaches the games")
    parser.add_argument("--out-root",
                        default=str(ROOT / "evidence" / "input_bridge"))
    args = parser.parse_args()
    out_dir = Path(args.out_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    space = ActionSpace()
    results: list[dict] = []
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    try:
        context = browser.new_context(**TOUCH_CONTEXT_KWARGS)

        # ------------- star-visitor: tilt, touch, gamepad -------------------
        # LANE ISOLATION LAW: the tilt probe runs on its OWN page + CDP
        # session, with no gamepad shim and no prior touch traffic. Chromium's
        # orientation controller stops delivering once the page's session has
        # carried Input.dispatchTouch* traffic or the shim has replaced
        # navigator.getGamepads (found live, twice) — a real phone would have
        # its own sensor lane anyway, so the isolation mirrors the hardware.
        page = context.new_page()
        goto_retry(page, STAR_VISITOR)
        page.wait_for_timeout(SETTLE_MS)
        cdp = context.new_cdp_session(page)
        say(f"loaded {STAR_VISITOR} (loader cleared: {wait_boot(page)})")
        arm(page, "star-visitor")

        def act_tilt():
            # headless has no sensor backend: the delivered event carries a
            # null payload, and only on a controller state transition. Focus
            # + clear + set is the transition, and we poll for the event.
            page.bring_to_front()
            try:
                cdp.send("DeviceOrientation.clearDeviceOrientationOverride", {})
            except Exception:
                pass
            steps = Bridge(page).perform(space.tilt(0.0, 42.0, -28.0))
            for _ in range(8):
                if any(e["kind"] == "deviceorientation" for e in drain_peek(page)):
                    break
                page.wait_for_timeout(700)
            return [{"step": s["op"], **s.get("params", {}),
                     "note": "headless has no sensor backend: a delivered "
                             "event may carry a null payload"} for s in steps]

        results.append(probe(page, cdp, "star-visitor-tilt", act_tilt, out_dir))
        page.close()

        # ------------- star-visitor: touch + gamepad on a fresh page --------
        page = context.new_page()
        goto_retry(page, STAR_VISITOR)
        page.wait_for_timeout(SETTLE_MS)
        cdp = context.new_cdp_session(page)
        say(f"reloaded {STAR_VISITOR} (loader cleared: {wait_boot(page)})")
        arm(page, "star-visitor-touch")
        bridge = Bridge(page).install()

        def act_swipe():
            steps = bridge.perform(space.swipe(620, 270, 260, 270, 300, 8))
            return [{"step": s["op"], **s.get("params", {})} for s in steps]

        results.append(probe(page, cdp, "star-visitor-touch-swipe",
                             act_swipe, out_dir))

        def act_pad():
            shim = page.evaluate("() => !!navigator.getGamepads && "
                                 "!!window.__P1_GAMEPAD__")
            say(f"  shim state: navigator.getGamepads overridden={shim}")
            steps = bridge.perform(space.pad(2, True))
            readback = page.evaluate(
                "() => { const pads = navigator.getGamepads();"
                " return {count: pads.length, id: pads[0] && pads[0].id,"
                " pressed: pads[0] && pads[0].buttons[2].pressed}; }")
            say(f"  gamepad readback: {readback}")
            rows = [{"step": "shim", "installed": shim}]
            rows += [{"step": s["op"], **s.get("params", {})} for s in steps]
            return rows
        row = probe(page, cdp, "star-visitor-gamepad", act_pad, out_dir)
        row["readback"] = page.evaluate(
            "() => { const p = navigator.getGamepads()[0];"
            " return p ? {id: p.id, connected: p.connected,"
            " buttons: p.buttons.length, axes: p.axes.length} : null; }")
        results.append(row)
        bridge.release()
        page.close()

        # ---------------- sonar: the keyboard lane -------------------------
        page = context.new_page()
        goto_retry(page, SONAR)
        page.wait_for_timeout(SETTLE_MS)
        cdp = context.new_cdp_session(page)
        say(f"loaded {SONAR} (loader cleared: {wait_boot(page)})")
        arm(page, "sonar")
        bridge = Bridge(page).install()

        def act_key():
            steps = bridge.perform(space.key("ArrowRight", 250))
            return [{"step": s["op"], **{k: v for k, v in s.items()
                                         if k in ("key", "action", "ms")}}
                    for s in steps]

        results.append(probe(page, cdp, "sonar-keyboard", act_key, out_dir))
        bridge.release()
        page.close()
    finally:
        browser.close()
        pw.stop()

    summary = {"when": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "out_dir": str(out_dir), "probes": results,
               "all_captured": all(r["captured"] for r in results)}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2),
                                          encoding="utf-8")
    for row in results:
        say(f"{'PASS' if row['captured'] else 'FAIL'} {row['probe']}: "
            f"{len(row['events'])} page event(s)")
    say(f"summary -> {out_dir / 'summary.json'}")
    return 0 if summary["all_captured"] else 1


if __name__ == "__main__":
    sys.exit(main())
