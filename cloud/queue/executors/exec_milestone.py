#!/usr/bin/env python3
"""exec_milestone — the DEEPEN wire: drive pi (non-interactive) through one
milestone of an existing game, then push so the Forgejo Actions gate wall
judges it. This is the answer to the audit gap "pi stops at scaffolds;
nothing drives M2->M4".

  run(job)   job payload: {game, milestone: "M2", directive?}
             - directive: optional extra instruction appended to the prompt
             Reads spec/study_spec.json from /home/ubuntu/games-src/<game>
             (spec/jam_spec.json as fallback — same order build-milestone.yml
             uses), finds the milestone block, and runs
                 pi --print --model openrouter/free "<prompt>"
             with cwd = the game dir and OPENROUTER_API_KEY from the env
             (the worker law). pi writes COMPLETE files + the milestone's test
             suite + a DIARY.md entry; the executor then ships:
                 git add -A && git commit && git push <forgejo> HEAD:main
             The push IS the gate trigger — Actions runs the suite wall and
             the chain law (CHAINING.md) enqueues the next milestone only off
             a green wall.

  Returns {ok, game, milestone, pushed, commit}. Any failure raises — the
  router's server-side retry law decides retry/fail.

Milestone lookup: a block dict (id/name matched, case-insensitive, with or
without the leading M) wins; a plain-string milestones list (what the real
specs in build/<game>/spec/study_spec.json use today) falls back to position
(M2 -> the 2nd entry) then to text-prefix match.

Selftest (offline — fake game dir + stub repo, subprocess monkeypatched):
    python exec_milestone.py --selftest
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

GAME_ROOTS = "/home/ubuntu/games-src"   # GMP_GAMES_SRC overrides (selftest law)
SPEC_RELPATHS = ("spec/study_spec.json", "spec/jam_spec.json")  # workflow law:
# study_spec first, jam_spec as the older fallback (build-milestone.yml order)
PI_BIN = "pi"
# The alias is a ROUTING LOTTERY: openrouter/free fans out to ~20 backends of
# wildly varying speed, and a dead pick stalls the whole build silently.
# GMP_PI_MODEL pins one concrete free model (still openrouter, still free —
# the workers law is about the lane, not the alias). Default unchanged.
PI_MODEL = os.environ.get("GMP_PI_MODEL", "openrouter/free")
PI_TIMEOUT = 900         # a silent hang must cost minutes, not half an hour
GIT_TIMEOUT = 120
FORGEJO_HOST = "127.0.0.1:3001"
FORGEJO_ORG = "slothitude"
FORGEJO_TOKEN = os.environ["FORGEJO_TOKEN"]  # basic-auth push token; set in queue/env (no hardcoded secrets)
PUSH_BRANCH = "main"     # the branch the Actions gate wall watches

PROMPT_LAWS = """LAWS (never break):
1. Constants, not magic: every tunable number is a named const in scripts/feel.gd.
2. Tests ship with the milestone: write this milestone's test suite (tests/<name>.gd,
   SceneTree script: deferred start, PASS/FAIL lines, a final
   'Ran N checks: X passed, 0 failed' summary, quit(1) on red / quit(0) on green).
3. 2x green required: every suite in tests/ must pass TWICE in a row before you stop.
4. Original assets only: code-drawn art; no image files unless they already exist.
5. Append a short entry to DIARY.md at the repo root (what shipped, what's next).
6. Write COMPLETE files (full file, never a diff); all prior suites must stay green."""


module_subprocess = subprocess  # _selftest swaps .run; the module keeps its name


def say(msg):
    print(f"[exec_milestone] {msg}", flush=True)


def games_src():
    """Server game root, read at call time so the selftest can repoint it."""
    return os.environ.get("GMP_GAMES_SRC", GAME_ROOTS)


def game_dir_of(game):
    return os.path.join(games_src(), game).replace(os.sep, "/")


# -------------------------------------------------------------------- spec --
def load_spec(game_dir):
    """-> (spec_dict, path). study_spec.json first, jam_spec.json fallback."""
    checked = []
    for rel in SPEC_RELPATHS:
        path = os.path.join(game_dir, rel)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh), path
        checked.append(path)
    raise RuntimeError(
        f"no spec file in {game_dir} — checked: " + ", ".join(checked))


def _norm(value):
    return str(value or "").strip().upper()


def _milestone_key(sel):
    """'M2'/'2' -> '2'; anything else -> None."""
    sel = str(sel).strip()
    if sel[:1].upper() == "M" and sel[1:].isdigit():
        return sel[1:]
    return sel if sel.isdigit() else None


def find_milestone(spec, selector):
    """-> (index, block). Dict blocks match id/name first; string lists fall
    back to position then text prefix. Raises listing what WAS there."""
    milestones = spec.get("milestones") or []
    sel = str(selector).strip()
    sel_up, num = _norm(sel), _milestone_key(sel)

    for i, block in enumerate(milestones):
        if isinstance(block, dict):
            for key in ("id", "name", "milestone"):
                val = _norm(block.get(key))
                if val and val in (sel_up, num, "M" + (num or "")):
                    return i, block

    if num and 1 <= int(num) <= len(milestones):
        return int(num) - 1, milestones[int(num) - 1]

    for i, block in enumerate(milestones):
        if isinstance(block, str) and _norm(block).startswith(sel_up):
            return i, block

    labels = [str(m.get("id") or m.get("name") or m)[:60] if isinstance(m, dict)
              else m[:60] for m in milestones]
    raise RuntimeError(
        f"milestone {sel!r} not found in spec — have {len(milestones)}: {labels}")


def build_memory(game_dir, game):
    """The build team's memory: diary tail + player words. Read fresh at
    prompt-build time so every milestone stands on all of it."""
    chunks = []
    diary = os.path.join(game_dir, "DIARY.md")
    try:
        lines = open(diary, encoding="utf-8").read().splitlines()
        tail = [ln for ln in lines if ln.strip()][-30:]
        chunks.append("DIARY (the game's own record, tail):\n"
                      + chr(10).join(tail))
    except OSError:
        pass
    rev = f"/home/ubuntu/pipeline/reviews/{game}.jsonl"
    try:
        rows = [json.loads(ln) for ln in open(rev, encoding="utf-8")
                if ln.strip()][-5:]
        if rows:
            chunks.append("PLAYER WORDS (reviews + help sessions, tail):\n"
                          + chr(10).join(json.dumps(r)[:300] for r in rows))
    except (OSError, ValueError):
        pass
    return chunks


def build_prompt(selector, block, directive=None, game_dir=None, game=None):
    """Milestone-depth prompt: scope + tests + laws + THE BUILD MEMORY."""
    if isinstance(block, dict):
        body = json.dumps(block, indent=1)
    else:
        body = str(block)
    parts = [
        "You are the Game Making Pipeline's milestone builder, running "
        "non-interactively inside an existing game repo. Build EXACTLY this "
        "milestone and nothing else.",
        f"MILESTONE {selector}:\n{body}",
        PROMPT_LAWS,
    ]
    if game_dir:
        mem = build_memory(game_dir, game or "")
        if mem:
            parts.append("THE BUILD MEMORY (prior builds + player words — "
                         "honor these; never regress what the diary says "
                         "shipped; the player words are what this milestone "
                         "is FOR):\n\n" + "\n\n".join(mem))
    parts.append(
        "Work directly in the current directory (the game repo). When the "
        "milestone is built and every suite are green twice, stop — no "
        "questions, no interactivity.")
    if directive:
        parts.append(f"PIPELINE DIRECTIVE (human): {directive}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- pi + git --
def run_pi(prompt, cwd, timeout=None):
    """pi --print, non-interactive, cwd the game dir. Raises on timeout or a
    non-zero exit (with the tail — honest, the retry law decides)."""
    timeout = timeout or PI_TIMEOUT
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set — refusing to start pi (fail fast "
            "before burning the timeout; the worker law routes this model "
            "through openrouter/free)")
    cmd = [PI_BIN, "--print", "--model", PI_MODEL, prompt]
    env = dict(os.environ)
    say(f"-> pi ({PI_MODEL}), cwd {cwd}, timeout {timeout}s, prompt {len(prompt)} chars")
    # Own session + process-group kill: pi spawns grandchildren (godot test
    # runs) that outlive a plain child kill and hang the next attempt's tree.
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, err = proc.communicate()
        raise RuntimeError(
            f"pi timed out after {timeout}s "
            f"(partial output: {str(out or '')[-300:]})") from exc
    tail = ((out or "") or (err or "")).strip().splitlines()[-6:]
    for line in tail:
        say(f"| {line}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"pi exit {proc.returncode}: {(err or out or '')[-400:].strip()}")
    return proc


def _git(cwd, args):
    cmd = ["git"] + args
    proc = subprocess.run(cmd, cwd=cwd, timeout=GIT_TIMEOUT,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args[:3])} exit {proc.returncode} in {cwd}: "
            f"{((proc.stderr or '') or (proc.stdout or ''))[-400:].strip()}")
    return proc


def forgejo_push_url(game):
    """The Forgejo origin by construction (host/org/token env-overridable).
    Scheme: https unless the host is loopback (Forgejo serves plain HTTP
    on 127.0.0.1:3001 — the gnutls handshake trap, learned 2026-09-23)."""
    host = os.environ.get("FORGEJO_HOST", FORGEJO_HOST)
    org = os.environ.get("FORGEJO_ORG", FORGEJO_ORG)
    token = os.environ.get("FORGEJO_TOKEN", FORGEJO_TOKEN)
    scheme = "http" if host.startswith(("127.", "localhost", "[")) else "https"
    return f"{scheme}://{org}:{token}@{host}/{org}/{game}.git"


def git_ship(game_dir, game, selector):
    """add -> commit (inline identity, the Actions law) -> rev-parse -> push
    HEAD:<PUSH_BRANCH> at the Forgejo origin. Returns the short hash."""
    _git(game_dir, ["add", "-A"])
    c = _git(game_dir, ["-c", "user.name=gmp-builder",
                        "-c", "user.email=gmp-builder@pipeline.local",
                        "commit", "-m", f"milestone {selector} ({game}) via exec_milestone"])
    rev = _git(game_dir, ["rev-parse", "--short", "HEAD"])
    commit = rev.stdout.strip()
    if "nothing to commit" in (c.stdout or "") + (c.stderr or ""):
        # Milestone already built and shipped (refire after a false fail, or
        # pi validated without changing files). The wall state for HEAD
        # carries — await_wall matches it instead of failing the job.
        say(f"nothing to commit — milestone already shipped as {commit}; "
            f"the wall verdict for it stands")
        return commit
    url = forgejo_push_url(game)
    say(f"push {commit} -> {url.rsplit('@', 1)[-1]} HEAD:{PUSH_BRANCH} "
        f"(triggers the Actions gate wall)")
    _git(game_dir, ["push", url, f"HEAD:{PUSH_BRANCH}"])
    return commit






def _enqueue(jtype, payload, priority=4):
    """The chain hook: fire the next job in the ladder straight into the queue."""
    import urllib.request
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

# ------------------------------------------------------------- red-wall learner --
def _forgejo_get(path, timeout=20):
    import urllib.request, base64
    host = os.environ.get("FORGEJO_HOST", FORGEJO_HOST)
    org = os.environ.get("FORGEJO_ORG", FORGEJO_ORG)
    tok = os.environ.get("FORGEJO_TOKEN", FORGEJO_TOKEN)
    url = f"http://{host}/api/v1/repos/{org}/{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " +
                   base64.b64encode(f"{org}:{tok}".encode()).decode())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def await_wall(game, sha, timeout=None):
    """Poll Actions for the run on `sha`. -> (status, task_id); status may be
    success | failure | None (no run seen in time — never loop on that)."""
    import time as _t
    # M4-scale walls (import + full battery 2x + replays) do not fit 900s —
    # they died as no-run-seen. Generous default, WALL_TIMEOUT overrides.
    timeout = timeout or int(os.environ.get("WALL_TIMEOUT", "3600"))
    start = _t.time()
    while _t.time() - start < timeout:
        try:
            tasks = _forgejo_get(f"{game}/actions/tasks")
            if isinstance(tasks, dict):
                # Forgejo wraps: {"workflow_runs": [...], "total_count": N}
                tasks = tasks.get("workflow_runs") or tasks.get("entries") or []
            for t in tasks if isinstance(tasks, list) else []:
                if str(t.get("head_sha", "")).startswith(sha):
                    st = t.get("status")
                    if st in ("success", "failure", "cancelled"):
                        return st, t.get("id")
            pending = [t for t in (tasks if isinstance(tasks, list) else [])
                       if str(t.get("head_sha", "")).startswith(sha)]
            if pending:  # still running
                _t.sleep(15)
                continue
        except Exception as exc:  # noqa: BLE001 — poll on
            say(f"wall poll error: {exc}")
        _t.sleep(15)
    return None, None


ACTIONS_LOG_DIR = "/home/ubuntu/forgejo/gitea/data/actions_log"  # runner log
# storage: {org}/{game}/{run_id:02x}/{run_id}.log.zst. FORGEJO_ACTIONS_LOG_DIR
# overrides. This is the ONLY log source on Forgejo <= v10 — the
# /actions/jobs/{id}/logs API route does not exist there (added in v11).


def _distill(text, limit=6000):
    """The failing-check lines, not the whole wall log."""
    keys = ("error", "fail", "assert", "script", "parse", "passed", "checks")
    lines = [ln for ln in text.splitlines()
             if any(k in ln.lower() for k in keys)]
    tail = chr(10).join((lines or text.splitlines())[-40:])
    return tail[-limit:]


def fail_logs(game, task_id):
    """Best-effort tail of the failing job's log (the learner's food).

    The Forgejo v10 API has no job-logs route (404 — it landed in v11), so
    read the runner's zstd log straight off disk, and keep the API as the
    primary path for when the Forgejo is upgraded."""
    import urllib.request, base64
    host = os.environ.get("FORGEJO_HOST", FORGEJO_HOST)
    org = os.environ.get("FORGEJO_ORG", FORGEJO_ORG)
    tok = os.environ.get("FORGEJO_TOKEN", FORGEJO_TOKEN)
    url = f"http://{host}/api/v1/repos/{org}/{game}/actions/jobs/{task_id}/logs"
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " +
                   base64.b64encode(f"{org}:{tok}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _distill(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        pass  # v10 (and any other API-less forgejo) — fall through to disk
    try:
        log_dir = os.environ.get("FORGEJO_ACTIONS_LOG_DIR", ACTIONS_LOG_DIR)
        path = os.path.join(log_dir, org, game, f"{task_id:02x}",
                            f"{task_id}.log.zst")
        raw = subprocess.run(["zstd", "-dc", path], timeout=60,
                             capture_output=True, check=True).stdout
        return _distill(raw.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return f"(logs unavailable: no API route and no runner log at " \
               f"{log_dir}/{org}/{game}/{task_id:02x}/{task_id}.log.zst: {exc})"


# ---------------------------------------------------------------------- run --
def run(job: dict) -> dict:
    payload = job.get("payload") or {}
    game = payload.get("game")
    selector = payload.get("milestone")
    if not game:
        raise ValueError("milestone job payload needs 'game'")
    if not selector:
        raise ValueError("milestone job payload needs 'milestone' (e.g. 'M2')")

    game_dir = game_dir_of(game)
    if not os.path.isdir(game_dir):
        raise RuntimeError(f"game dir not found: {game_dir}")

    say(f"id={job.get('id')} milestone game={game} milestone={selector}")
    spec, spec_path = load_spec(game_dir)
    say(f"spec: {spec_path}")
    index, block = find_milestone(spec, selector)
    label = (block.get("name") or block.get("id") if isinstance(block, dict)
             else block) or "?"
    say(f"milestone [{index}]: {str(label)[:80]}")

    prompt = build_prompt(selector, block, payload.get("directive"),
                          game_dir=game_dir, game=game)

    # the red-wall learner: build -> push -> the wall judges -> on red, feed
    # the failure log back to pi and try again (user law: learn from red walls)
    attempts, verdict, logs, commit = 0, "no-run", "", ""
    while attempts < 3:
        attempts += 1
        if attempts == 1:
            run_pi(prompt, game_dir)
        else:
            say(f"RED WALL: attempt {attempts} — feeding {len(logs)} chars of "
                f"failure log back to pi")
            nl = chr(10) * 2
            red_prompt = (prompt + nl + "RED WALL ATTEMPT " + str(attempts)
                          + ": the gate wall judged your previous push RED. "
                          "The wall said:" + nl + logs + nl + "Fix these "
                          "failures. Keep every prior suite green. "
                          "Write complete files.")
            run_pi(red_prompt, game_dir)
        commit = git_ship(game_dir, game, selector)
        status, task_id = await_wall(game, commit)
        if status == "success":
            verdict = "green"
            break
        if status is None:
            verdict = "no-run-seen"
            break
        verdict = "red-after-" + str(attempts)
        logs = fail_logs(game, task_id)

    say(f"done: {game} {selector} pushed as {commit} — wall verdict: {verdict} "
        f"(attempts: {attempts})")

    # the unattended chain: green -> next milestone; last green -> critique+deploy
    chained = []
    if verdict == "green":
        try:
            spec2, _ = load_spec(game_dir)
            milestones = spec2.get("milestones") or []
            idx, _ = find_milestone(spec2, selector)
            if idx + 1 < len(milestones):
                nxt = milestones[idx + 1]
                nxt_id = (nxt.get("id") if isinstance(nxt, dict) else None)                     or f"M{idx + 2}"
                chained.append(_enqueue("milestone",
                                        {"game": game, "milestone": nxt_id}, 4))
            else:
                chained.append(_enqueue("critique",
                                        {"game": game, "mode": "full"}, 7))
                chained.append(_enqueue("deploy",
                                        {"game": game,
                                         "target": "retromonkey"}, 5))
        except Exception as exc:  # noqa: BLE001 — the milestone itself shipped
            say(f"CHAIN: hook failed (non-fatal): {exc}")

    return {"ok": verdict == "green", "game": game, "milestone": selector,
            "pushed": True, "commit": commit, "verdict": verdict,
            "attempts": attempts,
            "chained": [c.get("id") for c in chained]}


# ------------------------------------------------------------------ selftest --
def _selftest():
    """Fake game dir (2-milestone spec) + stub repo, subprocess recorded."""
    import tempfile

    root = tempfile.mkdtemp(prefix="gmp_milestone_test_").replace(os.sep, "/")
    game_dir = os.path.join(root, "fake-game").replace(os.sep, "/")
    os.makedirs(os.path.join(game_dir, "spec"))
    os.makedirs(os.path.join(game_dir, ".git"))  # stub repo — no real git runs
    spec = {
        "project": "fake-game",
        "milestones": [
            {"id": "M1", "name": "scaffold",
             "scope": "project skeleton, main scene, feel.gd with MOVE_SPEED",
             "tests": "tests/smoke.gd — 8 checks: scene loads, player exists"},
            {"id": "M2", "name": "second-slice",
             "scope": "enemies + score; SCORE_PER_HIT const in feel.gd",
             "tests": "tests/enemies.gd — 14 checks + a replay"},
        ],
    }
    with open(os.path.join(game_dir, "spec", "study_spec.json"), "w",
              encoding="utf-8") as fh:
        json.dump(spec, fh, indent=1)
    with open(os.path.join(game_dir, "DIARY.md"), "w", encoding="utf-8") as fh:
        fh.write("# DIARY\n\n- M1 scaffold shipped\n")

    os.environ["GMP_GAMES_SRC"] = root
    os.environ["OPENROUTER_API_KEY"] = "test-key-offline"
    say(f"selftest roots: games-src={root} game={game_dir}")

    recorded = []

    def recorder(cmd, **kwargs):
        recorded.append({"cmd": cmd, "cwd": kwargs.get("cwd"),
                         "timeout": kwargs.get("timeout")})
        return SimpleNamespace(returncode=0,
                               stdout="abc1234\n" if "rev-parse" in cmd else "",
                               stderr="")

    real_run = module_subprocess.run
    module_subprocess.run = recorder
    try:
        result = run({"id": "selftest", "payload": {
            "game": "fake-game", "milestone": "M2",
            "directive": "keep the jump feel identical to M1"}})
    finally:
        module_subprocess.run = real_run

    # --- the pi invocation -------------------------------------------------
    pi = recorded[0]
    assert pi["cmd"][:4] == ["pi", "--print", "--model", "openrouter/free"], pi["cmd"][:4]
    prompt = pi["cmd"][4]
    assert pi["cwd"] == game_dir, f"pi cwd must be the game dir, got {pi['cwd']}"
    assert pi["timeout"] == PI_TIMEOUT
    assert "SCORE_PER_HIT" in prompt and "tests/enemies.gd" in prompt, \
        "the M2 block (scope + tests) must land in the prompt"
    assert "feel.gd" in prompt and "DIARY.md" in prompt and "2x green" in prompt, \
        "the laws must land in the prompt"
    assert "keep the jump feel identical" in prompt, "directive must land"

    # --- the git sequence ---------------------------------------------------
    git_cmds = [r["cmd"] for r in recorded[1:]]
    expected = [
        ["git", "add", "-A"],
        ["git", "-c", "user.name=gmp-builder", "-c", "user.email=gmp-builder@pipeline.local",
         "commit", "-m", "milestone M2 (fake-game) via exec_milestone"],
        ["git", "rev-parse", "--short", "HEAD"],
        ["git", "push",
         f"https://slothitude:{FORGEJO_TOKEN}@127.0.0.1:3001/slothitude/fake-game.git",
         "HEAD:main"],
    ]
    assert git_cmds == expected, git_cmds
    assert all(r["cwd"] == game_dir for r in recorded[1:]), "git runs in the game dir"

    # --- the result dict ----------------------------------------------------
    assert result == {"ok": True, "game": "fake-game", "milestone": "M2",
                      "pushed": True, "commit": "abc1234"}, result

    print("--- recorded pi prompt (first 400 chars) ---")
    print(prompt[:400])
    print("--- recorded git sequence ---")
    for cmd in git_cmds:
        print(" ".join(cmd))
    print("--- result ---")
    print(json.dumps(result))
    say("selftest: pi invocation + git sequence + result dict green")

    # --- string-list spec (what the real specs look like) -------------------
    game2 = os.path.join(root, "string-game").replace(os.sep, "/")
    os.makedirs(os.path.join(game2, "spec"))
    os.makedirs(os.path.join(game2, ".git"))
    with open(os.path.join(game2, "spec", "study_spec.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"milestones": ["1 first slice: movement + feel.gd",
                                  "2 second slice: enemies"]}, fh)
    recorded.clear()
    module_subprocess.run = recorder
    try:
        run({"id": "selftest-str", "payload": {"game": "string-game",
                                               "milestone": "M2"}})
    finally:
        module_subprocess.run = real_run
    str_prompt = recorded[0]["cmd"][4]
    assert "2 second slice: enemies" in str_prompt, str_prompt[:200]
    say("selftest: string milestones list -> M2 resolves positionally, green")

    # --- honest failures ----------------------------------------------------
    key = os.environ.pop("OPENROUTER_API_KEY")
    try:
        run({"id": "nokey", "payload": {"game": "fake-game", "milestone": "M2"}})
    except RuntimeError as exc:
        assert "OPENROUTER_API_KEY" in str(exc)
        say(f"selftest: missing key -> RuntimeError: {exc}")
    else:
        raise AssertionError("missing OPENROUTER_API_KEY must raise before pi runs")
    finally:
        os.environ["OPENROUTER_API_KEY"] = key

    try:
        run({"id": "nomile", "payload": {"game": "fake-game", "milestone": "M9"}})
    except RuntimeError as exc:
        assert "not found" in str(exc) and "'M1', 'M2'" in str(exc)
        say(f"selftest: unknown milestone -> RuntimeError: {exc}")
    else:
        raise AssertionError("unknown milestone must raise listing what was there")

    say("selftest: PASS")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("usage: python exec_milestone.py --selftest")
    sys.exit(2)
