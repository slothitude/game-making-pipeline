"""ladder_refresh — every-7-days LLM ladder refresher for the server.

Reads daily/ladder.json, re-enumerates the live NVIDIA catalog
(GET /v1/models), health-checks each current role model with a tiny live
call, and when a model is gone or erroring promotes the best remaining
candidate for that role, rewriting ladder.json atomically with an
"updated_at" and a "history" trail.

Laws baked in:
  * The brains are the big NVIDIA models. boss/backup/vision are only ever
    filled from the large-parameter reasoning class — never openrouter,
    never the small/fast tiers (quality >> latency; the boss thinks once
    per job).
  * worker is PINNED to openrouter/free. It is health-checked but never
    auto-replaced — worker swaps are human decisions.
  * Gemini stays a failure-catch rung in code; it is not managed here.

Role heuristics (same ranking used by the manual curation round):
  * boss/backup: chat-capable big-class NVIDIA models ranked by parameter
    scale (EXPLICIT_SCALE from the build.nvidia.com cards, else the param
    number in the model id). backup never mirrors the boss pick.
  * vision: known vision/multimodal-capable ids, ranked by scale.
  * Promotions only land after the candidate passes a live health check.

Key discovery: env NVAPI_KEY, else cloud_editor/config.json "nvapi_key",
else the key baked into build/site/index.html (Rog dev fallback).
Worker health check uses env OPENROUTER_KEY (skipped if unset).

Usage:
  python ladder_refresh.py            # refresh, write ladder.json on change
  python ladder_refresh.py --dry-run  # print the plan, touch nothing

Env: NVAPI_KEY (catalog + NVIDIA roles), OPENROUTER_KEY (worker check),
LADDER_PATH (default: <this dir>/ladder.json), GMP_ROOT (key fallbacks).
Stdlib only.
"""
from __future__ import annotations

import sys

# Windows cp1252 console can't print box-drawing chars / emoji below
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
GMP = os.environ.get("GMP_ROOT") or os.path.dirname(HERE)
LADDER_PATH = os.environ.get("LADDER_PATH") or os.path.join(HERE, "ladder.json")
MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SCHEMA = 1
HISTORY_CAP = 50
NVIDIA_ROLES = ("boss", "backup", "vision")   # auto-refreshable brain roles
PINNED_ROLES = ("worker",)                    # health-checked, never replaced

HEALTH_PROMPT = "Reply with the single word: ok."
HEALTH_MAX_TOKENS = 512   # thinking models burn budget before the answer
HEALTH_ATTEMPTS = 2
CATALOG_TIMEOUT = 60
HEALTH_TIMEOUT = 180

# Seed used when ladder.json is missing entirely — the manual curation
# round's winners (2026-09-23 benchmark, build.nvidia.com catalog).
DEFAULT_LADDER = {
    "schema": SCHEMA,
    "roles": {
        "boss":   {"model": "nvidia/nemotron-3-ultra-550b-a55b", "url": CHAT_URL, "key": "nvapi"},
        "backup": {"model": "deepseek-ai/deepseek-v4.1-flash",   "url": CHAT_URL, "key": "nvapi"},
        "vision": {"model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
                   "url": CHAT_URL, "key": "nvapi"},
        "worker": {"model": "openrouter/free", "url": OPENROUTER_URL,
                   "key": "openrouter", "pinned": True},
    },
    "history": [],
}

# Parameter scale (billions, total) from the build.nvidia.com model cards
# for the models we verified. Unknown ids fall back to the param number in
# the id itself ("...-550b-..." -> 550), else 0 (ranks last).
EXPLICIT_SCALE = {
    "moonshotai/kimi-k3": 2800.0,                    # 2.8T total / 104B active
    "nvidia/nemotron-3-ultra-550b-a55b": 550.0,      # 550B total / 55B active
    "nvidia/llama-3.1-nemotron-ultra-253b-v1": 253.0,
    "nvidia/nemotron-3-super-120b-a12b": 120.0,
    "meta/llama-3.2-90b-vision-instruct": 90.0,
    "nvidia/llama-3.1-nemotron-70b-instruct": 70.0,
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": 33.0,  # omni multimodal
    "meta/llama-3.2-11b-vision-instruct": 11.0,
}

# Known vision/multimodal-capable chat models (image_url content parts).
VISION_CAPABLE = {
    "meta/llama-3.2-90b-vision-instruct",
    "meta/llama-3.2-11b-vision-instruct",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/vila",
    "nvidia/neva-22b",
    "adept/fuyu-8b",
    "microsoft/kosmos-2",
    "microsoft/phi-3-vision-128k-instruct",
    "google/deplot",
    "moonshotai/kimi-k3",   # multimodal per catalog card (MoonViT-V2)
}

# Never promote these into a brain role: embeddings, guards, reward models,
# parsers, OCR/translate, clip, code-only tiers.
BRAIN_BLOCKLIST = (
    "embed", "rerank", "retriever", "reward", "guard", "safety", "parse",
    "clip", "riva", "detector", "starcoder", "codellama", "codegemma",
    "code-instruct", "diffusion", "arctic",
)


def scale_of(model_id: str) -> float:
    """Total-parameter estimate in billions: card-verified value first, then
    the param number in the id, else 0 (unknown — ranks last)."""
    if model_id in EXPLICIT_SCALE:
        return EXPLICIT_SCALE[model_id]
    m = re.search(r"(\d+(?:\.\d+)?)b(?=$|[-_])", model_id.lower())
    return float(m.group(1)) if m else 0.0


def is_brain_candidate(model_id: str) -> bool:
    """Big-NVIDIA-class filter for boss/backup: chat-capable, not blocked."""
    low = model_id.lower()
    return not any(tag in low for tag in BRAIN_BLOCKLIST)


def rank_candidates(role: str, live_ids: set, exclude: set) -> list[str]:
    """Role heuristics: brain roles take big-class models by scale,
    vision takes vision-capable models by scale. Highest scale first."""
    if role == "vision":
        pool = [m for m in live_ids if m in VISION_CAPABLE]
    else:
        pool = [m for m in live_ids if is_brain_candidate(m)]
    pool = [m for m in pool if m not in exclude]
    # backup diversity: prefer a different family than the boss when the id
    # suggests one (nemotron/kimi/glm/deepseek/mistral/llama)
    return sorted(pool, key=lambda m: (-scale_of(m), m))


def _nvapi_key() -> str:
    key = os.environ.get("NVAPI_KEY", "").strip()
    if key:
        return key
    try:
        with open(os.path.join(GMP, "cloud_editor", "config.json"),
                  "r", encoding="utf-8") as f:
            key = (json.load(f).get("nvapi_key") or "").strip()
        if key:
            return key
    except (OSError, ValueError):
        pass
    try:  # Rog dev fallback: the key baked into the built site bundle
        with open(os.path.join(GMP, "build", "site", "index.html"),
                  "r", encoding="utf-8") as f:
            m = re.search(r'"(nvapi-[A-Za-z0-9_\-]+)"', f.read())
        return m.group(1) if m else ""
    except OSError:
        return ""


def _post(url: str, payload: dict, key: str, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_catalog(key: str) -> set:
    req = urllib.request.Request(MODELS_URL, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=CATALOG_TIMEOUT) as resp:
        body = json.loads(resp.read().decode())
    return {m["id"] for m in body.get("data", [])}


def health_check(model: str, key: str, url: str = CHAT_URL,
                 timeout: int = 0) -> tuple[bool, float, str]:
    """Tiny live call. Healthy = HTTP 200 with a choices array (content may
    be empty for thinking models — the wire round-trip is the proof of life).
    Two attempts; (ok, latency_s, note)."""
    timeout = timeout or HEALTH_TIMEOUT
    payload = {"model": model, "temperature": 0, "max_tokens": HEALTH_MAX_TOKENS,
               "messages": [{"role": "user", "content": HEALTH_PROMPT}]}
    last = ""
    for attempt in range(HEALTH_ATTEMPTS):
        t0 = time.time()
        try:
            body = _post(url, payload, key, timeout)
            dt = time.time() - t0
            if body.get("choices"):
                return True, round(dt, 1), "ok"
            last = "empty choices"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}: " + exc.read().decode(errors="replace")[:120]
            if exc.code in (401, 402, 404):
                break
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
        if attempt + 1 < HEALTH_ATTEMPTS:
            time.sleep(5)
    return False, 0.0, last


def load_ladder() -> dict:
    try:
        with open(LADDER_PATH, "r", encoding="utf-8") as f:
            ladder = json.load(f)
        if not isinstance(ladder.get("roles"), dict):
            raise ValueError("roles missing")
        return ladder
    except (OSError, ValueError):
        print(f"ladder: {LADDER_PATH} missing/corrupt -- seeding defaults",
              flush=True)
        return json.loads(json.dumps(DEFAULT_LADDER))


def atomic_write_ladder(ladder: dict) -> None:
    """Rewrite ladder.json atomically (tempfile + os.replace, same dir)."""
    ladder["schema"] = SCHEMA
    ladder["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ladder["history"] = ladder.get("history", [])[-HISTORY_CAP:]
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(LADDER_PATH) or ".",
                               prefix="ladder.", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ladder, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, LADDER_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM ladder refresher (every 7 days)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan without writing ladder.json")
    ap.add_argument("--timeout", type=int, default=HEALTH_TIMEOUT,
                    help="per-call health check timeout seconds")
    args = ap.parse_args()

    print(f"ladder_refresh: {LADDER_PATH}"
          + ("  (dry-run -- no writes)" if args.dry_run else ""), flush=True)

    key = _nvapi_key()
    if not key:
        print("FATAL: no NVAPI key (env NVAPI_KEY, cloud_editor/config.json, "
              "or build/site/index.html)", flush=True)
        return 1

    # 1. re-enumerate the live catalog
    try:
        live_ids = fetch_catalog(key)
    except Exception as exc:
        print(f"FATAL: catalog unreachable: {type(exc).__name__}: {str(exc)[:150]}",
              flush=True)
        return 1
    print(f"catalog: {len(live_ids)} models live", flush=True)

    ladder = load_ladder()
    roles = ladder.setdefault("roles", {})
    changes = []
    health_report = {}
    plan = []

    # 2. NVIDIA brain roles: health-check, promote on failure
    taken = set()
    for role in NVIDIA_ROLES:
        entry = roles.get(role) or {}
        model = entry.get("model", "")
        taken.add(model)
        in_catalog = model in live_ids
        ok, latency, note = (False, 0.0, "not in catalog")
        if in_catalog:
            ok, latency, note = health_check(model, key, timeout=args.timeout)
        health_report[role] = {"model": model, "ok": ok,
                               "latency_s": latency, "note": note}
        state = "healthy" if ok else f"DOWN ({note})"
        print(f"  {role}: {model} -> {state}"
              + (f" {latency}s" if ok else ""), flush=True)
        if ok:
            continue
        # promote the best remaining candidate for this role
        exclude = (taken - {model}) | {model}
        ranked = rank_candidates(role, live_ids, exclude)
        promoted = None
        for cand in ranked[:6]:   # live-test the top handful, not the whole catalog
            c_ok, c_lat, c_note = health_check(cand, key, timeout=args.timeout)
            print(f"    candidate {cand}: "
                  + (f"ok {c_lat}s" if c_ok else f"down ({c_note[:80]})"), flush=True)
            if c_ok:
                promoted = (cand, c_lat)
                break
        if promoted:
            new_model, new_lat = promoted
            reason = f"{role} {model} {'missing from catalog' if not in_catalog else 'erroring: ' + note[:80]}"
            plan.append(f"PROMOTE {role}: {model} -> {new_model}")
            changes.append({"role": role, "from": model, "to": new_model,
                            "reason": reason})
            roles[role] = {"model": new_model,
                           "url": entry.get("url") or CHAT_URL,
                           "key": entry.get("key") or "nvapi"}
            health_report[role] = {"model": new_model, "ok": True,
                                   "latency_s": new_lat, "note": "promoted"}
            taken.add(new_model)
        else:
            plan.append(f"LEAVE {role}: {model} (no healthy candidate found)")
            print(f"    no healthy candidate for {role} -- leaving {model}",
                  flush=True)

    # 3. worker: pinned, health-check only (never auto-replace)
    worker = roles.get("worker") or {}
    wkey = os.environ.get("OPENROUTER_KEY", "").strip()
    if wkey and worker.get("url"):
        w_ok, w_lat, w_note = health_check(worker.get("model", "openrouter/free"),
                                           wkey, worker.get("url", OPENROUTER_URL),
                                           timeout=args.timeout)
        health_report["worker"] = {"model": worker.get("model"), "ok": w_ok,
                                   "latency_s": w_lat, "note": w_note,
                                   "pinned": True}
        print(f"  worker (pinned): {worker.get('model')} -> "
              + ("healthy " + str(w_lat) + "s" if w_ok else f"DOWN ({w_note})"),
              flush=True)
        if not w_ok:
            plan.append(f"NOTE worker {worker.get('model')} is erroring -- PINNED, "
                        "not auto-replaced (human decision required)")
    else:
        print("  worker (pinned): no OPENROUTER_KEY -- health check skipped",
              flush=True)
        health_report["worker"] = {"model": worker.get("model"), "ok": None,
                                   "note": "unchecked (no key)", "pinned": True}

    # 4. report + write
    print("plan:", flush=True)
    for line in plan or ["  no changes -- all roles healthy"]:
        print("  " + line, flush=True)

    if args.dry_run:
        print("dry-run: ladder.json NOT written", flush=True)
        return 0
    if changes:
        ladder.setdefault("history", []).append({
            "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "changes": changes,
            "health": health_report,
        })
        atomic_write_ladder(ladder)
        print(f"wrote {LADDER_PATH} ({len(changes)} change"
              + ("s" if len(changes) != 1 else "") + ")", flush=True)
    else:
        # refresh updated_at only when nothing changed? No -- untouched file
        # proves stability; only health snapshot goes to stdout.
        print("no changes -- ladder.json left untouched", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
