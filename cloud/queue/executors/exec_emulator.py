#!/usr/bin/env python3
"""exec_emulator — the play lane: PlayerOne on the Android emulator.

  run(job)   job payload: {game, seconds}

Runs `/home/ubuntu/playerone/venv/bin/python /home/ubuntu/playerone/emu_play.py
--game <game> --seconds <n>` (cwd /home/ubuntu/playerone). Real device pixels,
real adb touches, evidence + screenshots written under
/home/ubuntu/playerone/evidence/<game>/<stamp>/ — which is exactly what
exec_critique's vision tier consumes. emu_play prints a JSON run report; that
parsed report is the executor's result.

ATD-IS-PARKED REALITY: the gmp-atd AVD is not kept booted on retromonkey
(956MB box, the emulator is a luxury). This executor works when the emulator
is booted and `adb devices` shows it; when it is parked, emu_play fails fast
(no device / no CDP tab) and this module raises with the stderr tail — the
router's retry law gives it one requeue, then the job fails honestly. Don't
file emulator jobs (or full critiques that need screenshots) while the
emulator is parked; boot it first.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

VENV_PY = "/home/ubuntu/playerone/venv/bin/python"
EMU_PLAY = "/home/ubuntu/playerone/emu_play.py"
CWD = "/home/ubuntu/playerone"
DEFAULT_SECONDS = 90


def say(msg):
    print(f"[exec_emulator] {msg}", flush=True)


def run(job: dict) -> dict:
    payload = job.get("payload") or {}
    game = payload.get("game")
    if not game:
        raise ValueError("emulator job payload needs 'game'")
    seconds = float(payload.get("seconds") or DEFAULT_SECONDS)

    cmd = [VENV_PY, EMU_PLAY, "--game", game, "--seconds", str(seconds)]
    say(f"id={job.get('id')} emulator game={game} seconds={seconds}")
    say(f"-> {' '.join(cmd)} (cwd {CWD}) — needs the gmp-atd emulator booted")
    try:
        proc = subprocess.run(cmd, cwd=CWD, timeout=seconds * 4 + 300,
                              capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"emu_play timed out after {seconds * 4 + 300:.0f}s — is the "
            "emulator wedged? (boot + playwright overhead included in budget)")

    err_tail = (proc.stderr or "").strip().splitlines()[-8:]
    if proc.returncode != 0:
        raise RuntimeError(
            f"emu_play exit {proc.returncode} for {game}: " + " | ".join(err_tail)
            + " — check: emulator booted? adb devices? game in emu_play.GAMES?")

    report = None
    try:
        report = json.loads((proc.stdout or "").strip())
    except ValueError:
        pass
    for line in (proc.stdout or "").strip().splitlines()[-8:]:
        say(f"| {line}")
    if report:
        say(f"turns={report.get('turns')} evidence={report.get('evidence')}")
    else:
        say("no parseable JSON report in stdout — returning the tail only")
    return {"ok": True, "game": game, "seconds": seconds,
            "report": report, "stderr_tail": err_tail}


if __name__ == "__main__":
    print("usage: import me — REGISTRY['emulator'] is this module's run()")
    sys.exit(2)
