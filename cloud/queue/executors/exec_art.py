#!/usr/bin/env python3
"""exec_art — the T2 lane: hand an art work-order to the deployed
daily/art_work_order.py (Flux render -> PIL key -> acceptance bounds -> gate
wall -> export -> ship).

  run(job)   job payload: {game, prompt, asset_id?}

Runs `python3 /home/ubuntu/pipeline/daily/art_work_order.py --game <game>
--prompt <prompt> [--asset-id <asset_id>]` with cwd /home/ubuntu/pipeline/daily
and GAME_ROOTS=/home/ubuntu/games-src. Non-zero exit raises (the script uses
2/3/4 for acceptance-fail / gate-red / deploy-fail — the router's retry law
decides whether the order gets a second swing). Success returns the exit code
plus the output tails.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT = "/home/ubuntu/pipeline/daily/art_work_order.py"
CWD = "/home/ubuntu/pipeline/daily"
GAME_ROOTS = "/home/ubuntu/games-src"
TIMEOUT = 2400  # up to 3 Flux attempts + gate wall + export + ship


def say(msg):
    print(f"[exec_art] {msg}", flush=True)


def run(job: dict) -> dict:
    payload = job.get("payload") or {}
    game = payload.get("game")
    prompt = payload.get("prompt")
    asset_id = payload.get("asset_id")
    if not (game and prompt):
        raise ValueError("art job payload needs 'game' and 'prompt'")

    cmd = ["python3", SCRIPT, "--game", game, "--prompt", prompt]
    if asset_id:
        cmd += ["--asset-id", asset_id]
    env = dict(os.environ)
    if not env.get("GAME_ROOTS"):
        # art_work_order.py reads GAME_ROOTS as a JSON dict {game: dir}
        env["GAME_ROOTS"] = json.dumps(
            {d.name: str(d) for d in pathlib.Path(GAME_ROOTS).iterdir() if d.is_dir()}
        )

    say(f"id={job.get('id')} art game={game} asset={asset_id or '(auto)'} "
        f"prompt={prompt[:80]!r}")
    say(f"-> {' '.join(cmd)} (cwd {CWD}, GAME_ROOTS={env['GAME_ROOTS']})")
    try:
        proc = subprocess.run(cmd, cwd=CWD, env=env, timeout=TIMEOUT,
                              capture_output=True, text=True)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"art_work_order timed out after {TIMEOUT}s "
            f"(partial output: {(exc.stderr or b'')[-300:] if isinstance(exc.stderr, bytes) else (exc.stderr or '')[-300:]})") from exc

    out_tail = (proc.stdout or "").strip().splitlines()[-8:]
    err_tail = (proc.stderr or "").strip().splitlines()[-8:]
    for line in out_tail:
        say(f"| {line}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"art_work_order exit {proc.returncode} for {game}/{asset_id or '?'}: "
            + " | ".join(err_tail or out_tail))
    return {"ok": True, "exit": proc.returncode, "game": game,
            "asset_id": asset_id, "stdout_tail": out_tail, "stderr_tail": err_tail}


if __name__ == "__main__":
    print("usage: import me — REGISTRY['generate_art'] is this module's run()")
    sys.exit(2)
