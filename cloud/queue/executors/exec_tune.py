#!/usr/bin/env python3
"""exec_tune — the T1 lane: rewrite a feel.gd const, push, let the Actions wall
gate it, and on green chain the deploy (Wire 1: a green tune SHIPS).

The critic's issues_to_jobs files tune_tunable orders (its most common
prescription). Server law: no local Godot — the rewrite pushes to Forgejo and
the Actions gate wall (gates.yml) is the judge. Green wall = shipped value,
and the chain hook enqueues {type: deploy, payload: {game, target:
retromonkey}} so the fresh build actually reaches the live site.

Credentials: FORGEJO_TOKEN from the env when set; otherwise the credential
already stored in this clone's origin remote (new_game clones with a tokened
URL). Never hardcoded — read at runtime only.

run(job): payload {game, tunable, new_value} -> {ok, game, tunable, commit,
verdict, chained}. A re-tune of an already-shipped value is a no-op success.
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

module_subprocess = subprocess   # _selftest swaps .run

GAME_ROOTS = os.environ.get("GMP_GAMES_SRC", "/home/ubuntu/games-src")
FORGEJO_HOST = os.environ.get("FORGEJO_HOST", "127.0.0.1:3001").strip() \
    .removeprefix("https://").removeprefix("http://")
FORGEJO_ORG = os.environ.get("FORGEJO_ORG", "slothitude")
FEEL_CANDIDATES = ("scripts/feel.gd", "scripts/rpg_config.gd", "scripts/rules8.gd",
                   "scripts/score.gd")
WALL_TIMEOUT = 900          # the gate wall gets 15 minutes
PUSH_BRANCH = "main"
DEPLOY_PRIORITY = 5


def say(msg):
    print(f"[exec_tune] {msg}", flush=True)


def games_src():
    """Server game root, read at call time so the selftest can repoint it."""
    return os.environ.get("GMP_GAMES_SRC", GAME_ROOTS)


def _git(game_dir, args, timeout=120):
    proc = module_subprocess.run(["git", *args], cwd=game_dir,
                                 capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} rc={proc.returncode}: "
                           f"{(proc.stderr or proc.stdout)[:200]}")
    return proc


def _try_git(game_dir, args, timeout=120):
    """-> (rc, combined output). For git calls whose failure is information."""
    proc = module_subprocess.run(["git", *args], cwd=game_dir,
                                 capture_output=True, text=True, timeout=timeout)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))


# ------------------------------------------------------------- credentials --
def _origin_url(game_dir):
    """The clone's origin remote URL, or '' (it carries the tokened credential)."""
    rc, out = _try_git(game_dir, ["remote", "get-url", "origin"])
    return out.strip() if rc == 0 else ""


def forgejo_credentials(game_dir):
    """-> (user, token). FORGEJO_TOKEN env wins; else the credential already
    stored in this clone's origin (new_game clones with a tokened URL)."""
    tok = os.environ.get("FORGEJO_TOKEN", "").strip()
    if tok:
        return FORGEJO_ORG, tok
    url = _origin_url(game_dir)
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^:@/]+):([^@/]+)@", url)
    if m:
        return m.group(1), m.group(2)
    return FORGEJO_ORG, ""


def push_target(game_dir, game, user, token):
    """Push origin when it exists (its stored credential already authenticates
    — never rebuild a URL with a token we do not have)."""
    if _origin_url(game_dir):
        return "origin"
    scheme = "http" if FORGEJO_HOST.startswith(("127.", "localhost")) else "https"
    return f"{scheme}://{user}:{token}@{FORGEJO_HOST}/{FORGEJO_ORG}/{game}.git"


# ------------------------------------------------------------------ wall api --
def _forgejo_get(game_dir, path, timeout=20):
    user, token = forgejo_credentials(game_dir)
    url = f"http://{FORGEJO_HOST}/api/v1/repos/{FORGEJO_ORG}/{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " +
                   base64.b64encode(f"{user}:{token}".encode()).decode())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def await_wall(game_dir, game, sha, timeout=WALL_TIMEOUT):
    """Poll Actions for the run on `sha`. -> (status, task_id); status may be
    success | failure | cancelled | None (no run seen in time — never loop
    on that). The API wraps the runs in {"workflow_runs": [...]} — unwrap it."""
    import time as _t
    start = _t.time()
    while _t.time() - start < timeout:
        try:
            body = _forgejo_get(game_dir, f"{game}/actions/tasks")
            if isinstance(body, dict):
                runs = body.get("workflow_runs") or []
            elif isinstance(body, list):
                runs = body
            else:
                runs = []
            for t in runs:
                if str(t.get("head_sha", "")).startswith(sha):
                    st = t.get("status")
                    if st in ("success", "failure", "cancelled"):
                        return st, t.get("id")
        except Exception as exc:  # noqa: BLE001 — poll on
            say(f"wall poll error: {exc}")
        _t.sleep(15)
    return None, None


def _enqueue(jtype, payload, priority=DEPLOY_PRIORITY):
    """The chain hook: fire the next job straight into the queue."""
    tok = os.environ.get("QUEUE_TOKEN", "")
    body = json.dumps({"type": jtype, "payload": payload,
                       "priority": priority}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8901/jobs", data=body,
        headers={"X-Token": tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        out = json.loads(resp.read().decode())
    say(f"CHAIN: enqueued {jtype} -> job {out.get('id')} ({payload})")
    return out


# ---------------------------------------------------------------------- ship --
def _already_pushed(game_dir):
    """True when HEAD is provably on origin (nothing local left to publish).
    Undeterminable counts as NOT pushed — pushing an up-to-date branch is
    harmless (git answers 'Everything up-to-date'), missing a push is not."""
    rc, out = _try_git(game_dir, ["rev-list", "--count", f"origin/{PUSH_BRANCH}..HEAD"])
    return rc == 0 and out.strip() == "0"


def _ship(game_dir, game, tunable, new_value):
    """add -> commit -> push -> wall verdict -> green chains a deploy.
    A re-tune of an already-shipped value is a no-op success, not an error."""
    _git(game_dir, ["add", "-A"])
    rc, out = _try_git(game_dir, ["-c", "user.name=gmp-builder",
                                  "-c", "user.email=gmp-builder@pipeline.local",
                                  "commit", "-m",
                                  f"tune: {tunable} = {new_value} ({game}) via exec_tune"])
    if rc != 0 and "nothing to commit" in out:
        if not _already_pushed(game_dir):
            say(f"nothing new to commit but the branch is ahead — "
                f"pushing the committed tune")
        else:
            say(f"{tunable} already equals {new_value} on origin/{PUSH_BRANCH} "
                f"— no-op (nothing to ship)")
            return {"commit": _git(game_dir, ["rev-parse", "--short", "HEAD"]).stdout.strip(),
                    "verdict": "already-green", "no_op": True}

    commit = _git(game_dir, ["rev-parse", "--short", "HEAD"]).stdout.strip()
    user, token = forgejo_credentials(game_dir)
    target = push_target(game_dir, game, user, token)
    safe = target if target == "origin" else re.sub(r":[^@/]+@", ":***@", target)
    say(f"push {commit} -> {safe} HEAD:{PUSH_BRANCH} (triggers the Actions gate wall)")
    _git(game_dir, ["push", target, f"HEAD:{PUSH_BRANCH}"])
    say("pushed — the Actions wall judges the value now")

    status, task_id = await_wall(game_dir, game, commit)
    verdict = status or "no-run-seen"
    say(f"wall verdict: {verdict} (task {task_id})")
    chained = []
    if verdict == "success":
        try:
            chained.append(_enqueue("deploy", {"game": game,
                                               "target": "retromonkey"}))
        except Exception as exc:  # noqa: BLE001 — the tune itself shipped
            say(f"CHAIN: deploy enqueue failed (non-fatal): {exc}")
    return {"commit": commit, "verdict": verdict, "chained":
            [c.get("id") for c in chained]}


def run(job: dict) -> dict:
    payload = job.get("payload") or {}
    game = payload.get("game")
    tunable = payload.get("tunable")
    new_value = payload.get("new_value")
    if not (game and tunable and new_value is not None):
        raise ValueError("tune payload needs 'game', 'tunable', 'new_value'")

    game_dir = os.path.join(games_src(), game).replace(os.sep, "/")
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
                shipped = _ship(game_dir, game, target, new_value)
                return {"ok": True, "game": game, "tunable": target,
                        "requested": str(tunable), "new_value": new_value,
                        **shipped}
    raise RuntimeError(f"const {tunable!r} (nor any fuzzy match) found in "
                       f"{game}'s constants files (checked {FEEL_CANDIDATES})")


# ------------------------------------------------------------------ selftest --
class _FakeResp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _selftest():
    """Fake repo + scripted git/urlopen: the three ship paths (fresh push,
    no-op re-tune, clean-but-ahead) and a no-token-in-commands law."""
    import time as _t
    global module_subprocess

    root = __import__("tempfile").mkdtemp(prefix="gmp_tune_test_").replace(os.sep, "/")
    game_dir = os.path.join(root, "fake-game").replace(os.sep, "/")
    os.makedirs(os.path.join(game_dir, "scripts"))
    feel = os.path.join(game_dir, "scripts", "feel.gd")
    open(feel, "w", encoding="utf-8").write("const SCORE := 500\n")
    os.environ["GMP_GAMES_SRC"] = root
    os.environ.pop("FORGEJO_TOKEN", None)
    os.environ["QUEUE_TOKEN"] = "selftest-token"

    ORIGIN = "http://slothitude:SECRET-TOKEN@127.0.0.1:3001/slothitude/fake-game.git"
    SHA = "abc1234deadbeef"
    recorded, state = [], {"ahead": "0", "commit_ok": True, "pushes": 0,
                           "wall": [{"head_sha": SHA + "ffffff", "status":
                                     "success", "id": 7}]}

    def fake_git(cmd):
        args = cmd[1:]
        recorded.append(" ".join(args))
        if args[0] == "remote":
            return 0, (ORIGIN + "\n")
        if args[0] == "rev-list":
            return 0, state["ahead"] + "\n"
        if args[0] == "rev-parse":
            return 0, SHA[:9] + "\n"
        if "commit" in args:  # real form: git -c user.name=.. -c user.email=.. commit
            if state["commit_ok"]:
                return 0, ""
            return 1, ("On branch main\nnothing to commit, working tree clean\n")
        if args[0] == "push":
            state["pushes"] += 1
            return 0, ""
        return 0, ""

    def fake_run(cmd, **kwargs):
        if cmd[:1] == ["git"]:
            rc, out = fake_git(cmd)
            return type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()
        raise AssertionError(f"unexpected subprocess: {cmd[:2]}")

    def fake_urlopen(req, timeout=20):
        url = getattr(req, "full_url", "")
        if "/actions/tasks" in url:
            return _FakeResp({"workflow_runs": state["wall"]})
        if url.startswith("http://127.0.0.1:8901/jobs"):
            return _FakeResp({"id": 99})
        raise AssertionError(f"unexpected urlopen: {url}")

    real_run, real_url, real_sleep = (module_subprocess.run,
                                      urllib.request.urlopen, _t.sleep)
    module_subprocess.run = fake_run
    urllib.request.urlopen = fake_urlopen
    _t.sleep = lambda *_: None
    try:
        # --- 1. fresh tune: rewrite -> commit -> push origin -> green wall --
        result = run({"id": "st1", "payload": {"game": "fake-game",
                                              "tunable": "score",
                                              "new_value": 750}})
        assert "const SCORE := 750" in open(feel, encoding="utf-8").read()
        assert result["tunable"] == "SCORE" and result["verdict"] == "success", result
        assert result["chained"] == [99], "green wall must chain the deploy"
        pushes = [r for r in recorded if r.startswith("push ")]
        assert len(pushes) == 1 and pushes[0].startswith("push origin "), pushes
        assert "SECRET-TOKEN" not in " | ".join(recorded), "token leaked into a git command"
        say("selftest 1: fresh tune -> push origin (no token in the command) -> "
            "green wall -> deploy job 99 chained")

        # --- 2. no-op re-tune: same value, already pushed -> success, no push
        recorded.clear()
        state.update(pushes=0, commit_ok=False, ahead="0")
        result = run({"id": "st2", "payload": {"game": "fake-game",
                                              "tunable": "SCORE",
                                              "new_value": 750}})
        assert state["pushes"] == 0, "an already-shipped value must not push"
        assert result["verdict"] == "already-green" and result["no_op"], result
        say("selftest 2: re-tune of a shipped value -> no-op success (job 69 fixed)")

        # --- 3. clean but ahead: the committed tune still gets pushed --------
        recorded.clear()
        state.update(pushes=0, ahead="1")
        result = run({"id": "st3", "payload": {"game": "fake-game",
                                              "tunable": "SCORE",
                                              "new_value": 750}})
        assert state["pushes"] == 1, "a committed-but-unpushed tune must push"
        assert result["verdict"] == "success", result
        say("selftest 3: clean-but-ahead -> pushes the committed tune")

        # --- 4. red wall -> no deploy chain ---------------------------------
        recorded.clear()
        state.update(pushes=0, commit_ok=True, ahead="0",
                     wall=[{"head_sha": SHA + "ffffff",
                            "status": "failure", "id": 8}])
        result = run({"id": "st4", "payload": {"game": "fake-game",
                                              "tunable": "SCORE",
                                              "new_value": 900}})
        assert result["verdict"] == "failure" and result["chained"] == [], result
        say("selftest 4: red wall -> no deploy chained (the gate holds)")
    finally:
        module_subprocess.run, urllib.request.urlopen, _t.sleep = (
            real_run, real_url, real_sleep)
        os.environ.pop("QUEUE_TOKEN", None)
    say("selftest: PASS")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("usage: python exec_tune.py --selftest")
    sys.exit(2)
