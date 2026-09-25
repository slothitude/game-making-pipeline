#!/usr/bin/env python3
"""exec_deploy — Wire 1: export a fresh web build and ship it to the live site.

  run(job)   job payload: {game, target: retromonkey|itch, skip_export?}

  target retromonkey:
    finds the game's repo clone at GMP_GAMES_SRC/<game> (the same clones
    exec_milestone / exec_tune push to), reads export_presets.cfg for the Web
    preset, exports headless with the server's native godot, then rsyncs the
    fresh build to GMP_SITE_GAMES/<game>/ (--delete: the served dir mirrors
    exactly what the export produced — a stale file is a stale game).
    Preset gap: pi never writes export_presets.cfg, so a game can reach this
    lane green and still have nothing to export. When the cfg is missing or
    carries no Web block the standard Web preset is SYNTHESIZED into the repo
    (sonar's proven shape), the export dir is created (godot does not mkdir
    it), and the file is committed via the lane's Forgejo push law so future
    deploys and fresh clones find it.
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
import time as _time
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SITE_URL_BASE = "https://retromonkey.com.au/games/"
RSYNC_TIMEOUT = 900
GIT_TIMEOUT = 120              # preset commit/push only — never a clone
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


# The standard Web preset, copied from sonar's proven export_presets.cfg (the
# reference build of this lane). export_path is the relative form godot
# resolves against the project dir; variant/thread_support=false matches the
# web_nothreads template the server ships.
WEB_PRESET_TEMPLATE = """[preset.0]

name="Web"
platform="Web"
runnable=true
advanced_options=false
dedicated_server=false
custom_features=""
export_filter="all_resources"
include_filter=""
exclude_filter=""
export_path="build/web/index.html"
patches=PackedStringArray()
encryption_include_filters=""
encryption_exclude_filters=""
seed=0
encrypt_pck=false
encrypt_directory=false
script_export_mode=2

[preset.0.options]

custom_template/debug=""
custom_template/release=""
variant/extensions_support=false
variant/thread_support=false
vram_texture_compression/for_desktop=true
vram_texture_compression/for_mobile=false
html/export_icon=true
html/custom_html_shell=""
html/head_include=""
html/canvas_resize_policy=2
html/focus_canvas_on_start=true
html/experimental_virtual_keyboard=false
progressive_web_app/enabled=false
progressive_web_app/ensure_cross_origin_isolation_headers=true
progressive_web_app/offline_page=""
progressive_web_app/display=1
progressive_web_app/orientation=0
progressive_web_app/icon_144x144=""
progressive_web_app/icon_180x180=""
progressive_web_app/icon_512x512=""
progressive_web_app/background_color=Color(0, 0, 0, 1)
"""


def _next_preset_index(text):
    """First free [preset.N] index in an existing export_presets.cfg."""
    return 1 + max((int(m.group(1)) for m in
                    re.finditer(r"^\[preset\.(\d+)\]", text, re.MULTILINE)),
                   default=-1)


def ensure_web_preset(game_dir, game=None):
    """-> (preset_name, export_path) — the repo's Web preset, synthesized if
    the repo does not have one.

    pi writes milestones but never export_presets.cfg, so a game can arrive
    here green with nothing to export (powder-run, jezzball: both died on
    "no export_presets.cfg"). Missing cfg -> write the standard template; cfg
    without a Web block -> append the Web preset at the next free index and
    leave every existing preset untouched. The export folder is created here
    (godot does not mkdir it — the Wire 1 lane's own law) and the synthesized
    file is committed via the lane's Forgejo push law."""
    game = game or os.path.basename(game_dir.rstrip("/"))
    path = os.path.join(game_dir, "export_presets.cfg")
    if os.path.isfile(path):
        try:
            return web_preset(game_dir)
        except RuntimeError as miss:
            old = open(path, encoding="utf-8").read()
            idx = _next_preset_index(old)
            # the .options header first: "[preset.0.options]" does not contain
            # the substring "[preset.0]", so one blind replace would leave the
            # synthesized options under a DUPLICATE [preset.0.options] section
            block = (WEB_PRESET_TEMPLATE
                     .replace("[preset.0.options]", f"[preset.{idx}.options]")
                     .replace("[preset.0]", f"[preset.{idx}]"))
            text = old.rstrip("\n") + "\n\n" + block
            say(f"no Web preset in {path} ({str(miss)[:70]}) — appending "
                f"synthesized [preset.{idx}] Web block, existing presets kept")
    else:
        text = WEB_PRESET_TEMPLATE
        say(f"no export_presets.cfg in {game_dir} — synthesizing the standard "
            f"Web preset (sonar shape)")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.makedirs(os.path.join(game_dir, "build", "web"), exist_ok=True)
    _ship_preset(game_dir, game)
    return web_preset(game_dir)


def _ship_preset(game_dir, game):
    """Commit the synthesized export_presets.cfg so future deploys (and fresh
    clones) find it. git_ship's law — inline identity, push HEAD:main at the
    Forgejo origin built from FORGEJO_HOST/ORG/TOKEN env — but surgical: only
    the preset file is staged, never a sweep of the repo's other untracked
    files. A push that cannot happen (no token, no remote, rejected branch) is
    logged, not fatal: this export uses the working-tree file either way."""
    if not os.path.isdir(os.path.join(game_dir, ".git")):
        say("preset synthesized but not committed — no .git here")
        return None

    def _git(args, check=True):
        proc = module_subprocess.run(["git"] + args, cwd=game_dir,
                                     capture_output=True, text=True,
                                     timeout=GIT_TIMEOUT)
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args[:2])} exit {proc.returncode}: "
                f"{((proc.stderr or '') or (proc.stdout or ''))[-300:].strip()}")
        return proc

    try:
        _git(["add", "export_presets.cfg"])
        c = _git(["-c", "user.name=gmp-deploy", "-c",
                  "user.email=gmp-deploy@pipeline.local", "commit", "-m",
                  f"deploy: synthesize Web export preset ({game})"],
                 check=False)
        if "nothing to commit" in (c.stdout or "") + (c.stderr or ""):
            say("preset already committed — nothing to do")
            return None
        rev = _git(["rev-parse", "--short", "HEAD"]).stdout.strip()
    except (RuntimeError, OSError) as exc:
        say(f"preset commit failed (deploy continues): {str(exc)[:140]}")
        return None

    host = (os.environ.get("FORGEJO_HOST", "127.0.0.1:3001").strip()
            or "127.0.0.1:3001")
    org = os.environ.get("FORGEJO_ORG", "slothitude")
    tok = os.environ.get("FORGEJO_TOKEN", "")
    scheme = "http" if host.startswith(("127.", "localhost", "[")) else "https"
    if not tok:
        say(f"preset committed at {rev} — FORGEJO_TOKEN not set, not pushed "
            f"(this export uses the working-tree file)")
        return rev
    url = f"{scheme}://{org}:{tok}@{host}/{org}/{game}.git"
    try:
        _git(["push", url, "HEAD:main"])
        say(f"pushed {rev} -> {org}/{game}.git HEAD:main "
            f"(the Actions gate wall judges it)")
    except (RuntimeError, OSError) as exc:
        say(f"preset push failed (deploy continues): "
            f"{str(exc).split('@')[-1][:140]}")
    return rev


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
def _artifacts_ready(out_path, started):
    """True when the export's output exists AND is fresh (godot 4.7 headless
    sometimes finishes the build and then will not exit — the artifact is the
    truth, the process is not)."""
    pck = out_path[:-5] + ".pck" if out_path.endswith(".html") else out_path
    for path in (out_path, pck):
        if not (os.path.isfile(path) and os.path.getmtime(path) >= started - 5):
            return False
    return True


def export(game_dir, preset, out_rel):
    """godot --headless --export-release. Returns the absolute build dir.

    The target folder is created first (godot does not mkdir it — a missing
    folder is a failed export). If godot finishes the build but will not exit
    (observed on the loaded server: build DONE, process idles), the fresh
    artifacts end the wait and the lingering process is terminated."""
    _ensure_web_templates()
    out_path = os.path.join(game_dir, out_rel).replace(os.sep, "/")
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    godot = os.path.expanduser(godot_bin())
    cmd = [godot, "--headless", "--path", game_dir,
           "--export-release", preset, out_path]
    say(f"export: {os.path.basename(godot)} preset={preset!r} -> {out_rel}")
    started = _time.time()
    # output goes to a temp file, not a pipe: godot writes a lot, and an
    # unread pipe fills and stalls the export itself
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                errors="replace") as log:
        proc = module_subprocess.Popen(cmd, cwd=game_dir, stdout=log,
                                       stderr=subprocess.STDOUT)
        settled = None
        deadline = started + EXPORT_TIMEOUT
        while _time.time() < deadline:
            if proc.poll() is not None:
                break
            if _artifacts_ready(out_path, started):
                if settled is None:
                    settled = os.path.getsize(out_path)
                    say("export output written — confirming it is settled")
                elif settled == os.path.getsize(out_path):
                    say("build settled but godot did not exit — terminating it")
                    proc.terminate()
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                else:
                    settled = os.path.getsize(out_path)
            _time.sleep(10)
        else:
            proc.kill()
            raise RuntimeError(
                f"godot export timed out after {EXPORT_TIMEOUT}s "
                f"(no settled build at {out_path})")
        log.seek(0)
        out = log.read()
    tail = out.strip().splitlines()[-5:]
    for line in tail:
        say(f"| {line}")
    rc = proc.returncode
    if rc not in (0, None) and not _artifacts_ready(out_path, started):
        raise RuntimeError(
            f"godot export rc={rc} for preset {preset!r}: {out[-400:].strip()}")
    if not os.path.isfile(out_path):
        raise RuntimeError(
            f"godot export ended rc={rc} but {out_path} does not exist")
    return parent or game_dir


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

    preset, out_rel = ensure_web_preset(game_dir, game)
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
    """Fake repo + monkeypatched godot/rsync (and no .git, so the preset
    commit law never fires): the preset parse, the preset synthesis (missing
    cfg, Web-less cfg, untouched cfg), the export command shape, the rsync
    flags, honest misses, the hung-godot straggler."""
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

    class FakePopen:
        """The godot call: writes the build, exits immediately."""

        def __init__(self, cmd, **kwargs):
            recorded.append({"cmd": list(cmd), "kwargs": kwargs})
            out_rel = cmd[-1]
            build = os.path.dirname(out_rel)
            os.makedirs(build, exist_ok=True)
            for name, body in (("index.html", "<html>moon-ladder</html>"),
                               ("index.js", "// loader"), ("index.pck", "bin")):
                with open(os.path.join(build, name), "w",
                          encoding="utf-8") as fh:
                    fh.write(body)
            # a stale leftover the --delete must be credited with removing
            with open(os.path.join(build, "stale-leftover.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("old")
            self.returncode = 0

        def poll(self):
            return self.returncode

    def fake_rsync(cmd, **kwargs):
        recorded.append({"cmd": list(cmd), "kwargs": kwargs})
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    # pretend the templates are installed so the download path stays cold —
    # and NEVER touch the real ~/.local/share templates dir from a selftest
    tdir = os.path.join(root, "fake-templates")
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "web_release.zip"), "w", encoding="utf-8") as fh:
        fh.write("PK")

    real_run, real_popen = module_subprocess.run, module_subprocess.Popen
    real_url, real_tpl = (module_urlopen,
                          os.environ.get("GMP_GODOT_TEMPLATES"))
    module_subprocess.Popen = FakePopen
    module_subprocess.run = fake_rsync
    module_urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError(
        "selftest must not download templates"))
    os.environ["GMP_GODOT_TEMPLATES"] = tdir
    try:
        result = run({"id": "selftest", "payload": {
            "game": "moon-ladder", "target": "retromonkey"}})
    finally:
        module_subprocess.run = real_run
        module_urlopen = real_url

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

    def last_godot_cmd():
        return recorded[-2]["cmd"]          # [-1] is the rsync call

    def synth_run(payload):
        """The synthesis scenarios run after the baseline restored .run —
        re-apply the same monkeypatch shield the baseline used."""
        module_subprocess.Popen = FakePopen
        module_subprocess.run = fake_rsync
        try:
            return run(payload)
        finally:
            module_subprocess.run = real_run
            module_subprocess.Popen = FakePopen

    cfg_path = os.path.join(game_dir, "export_presets.cfg")

    # --- 4. cfg present but no Web block in it -> Web is APPENDED ----------
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write('[preset.0]\n\nname="Linux"\nplatform="Linux"\n'
                 'export_path="build/linux/ml.x86_64"\n')
    result = synth_run({"id": "selftest-append", "payload": {
        "game": "moon-ladder", "target": "retromonkey"}})
    assert result["ok"], result
    assert last_godot_cmd()[-3:-1] == ["--export-release", "Web"], \
        last_godot_cmd()
    text = open(cfg_path, encoding="utf-8").read()
    assert 'name="Linux"' in text and 'name="Web"' in text, text
    assert "[preset.1]" in text and "[preset.1.options]" in text, text
    assert text.index('name="Linux"') < text.index("[preset.1]"), \
        "the existing preset must survive, untouched, before the appended one"
    assert os.path.isdir(os.path.join(game_dir, "build", "web"))
    assert not any(cmd["cmd"][:1] == ["git"] for cmd in recorded), \
        "the selftest sandbox has no .git — no git call may happen"
    say("selftest 4: cfg without a Web preset -> Web block appended at "
        "[preset.1], Linux preset kept, export dir created")

    # --- 5. no export_presets.cfg at all -> the standard preset is written -
    os.remove(cfg_path)
    result = synth_run({"id": "selftest-synth", "payload": {
        "game": "moon-ladder", "target": "retromonkey"}})
    assert result["ok"], result
    assert last_godot_cmd()[-3:-1] == ["--export-release", "Web"], \
        last_godot_cmd()
    assert last_godot_cmd()[-1].endswith("build/web/index.html"), \
        last_godot_cmd()
    text = open(cfg_path, encoding="utf-8").read()
    assert text == WEB_PRESET_TEMPLATE, "synthesized cfg must be the template"
    assert 'export_path="build/web/index.html"' in text, text
    say("selftest 5: missing export_presets.cfg -> sonar-shape Web preset "
        "synthesized, export path build/web/index.html")

    # --- 6. a cfg that already has Web -> untouched, no second write -------
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write('[preset.0]\n\nname="Web"\nplatform="Web"\n'
                 'export_path="build/web/index.html"\n')
    before = open(cfg_path, encoding="utf-8").read()
    result = synth_run({"id": "selftest-kept", "payload": {
        "game": "moon-ladder", "target": "retromonkey"}})
    assert result["ok"], result
    assert open(cfg_path, encoding="utf-8").read() == before, \
        "an existing Web preset must be left exactly as it was"
    assert last_godot_cmd()[-3:-1] == ["--export-release", "Web"], \
        last_godot_cmd()
    say("selftest 6: existing Web preset -> cfg byte-identical, deploy as before")

    # --- 7. godot finishes the build then will not exit ----------------------
    # (observed on the loaded server: build DONE, process idles) — the fresh
    # artifacts must end the wait, terminate the straggler, and still ship.
    with open(os.path.join(game_dir, "export_presets.cfg"), "w",
              encoding="utf-8") as fh:
        fh.write('[preset.0]\n\nname="Web"\nplatform="Web"\n'
                 'export_path="build/web/index.html"\n')

    class StubbornPopen(FakePopen):
        """Writes the build but poll() never says yes until terminated."""

        def __init__(self, cmd, **kwargs):
            FakePopen.__init__(self, cmd, **kwargs)
            self.returncode = None
            self.terminated = False

        def poll(self):
            return None if not self.terminated else 0

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    module_subprocess.Popen = StubbornPopen
    module_subprocess.run = fake_rsync   # rsync must not meet StubbornPopen
    real_sleep = _time.sleep
    _time.sleep = lambda *_: None     # the watchdog's settle wait
    try:
        result = run({"id": "selftest-hang", "payload": {
            "game": "moon-ladder", "target": "retromonkey"}})
    finally:
        _time.sleep = real_sleep
        module_subprocess.Popen, module_subprocess.run = real_popen, real_run
        module_urlopen = real_url
        if real_tpl is None:
            os.environ.pop("GMP_GODOT_TEMPLATES", None)
        else:
            os.environ["GMP_GODOT_TEMPLATES"] = real_tpl
    assert result["ok"] and result["build_dir"].endswith("build/web"), result
    say("selftest 7: build settles + godot idles -> artifacts win, straggler "
        "terminated, deploy still ships")
    say("selftest: PASS (7 scenarios)")
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
