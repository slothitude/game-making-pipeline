#!/usr/bin/env python3
"""exec_tune — the T1 lane: rewrite a feel.gd const, push, let the Actions wall gate it.

The critic's issues_to_jobs files tune_tunable orders (its most common
prescription). Server law: no local Godot — the rewrite pushes to Forgejo and
the Actions gate wall (gates.yml) is the judge. Green wall = shipped value.

run(job): payload {game, tunable, new_value} -> {ok, game, tunable, commit}
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
import base64

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GAME_ROOTS = os.environ.get("GMP_GAMES_SRC", "/home/ubuntu/games-src")
FORGEJO_HOST = os.environ.get("FORGEJO_HOST", "127.0.0.1:3001").strip() \
    .removeprefix("https://").removeprefix("http://")
FORGEJO_ORG = os.environ.get("FORGEJO_ORG", "slothitude")
FORGEJO_TOKEN = os.environ.get("FORGEJO_TOKEN", "")
FEEL_CANDIDATES = ("scripts/feel.gd", "scripts/rpg_config.gd", "scripts/rules8.gd",
                   "scripts/score.gd")


def say(msg):
    print(f"[exec_tune] {msg}", flush=True)


def _git(game_dir, args):
    proc = subprocess.run(["git", *args], cwd=game_dir, capture_output=True,
                          text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} rc={proc.returncode}: "
                           f"{(proc.stderr or proc.stdout)[:200]}")
    return proc


def run(job: dict) -> dict:
    payload = job.get("payload") or {}
    game = payload.get("game")
    tunable = payload.get("tunable")
    new_value = payload.get("new_value")
    if not (game and tunable and new_value is not None):
        raise ValueError("tune payload needs 'game', 'tunable', 'new_value'")

    game_dir = os.path.join(GAME_ROOTS, game).replace(os.sep, "/")
    if not os.path.isdir(game_dir):
        raise RuntimeError(f"game dir not found: {game_dir}")

    # find the const wherever it lives; fuzzy first (BEACONS -> BEACON_SCORE),
    # exact second (the critic names consts loosely)
    wanted = str(tunable).strip().upper()
    for rel in FEEL_CANDIDATES:
        path = os.path.join(game_dir, rel)
        if not os.path.isfile(path):
            continue
        text = open(path, encoding="utf-8").read()
        all_consts = re.findall(r"^const\s+([A-Za-z_][A-Za-z_0-9]*)", text,
                                re.MULTILINE)
        # exact match wins; else prefix containment (SCORE -> SONAR_SCORE /
        # BEACON_SCORE); else fuzzy token overlap
        target = None
        if wanted in all_consts:
            target = wanted
        else:
            cands = [c for c in all_consts if c.upper().startswith(wanted)]
            if not cands:
                cands = [c for c in all_consts if wanted in c.upper()]
            if len(cands) == 1:
                target = cands[0]
            elif cands:
                # multiple: prefer the shortest (SCORE -> SCORE_BASE not
                # SCORE_DISPLAY_MARGIN)
                target = sorted(cands, key=len)[0]
        if target:
            pattern = re.compile(
                r"^(const\s+" + re.escape(target) +
                r"\s*:=?\s*)([^#\n]+)", re.MULTILINE)
            if pattern.search(text):
                say(f"fuzzy: {tunable!r} -> {target!r} in {rel}")
                new_text = pattern.sub(
                    lambda m: m.group(1) + str(new_value), text, count=1)
                open(path, "w", encoding="utf-8").write(new_text)
                _ship(game_dir, game, target, new_value)
                return {"ok": True, "game": game, "tunable": target,
                        "requested": str(tunable),
                        "new_value": new_value}
    raise RuntimeError(f"const {tunable!r} (nor any fuzzy match) found in "
                       f"{game}'s constants files (checked {FEEL_CANDIDATES})")


def _ship(game_dir, game, tunable, new_value):
    _git(game_dir, ["add", "-A"])
    # retry made the same change twice -> nothing to commit; push what's there
    proc = subprocess.run(["git", "-c", "user.name=gmp-builder",
                           "-c", "user.email=gmp-builder@pipeline.local",
                           "commit", "-m",
                           f"tune: {tunable} = {new_value} ({game}) via exec_tune"],
                          cwd=game_dir, capture_output=True, text=True, timeout=120)
    scheme = "http" if FORGEJO_HOST.startswith(("127.", "localhost")) else "https"
    url = f"{scheme}://{FORGEJO_ORG}:{FORGEJO_TOKEN}@{FORGEJO_HOST}/{FORGEJO_ORG}/{game}.git"
    _git(game_dir, ["push", url, "HEAD:main"])
    say(f"pushed — the Actions wall judges the value now")
