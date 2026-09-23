#!/usr/bin/env python3
"""exec_deploy — ship a green web build.

  run(job)   job payload: {game, target: retromonkey|itch}

  target retromonkey:
    looks for a web build at /home/ubuntu/games-src/<game>/build/web or
    /home/ubuntu/games-src/<game>/export/web; if found (dir with an
    index.html) rsyncs it to /home/ubuntu/site/games/<game>/ and returns the
    live URL. If no build exists it raises listing the exact paths checked —
    an honest failure, never a half-ship.
  target itch:
    NotImplementedError — butler is not installed on the server yet.

Selftest (no rsync, no server paths touched):
    python exec_deploy.py --dry-run --game <slug>     # plan against real paths
    python exec_deploy.py --selftest                  # temp build dir, end to end
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SITE_URL_BASE = "https://retromonkey.com.au/games/"
RSYNC_TIMEOUT = 600


def say(msg):
    print(f"[exec_deploy] {msg}", flush=True)


def games_src():
    """Server build root, read at call time so the selftest can repoint it."""
    return os.environ.get("GMP_GAMES_SRC", "/home/ubuntu/games-src")


def site_games():
    return os.environ.get("GMP_SITE_GAMES", "/home/ubuntu/site/games")


def candidate_paths(game):
    """Build dirs checked, in order — also what the failure message lists."""
    return [os.path.join(games_src(), game, "build", "web"),
            os.path.join(games_src(), game, "export", "web")]


def web_build_files(src):
    """Files the rsync would ship (relative), for narration and the selftest."""
    shipped = []
    for dirpath, _dirnames, filenames in os.walk(src):
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            shipped.append(os.path.relpath(full, src).replace(os.sep, "/"))
    return sorted(shipped)


def find_build(game):
    """-> build dir containing a web build, else raise with the exact paths."""
    checked = candidate_paths(game)
    for path in checked:
        if os.path.isdir(path):
            if os.path.isfile(os.path.join(path, "index.html")):
                return path
            say(f"{path} exists but has no index.html — not a web build, skipping")
    raise RuntimeError(
        f"no web build to ship for {game!r} — checked: "
        + ", ".join(checked)
        + " (export it first: the builder lane's --export-release Web step)")


def rsync_command(src, dest, dry_run):
    cmd = ["rsync", "-a"]
    if dry_run:
        cmd.append("-n")
    cmd += [src.rstrip("/") + "/", dest.rstrip("/") + "/"]
    return cmd


def run(job: dict) -> dict:
    payload = job.get("payload") or {}
    game = payload.get("game")
    target = payload.get("target") or "retromonkey"
    if not game:
        raise ValueError("deploy job payload needs 'game'")

    say(f"id={job.get('id')} deploy game={game} target={target}")
    if target == "itch":
        raise NotImplementedError(
            "itch deploy needs butler on the server — TODO: install butler + "
            "BUTLER_API_KEY on the queue host, then "
            "`butler push <build_dir> <user>/<game>:html`")
    if target != "retromonkey":
        raise ValueError(f"unknown deploy target {target!r} (retromonkey|itch)")

    dry_run = bool(payload.get("dry_run")) or os.environ.get("GMP_DEPLOY_DRY_RUN") == "1"
    build = find_build(game)
    dest = os.path.join(site_games(), game)
    files = web_build_files(build)
    cmd = rsync_command(build, dest, dry_run)
    say(f"build: {build} ({len(files)} file(s)) -> {dest}"
        + (" [DRY RUN]" if dry_run else ""))
    for name in files:
        say(f"  ship {name}")

    if dry_run:
        say(f"dry run: would run {' '.join(cmd)}")
        return {"ok": True, "dry_run": True, "url": SITE_URL_BASE + game + "/",
                "build_dir": build, "dest": dest, "files": files,
                "command": cmd}

    proc = subprocess.run(cmd, timeout=RSYNC_TIMEOUT, capture_output=True, text=True)
    say(f"rsync exit={proc.returncode} :: {(proc.stdout or '').strip().splitlines()[-1:] or ['(no output)']}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"rsync failed (exit {proc.returncode}): {(proc.stderr or '')[-400:].strip()}")
    return {"ok": True, "dry_run": False, "url": SITE_URL_BASE + game + "/",
            "build_dir": build, "dest": dest, "files": files}


def _selftest():
    """Temp build dir -> find_build -> dry-run plan -> the honest miss case."""
    src_root = tempfile.mkdtemp(prefix="gmp_deploy_test_")
    site_root = tempfile.mkdtemp(prefix="gmp_deploy_site_")
    build = os.path.join(src_root, "moon-ladder", "build", "web")
    os.makedirs(build)
    with open(os.path.join(build, "index.html"), "w", encoding="utf-8") as fh:
        fh.write("<html><body>moon-ladder</body></html>\n")
    with open(os.path.join(build, "index.js"), "w", encoding="utf-8") as fh:
        fh.write("// dummy wasm loader\n")
    with open(os.path.join(build, "index.wasm"), "wb") as fh:
        fh.write(b"\x00asm\x01\x00\x00\x00")
    # an empty export/web must NOT be picked over the real build/web
    os.makedirs(os.path.join(src_root, "moon-ladder", "export", "web"))

    os.environ["GMP_GAMES_SRC"] = src_root
    os.environ["GMP_SITE_GAMES"] = site_root
    say(f"selftest roots: games-src={src_root} site={site_root}")

    result = run({"id": "selftest", "payload": {"game": "moon-ladder",
                                                "target": "retromonkey",
                                                "dry_run": True}})
    print(json.dumps(result, indent=2))

    assert result["dry_run"] and result["ok"]
    assert result["build_dir"] == build, "must pick build/web with index.html"
    assert result["files"] == ["index.html", "index.js", "index.wasm"], result["files"]
    assert result["url"] == SITE_URL_BASE + "moon-ladder/"
    say("selftest: dry-run plan green (right build dir, 3 files listed, live URL)")

    try:
        run({"id": "selftest-miss", "payload": {"game": "no-such-game",
                                                "target": "retromonkey"}})
    except RuntimeError as exc:
        say(f"selftest: honest miss -> RuntimeError: {exc}")
        message = str(exc).replace(os.sep, "/")
        assert ("no web build to ship" in message and "no-such-game" in message
                and "build/web" in message and "export/web" in message)
    else:
        raise AssertionError("missing build must raise, not pass silently")
    say("selftest: PASS")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--dry-run" in sys.argv:
        os.environ.setdefault("GMP_DEPLOY_DRY_RUN", "1")
        game = sys.argv[sys.argv.index("--dry-run") + 1] if \
            sys.argv.index("--dry-run") + 1 < len(sys.argv) else None
        if not game:
            print("usage: python exec_deploy.py --dry-run <game>")
            sys.exit(2)
        os.environ["GMP_DEPLOY_DRY_RUN"] = "1"
        print(json.dumps(run({"id": "cli-dry-run",
                              "payload": {"game": game, "target": "retromonkey"}}),
                         indent=2))
        sys.exit(0)
    print("usage: python exec_deploy.py --selftest | --dry-run <game>")
    sys.exit(2)
