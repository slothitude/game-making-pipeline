#!/usr/bin/env python3
"""playerone-lappy worker — PlayerOne's play/see lane as a Lappy service.

The move: PlayerOne's play/see lane (evidence collection, playtests,
self-play, critiques) is starved on retromonkey (956MB, the AVD parked,
chromium a stranger there). Lappy has 32GB and an RTX 3060. So Lappy becomes
a queue WORKER: it claims the PlayerOne job types off the retromonkey queue,
runs them locally with proper resources, and reports back over the same wire
every other worker uses.

    claim    POST <GATEWAY_URL>/jobs/claim        {"types": [...]} -> {job}|{job: null}
    report   POST <GATEWAY_URL>/jobs/<id>/result  {"result": ...} | {"error": ...}
    orders   POST <GATEWAY_URL>/jobs              one POST per actionable issue (prio 7)

Dispatch table:

    critique -> collect evidence (playwright chromium against the live game
                URL: phone viewport, 1.5s cadence, ring of 12 PNGs, latest.json)
             -> critique driver under the venv python (llm_critic ladder +
                vision_critic on the newest frame)
             -> issues -> work-orders on the queue (exec_critique's audited
                issues_to_jobs, priority 7)
    playtest -> the evidence feed for N seconds -> evidence summary as result
    emulator -> same lane as playtest (no AVD on lappy; chromium IS the phone)
    selfplay -> selfplay.py: the numpy brain plays the live game, records
                {context, options, action} rows, merges where evolve.sh reads

This process is stdlib-only BY DESIGN. The heavy lanes run as subprocesses
under the playerone venv python (playwright/numpy/the ladder live there), so
a wedged browser, an OOM, or a flaky ladder can never take the daemon down.
A runner that dies reports {"error": ...} — the queue's retry law requeues
it once, then fails it honestly.

Selftest (offline: no venv, no network, no browser — mocks the claim POST,
the subprocesses, and points ROOT at a temp tree with a fake evidence dir):

    python worker.py --selftest
"""
from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# -- wire + layout -------------------------------------------------------------
GATEWAY_URL = os.environ.get(
    "GATEWAY_URL", "https://retromonkey.com.au/api").rstrip("/")
ROOT = Path(os.environ.get("PLAYERONE_ROOT", "/home/aaron/playerone"))
VENV_PY = ROOT / "venv" / "bin" / "python"

POLL_SECONDS = 10.0        # the spec'd poll cadence
CLAIM_TYPES = ["critique", "playtest", "emulator", "selfplay"]
HTTP_TIMEOUT = 30          # per queue POST
LLM_TIMEOUT = 1800         # the ladder degrades 4 deep; give it room
VISION_TIMEOUT = 300

# the games selfplay knows (mirror of selfplay.py's registry; a custom slug
# resolves module <game>_playthrough inside selfplay itself)
GAMES = {
    "sonar": "sonar_playthrough",
    "slime": "slime_playthrough",
    "arcade": "arcade_playthrough",
}
DEFAULT_URL = "https://retromonkey.com.au/games/{game}/"
DEFAULT_SECONDS = 60.0     # evidence-window default (playtest/emulator/critique)
DEFAULT_SESSIONS = 3       # selfplay sessions
DEFAULT_DECISIONS = 80     # selfplay decisions per session
DEFAULT_BOOT_SECONDS = 420.0  # selfplay loader-overlay wait (cold wasm cache)

_stop = threading.Event()


def say(msg: str) -> None:
    print(f"[playerone-lappy] {msg}", flush=True)


# ------------------------------------------------------------------ the wire --
def _token() -> str:
    return os.environ.get("GATEWAY_TOKEN", "").strip()


def _post_json(path: str, body: dict, timeout: float = HTTP_TIMEOUT) -> dict:
    data = json.dumps(body, default=str).encode("utf-8")
    req = urllib.request.Request(
        GATEWAY_URL + path, data=data, method="POST",
        headers={"X-Token": _token(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class SubResult:
    """What a lane's subprocess came back as. stopped/timed_out are distinct
    from a plain non-zero exit: a shutdown mid-job must requeue, not fail."""

    def __init__(self, returncode: int, stdout: str, stderr: str,
                 stopped: bool = False, timed_out: bool = False) -> None:
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        self.stopped = stopped
        self.timed_out = timed_out

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.stopped and not self.timed_out

    def tail(self, n: int = 6, which: str = "stderr") -> str:
        text = self.stderr if which == "stderr" else self.stdout
        return " | ".join(text.strip().splitlines()[-n:])


def _subprocess(cmd: list, timeout: float, cwd: str | None = None,
                env: dict | None = None) -> SubResult:
    """Run a lane child, cooperatively: a SIGTERM stops the child gracefully
    (terminate, then kill after 10s) instead of racing systemd's SIGKILL —
    that way the in-flight job gets its error POST and the queue requeues it."""
    argv = [os.fspath(part) for part in cmd]
    say(f"run: {argv[1]} {' '.join(argv[2:6])}... (budget {timeout:.0f}s)")
    proc = subprocess.Popen(argv, cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    deadline = time.monotonic() + timeout
    stopped = timed_out = False
    while proc.poll() is None:
        if _stop.is_set():
            stopped = True
            say("shutdown signal in flight — stopping the lane child")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
        if time.monotonic() > deadline:
            timed_out = True
            say(f"lane child over budget ({timeout:.0f}s) — killing")
            proc.kill()
            break
        time.sleep(0.5)
    out, err = proc.communicate()
    return SubResult(proc.returncode if proc.returncode is not None else -1,
                     out, err, stopped=stopped, timed_out=timed_out)


def claim_job() -> dict | None:
    return _post_json("/jobs/claim", {"types": CLAIM_TYPES}).get("job")


def post_result(job_id, result=None, error=None, permanent: bool = False) -> dict:
    if error is None:
        body: dict = {"result": result}
    else:
        body = {"error": str(error)[:2000]}
        if permanent:
            body["permanent"] = True  # retrying can't wire it
    resp = _post_json(f"/jobs/{job_id}/result", body)
    say(f"reported id={job_id} -> {resp.get('status')} "
        f"({'result' if error is None else 'error'})")
    return resp


# --------------------------------------------------------- the order converter --
_CONVERTER: object | None = None


def load_converter():
    """exec_critique.issues_to_jobs — the audited issues->work-orders
    converter, used byte-identically (one source of truth). Deployed beside
    worker.py; a repo checkout also finds it at ../queue/executors/.
    Returns None (loudly) when not deployed: the critique still reports."""
    global _CONVERTER
    if _CONVERTER is not None:
        return _CONVERTER or None
    here = Path(__file__).resolve().parent
    for cand in (here, here.parent / "queue" / "executors"):
        if (cand / "exec_critique.py").is_file():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            _CONVERTER = importlib.import_module("exec_critique")
            say(f"converter: exec_critique loaded from {cand}")
            return _CONVERTER
    _CONVERTER = False
    say("converter: exec_critique.py NOT deployed beside worker.py — "
        "critiques will report but issues will NOT become work-orders")
    return None


def enqueue_orders(critique: dict) -> tuple[list, str | None]:
    """File the critique's actionable issues as queue jobs. Returns
    (job_ids, error_or_None) — an actioning failure never voids the critique."""
    conv = load_converter()
    if conv is None:
        return [], "exec_critique.py not deployed — issues not actioned"
    ids: list = []
    for order in conv.issues_to_jobs(critique):
        resp = _post_json("/jobs", order)
        ids.append(resp.get("id"))
        say(f"work-order {order.get('type')} "
            f"priority={order.get('priority')} -> id={resp.get('id')}")
    return ids, None


# ----------------------------------------------------------- the critique lane --
_DRIVER_TEMPLATE = r'''"""GMP critique driver (lappy variant) — written to /tmp by the playerone-lappy
worker, run with the PlayerOne venv python. Port of exec_critique's server
driver: same judge-with-retry ladder law, same vision tier, lappy paths.
(The server driver injects games-src tunables.json const names into the
evidence; lappy carries no games-src, so the critic prescribes from the
evidence and its own knowledge only.)"""
import json, os, sys, time

sys.path[0:0] = list({pkg_dirs})
out_path, evidence_path, mode, game = sys.argv[1:5]
screenshot = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None

with open(evidence_path, "r", encoding="utf-8") as fh:
    evidence = json.load(fh)

critique = {{"game": game}}

def _judge_with_retry(game, evidence, pngs, attempts=3, wait=45):
    """The ladder flakes (NVIDIA 504s). Be patient, then degrade honestly —
    a transport failure must never kill the whole critique."""
    from llm_critic import LLMCritic
    last = None
    for i in range(attempts):
        try:
            return LLMCritic().judge(game, evidence, pngs) or {{}}
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"driver: judge attempt {{i+1}} failed: {{exc}}", flush=True)
            time.sleep(wait)
    return {{"ladder_error": str(last)[:300],
             "verdict": "ladder unreachable after {{attempts}} attempts — "
                        "measurements below, judgment deferred"}}

if mode in ("llm", "full"):
    pngs = []
    if screenshot:
        with open(screenshot, "rb") as fh:
            pngs = [fh.read()]
    critique.update(_judge_with_retry(game, evidence, pngs))

if mode in ("vision", "full"):
    from vision_critic import vision_critique
    if not screenshot:
        raise RuntimeError("vision tier needs a screenshot; none given")
    key = os.environ.get("NVAPI_KEY", "").strip()
    if not key:
        with open(os.path.expanduser("~/.nvapi"), "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    try:
        vision = vision_critique(game, screenshot, key)
    except Exception as exc:  # noqa: BLE001
        vision = {{"vision_error": str(exc)[:200]}}
    if mode == "vision":
        critique.update(vision)
    else:
        critique["vision"] = vision

with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(critique, fh, indent=2)
print("driver: wrote " + out_path)
'''


def evidence_dir(game: str) -> Path:
    return ROOT / "evidence" / game


def newest_screenshot(game: str) -> str | None:
    """Newest *.png under evidence/<game>/ — the frame vision_critic sees."""
    root = evidence_dir(game)
    if not root.is_dir():
        return None
    shots = list(root.rglob("*.png"))
    if not shots:
        return None
    return str(max(shots, key=lambda p: p.stat().st_mtime))


def run_collector(game: str, seconds: float, url: str | None = None) -> dict:
    """The eyes: collect_evidence.py under the venv python. Its stdout JSON
    report IS the return value (exec-style, like exec_emulator's emu_play)."""
    cmd = [VENV_PY, ROOT / "collect_evidence.py", "--game", game,
           "--seconds", str(seconds), "--out-root", str(ROOT / "evidence")]
    if url:
        cmd += ["--url", url]
    timeout = seconds * 3 + 240   # goto(60s) + boot settle + cadence slack
    proc = _subprocess(cmd, timeout=timeout, cwd=str(ROOT))
    if proc.stopped:
        raise RuntimeError("evidence feed interrupted by shutdown")
    if proc.timed_out:
        raise RuntimeError(f"collect_evidence timed out after {timeout:.0f}s")
    if proc.returncode != 0:
        raise RuntimeError("collect_evidence failed (exit "
                           f"{proc.returncode}): {proc.tail()}")
    try:
        return json.loads(proc.stdout.strip())
    except ValueError:
        raise RuntimeError("collect_evidence wrote no JSON report: "
                           + proc.stdout[-300:])


def run_driver(game: str, mode: str, evidence_path: str,
               screenshot: str | None) -> dict:
    """The brain: llm_critic ladder + vision_critic under the venv python."""
    scratch = tempfile.mkdtemp(prefix="p1_driver_")
    driver_path = os.path.join(scratch, "driver.py")
    out_path = os.path.join(scratch, "critique.json")
    with open(driver_path, "w", encoding="utf-8") as fh:
        fh.write(_DRIVER_TEMPLATE.format(
            pkg_dirs=repr([str(ROOT), str(ROOT / "scripts")])))
    cmd = [VENV_PY, driver_path, out_path, evidence_path, mode, game,
           screenshot or ""]
    timeout = LLM_TIMEOUT if mode in ("llm", "full") else VISION_TIMEOUT
    env = dict(os.environ)
    # llm_critic discovers the ladder at <GMP_ROOT>/daily/ladder.json; the
    # code default is a Windows path — point it at the lappy playerone root
    env.setdefault("GMP_ROOT", str(ROOT))
    proc = _subprocess(cmd, timeout=timeout, cwd=str(ROOT), env=env)
    if proc.stopped:
        raise RuntimeError("critique driver interrupted by shutdown")
    if proc.timed_out:
        raise RuntimeError(f"critique driver timed out after {timeout:.0f}s")
    if not os.path.isfile(out_path):
        raise RuntimeError(
            f"critique driver produced no critique json (exit "
            f"{proc.returncode}): {proc.tail() or proc.tail(6, 'stdout')}")
    with open(out_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def runner_critique(job: dict) -> dict:
    payload = job.get("payload") or {}
    game = payload.get("game")
    if not game:
        raise ValueError("critique job payload needs 'game'")
    mode = payload.get("mode") or "full"
    if mode not in ("llm", "vision", "full"):
        raise ValueError(f"critique mode must be llm|vision|full, got {mode!r}")

    # 1. the eyes — inline payload evidence wins (exec_critique's law),
    #    otherwise a FRESH local playwright feed. On retromonkey mode=full
    #    needed the AVD booted; on lappy chromium is always available.
    screenshot = None
    inline = payload.get("evidence")
    if isinstance(inline, dict):
        scratch = tempfile.mkdtemp(prefix="p1_critique_")
        evidence_path = os.path.join(scratch, "evidence.json")
        with open(evidence_path, "w", encoding="utf-8") as fh:
            json.dump(inline, fh, indent=2)
        evidence_note = "payload (inline dict)"
        if mode in ("vision", "full"):
            screenshot = (payload.get("screenshot")
                          or newest_screenshot(game))
            if screenshot is None:
                raise RuntimeError(
                    f"mode={mode} with inline evidence needs a screenshot; "
                    f"none in payload and no .png under {evidence_dir(game)}")
    else:
        seconds = float(payload.get("seconds") or DEFAULT_SECONDS)
        say(f"id={job.get('id')} critique game={game} mode={mode} — "
            f"collecting fresh evidence ({seconds:.0f}s feed)")
        report = run_collector(game, seconds, payload.get("url"))
        if report.get("error"):
            raise RuntimeError(f"evidence feed error for {game}: "
                               f"{report['error']}")
        evidence_path = str(evidence_dir(game) / "latest.json")
        evidence_note = f"{evidence_path} ({report.get('shots')} shots)"
        if mode in ("vision", "full"):
            screenshot = newest_screenshot(game)
            if screenshot is None:
                raise RuntimeError(
                    f"mode={mode} needs a screenshot but the feed wrote no "
                    f".png under {evidence_dir(game)}")
    say(f"id={job.get('id')} evidence: {evidence_note}; screenshot: {screenshot}")

    # 2. the brain, 3. the record
    critique = run_driver(game, mode, evidence_path, screenshot)
    critique.setdefault("game", game)
    out_dir = ROOT / "critiques"
    out_dir.mkdir(parents=True, exist_ok=True)
    critique_path = out_dir / (
        f"{game}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(critique_path, "w", encoding="utf-8") as fh:
        json.dump(critique, fh, indent=2)
    issues = critique.get("issues") or []
    say(f"id={job.get('id')} critique: {len(issues)} issue(s), verdict: "
        f"{str(critique.get('verdict') or critique.get('raw') or '?')[:100]}")

    # 4. the actioning wire — issues become queue work-orders at prio 7
    order_ids, order_error = enqueue_orders(critique)
    if order_error:
        say(f"id={job.get('id')} work-orders NOT filed: {order_error}")
    return {"ok": True, "lane": "critique", "game": game, "mode": mode,
            "verdict": str(critique.get("verdict")
                           or critique.get("raw") or "")[:300],
            "scores": critique.get("scores"),
            "issue_count": len(issues),
            "evidence": evidence_note, "screenshot": screenshot,
            "critique_path": str(critique_path),
            "enqueued_job_ids": order_ids,
            "work_orders_error": order_error,
            "critique": critique}


# ---------------------------------------------------------- the play lanes --
def runner_feed(job: dict, lane: str) -> dict:
    """playtest / emulator — the evidence feed for N seconds. Lappy has no
    AVD and never will; live chromium against the game URL IS the phone."""
    payload = job.get("payload") or {}
    game = payload.get("game")
    if not game:
        raise ValueError(f"{lane} job payload needs 'game'")
    seconds = float(payload.get("seconds") or DEFAULT_SECONDS)
    say(f"id={job.get('id')} {lane} game={game} seconds={seconds:.0f}")
    report = run_collector(game, seconds, payload.get("url"))
    if report.get("error"):
        raise RuntimeError(f"evidence feed error for {game}: "
                           f"{report['error']}")
    if lane == "emulator":
        report["note"] = ("lappy has no AVD — the playwright chromium feed "
                          "is the emulator lane's stand-in (observation only)")
    return {"ok": True, "lane": lane, "game": game, "seconds": seconds,
            "shots": report.get("shots"),
            "survival_seconds": report.get("survival_seconds"),
            "actions_taken": report.get("actions_taken"),
            "events": (report.get("events") or [])[:10],
            "evidence_dir": str(evidence_dir(game)),
            "report": report}


# --------------------------------------------------------- the selfplay lane --
def pick_brain(game: str, explicit: str | None) -> Path:
    """payload brain wins; else the newest runs/<game>-evolve-*.npz (then any
    runs/<game>*.npz). No brain is a setup gap, not a job failure shape."""
    runs = ROOT / "runs"
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = runs / path
        if not path.is_file():
            raise RuntimeError(f"payload brain not found: {path}")
        return path
    if not runs.is_dir():
        raise RuntimeError(f"no runs/ dir at {runs} — brains not deployed")
    for pattern in (f"{game}-evolve-*.npz", f"{game}*.npz"):
        found = sorted(runs.glob(pattern), key=lambda p: p.stat().st_mtime)
        if found:
            return found[-1]
    raise RuntimeError(f"no brain in {runs} matching {game}* — "
                       "scp the runs/ dir (deploy step 2)")


def runner_selfplay(job: dict) -> dict:
    payload = job.get("payload") or {}
    game = payload.get("game")
    if not game:
        raise ValueError("selfplay job payload needs 'game'")
    sessions = int(payload.get("sessions") or DEFAULT_SESSIONS)
    decisions = int(payload.get("decisions") or DEFAULT_DECISIONS)
    boot = float(payload.get("boot_seconds") or DEFAULT_BOOT_SECONDS)
    brain = pick_brain(game, payload.get("brain"))
    url = payload.get("url") or DEFAULT_URL.format(game=game)
    cmd = [VENV_PY, ROOT / "selfplay.py",
           "--game", game,
           "--sessions", str(sessions),
           "--decisions", str(decisions),
           "--brain", str(brain),
           "--url", url,
           "--root", str(ROOT),          # selfplay's default is the server path
           "--scripts-dir", str(ROOT / "scripts"),
           "--out-root", str(ROOT / "data" / f"{game}-evolve"),
           "--merge-to", str(ROOT / "data" / game),
           "--profile", str(ROOT / "data" / "pw-profiles" / f"{game}-selfplay"),
           "--boot-seconds", str(boot)]
    if payload.get("module"):
        cmd += ["--module", str(payload["module"])]
    # worst case: every session pays the full boot wait, every decision pays
    # settle + action overhead; then the merges
    timeout = sessions * (boot + decisions * 6) + 600
    say(f"id={job.get('id')} selfplay game={game} brain={brain.name} "
        f"sessions={sessions} decisions={decisions}")
    proc = _subprocess(cmd, timeout=timeout, cwd=str(ROOT))
    if proc.stopped:
        raise RuntimeError("selfplay interrupted by shutdown")
    if proc.timed_out:
        raise RuntimeError(f"selfplay timed out after {timeout:.0f}s")
    done: dict = {}
    merges: list = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            if row.get("done"):
                done = row
            elif row.get("merge"):
                merges.append(row)
    rows = done.get("rows") or 0
    if proc.returncode != 0 or not done or not rows:
        raise RuntimeError(
            f"selfplay exit {proc.returncode} rows={rows}: "
            f"{proc.tail() or proc.tail(4, 'stdout')}")
    return {"ok": True, "lane": "selfplay", "game": game,
            "brain": str(brain), "brain_name": brain.name,
            "sessions": done.get("sessions"),
            "sessions_ok": done.get("sessions_ok"),
            "rows": rows, "rows_file": done.get("rows_file"),
            "merges": merges[-2:], "stderr_tail": proc.tail(4)}


# -------------------------------------------------------------- the loop --
RUNNERS = {
    "critique": runner_critique,
    "playtest": lambda job: runner_feed(job, "playtest"),
    "emulator": lambda job: runner_feed(job, "emulator"),
    "selfplay": runner_selfplay,
}


def process(job: dict) -> None:
    jid, jtype = job.get("id"), job.get("type")
    runner = RUNNERS.get(jtype)
    if runner is None:
        # the not-wired law: retrying can't wire it — fail it permanently
        post_result(jid, error=f"no local runner for job type {jtype!r}",
                    permanent=True)
        return
    say(f"claimed id={jid} type={jtype} priority={job.get('priority')} "
        f"payload={json.dumps(job.get('payload') or {})[:200]}")
    try:
        result = runner(job)
    except Exception as exc:  # noqa: BLE001 — a failed job reports, never kills
        say(f"id={jid} {jtype} FAILED: {type(exc).__name__}: {str(exc)[:300]}")
        post_result(jid, error=f"{type(exc).__name__}: {exc}")
        return
    post_result(jid, result=result)


def run_loop(max_jobs: int = 0) -> int:
    if not _token():
        say("GATEWAY_TOKEN is not set — refusing to start (fail closed)")
        return 1
    say(f"worker up: gateway={GATEWAY_URL} root={ROOT} "
        f"types={CLAIM_TYPES} poll={POLL_SECONDS:g}s")
    done = 0
    while not _stop.is_set():
        if max_jobs and done >= max_jobs:
            break
        try:
            job = claim_job()
        except Exception as exc:  # noqa: BLE001 — a dead gateway is a sleep
            say(f"claim failed ({type(exc).__name__}: {str(exc)[:200]}) — "
                f"retrying in {POLL_SECONDS:g}s")
            _stop.wait(POLL_SECONDS)
            continue
        if not job:
            _stop.wait(POLL_SECONDS)
            continue
        process(job)
        done += 1
    say(f"worker down after {done} job(s) "
        f"(stop={'signal' if _stop.is_set() else 'max-jobs'})")
    return 0


def _on_signal(_sig, _frame) -> None:
    _stop.set()
    say("shutdown signal — finishing the current step, then exiting")


def main() -> int:
    global POLL_SECONDS
    parser = argparse.ArgumentParser(
        description="PlayerOne lappy worker — claims critique/playtest/"
                    "emulator/selfplay jobs from the retromonkey queue")
    parser.add_argument("--poll", type=float, default=POLL_SECONDS,
                        help=f"seconds between polls (default {POLL_SECONDS:g})")
    parser.add_argument("--max-jobs", type=int, default=0,
                        help="exit after N jobs (0 = run until SIGTERM)")
    parser.add_argument("--selftest", action="store_true",
                        help="offline: mock the queue + subprocesses, run one "
                             "critique + one playtest, show the flow")
    args = parser.parse_args()
    POLL_SECONDS = args.poll
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _on_signal)
    if args.selftest:
        return _selftest()
    return run_loop(args.max_jobs)


# ------------------------------------------------------------ the selftest --
# a real 1x1 png so newest_screenshot() has something to find
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg==")


def _selftest() -> int:
    """One full iteration offline: the claim POST is mocked to hand out a
    fake critique job then a fake playtest job, the subprocesses are mocked
    (fake evidence report, fake critique driver), and ROOT points at a temp
    tree with a fake evidence dir + a fake brain. Proves the dispatch table,
    the brain picker, the work-order wire, and both result POSTs."""
    global ROOT, VENV_PY, GATEWAY_URL, _post_json
    root = Path(tempfile.mkdtemp(prefix="p1_selftest_"))
    ROOT = root
    GATEWAY_URL = "http://selftest.invalid/api"
    os.environ["GATEWAY_TOKEN"] = "selftest-token"

    # the fake deployed tree
    ev = root / "evidence" / "sonar"
    ev.mkdir(parents=True)
    (ev / "000.png").write_bytes(_PNG_1X1)
    (ev / "latest.json").write_text(json.dumps(
        {"game": "sonar", "shots": 12, "seconds": 30,
         "survival_seconds": 30, "actions_taken": 12,
         "note": "selftest fake feed",
         "events": [{"t": 1.0, "note": "load complete in 0.8s"}]},
        indent=2), encoding="utf-8")
    runs = root / "runs"
    runs.mkdir()
    (runs / "sonar-evolve-20260920.npz").write_bytes(b"")
    (root / "venv" / "bin").mkdir(parents=True)
    VENV_PY = root / "venv" / "bin" / "python"

    picked = pick_brain("sonar", None)
    assert picked.name == "sonar-evolve-20260920.npz", picked
    say(f"selftest: brain picker -> {picked.name} (newest evolve export)")

    posts: list[tuple[str, dict]] = []
    fake_jobs = [
        {"id": 901, "type": "critique", "priority": 3, "status": "running",
         "payload": {"game": "sonar", "mode": "full", "seconds": 30}},
        {"id": 902, "type": "playtest", "priority": 5, "status": "running",
         "payload": {"game": "sonar", "seconds": 20}},
    ]

    def fake_post(path: str, body: dict, timeout: float = HTTP_TIMEOUT) -> dict:
        posts.append((path, body))
        if path == "/jobs/claim":
            return {"job": fake_jobs.pop(0) if fake_jobs else None}
        if path == "/jobs":
            return {"id": 5000 + len(posts)}
        return {"id": 999, "status": "done"}

    def fake_subprocess(cmd, timeout, cwd=None, env=None):
        argv = [os.fspath(part) for part in cmd]
        if argv[1].endswith("collect_evidence.py"):
            game = argv[argv.index("--game") + 1]
            return SubResult(0, json.dumps(
                {"game": game, "shots": 12, "seconds": 20,
                 "survival_seconds": 20, "actions_taken": 12,
                 "note": "selftest fake feed",
                 "events": [{"t": 0.8, "note": "load complete in 0.8s"}]}), "")
        if argv[1].endswith("driver.py"):
            with open(argv[2], "w", encoding="utf-8") as fh:
                json.dump({"game": "sonar",
                           "verdict": "core loop holds; the ping feels slow "
                                      "mid-dive and the abyss glow is flat",
                           "scores": {"fun": 6, "polish": 5},
                           "issues": [
                               {"severity": "annoyance",
                                "description": "ping cooldown feels sluggish "
                                               "when diving fast",
                                "suggestion": "try PING_COOLDOWN 0.4 instead "
                                              "of the current value",
                                "files_hint": ["scripts/sonar.gd"]},
                               {"severity": "polish",
                                "description": "the abyss glow could be "
                                               "moodier",
                                "suggestion": "make it pop more",
                                "files_hint": []}]}, fh, indent=2)
            return SubResult(0, "driver: wrote " + argv[2], "")
        raise AssertionError(f"selftest ran an unexpected subprocess: {argv[:3]}")

    _post_json = fake_post
    _subprocess_ref = globals()["_subprocess"]
    globals()["_subprocess"] = fake_subprocess

    say("selftest: running the loop for 2 mocked jobs "
        "(critique id=901, playtest id=902)")
    rc = run_loop(max_jobs=2)
    globals()["_subprocess"] = _subprocess_ref

    # the flow, walked
    paths = [path for path, _ in posts]
    assert paths[0] == "/jobs/claim", paths
    order_posts = [(p, b) for p, b in posts if p == "/jobs"]
    assert len(order_posts) == 1, f"expected 1 work-order post: {paths}"
    order = order_posts[0][1]
    assert order["type"] == "tune_tunable", order
    assert order["payload"]["tunable"] == "PING_COOLDOWN", order
    assert order["payload"]["new_value"] == 0.4, order
    assert order["priority"] == 7, order
    result_crit = [b for p, b in posts
                   if p == "/jobs/901/result" and "result" in b]
    assert len(result_crit) == 1, paths
    crit = result_crit[0]["result"]
    assert crit["ok"] and crit["issue_count"] == 2, crit
    assert crit["enqueued_job_ids"], crit
    assert crit["critique"].get("verdict"), crit
    result_play = [b for p, b in posts
                   if p == "/jobs/902/result" and "result" in b]
    assert len(result_play) == 1, paths
    assert result_play[0]["result"]["shots"] == 12, result_play
    assert not any("error" in b for _, b in posts), posts

    say(f"selftest: posts in order -> {paths}")
    say(f"selftest: critique id=901 -> ok, verdict="
        f"{crit['verdict'][:48]!r}, {crit['issue_count']} issues, "
        f"work-order {order['payload']['tunable']} "
        f"{order['payload']['new_value']} -> queue id "
        f"{crit['enqueued_job_ids'][0]} (vibe-only issue skipped by the "
        f"converter)")
    say(f"selftest: playtest id=902 -> ok, {result_play[0]['result']['shots']} "
        f"shots, evidence at {result_play[0]['result']['evidence_dir']}")
    say("selftest: PASS — dispatch table, brain picker, evidence feed, "
        "critique driver, work-order wire, and both result POSTs verified "
        f"offline (temp tree {root})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
