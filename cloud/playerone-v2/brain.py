#!/usr/bin/env python3
"""playerone-v2 brain — the reasoning ladder the player asks when it thinks.

Three tiers, one entry point:

    think(context, question, tier="strategic") -> str

  reflex     LOCAL ONLY, no network. The jevlike scorer (numpy_scorer on the
             exported .npz brain) scores a canned answer bank against the
             context and the top line wins — milliseconds, zero cost, always
             available. No brain / no numpy_scorer -> a keyword law answers
             from the same bank (degrades, never dies).
  strategic  THE LADDER, exactly the pipeline's law (llm_proxy._ladder +
             ladder.json): boss -> backup -> gemini (only when ~/.gemini_key
             exists) -> openrouter/free. First success wins. openrouter/free
             (ladder.json role "worker", pinned by law) is ALWAYS the last
             rung — the guaranteed floor, because it never rate-limits.
  vision     ladder.json role "vision" (the NVIDIA vision call shape from
             generic_player.vision_describe) on the screenshot + context.
             Pass the PNG with png=...; no PNG -> falls through to the
             strategic ladder and says so in the trace.

Each rung: ONE attempt, 30s timeout, ANY error (HTTP 504, timeout, empty
reply, missing key) falls through to the next rung. brain.last_trace()
returns the per-rung record so callers (and the selftest) can show the
degradation chain honestly.

Ladder discovery (same law the critic uses): $LADDER_PATH -> ladder.json
beside this file -> $GMP_ROOT/daily/ladder.json -> the repo checkout's
daily/ladder.json. Missing/corrupt -> code-default NVIDIA rungs.

Keys: NVAPI_KEY env (or ~/.nvapi file, the vision_critic law),
OPENROUTER_KEY / OPENROUTER_API_KEY env, GEMINI from ~/.gemini_key.

Selftest (offline — mocks the HTTP seam, no network):

    python brain.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
NVAPI_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
OR_EXTRA = {"HTTP-Referer": "https://retromonkey.com.au",
            "X-Title": "Slothitude Games"}
RUNG_TIMEOUT = 30          # seconds — the spec'd per-rung budget
DEFAULT_ROLE_MODEL = "z-ai/glm-5.3"   # only if ladder.json is missing entirely
THINK_TOKENS = 120

# -- the reflex answer bank (scored by the jevlike brain, keyword-law fallback) --
ANSWER_BANK = (
    "hold steady and drift away from the motion",
    "move toward the bright zone",
    "tap the menu away and keep playing",
    "the bar is low — play safe and avoid contact",
    "nothing is moving — try a swipe to wake the game",
    "keep the current approach",
)


# ---------------------------------------------------------------- ladder cfg --
def _ladder_path() -> Path | None:
    env = os.environ.get("LADDER_PATH", "").strip()
    if env:
        return Path(env)
    for cand in (HERE / "ladder.json",
                 Path(os.environ.get("GMP_ROOT", "")) / "daily" / "ladder.json",
                 HERE.parent.parent / "daily" / "ladder.json"):
        if str(cand) and cand.is_file():
            return cand
    return None


def load_ladder() -> dict:
    """The curated roles (boss/backup/vision/worker) from ladder.json, with a
    code default so a missing file degrades instead of dying."""
    path = _ladder_path()
    if path:
        try:
            roles = json.loads(path.read_text(encoding="utf-8")).get("roles", {})
        except (OSError, ValueError):
            roles = {}
    else:
        roles = {}
    return {
        "boss": roles.get("boss") or {"model": DEFAULT_ROLE_MODEL,
                                      "url": NVAPI_URL, "key": "nvapi"},
        "backup": roles.get("backup") or {"model": "moonshotai/kimi-k3",
                                          "url": NVAPI_URL, "key": "nvapi"},
        "vision": roles.get("vision") or {"model": "meta/llama-3.2-11b-vision-instruct",
                                          "url": NVAPI_URL, "key": "nvapi"},
        "worker": roles.get("worker") or {"model": "openrouter/free",
                                          "url": OR_URL, "key": "openrouter",
                                          "pinned": True},
        "path": str(path) if path else None,
    }


# ---------------------------------------------------------------------- keys --
def nvapi_key() -> str:
    key = os.environ.get("NVAPI_KEY", "").strip()
    if not key:
        try:
            key = Path(os.path.expanduser("~/.nvapi")).read_text(
                encoding="utf-8").strip()
        except OSError:
            pass
    return key


def openrouter_key() -> str:
    return (os.environ.get("OPENROUTER_KEY", "")
            or os.environ.get("OPENROUTER_API_KEY", "")).strip()


def gemini_key() -> str:
    try:
        return Path(os.path.expanduser("~/.gemini_key")).read_text(
            encoding="utf-8").strip()
    except OSError:
        return ""


# ----------------------------------------------------------------- the wire --
def _call(url: str, payload: dict, key: str, extra: dict | None,
          timeout: int) -> str:
    """One OpenAI-shaped chat POST -> text. The seam the selftest mocks."""
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    headers.update(extra or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    message = (body.get("choices") or [{}])[0].get("message") or {}
    text = str(message.get("content") or message.get("reasoning_content") or "")
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    if not text:
        raise RuntimeError("empty reply")
    return text


def _gemini_call(messages: list[dict], key: str, timeout: int) -> str:
    """The gemini rung speaks Google's shape, not OpenAI's (llm_proxy._gemini)."""
    system, contents = "", []
    for msg in messages:
        content = msg.get("content") or ""
        if msg.get("role") == "system":
            system = content
            continue
        contents.append({"role": "model" if msg.get("role") == "assistant"
                         else "user", "parts": [{"text": content}]})
    payload: dict = {"contents": contents,
                     "generationConfig": {"temperature": 0.7,
                                          "maxOutputTokens": THINK_TOKENS}}
    if system:
        payload["system_instruction"] = {"parts": [{"text": system}]}
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent",
        data=json.dumps(payload).encode(),
        headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    parts = (body.get("candidates") or [{}])[0].get("content", {}) \
        .get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("empty gemini reply")
    return text


# ------------------------------------------------------------ the rung plan --
def ladder_rungs(ladder: dict | None = None) -> list[dict]:
    """boss -> backup -> [gemini if keyed] -> openrouter/free (ALWAYS last).
    The worker rung carries pinned:True — swaps are human decisions."""
    ladder = ladder or load_ladder()
    rungs = [
        {"name": "boss", "kind": "openai", "model": ladder["boss"]["model"],
         "url": ladder["boss"].get("url", NVAPI_URL), "key": nvapi_key()},
        {"name": "backup", "kind": "openai", "model": ladder["backup"]["model"],
         "url": ladder["backup"].get("url", NVAPI_URL), "key": nvapi_key()},
    ]
    gkey = gemini_key()
    if gkey:
        rungs.append({"name": "gemini", "kind": "gemini",
                      "model": "gemini-2.0-flash", "url": "google", "key": gkey})
    worker = ladder["worker"]
    rungs.append({"name": "openrouter/free", "kind": "openai",
                  "model": worker["model"], "url": worker.get("url", OR_URL),
                  "key": openrouter_key(), "extra": OR_EXTRA,
                  "floor": True})
    return rungs


# ------------------------------------------------------------------- reflex --
def reflex_answer(context: str, question: str, brain_path: Path | None) \
        -> tuple[str, str]:
    """Tier reflex: the jevlike brain scores the answer bank against the
    context (numpy_scorer's probabilities(context, options) law, options ARE
    the canned answers). Without a brain/numpy_scorer: keyword overlap picks
    the line. Returns (answer, how)."""
    how = "keyword"
    probs = None
    if brain_path is not None and Path(brain_path).is_file():
        try:
            for cand in (str(HERE), str(HERE / "scripts")):
                if Path(cand).is_dir() and cand not in sys.path:
                    sys.path.insert(0, cand)
            from numpy_scorer import NumpyScorer      # noqa: PLC0415 — lazy
            if not hasattr(reflex_answer, "_scorer") or \
                    reflex_answer._scorer_path != str(brain_path):
                reflex_answer._scorer = NumpyScorer(str(brain_path))
                reflex_answer._scorer_path = str(brain_path)
            probs = reflex_answer._scorer.probabilities(
                f"{context} | {question}", list(ANSWER_BANK))
            how = "jevlike"
        except Exception as exc:  # noqa: BLE001 — reflex never networked, never dies
            how = f"keyword (scorer: {type(exc).__name__})"
    if probs and len(probs) == len(ANSWER_BANK):
        return ANSWER_BANK[max(range(len(probs)),
                               key=lambda i: probs[i])], how
    words = set(re.findall(r"[a-z]+", (context + " " + question).lower()))
    def score(line: str) -> float:
        tokens = set(re.findall(r"[a-z]+", line))
        return len(tokens & words)
    return max(ANSWER_BANK, key=score), how


# -------------------------------------------------------------------- think --
_TRACE: list[dict] = []


def last_trace() -> list[dict]:
    """The per-rung record of the most recent strategic/vision think()."""
    return _TRACE


def think(context: str, question: str, tier: str = "strategic",
          png: bytes | None = None, brain_path: Path | None = None,
          ladder: dict | None = None) -> str:
    """Ask the brain. Returns the answer text, or "" when even the floor
    rung had no key. The per-rung story lands in last_trace()."""
    _TRACE.clear()
    t0 = time.perf_counter()
    if tier == "reflex":
        answer, how = reflex_answer(context, question,
                                    brain_path or _default_brain())
        _TRACE.append({"tier": "reflex", "how": how, "ok": bool(answer),
                       "ms": round((time.perf_counter() - t0) * 1000, 1)})
        return answer

    rungs = ladder_rungs(ladder)
    if tier == "vision":
        model = (ladder or load_ladder())["vision"]
        if png is None:
            _TRACE.append({"tier": "vision", "model": model["model"],
                           "ok": False, "error": "no png — falling to strategic"})
        else:
            key = nvapi_key()
            if not key:
                _TRACE.append({"tier": "vision", "model": model["model"],
                               "ok": False, "error": "no NVAPI_KEY"})
            else:
                try:
                    answer = _vision_call(model, png, context, question, key)
                    _TRACE.append({"tier": "vision", "model": model["model"],
                                   "ok": True,
                                   "ms": round((time.perf_counter() - t0) * 1000, 1)})
                    return answer
                except Exception as exc:  # noqa: BLE001 — fall to the ladder
                    _TRACE.append({"tier": "vision", "model": model["model"],
                                   "ok": False,
                                   "error": f"{type(exc).__name__}: "
                                            f"{str(exc)[:120]}"})

    messages = [
        {"role": "system",
         "content": "You are PlayerOne's in-game brain. You get a compact "
                    "perception context of a game screen. Answer the question "
                    "in <=25 words, concrete, no preamble."},
        {"role": "user", "content": f"Game state: {context}\nQuestion: {question}"},
    ]
    for rung in rungs:
        if not rung["key"]:
            _TRACE.append({"tier": tier, "rung": rung["name"],
                           "model": rung["model"], "ok": False,
                           "error": "no key"})
            continue
        try:
            if rung["kind"] == "gemini":
                answer = _gemini_call(messages, rung["key"], RUNG_TIMEOUT)
            else:
                answer = _call(rung["url"],
                               {"model": rung["model"], "messages": messages,
                                "temperature": 0.7,
                                "max_tokens": THINK_TOKENS},
                               rung["key"], rung.get("extra"), RUNG_TIMEOUT)
            _TRACE.append({"tier": tier, "rung": rung["name"],
                           "model": rung["model"], "ok": True,
                           "ms": round((time.perf_counter() - t0) * 1000, 1)})
            return answer
        except Exception as exc:  # noqa: BLE001 — ladder law: next rung
            _TRACE.append({"tier": tier, "rung": rung["name"],
                           "model": rung["model"], "ok": False,
                           "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
    return ""


def _vision_call(model: dict, png: bytes, context: str, question: str,
                 key: str) -> str:
    """The generic_player.vision_describe call shape, ladder.json role."""
    import base64
    b64 = base64.b64encode(png).decode("ascii")
    payload = {"model": model["model"], "max_tokens": THINK_TOKENS,
               "temperature": 0.2, "top_p": 1,
               "messages": [{"role": "user", "content": [
                   {"type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + b64}},
                   {"type": "text",
                    "text": f"Game state: {context}\nQuestion: {question}"},
               ]}]}
    return _call(model.get("url", NVAPI_URL), payload, key, None, RUNG_TIMEOUT)


def _default_brain() -> Path | None:
    """Newest runs/*.npz beside this module (the pick_brain law from
    worker.py, trimmed to what a single process needs)."""
    runs = HERE / "runs"
    if not runs.is_dir():
        return None
    found = sorted(runs.glob("*.npz"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


# ---------------------------------------------------------------- selftest --
class _Mock504(urllib.error.HTTPError):
    def __init__(self, url: str):
        super().__init__(url, 504, "Gateway Timeout", hdrs=None, fp=None)


def _selftest() -> int:
    """Offline: mock _call/_gemini_call — nemotron 504 -> backup 504 ->
    openrouter/free answers; then a first-rung happy path; then reflex.
    Both seams are mocked, so a ~/.gemini_key on the host can't leak this
    test onto the network."""
    global _TRACE
    # fake keys: the selftest exercises the ladder LOGIC through the mocked
    # HTTP seam, so keys must exist or every rung is skipped at "no key"
    saved_env = {k: os.environ.get(k) for k in
                 ("NVAPI_KEY", "OPENROUTER_KEY", "OPENROUTER_API_KEY")}
    os.environ["NVAPI_KEY"] = "selftest-nv"
    os.environ["OPENROUTER_KEY"] = "selftest-or"
    os.environ.pop("OPENROUTER_API_KEY", None)
    ladder = load_ladder()
    print(f"ladder.json : {ladder['path'] or 'MISSING -> code defaults'}")
    print(f"roles       : boss={ladder['boss']['model']} "
          f"backup={ladder['backup']['model']} "
          f"vision={ladder['vision']['model']} "
          f"worker={ladder['worker']['model']} (pinned="
          f"{ladder['worker'].get('pinned', False)})")
    real_call, real_gemini = _call, _gemini_call

    # scenario 1: NVIDIA rungs 504, gemini 504 too, the floor answers
    def flaky_call(url, payload, key, extra, timeout):
        if payload["model"] == ladder["worker"]["model"]:
            return "yes — the bar is at 30%, switch to defensive play"
        raise _Mock504(url)          # boss AND backup: NVIDIA 504s

    def flaky_gemini(messages, key, timeout):
        raise _Mock504("gemini")

    globals()["_call"] = flaky_call
    globals()["_gemini_call"] = flaky_gemini
    answer = think("motion:strong-left | health:30% | menu:no | frame:42",
                   "should I change my approach?", tier="strategic",
                   ladder=ladder)
    trace = last_trace()
    print("scenario 1  : boss/backup (and gemini if keyed) all 504 -> "
          "openrouter/free answers")
    for step in trace:
        print(f"    rung {step['rung']:<16} model={step.get('model', '-'):<45}"
              f" ok={step['ok']} "
              f"{step.get('error', '') if not step['ok'] else step.get('ms', '')}")
    assert answer == "yes — the bar is at 30%, switch to defensive play", answer
    assert [s["ok"] for s in trace] == [False] * (len(trace) - 1) + [True], trace
    assert trace[-1]["rung"] == "openrouter/free" and trace[-1]["ok"], trace

    # scenario 2: first rung healthy -> first success wins
    def happy_call(url, payload, key, extra, timeout):
        assert payload["model"] == ladder["boss"]["model"], payload["model"]
        return "keep going — motion is moving away from you"

    globals()["_call"] = happy_call
    answer = think("motion:strong-right | health:80% | menu:no | frame:7",
                   "should I change my approach?", tier="strategic",
                   ladder=ladder)
    trace = last_trace()
    print(f"scenario 2  : boss answers first try -> {answer!r} "
          f"({len(trace)} rung tried)")
    assert len(trace) == 1 and trace[0]["rung"] == "boss", trace

    # scenario 3: everything down INCLUDING the floor (no OR key) -> ""
    globals()["_call"] = flaky_call
    os.environ.pop("OPENROUTER_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY", None)
    ladder_nofloor = dict(ladder)
    answer = think("motion:none | health:5% | frame:99", "what now?",
                   tier="strategic", ladder=ladder_nofloor)
    trace = last_trace()
    print(f"scenario 3  : all rungs 504 + no openrouter key -> {answer!r} "
          f"({len(trace)} rungs tried, honest empty answer)")
    assert answer == "" and len(trace) == 3, trace

    globals()["_call"] = real_call
    globals()["_gemini_call"] = real_gemini
    _TRACE = []
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    answer = think("motion:none | health:5% | game_over:no | frame:99",
                   "I died. What went wrong?", tier="reflex",
                   brain_path=None)
    how = last_trace()[0]["how"]
    print(f"scenario 4  : reflex tier (local, no network) -> {answer!r} "
          f"[{how}]")
    assert answer in ANSWER_BANK, answer

    print("selftest: PASS — ladder fallback (504 -> 504 -> openrouter/free), "
          "first-success-wins, honest exhaustion, and the local reflex tier "
          "all verified offline")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="playerone-v2 brain — the "
                                 "reflex/strategic/vision reasoning ladder")
    ap.add_argument("--selftest", action="store_true",
                    help="mocked ladder, offline: prove the tier fallback "
                         "504 -> 504 -> openrouter/free")
    ap.add_argument("--context", default="motion:none | menu:no | frame:0")
    ap.add_argument("--question", default="what should I do next?")
    ap.add_argument("--tier", choices=("reflex", "strategic", "vision"),
                    default="strategic")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    answer = think(args.context, args.question, tier=args.tier)
    for step in last_trace():
        print(f"    {step}", flush=True)
    print(answer or "(no rung answered — check the trace above)")
    return 0 if answer else 1


if __name__ == "__main__":
    sys.exit(main())
