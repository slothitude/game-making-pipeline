#!/usr/bin/env python3
"""exec_critique — the ACTIONING wire: run the deployed PlayerOne critic stack
on a game, then convert its issues into queue jobs so critiques get acted on.

  run(job)   job payload: {game, mode: llm|vision|full, evidence?}
             - evidence: inline dict, or a path to an evidence json (optional;
               default is evidence/<game>/latest.json under the PlayerOne root)
             - mode llm/full  -> LLMCritic (ladder: boss -> backup -> gemini
               -> openrouter/free) via the venv python, in a /tmp driver script
               (llm_critic needs numpy + the ladder env, so it runs in the
               PlayerOne venv, not the router's interpreter)
             - mode vision/full -> vision_critic on the newest screenshot under
               evidence/<game>/**
             Returns the critique dict. When the critique has issues, up to 3
             of them are converted (issues_to_jobs) and enqueued at priority 7
             (enqueue) — that last step is the wire the audit found missing.

Server paths (this module runs ON the queue host; the human deploys it there
unchanged): PlayerOne stack at /home/ubuntu/playerone, its venv python at
/home/ubuntu/playerone/venv/bin/python, pipeline at /home/ubuntu/pipeline.

Selftest (no server, no subprocess):
    python exec_critique.py --selftest
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PLAYERONE_ROOT = "/home/ubuntu/playerone"
PLAYERONE_PKG_DIRS = [  # driver import roots, first hit wins
    "/home/ubuntu/playerone/examples/pipeline",  # repo layout
    "/home/ubuntu/playerone",                    # flat deploy layout
]
VENV_PY = "/home/ubuntu/playerone/venv/bin/python"
PIPELINE_ROOT = "/home/ubuntu/pipeline"
DEFAULT_QUEUE_URL = "http://127.0.0.1:8901"
MAX_ACTIONED_ISSUES = 3   # a critique never files more than this
ISSUE_PRIORITY = 7        # background-ish: critique fixes don't jump the queue
LLM_TIMEOUT = 1800        # the ladder degrades 4 deep; give it room
VISION_TIMEOUT = 300

# an issue whose text leans visual gets the T2 lane (art), everything else
# actionable gets T1 (tunables)
ART_WORDS = ("art", "sprite", "texture", "colour", "color", "palette",
             "visual", "icon", "background", "animation", "render", "gradient",
             "contrast", "artwork", "illustration")

# an issue without files_hint is only actionable if its text sounds tunable
TUNABLE_WORDS = ("speed", "gravity", "jump", "spawn", "rate", "timer", "delay",
                 "cooldown", "cost", "health", "damage", "score", "points",
                 "height", "width", "size", "count", "duration", "threshold",
                 "volume", "sensitivity", "acceleration", "friction", "time")

TUNABLE_TOKEN_RE = r"\b[A-Z][A-Z0-9_]{2,}\b"  # FALL_SPEED, TILE_SIZE, ...
VALUE_RE = r"(-?\d+(?:\.\d+)?)"


def say(msg):
    print(f"[exec_critique] {msg}", flush=True)


# ----------------------------------------------------------------- evidence --
def load_evidence(game, payload_evidence=None):
    """-> (evidence_dict, source_note). Inline dict wins, then an explicit
    path, then evidence/<game>/latest.json, else the honest minimal stub."""
    if isinstance(payload_evidence, dict):
        return dict(payload_evidence), "payload (inline dict)"
    if isinstance(payload_evidence, str):
        if not os.path.isfile(payload_evidence):
            raise RuntimeError(f"payload evidence path not found: {payload_evidence}")
        with open(payload_evidence, "r", encoding="utf-8") as fh:
            return json.load(fh), f"payload path {payload_evidence}"

    latest = os.path.join(PLAYERONE_ROOT, "evidence", game, "latest.json")
    if os.path.isfile(latest):
        with open(latest, "r", encoding="utf-8") as fh:
            return json.load(fh), latest
    say(f"no evidence json at {latest} — synthesizing minimal evidence")
    return ({"note": "no playthrough evidence yet"}, "synthesized minimal")


def newest_screenshot(game):
    """Newest *.png under evidence/<game>/** (the emu_play lane writes stamped
    dirs of turn shots), or None."""
    root = os.path.join(PLAYERONE_ROOT, "evidence", game)
    if not os.path.isdir(root):
        return None
    shots = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.lower().endswith(".png"):
                full = os.path.join(dirpath, name)
                shots.append((os.path.getmtime(full), full))
    if not shots:
        return None
    return sorted(shots)[-1][1]


# ------------------------------------------------------------------- driver --
_DRIVER_TEMPLATE = r'''"""GMP critique driver — written to /tmp by exec_critique, run with the
PlayerOne venv python (numpy + requests-free ladder stack live there)."""
import json, os, sys

sys.path[0:0] = list({pkg_dirs})
mode, game, evidence_path, out_path = sys.argv[1:5]
screenshot = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None

with open(evidence_path, "r", encoding="utf-8") as fh:
    evidence = json.load(fh)

# the real const inventory — the critic may ONLY prescribe from this list
try:
    tun = json.load(open("/home/ubuntu/games-src/" + game +
                         "/data/tunables.json", encoding="utf-8"))
    names = sorted(tun.keys()) if isinstance(tun, dict) else []
    if names:
        evidence["available_constants"] = names[:80]
        print("driver: injected " + str(len(names)) +
              " real const names into evidence", flush=True)
except OSError:
    pass

critique = {{"game": game}}
import time as _time

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
            _time.sleep(wait)
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
        nvapi = os.path.expanduser("~/.nvapi")
        with open(nvapi, "r", encoding="utf-8") as fh:
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


def run_critique(game, mode, evidence, screenshot, timeout=None):
    """Run the venv-python driver for `mode`, return the critique dict."""
    timeout = timeout or (LLM_TIMEOUT if mode in ("llm", "full") else VISION_TIMEOUT)
    scratch = tempfile.mkdtemp(prefix="gmp_critique_")
    evidence_path = os.path.join(scratch, "evidence.json")
    out_path = os.path.join(scratch, "critique.json")
    driver_path = os.path.join(scratch, "driver.py")
    with open(evidence_path, "w", encoding="utf-8") as fh:
        json.dump(evidence, fh, indent=2)
    with open(driver_path, "w", encoding="utf-8") as fh:
        fh.write(_DRIVER_TEMPLATE.format(pkg_dirs=repr(PLAYERONE_PKG_DIRS)))

    cmd = [VENV_PY, driver_path, mode, game, evidence_path, out_path]
    cmd.append(screenshot or "")
    env = dict(os.environ)
    # llm_critic/vision_critic discover ladder.json via GMP_ROOT; the code
    # default is a Windows path — point it at the server pipeline
    env.setdefault("GMP_ROOT", PIPELINE_ROOT)
    say(f"mode={mode} -> venv driver ({VENV_PY}), timeout {timeout}s")
    proc = subprocess.run(cmd, cwd=PLAYERONE_ROOT, env=env, timeout=timeout,
                          capture_output=True, text=True)
    say(f"driver exit={proc.returncode} :: {(proc.stdout or '').strip().splitlines()[-1:] or ['(no stdout)']}")

    if not os.path.isfile(out_path):
        tail = ((proc.stderr or "") or (proc.stdout or ""))[-600:].strip()
        raise RuntimeError(
            f"critique driver produced no critique json (exit {proc.returncode}): {tail}")
    with open(out_path, "r", encoding="utf-8") as fh:
        critique = json.load(fh)
    return critique


# ---------------------------------------------------------------- converter --
def _text_of(issue):
    return " ".join(str(issue.get(k) or "") for k in
                    ("description", "suggestion") + tuple(issue.get("files_hint") or []))


def _token_value(issue):
    """-> (tunable_token, value) best-effort. A 'TOKEN 420' pairing wins, then
    the first SCREAMING token anywhere, then the first number in the
    suggestion, then the first number in the description."""
    import re
    description = str(issue.get("description") or "")
    suggestion = str(issue.get("suggestion") or "")
    hint_text = " ".join(str(h) for h in issue.get("files_hint") or [])

    paired = re.search(r"(" + TUNABLE_TOKEN_RE + r")\s*[:=]?\s*" + VALUE_RE,
                       f"{suggestion} {description}")
    if paired:
        raw = paired.group(2)
        return paired.group(1), (float(raw) if "." in raw else int(raw))

    token = None
    for text in (suggestion, description, hint_text):
        match = re.search(TUNABLE_TOKEN_RE, text)
        if match:
            token = match.group(0).strip(".,;:)")
            break

    value = None
    for text in (suggestion, description):
        match = re.search(VALUE_RE, text)
        if match:
            raw = match.group(1)
            value = float(raw) if "." in raw else int(raw)
            break
    return token, value


def _tunable_token(issue):
    return _token_value(issue)[0]


def _suggested_value(issue):
    return _token_value(issue)[1]


def _asset_id_from_hint(issue):
    for hint in issue.get("files_hint") or []:
        stem = str(hint).replace("\\", "/").rsplit("/", 1)[-1]
        stem = stem.rsplit(".", 1)[0].lower()
        slug = "".join(ch if ch.isalnum() else "_" for ch in stem).strip("_")
        if len(slug) >= 3:
            return slug
    return None


def _is_artish(issue):
    text = _text_of(issue).lower()
    return any(word in text for word in ART_WORDS)


def _is_tunable_sounding(issue):
    text = _text_of(issue).lower()
    return bool(_tunable_token(issue)) or any(word in text for word in TUNABLE_WORDS)


def issues_to_jobs(critique: dict) -> list[dict]:
    """Map a critique's actionable issues onto queue jobs (T1/T2 lanes).

    - art-sounding issue            -> {type: generate_art, payload: {game, prompt, asset_id?}}
    - files_hint / tunable-sounding -> {type: tune_tunable, payload: {game, tunable, new_value, ...}}
    - neither (pure vibe)           -> skipped, narrated
    Cap: MAX_ACTIONED_ISSUES jobs per critique. tunable/new_value are
    best-effort extracts; when the issue names neither, they go through as
    null with the full issue text attached — honest, and the T1 lane decides.
    """
    game = critique.get("game")
    if not game:
        say("critique carries no 'game' — cannot file work-orders")
        return []

    jobs, skipped = [], []
    for issue in critique.get("issues") or []:
        if len(jobs) >= MAX_ACTIONED_ISSUES:
            break
        description = str(issue.get("description") or "").strip()
        suggestion = str(issue.get("suggestion") or "").strip()
        files_hint = list(issue.get("files_hint") or [])
        if not (description or suggestion):
            continue
        severity = issue.get("severity") or "polish"
        source = "playerone-critique"

        # 1st law: the critic's own structured directives win (it knows the
        # game; regex-guessing const names produced null tunables 2026-09-23)
        td = issue.get("tunable_directive") or {}
        if td.get("tunable") and td.get("new_value") is not None:
            jobs.append({"type": "tune_tunable", "payload": {
                "game": td.get("game") or game,
                "tunable": td.get("tunable"),
                "new_value": td.get("new_value"),
                "issue": description, "suggestion": suggestion,
                "severity": severity, "source": source,
                "files_hint": files_hint,
            }, "priority": ISSUE_PRIORITY})
            continue
        ad = issue.get("art_directive") or {}
        if ad.get("prompt"):
            jobs.append({"type": "generate_art", "payload": {
                "game": ad.get("game") or game,
                "prompt": ad.get("prompt"),
                "asset_id": ad.get("asset_id"),
                "issue": description, "severity": severity, "source": source,
            }, "priority": ISSUE_PRIORITY})
            continue

        if _is_artish(issue):
            payload = {"game": game, "prompt": suggestion or description,
                       "severity": severity, "source": source}
            asset_id = _asset_id_from_hint(issue)
            if asset_id:
                payload["asset_id"] = asset_id
            jobs.append({"type": "generate_art", "payload": payload,
                         "priority": ISSUE_PRIORITY})
            continue

        if files_hint or _is_tunable_sounding(issue):
            tunable = _tunable_token(issue)
            if not tunable:
                # a null-tunable order is a guaranteed executor failure —
                # skip it honestly instead of queueing a dead job
                skipped.append(f"(no const name extractable) {description[:60]}")
                continue
            jobs.append({"type": "tune_tunable", "payload": {
                "game": game,
                "tunable": _tunable_token(issue),
                "new_value": _suggested_value(issue),
                "issue": description,
                "suggestion": suggestion,
                "files_hint": files_hint,
                "severity": severity,
                "source": source,
            }, "priority": ISSUE_PRIORITY})
            continue

        skipped.append(description[:80])

    for note in skipped:
        say(f"issue not actionable (no files_hint, no tunable wording): {note}")
    return jobs


def enqueue(critic_result, queue_url, token):
    """POST issues_to_jobs(critic_result) to the queue. Returns the job ids."""
    ids = []
    for job in issues_to_jobs(critic_result):
        req = urllib.request.Request(
            queue_url.rstrip("/") + "/jobs",
            data=json.dumps(job).encode("utf-8"),
            method="POST",
            headers={"X-Token": token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            new_id = json.loads(resp.read().decode("utf-8")).get("id")
        ids.append(new_id)
        say(f"enqueued {job['type']} priority={job['priority']} -> id={new_id}")
    return ids


# ---------------------------------------------------------------------- run --
def run(job: dict) -> dict:
    payload = job.get("payload") or {}
    game = payload.get("game")
    if not game:
        raise ValueError("critique job payload needs 'game'")
    mode = payload.get("mode") or "llm"
    if mode not in ("llm", "vision", "full"):
        raise ValueError(f"critique mode must be llm|vision|full, got {mode!r}")

    say(f"id={job.get('id')} critique game={game} mode={mode}")
    evidence, source = load_evidence(game, payload.get("evidence"))
    say(f"evidence: {source} ({len(evidence)} keys)")

    screenshot = None
    if mode in ("vision", "full"):
        screenshot = newest_screenshot(game)
        if screenshot is None:
            raise RuntimeError(
                f"mode={mode} needs a screenshot but evidence/<game>/** has no "
                f".png — checked {os.path.join(PLAYERONE_ROOT, 'evidence', game)} "
                f"(run the emulator lane first)")
        say(f"screenshot: {screenshot}")

    critique = run_critique(game, mode, evidence, screenshot)
    critique.setdefault("game", game)
    issues = critique.get("issues") or []
    say(f"critique: {len(issues)} issue(s), verdict: "
        f"{str(critique.get('verdict') or critique.get('raw') or '?')[:100]}")

    if issues:
        token = os.environ.get("QUEUE_TOKEN", "").strip()
        if token:
            queue_url = os.environ.get("QUEUE_API", DEFAULT_QUEUE_URL)
            critique["enqueued_job_ids"] = enqueue(critique, queue_url, token)
        else:
            say("QUEUE_TOKEN not set — issues NOT enqueued "
                "(critique returned unactioned)")
    else:
        say("critique has no issues — nothing to action")
    return critique


# ------------------------------------------------------------------ selftest --
def _selftest():
    """issues_to_jobs on a canned critique — no server, no subprocess."""
    canned = {
        "game": "moon-ladder",
        "verdict": "fun core loop, but the tide is unfair and the UI is muddy",
        "scores": {"fun": 6, "polish": 4, "readability": 5,
                   "phone_ux": 5, "adhd_friendly": 7},
        "issues": [
            {"severity": "blocker",
             "description": "the tide rises about twice as fast as I can climb — "
                            "death felt arbitrary around rung 30",
             "suggestion": "slow the rise: try FALL_SPEED 420 and TIDE_RATE 0.5 "
                           "before touching level design",
             "files_hint": ["scripts/tide.gd", "consts/tunables.gd"]},
            {"severity": "annoyance",
             "description": "rungs blend into the background wall, I misjudged "
                            "jumps constantly",
             "suggestion": "sunken ship beacon, glowing, game sprite, high contrast "
                           "rung marker",
             "files_hint": ["assets/generated/rung_marker.png"]},
            {"severity": "annoyance",
             "description": "sound cuts out if you tab away and come back",
             "suggestion": "retrigger the music bus on focus return",
             "files_hint": ["scripts/audio.gd"]},
            {"severity": "polish",
             "description": "the game is genuinely charming, title screen could "
                            "use a pulse animation",
             "suggestion": "add a gentle pulse to the title",
             "files_hint": []},
            {"severity": "polish",
             "description": "a pure vibe with no hint at all",
             "suggestion": "make it pop more",
             "files_hint": []},
            {"severity": "polish",
             "description": "fifth actionable — must be dropped by the cap",
             "suggestion": "spawn rate feels stingy",
             "files_hint": ["scripts/spawner.gd"]},
        ],
    }
    say(f"selftest: canned critique, {len(canned['issues'])} issues "
        f"(cap {MAX_ACTIONED_ISSUES})")
    jobs = issues_to_jobs(canned)
    print(json.dumps(jobs, indent=2))
    say(f"selftest: {len(jobs)} job dicts (expect {MAX_ACTIONED_ISSUES}, "
        f"vibe-only issue skipped, 5th actionable dropped by the cap)")

    empty = issues_to_jobs({"verdict": "no game key"})
    assert empty == [], "critique without 'game' must map to no jobs"
    say("selftest: gameless critique -> [] ok")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("usage: python exec_critique.py --selftest")
    sys.exit(2)
