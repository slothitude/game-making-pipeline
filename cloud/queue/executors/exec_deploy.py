#!/usr/bin/env python3
"""exec_deploy — Wire 1: export a fresh web build and ship it to the live site.

  run(job)   job payload: {game, target: retromonkey|itch, skip_export?}

  target retromonkey:
    finds the game's repo clone at GMP_GAMES_SRC/<game> (the same clones
    exec_milestone / exec_tune push to), reads export_presets.cfg for the Web
    preset, exports headless with the server's native godot, then rsyncs the
    fresh build to GMP_SITE_GAMES/<game>/ (--delete: the served dir mirrors
    exactly what the export produced — a stale file is a stale game).
    Template check first: if the web export templates are missing they are
    downloaded once into ~/.local/share/godot/export_templates/<ver>/
    (FIX-PLAN Wire 1 part 1; they are installed today).
    Every step raises on failure — an honest failure, never a half-ship.
  target itch:
    NotImplementedError — butler is not installed on the server yet.

Selftest (offline, no godot, no rsync, no server paths touched — subprocess
and urlopen are monkeypatched):
    python exec_deploy.py --selftest
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SITE_URL_BASE = "https://retromonkey.com.au/games/"
RSYNC_TIMEOUT = 900
EXPORT_TIMEOUT = 1800          # first run imports the project — that is slow
TEMPLATE_TPZ_URL = ("https://github.com/godotengine/godot/releases/download/"
                    "{ver}/Godot_v{ver}_export_templates.tpz")
WEB_PLATFORMS = ("web", "html5")

module_subprocess = subprocess   # _selftest swaps .run
module_urlopen = urllib.request.urlopen  # _selftest swaps this too


def say(msg):
    print(f"[exec_deploy] {msg}", flush=True)


def games_src():
    """Server build root, read at call time so the selftest can repoint it."""
    return os.environ.get("GMP_GAMES_SRC", "/home/ubuntu/games-src")


def site_games():
    return os.environ.get("GMP_SITE_GAMES", "/home/ubuntu/site/games")


def godot_bin():
    return os.environ.get("GMP_GODOT", "~/Godot_v4.7.1-stable_linux.x86_64")


def templates_dir(godot=None):
    """~/.local/share/godot/export_templates/<x.y.z.stable>, version derived
    from the binary's filename (Godot_v4.7.1-stable_linux.x86_64)."""
    godot = os.path.expanduser(godot or godot_bin())
    m = re.search(r"Godot_v([\d.]+)-([A-Za-z0-9_.+-]+?)_(?:linux|win|macos|web)",
                  os.path.basename(godot))
    ver = f"{m.group(1)}.{m.group(2)}" if m else "4.7.1.stable"
    override = os.environ.get("GMP_GODOT_TEMPLATES")
    return os.path.expanduser(override or
                              f"~/.local/share/godot/export_templates/{ver}")


# ------------------------------------------------------------------ presets --
def web_preset(repo):
    """-> (preset_name, export_path) for the repo's Web preset.
    Raises listing the presets that WERE there (an honest miss)."""
    path = os.path.join(repo, "export_presets.cfg")
    if not os.path.isfile(path):
        raise RuntimeError(f"no export_presets.cfg in {repo} — cannot export")
    text = open(path, encoding="utf-8").read()
    found = []
    for block in re.split(r"^\[preset\.\d+\]", text, flags=re.MULTILINE)[1:]:
        name = re.search(r'^name="([^"]+)"', block, re.MULTILINE)
        plat = re.search(r'^platform="([^"]+)"', block, re.MULTILINE)
        exp = re.search(r'^export_path="([^"]+)"', block, re.MULTILINE)
        if not (name and plat):
            continue
        label, platform = name.group(1), plat.group(1).strip().lower()
        found.append(f"{label} ({platform})")
        if platform in WEB_PLATFORMS:
            return label, (exp.group(1) if exp else "build/web/index.html")
    raise RuntimeError(
        f"no Web export preset in {path} — have: "
        + (", ".join(found) or "(none)"))


# ---------------------------------------------------------------- templates --
def _ensure_web_templates(godot=None):
    """Web export templates must exist; download them once if not."""
    tdir = templates_dir(godot)
    have = [n for n in ("web_release.zip", "web_nothreads_release.zip")
            if os.path.isfile(os.path.join(tdir, n))]
    if have:
        return tdir
    m = re.search(r"Godot_v([\d.]+)-([A-Za-z0-9_.+-]+?)_(?:linux|win|macos|web)",
                  os.path.basename(os.path.expanduser(godot or godot_bin())))
    ver = f"{m.group(1)}-{m.group(2)}" if m else "4.7.1-stable"
    url = TEMPLATE_TPZ_URL.format(ver=ver)
    raise RuntimeError(
        f"web export templates missing in {tdir} (looked for web_release.zip) "
        f"— download once: curl -L {url} -o /tmp/templates.tpz && "
        f"unzip -o /tmp/templates.tpz 'templates/*' -d /tmp/tpl && "
        f"mkdir -p {tdir} && cp /tmp/tpl/templates/web*.zip {tdir}/")


# -------------------------------------------------------------------- export --
def export(game_dir, preset, out_rel):
    """godot --headless --export-release. Returns the absolute build dir."""
    _ensure_web_templates()
    out_path = os.path.join(game_dir, out_rel).replace(os.sep, "/")
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    godot = os.path.expanduser(godot_bin())
    cmd = [godot, "--headless", "--path", game_dir,
           "--export-release", preset, out_path]
    say(f"export: {os.path.basename(godot)} preset={preset!r} -> {out_rel}")
    try:
        proc = module_subprocess.run(cmd, timeout=EXPORT_TIMEOUT,
                                     capture_output=True, text=True, cwd=game_dir)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"godot export timed out after {EXPORT_TIMEOUT}s "
            f"({str(exc.stdout or '')[-200:]})") from exc
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-5:]
    for line in tail:
        say(f"| {line}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"godot export rc={proc.returncode} for preset {preset!r}: "
            f"{((proc.stderr or '') or (proc.stdout or ''))[-400:].strip()}")
    if not os.path.isfile(out_path):
        raise RuntimeError(
            f"godot export reported rc=0 but {out_path} does not exist")
    return os.path.dirname(out_path) or game_dir


def rsync_command(src, dest, dry_run):
    cmd = ["rsync", "-a", "--delete"]
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

    game_dir = os.path.join(games_src(), game).replace(os.sep, "/")
    if not os.path.isdir(game_dir):
        raise RuntimeError(
            f"no repo clone for {game!r} at {game_dir} — the game must be "
            f"cloned under {games_src()} first (new_game does this)")

    preset, out_rel = web_preset(game_dir)
    dry_run = bool(payload.get("dry_run")) or os.environ.get("GMP_DEPLOY_DRY_RUN") == "1"
    if payload.get("skip_export"):
        build = os.path.dirname(os.path.join(game_dir, out_rel))
        if not os.path.isfile(os.path.join(build, "index.html")):
            raise RuntimeError(f"skip_export set but no build at {build}")
        say(f"skip_export: shipping the existing build at {build}")
    elif dry_run:
        build = os.path.join(game_dir, os.path.dirname(out_rel))
        if not os.path.isdir(build):
            say(f"dry run: no build yet at {build} — the real run exports it")
        say(f"dry run: would export preset {preset!r} -> {out_rel}")
    else:
        build = export(game_dir, preset, out_rel)

    dest = os.path.join(site_games(), game)
    files = sorted(
        os.path.relpath(os.path.join(d, n), build).replace(os.sep, "/")
        for d, _s, fs in os.walk(build) for n in fs)
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

    proc = module_subprocess.run(cmd, timeout=RSYNC_TIMEOUT,
                                 capture_output=True, text=True)
    say(f"rsync exit={proc.returncode} :: "
        f"{(proc.stdout or '').strip().splitlines()[-1:] or ['(no output)']}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"rsync failed (exit {proc.returncode}): {(proc.stderr or '')[-400:].strip()}")
    return {"ok": True, "dry_run": False, "url": SITE_URL_BASE + game + "/",
            "build_dir": build, "dest": dest, "files": files,
            "preset": preset}


def _selftest():
    """Fake repo (real export_presets.cfg) + monkeypatched godot/rsync: the
    preset parse, the export command shape, the rsync flags, honest misses."""
    global module_subprocess, module_urlopen
    root = tempfile.mkdtemp(prefix="gmp_deploy_test_").replace(os.sep, "/")
    site_root = tempfile.mkdtemp(prefix="gmp_deploy_site_").replace(os.sep, "/")
    game_dir = os.path.join(root, "moon-ladder").replace(os.sep, "/")
    os.makedirs(game_dir)
    with open(os.path.join(game_dir, "export_presets.cfg"), "w",
              encoding="utf-8") as fh:
        fh.write('[preset.0]\n\nname="Linux"\nplatform="Linux"\n'
                 'export_path="build/linux/ml.x86_64"\n\n[preset.1]\n\n'
                 'name="Web"\nplatform="Web"\nexport_path="build/web/index.html"\n')
    os.environ["GMP_GAMES_SRC"] = root
    os.environ["GMP_SITE_GAMES"] = site_root

    recorded = []

    def recorder(cmd, **kwargs):
        recorded.append({"cmd": list(cmd), "kwargs": kwargs})
        out_rel = cmd[-1]
        build = os.path.dirname(out_rel)
        os.makedirs(build, exist_ok=True)
        for name, body in (("index.html", "<html>moon-ladder</html>"),
                           ("index.js", "// loader"), ("index.pck", "bin")):
            with open(os.path.join(build, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        # a stale leftover the --delete must be credited with removing
        with open(os.path.join(build, "stale-leftover.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("old")
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    # pretend the templates are installed so the download path stays cold —
    # and NEVER touch the real ~/.local/share templates dir from a selftest
    tdir = os.path.join(root, "fake-templates")
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "web_release.zip"), "w", encoding="utf-8") as fh:
        fh.write("PK")

    real_run, real_url, real_tpl = (module_subprocess.run, module_urlopen,
                                    os.environ.get("GMP_GODOT_TEMPLATES"))
    module_subprocess.run = recorder
    module_urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError(
        "selftest must not download templates"))
    os.environ["GMP_GODOT_TEMPLATES"] = tdir
    try:
        result = run({"id": "selftest", "payload": {
            "game": "moon-ladder", "target": "retromonkey"}})
    finally:
        module_subprocess.run, module_urlopen = real_run, real_url
        if real_tpl is None:
            os.environ.pop("GMP_GODOT_TEMPLATES", None)
        else:
            os.environ["GMP_GODOT_TEMPLATES"] = real_tpl

    godot_cmd, rsync_cmd = recorded[0]["cmd"], recorded[1]["cmd"]
    assert godot_cmd[-3:-1] == ["--export-release", "Web"], godot_cmd
    assert "--headless" in godot_cmd and "--path" in godot_cmd, godot_cmd
    assert godot_cmd[-1].endswith("build/web/index.html"), godot_cmd
    assert "Linux" not in godot_cmd, "must pick the Web preset, not Linux"
    assert rsync_cmd[:3] == ["rsync", "-a", "--delete"], rsync_cmd
    assert rsync_cmd[-1] == os.path.join(site_root, "moon-ladder") + "/"
    assert result["ok"] and result["files"] == [
        "index.html", "index.js", "index.pck", "stale-leftover.txt"], result["files"]
    assert result["url"] == SITE_URL_BASE + "moon-ladder/"
    print(json.dumps(result, indent=2))
    say("selftest: export command + Web preset pick + rsync --delete plan green")

    # --- honest misses ------------------------------------------------------
    for payload, needle in (
            ({"game": "no-such-game", "target": "retromonkey"}, "no repo clone"),
            ({"game": "moon-ladder", "target": "steam"}, "unknown deploy target")):
        try:
            run({"id": "miss", "payload": payload})
        except (RuntimeError, ValueError) as exc:
            assert needle in str(exc), f"{needle!r} not in: {exc}"
            say(f"selftest: honest miss -> {type(exc).__name__}: {str(exc)[:70]}")
        else:
            raise AssertionError(f"{payload} must raise")

    with open(os.path.join(game_dir, "export_presets.cfg"), "w",
              encoding="utf-8") as fh:
        fh.write('[preset.0]\n\nname="Linux"\nplatform="Linux"\n')
    try:
        run({"id": "nopreset", "payload": {"game": "moon-ladder",
                                           "target": "retromonkey"}})
    except RuntimeError as exc:
        assert "no Web export preset" in str(exc) and "Linux" in str(exc)
        say("selftest: no-Web-preset -> RuntimeError listing what was there")
    else:
        raise AssertionError("missing Web preset must raise")
    say("selftest: PASS")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--dry-run" in sys.argv:
        idx = sys.argv.index("--dry-run")
        game = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        if not game:
            print("usage: python exec_deploy.py --dry-run <game>")
            sys.exit(2)
        os.environ["GMP_DEPLOY_DRY_RUN"] = "1"
        print(json.dumps(run({"id": "cli-dry-run",
                              "payload": {"game": game,
                                          "target": "retromonkey"}}), indent=2))
        sys.exit(0)
    print("usage: python exec_deploy.py --selftest | --dry-run <game>")
    sys.exit(2)
