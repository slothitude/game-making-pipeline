#!/usr/bin/env python3
"""exec_critique — the ACTIONING wire: run the deployed PlayerOne critic stack
on a game, then convert its issues into queue jobs so critiques get acted on.

  run(job)   job payload: {game, mode: llm|vision|full, evidence?,
                           collect?, seconds?, url?}
             - evidence: inline dict, or a path to an evidence json (optional;
               default is evidence/<game>/latest.json under the PlayerOne root)
             - mode llm/full  -> LLMCritic (ladder: boss -> backup -> gemini
               -> openrouter/free) via the venv python, in a /tmp driver script
               (llm_critic needs numpy + the ladder env, so it runs in the
               PlayerOne venv, not the router's interpreter)
             - mode vision/full -> vision_critic on the newest screenshot under
               evidence/<game>/** — and when there is no screenshot yet, the
               browser leg (collect_evidence) runs first: it drives headless
               chromium at the live game page ON LAPPY over the tailnet (this
               956MB box can't hold a browser), scp's the screenshot tar back,
               and drops it into evidence/<game>/ where the critic reads it.
               payload collect=true forces a fresh leg even when old shots sit
               on disk; the local playwright lane is the flag-gated fallback
               (GMP_CRITIQUE_LOCAL_BROWSER, default on) and usually fails
               cleanly — no browser, no RAM.
             Returns the critique dict. When the critique has issues, up to 3
             of them are converted (issues_to_jobs) and enqueued at priority 7
             (enqueue) — that last step is the wire the audit found missing.
             Art issues may only aim at a REAL asset slot (the spec's `art`
             list + assets/generated); no honest slot -> the art job is
             skipped, never sent with an invented asset_id.

Server paths (this module runs ON the queue host; the human deploys it there
unchanged): PlayerOne stack at /home/ubuntu/playerone, its venv python at
/home/ubuntu/playerone/venv/bin/python, pipeline at /home/ubuntu/pipeline,
game sources at /home/ubuntu/games-src, Lappy runner at
/home/aaron/critique-browser/run_evidence.py (see LAPPY_* below).

Selftest (no server, no subprocess, no network):
    python exec_critique.py --selftest
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
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
GAME_ROOTS = os.environ.get("GMP_GAME_ROOTS", "/home/ubuntu/games-src")
DEFAULT_QUEUE_URL = "http://127.0.0.1:8901"
MAX_ACTIONED_ISSUES = 3   # a critique never files more than this
ISSUE_PRIORITY = 7        # background-ish: critique fixes don't jump the queue
LLM_TIMEOUT = 1800        # the ladder degrades 4 deep; give it room
VISION_TIMEOUT = 300

# ---- the browser leg lives on Lappy (FIX: this box can't run chromium) -----
# retromonkey has 956MB RAM and no playwright browser; Lappy has both, and the
# rsync/ssh-over-tailnet pattern is already proven in queue/lappy_godot_wrapper.
LAPPY_HOST = "aaron@100.123.86.14"       # tailnet IP
LAPPY_KEY = os.path.expanduser("~/.ssh/gmp_lappy")
LAPPY_PY = "/home/aaron/playerone/venv/bin/python"   # playwright lives there
LAPPY_RUNNER = "/home/aaron/critique-browser/run_evidence.py"
LAPPY_JOBS = "/home/aaron/gmp-jobs"                  # job scratch (root disk)
DEFAULT_GAME_URL = "https://retromonkey.com.au/games/{game}/"
EVIDENCE_SECONDS = 60     # watch window (job payload `seconds` overrides)
EVIDENCE_CADENCE = 1.5    # seconds between screenshots
EVIDENCE_KEEP = 12        # ring size — cap the tar, newest wins
EVIDENCE_TRANSFER_TIMEOUT = 300  # scp over the WAN tailnet path
# flag-gated fallback: if the Lappy leg dies, the old local playwright lane
# gets one swing (it has no browser here, so it usually fails cleanly).
LOCAL_BROWSER_FALLBACK = os.environ.get("GMP_CRITIQUE_LOCAL_BROWSER", "1") != "0"
SSH_OPTS = ["-i", LAPPY_KEY, "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=10"]

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


# ---------------------------------------------------- evidence: browser leg --
def evidence_spec(game, seconds=None, url=None, cadence=None, keep=None):
    """The JSON job spec the Lappy runner speaks (pure — selftestable)."""
    return {
        "game": game,
        "url": url or DEFAULT_GAME_URL.format(game=game),
        "seconds": float(seconds or EVIDENCE_SECONDS),
        "shot_cadence": float(cadence or EVIDENCE_CADENCE),
        "keep_shots": int(keep or EVIDENCE_KEEP),
    }


def _ssh_lappy(remote_cmd, timeout):
    return subprocess.run(["ssh", *SSH_OPTS, LAPPY_HOST, remote_cmd],
                          capture_output=True, text=True, timeout=timeout)


def _scp_lappy(src, dst, timeout=EVIDENCE_TRANSFER_TIMEOUT):
    return subprocess.run(["scp", "-C", *SSH_OPTS, src, dst],
                          capture_output=True, text=True, timeout=timeout)


def collect_evidence_lappy(game, seconds=None, url=None, cadence=None, keep=None):
    """The browser leg, executed on Lappy. Only the screenshots travel: the
    runner shots the live game page there, tars the ring + latest.json, and
    this side untars it into evidence/<game>/ where the critic already reads.
    Returns the report dict (latest.json content)."""
    spec = evidence_spec(game, seconds, url, cadence, keep)
    remote_job = f"{LAPPY_JOBS}/critique-browser-{game}-{int(time.time())}"
    scratch = tempfile.mkdtemp(prefix="gmp_evidence_")
    spec_path = os.path.join(scratch, "spec.json")
    with open(spec_path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, indent=2)

    say(f"browser leg -> {LAPPY_HOST}:{remote_job} "
        f"({spec['seconds']}s @ {spec['shot_cadence']}s, keep {spec['keep_shots']})")
    proc = _ssh_lappy(f"mkdir -p '{remote_job}'", timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"lappy mkdir failed: "
                           f"{((proc.stderr or '') or (proc.stdout or ''))[-300:]}")

    proc = _scp_lappy(spec_path, f"{LAPPY_HOST}:{remote_job}/spec.json", timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"lappy spec scp failed: "
                           f"{((proc.stderr or '') or (proc.stdout or ''))[-300:]}")

    # the runner: one ssh round trip, generous budget for load + shoot + tar
    budget = spec["seconds"] + 420
    proc = _ssh_lappy(f"'{LAPPY_PY}' '{LAPPY_RUNNER}' '{remote_job}/spec.json'",
                      timeout=budget)
    tail = ((proc.stderr or '') or (proc.stdout or ''))[-400:].strip()
    if proc.returncode != 0:
        raise RuntimeError(f"lappy evidence runner exit {proc.returncode}: {tail}")
    say(f"runner: {tail.splitlines()[-1:] or ['(silent)']}")

    tar_local = os.path.join(scratch, "evidence.tar.gz")
    proc = _scp_lappy(f"{LAPPY_HOST}:{remote_job}/evidence.tar.gz", tar_local)
    if proc.returncode != 0:
        raise RuntimeError(f"lappy tar scp failed: "
                           f"{((proc.stderr or '') or (proc.stdout or ''))[-300:]}")

    out_dir = os.path.join(PLAYERONE_ROOT, "evidence", game)
    os.makedirs(out_dir, exist_ok=True)
    with tarfile.open(tar_local, "r:gz") as tar:
        for member in tar.getmembers():   # the contract is a flat tar; refuse
            if os.path.basename(member.name) != member.name:  # anything else
                raise RuntimeError(f"lappy tar is not flat: {member.name!r}")
        tar.extractall(out_dir)

    report = {}
    report_path = os.path.join(out_dir, "latest.json")
    if os.path.isfile(report_path):
        with open(report_path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    say(f"browser leg done: {report.get('shots', '?')} shot(s) -> {out_dir}"
        + (f", error={report['error']}" if report.get("error") else ""))
    return report


def collect_evidence_local(game, seconds=None, url=None):
    """The old lane: playwright straight on this box. No browser and 365MB of
    RAM means this usually fails — cleanly, loudly, and never silently."""
    script = os.path.join(PLAYERONE_ROOT, "collect_evidence.py")
    if not os.path.isfile(script):
        raise RuntimeError(f"no local collect_evidence at {script}")
    seconds = float(seconds or EVIDENCE_SECONDS)
    cmd = [VENV_PY, script, "--game", game, "--seconds", str(seconds)]
    if url:
        cmd += ["--url", url]
    say(f"local browser leg -> {script} ({seconds}s)")
    proc = subprocess.run(cmd, cwd=PLAYERONE_ROOT, capture_output=True,
                          text=True, timeout=seconds + 240)
    tail = ((proc.stderr or "") or (proc.stdout or ""))[-400:].strip()
    if proc.returncode != 0:
        raise RuntimeError(f"collect_evidence failed (exit {proc.returncode}): {tail}")
    try:
        return json.loads(proc.stdout or "{}")
    except ValueError:
        return {"shots": 0, "note": "local leg ran but printed no report json"}


def collect_evidence(game, seconds=None, url=None, cadence=None, keep=None):
    """Screenshots of the live game, however we can get them: Lappy first
    (chromium lives there), then the flag-gated local lane."""
    try:
        return collect_evidence_lappy(game, seconds, url, cadence, keep)
    except Exception as exc:  # noqa: BLE001 — the fallback decides, not us
        say(f"lappy browser leg failed: {str(exc)[:250]}")
        if not LOCAL_BROWSER_FALLBACK:
            raise
        say("trying the local playwright lane (GMP_CRITIQUE_LOCAL_BROWSER=1)")
        try:
            return collect_evidence_local(game, seconds, url)
        except Exception as local_exc:  # noqa: BLE001
            raise RuntimeError(
                "no browser anywhere — "
                f"lappy: {str(exc)[:180]} | local: {str(local_exc)[:180]}") from local_exc


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


# ------------------------------------------------------------ art registry --
# FIX (jobs 72/73): critique prose went out as an art prompt with asset_id
# "index" and died in art_work_order. Law now: a critique may only aim a
# render at a REAL slot — the spec's `art` list plus whatever already landed
# in assets/generated — and the prompt must read like an art prompt.
ART_STYLE_FALLBACK = "chunky game sprite, bold outline, readable at small size"


def _spec_jsons(game):
    spec_dir = os.path.join(GAME_ROOTS, game, "spec")
    if not os.path.isdir(spec_dir):
        return []
    return [os.path.join(spec_dir, name) for name in sorted(os.listdir(spec_dir))
            if name.endswith(".json")]


def _spec_data(game):
    for path in _spec_jsons(game):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                yield json.load(fh)
        except (OSError, ValueError):
            continue


def art_registry(game):
    """-> sorted list of real asset slots for `game` (spec `art` list + the
    generated pngs on disk). Empty means no registry: nothing to aim at."""
    slots = []
    for data in _spec_data(game):
        art = data.get("art") if isinstance(data, dict) else None
        if isinstance(art, list):
            slots += [str(a).strip() for a in art if str(a).strip()]
    gen = os.path.join(GAME_ROOTS, game, "assets", "generated")
    if os.path.isdir(gen):
        for name in sorted(os.listdir(gen)):
            stem, ext = os.path.splitext(name)
            if ext.lower() == ".png" and stem:
                slots.append(stem)
    return sorted(set(slots))


def art_style(game):
    """The spec's own art_direction prose — the style suffix on every prompt."""
    for data in _spec_data(game):
        if isinstance(data, dict) and str(data.get("art_direction") or "").strip():
            return str(data["art_direction"]).strip().rstrip(".")
    return ART_STYLE_FALLBACK


def art_prompt(issue, game):
    """suggestion -> art prompt: the critic's fix idea + the game's own style
    law, so the render lane gets direction instead of prose about code."""
    base = (str(issue.get("suggestion") or "").strip()
            or str(issue.get("description") or "").strip()).rstrip(". ")
    return f"{base}, {art_style(game)}, game asset sprite"


def _slot_tokens(slot):
    return [t for t in re.split(r"[^a-z0-9]+", slot.lower()) if len(t) >= 3]


def match_asset_slot(issue, registry):
    """Which real slot is this issue talking about? The slot named outright
    (as its id or as a phrase) wins, then the most complete token match —
    "wreck beacon" must beat tile_water_dark on a tie of raw hits. None = no
    honest answer, and a render aimed at nothing is garbage."""
    if not registry:
        return None
    text = " " + " ".join(w for w in re.split(r"[^a-z0-9]+",
                                              _text_of(issue).lower()) if w) + " "
    for slot in registry:                                   # 1) named outright
        if slot.lower() in text or " ".join(_slot_tokens(slot)) in text:
            return slot
    best, best_key = None, (0, 0.0)
    for slot in registry:                   # 2) most complete token coverage
        toks = _slot_tokens(slot)
        if len(toks) == 1:      # single-token slots need the exact name above
            continue
        hits = sum(1 for t in toks
                   if re.search(r"\b" + re.escape(t) + r"\b", text))
        key = (hits, hits / len(toks))
        if key > best_key:
            best, best_key = slot, key
    return best if best_key[0] >= 2 else None


# ---------------------------------------------------------------- converter --
def _text_of(issue):
    """description + suggestion + the files_hint strings themselves (the old
    version looked the hints up as KEYS, so their text never reached the
    art/tunable word tests — and slot matching needs them)."""
    return " ".join(
        [str(issue.get(k) or "") for k in ("description", "suggestion")]
        + [str(h) for h in issue.get("files_hint") or []])


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

    - art-sounding issue            -> {type: generate_art, payload: {game, prompt, asset_id}}
      but ONLY when a real asset slot answers for it (the game's spec `art`
      list + assets/generated); the prompt is the suggestion re-aimed as an
      art prompt with the spec's own art_direction as the style suffix. No
      honest slot -> the art order is skipped, never sent as garbage.
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

    registry = []       # computed once, on the first art issue

    def _registry():
        nonlocal registry
        if not registry:
            registry = art_registry(game)
            say(f"art registry for {game}: {registry or '(empty — art orders skip)'}")
        return registry

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
            # the critic invents slot names ("index", "player_ship_hi_contrast")
            # — only a slot the game actually has may go through
            slot = ad.get("asset_id")
            if slot not in _registry():
                slot = match_asset_slot(issue, _registry())
            if not slot:
                skipped.append(f"(art prompt, but no real slot in {game}'s "
                               f"registry) {description[:60]}")
                continue
            jobs.append({"type": "generate_art", "payload": {
                "game": ad.get("game") or game,
                "prompt": ad.get("prompt"),
                "asset_id": slot,
                "issue": description, "severity": severity, "source": source,
            }, "priority": ISSUE_PRIORITY})
            continue

        if _is_artish(issue):
            slot = _asset_id_from_hint(issue)
            if slot not in _registry():     # kills hint slugs like "index"
                slot = match_asset_slot(issue, _registry())
            if not slot:
                skipped.append(f"(art-sounding, but no real slot in {game}'s "
                               f"registry) {description[:60]}")
                continue
            jobs.append({"type": "generate_art", "payload": {
                "game": game, "prompt": art_prompt(issue, game),
                "asset_id": slot,
                "severity": severity, "source": source,
            }, "priority": ISSUE_PRIORITY})
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

        skipped.append(f"(pure vibe) {description[:80]}")

    for note in skipped:
        say(f"issue not actioned {note}")
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

    screenshot = None
    if mode in ("vision", "full"):
        screenshot = newest_screenshot(game)
        if screenshot is None or payload.get("collect"):
            # the browser leg: headless chromium ON LAPPY (this box can't),
            # screenshots + console events tar'd back into evidence/<game>/
            collect_evidence(game, seconds=payload.get("seconds"),
                             url=payload.get("url"),
                             cadence=payload.get("shot_cadence"),
                             keep=payload.get("keep_shots"))
            screenshot = newest_screenshot(game)
        if screenshot is None:
            raise RuntimeError(
                f"mode={mode} needs a screenshot but evidence/<game>/** has no "
                f".png — checked {os.path.join(PLAYERONE_ROOT, 'evidence', game)} "
                f"(the browser leg wrote nothing)")
        say(f"screenshot: {screenshot}")

    evidence, source = load_evidence(game, payload.get("evidence"))
    say(f"evidence: {source} ({len(evidence)} keys)")

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
    """issues_to_jobs + the Lappy spec builder — no server, no subprocess,
    no network. The art registry reads a throwaway fixture game root."""
    global GAME_ROOTS

    # ---- fixture game root: a spec with an `art` list + one generated png
    fixture_root = tempfile.mkdtemp(prefix="gmp_critique_selftest_")
    game = "moon-ladder"
    spec_dir = os.path.join(fixture_root, game, "spec")
    gen_dir = os.path.join(fixture_root, game, "assets", "generated")
    os.makedirs(spec_dir)
    os.makedirs(gen_dir)
    with open(os.path.join(spec_dir, "jam_spec.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"art": ["rung_marker", "wreck_beacon", "sub_player",
                           "tile_water_dark"],
                   "art_direction": "sunken ship cartoon, glowing beacon "
                                    "neon, bold outlines"}, fh, indent=2)
    open(os.path.join(gen_dir, "rung_marker.png"), "wb").close()
    GAME_ROOTS = fixture_root

    # ---- registry + slot matching laws
    reg = art_registry(game)
    assert reg == ["rung_marker", "sub_player", "tile_water_dark",
                   "wreck_beacon"], reg
    say(f"selftest: registry {reg}")
    assert art_style(game).startswith("sunken ship cartoon"), art_style(game)

    hinted = {"description": "beacon unreadable", "files_hint":
              ["assets/generated/wreck_beacon.png"]}
    assert match_asset_slot(hinted, reg) == "wreck_beacon"
    tokeny = {"description": "the ship is invisible against the water",
              "suggestion": "repaint the sub player sprite, bright hull"}
    assert match_asset_slot(tokeny, reg) == "sub_player"
    # the tie law: "wreck beacon" (2/2 tokens, named as a phrase) must beat
    # tile_water_dark (2/3) — the live bug this matcher was rewritten for
    tie = {"description": "the wreck beacon is unreadable against the dark "
                          "water", "suggestion": "repaint it brighter"}
    assert match_asset_slot(tie, reg) == "wreck_beacon", match_asset_slot(tie, reg)
    garbage = {"description": "add a loading indicator with progress",
               "suggestion": "audit what's blocking for 7s",
               "files_hint": ["index.html"]}
    assert match_asset_slot(garbage, reg) is None, "prose must not invent slots"
    assert match_asset_slot(garbage, []) is None
    say("selftest: slot matching — exact > tokens > none ok")

    # ---- full mapping, the jobs 72/73 shapes must never come back
    canned = {
        "game": game,
        "verdict": "fun core loop, but the tide is unfair and the UI is muddy",
        "issues": [
            {"severity": "blocker",
             "description": "the tide rises about twice as fast as I can climb",
             "suggestion": "slow the rise: try FALL_SPEED 420 and TIDE_RATE 0.5",
             "files_hint": ["scripts/tide.gd"]},
            {"severity": "annoyance",
             "description": "rungs blend into the background wall",
             "suggestion": "glowing marker, high contrast",
             "files_hint": ["assets/generated/rung_marker.png"]},
            {"severity": "annoyance",
             "description": "the player avatar is nearly invisible",
             "suggestion": "repaint the ship, bright hull, dark outline",
             "art_directive": {"prompt": "top-down pixel-art player ship "
                                         "sprite, 32x32, high contrast",
                               "asset_id": "player_ship_hi_contrast"}},
            {"severity": "annoyance",
             "description": "no loading feedback for 7s (likely asset decode "
                            "or audio init)",
             "suggestion": "add a loading indicator with progress, defer "
                           "assets until after the first frame renders",
             "files_hint": ["index.html"]},
            {"severity": "annoyance",
             "description": "the wreck beacon art is muddy",
             "art_directive": {"prompt": "wreck beacon, glowing, game sprite",
                               "asset_id": "index"}},
            {"severity": "polish",
             "description": "a pure vibe with no hint at all",
             "suggestion": "make it pop more"},
            {"severity": "annoyance",
             "description": "sound cuts out if you tab away",
             "suggestion": "retrigger the music bus on focus return",
             "files_hint": ["scripts/audio.gd"]},
        ],
    }
    jobs = issues_to_jobs(canned)
    print(json.dumps(jobs, indent=2))
    by_type = {}
    for j in jobs:
        by_type.setdefault(j["type"], []).append(j["payload"].get("asset_id"))
    assert by_type.get("tune_tunable") == [None], by_type
    art_slots = by_type.get("generate_art") or []
    assert art_slots == ["rung_marker", "wreck_beacon"], art_slots
    assert "index" not in art_slots and "player_ship_hi_contrast" not in art_slots
    art_payloads = [j["payload"] for j in jobs if j["type"] == "generate_art"]
    for payload in art_payloads:
        assert payload["prompt"] and "," in payload["prompt"], payload
    # the artish fallback gets the style-suffixed prompt; a structured
    # art_directive prompt passes through as the critic wrote it
    assert art_payloads[0]["prompt"].endswith(
        "game asset sprite"), art_payloads[0]["prompt"]
    assert "sunken ship cartoon" in art_payloads[0]["prompt"]
    assert art_payloads[1]["prompt"] == "wreck beacon, glowing, game sprite"
    assert len(jobs) <= MAX_ACTIONED_ISSUES
    say(f"selftest: {len(jobs)} job(s) — art only at real slots "
        f"{art_slots}, prose-only and invented-slot art skipped")

    # ---- a game with no registry at all: art orders must skip, not fire
    empty_game_root = tempfile.mkdtemp(prefix="gmp_critique_selftest_")
    os.makedirs(os.path.join(empty_game_root, "bare-game"))
    GAME_ROOTS = empty_game_root
    bare = issues_to_jobs({"game": "bare-game", "issues": [
        {"description": "the background is muddy",
         "suggestion": "paint a new background",
         "art_directive": {"prompt": "dark water background", "asset_id": "bg"}}]})
    assert bare == [], bare
    say("selftest: registry-less game -> no art jobs ok")
    GAME_ROOTS = fixture_root

    # ---- the Lappy job spec (pure — no ssh here)
    spec = evidence_spec("sonar", seconds=45, url="https://x.test/g/")
    assert spec == {"game": "sonar", "url": "https://x.test/g/",
                    "seconds": 45.0, "shot_cadence": 1.5, "keep_shots": 12}, spec
    default = evidence_spec("slime-line")
    assert default["url"] == DEFAULT_GAME_URL.format(game="slime-line")
    assert default["seconds"] == EVIDENCE_SECONDS
    say(f"selftest: evidence_spec {default}")

    empty = issues_to_jobs({"verdict": "no game key"})
    assert empty == [], "critique without 'game' must map to no jobs"
    say("selftest: gameless critique -> [] ok")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("usage: python exec_critique.py --selftest")
    sys.exit(2)
