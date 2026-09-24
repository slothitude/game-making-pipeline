#!/usr/bin/env python3
"""sculmm_world.py — F2: the SCULMM world AUTHOR (brief -> .sculmm.json).

Two paths:
  template  deterministic, LLM-free (tests + fallback law): the brief is
            hashed to a seed and a complete, schema-valid world is
            derived — one shell room with texture slots, two props with
            image_prompts (work orders), one actor, three narration
            lines, beats with a clip performance.
  llm       pluggable endpoint (SCULMM_LLM_BASE_URL / SCULMM_LLM_MODEL /
            key from --key-file or fetched from Lappy ~/.openrouter over
            ssh). STORED-ARTIFACT LAW: prompt+model+seed+raw response are
            saved beside the world as <stem>.provenance.json. The repair
            loop feeds validator errors back <=3 times, then flags.

Usage:
  sculmm_world.py author "<brief>" --out <world.sculmm.json> \
      [--mode template|llm] [--seed N] [--book <book>] [--key-file F]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

SRC = Path(__file__).parent
sys.path.insert(0, str(SRC))

STYLE = ("1960s retro cartoon style, bold flat colors, thick clean "
         "outlines, simple shapes, muted cold palette")
ROOM_HINTS = ("pub", "bar", "inn", "tavern", "lighthouse", "street",
              "hall", "kitchen", "cellar", "deck", "cabin", "shop")
PROP_ARCHETYPES = (
    ("lamp", "a storm lantern, iron and glass, warm weak flame"),
    ("crate", "a salt-stained wooden crate, iron bands, worn slats"),
    ("bottle", "a dark glass bottle, corked, paper label, wet sheen"),
    ("chair", "one wooden chair, worn paint, simple joints"),
    ("radio", "a valve radio, bakelite case, brass dial"),
    ("picture", "a framed portrait, oil paint, tarnished frame"),
)


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "scene"


def brief_seed(brief: str) -> int:
    return int.from_bytes(hashlib.sha256(brief.encode()).digest()[:4], "big")


# ----------------------------------------------------------- template path --
def template_world(brief: str, seed: int, book: str) -> dict:
    """Deterministic complete world from a one-line brief."""
    low = brief.lower()
    room_word = next((w for w in ROOM_HINTS if w in low), "hall")
    room_name = f"{slug(room_word)}_room"
    p0, p1 = PROP_ARCHETYPES[seed % len(PROP_ARCHETYPES)], \
        PROP_ARCHETYPES[(seed // 7) % len(PROP_ARCHETYPES)]
    while p1[0] == p0[0]:
        p1 = PROP_ARCHETYPES[(PROP_ARCHETYPES.index(p1) + 1) % len(PROP_ARCHETYPES)]

    def tex(i: int, what: str) -> dict:
        return {"prompt": f"{what}, {STYLE}", "engine": "hunyuandit",
                "seed": seed + i}

    sentences = [s.strip(" .") + "."
                 for s in re.split(r"(?<=[.!?])\s+", brief.strip()) if s][:3]
    while len(sentences) < 3:
        sentences.append(f"The scene holds. {brief[:60]}")

    return {
        "version": 2,
        "seed": seed,
        "book": book,
        "scene": slug(brief)[:32],
        "world": {
            "rooms": [{
                "name": room_name,
                "backdrop_prompt": f"interior of a {room_word}, {STYLE}",
                "backdrop_engine": "hunyuandit",
                "shell": {
                    "floor": {"size_m": [8.0, 6.0],
                              "texture": tex(1, "worn floorboards, salt stains, dark knots")},
                    "walls": [
                        {"wall": "north", "width_m": 8.0, "height_m": 3.2,
                         "texture": tex(2, f"{room_word} wall, peeling paint, damp patches")},
                        {"wall": "west", "width_m": 6.0, "height_m": 3.2,
                         "texture": tex(3, "side wall, framed notices, hooks and rope")},
                    ],
                },
                "props": [p0[0], p1[0]],
                "floor_marks": [{"x": 0.3, "name": "door_mark"},
                                {"x": -0.4, "name": "window_mark"}],
                "depth_layers": [],
            }],
            "props": [
                {"name": p0[0], "room": room_name, "place": "against_wall:north",
                 "image_prompt": f"{p0[1]}, {STYLE}, product shot",
                 "engine": "pixal3d"},
                {"name": p1[0], "room": room_name,
                 "place": {"left_of": p0[0]},
                 "image_prompt": f"{p1[1]}, {STYLE}, product shot",
                 "engine": "pixal3d"},
            ],
            "actors": [{
                "name": "the_protagonist",
                "asset": {"tier": "C",
                          "clips": ["ardy:walk", "mixamo:nod_yes"]},
            }],
        },
        "beats": [
            {"at": "scene:0", "cues": [
                {"do": "room", "room": room_name},
                {"do": "enter", "actor": "the_protagonist", "from": "door"}]},
            {"at": "line:0", "cues": [
                {"do": "perform", "actor": "the_protagonist",
                 "clip": "ardy:walk", "loop": "once"}]},
            {"at": "line:1", "cues": [
                {"do": "gesture", "actor": "the_protagonist",
                 "anim": "nod_yes", "hold_s": 2.0}]},
            {"at": "line:2", "cues": [
                {"do": "camera", "shot": "wide"},
                {"do": "exit", "actor": "the_protagonist", "to": "edge_r"}]},
        ],
        "audio": {
            "clock": "placeholder",
            "lines": [
                {"id": i, "speaker": "narrator" if i != 1 else "the_protagonist",
                 "text": s, "placeholder_dur_s": round(3.0 + (seed >> i) % 40 / 10, 1)}
                for i, s in enumerate(sentences)
            ],
        },
    }


# --------------------------------------------------------------- LLM path --
SYSTEM_PROMPT = """You are the SCULMM world author. Given a one-line brief, \
output ONE JSON object only (no prose, no markdown fence) that is a valid \
SCULMM v2 script. Shape:
{ "version": 2, "seed": <int>, "book": "<slug>", "scene": "<slug>",
  "world": { "rooms": [ { "name": str, "backdrop_prompt": str<=500 OR \
"backdrop": str, "backdrop_engine": "hunyuandit", "shell": { "floor": \
{ "size_m": [w,d], "texture": { "prompt": str, "engine": "hunyuandit", \
"seed": int } }, "walls": [ { "wall": "north|south|east|west", \
"width_m": num, "height_m": num, "texture": {prompt,engine,seed} } ] }, \
"props": [names], "floor_marks": [ {"x": num, "name": str} ], \
"depth_layers": [] } ], "props": [ { "name": str, "room": str, "place": \
"center"|"against_wall:north|south|east|west"|{"left_of": prop}|\
{"right_of": prop}, "image_prompt": str<=500, "engine": \
"pixal3d|trellis|hunyuandit" } ], "actors": [ { "name": str, "asset": \
{ "tier": "A|B|C", "clips": ["ardy:walk"] } } ] },
  "beats": [ { "at": "scene:0", "cues": [...] }, ... ],
  "audio": { "clock": "placeholder", "lines": [ {"id": int, "speaker": str, \
"text": str, "placeholder_dur_s": num} ] } }
Beat cues use exactly these do-forms: {"do":"room","room":name}, \
{"do":"enter","actor":name,"from":"door"}, {"do":"perform","actor":name,\
"clip":"ardy:walk","loop":"once"}, {"do":"gesture","actor":name,\
"anim":"talk","hold_s":num}, {"do":"camera","shot":"wide|close|medium"}, \
{"do":"exit","actor":name,"to":"edge_l|edge_r"}. No extra fields anywhere. \
Keep every prompt under 500 chars."""


def fetch_key(key_file: Path | None) -> str:
    if key_file and key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    out = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=8", "aaron@192.168.0.33",
         "cat ~/.openrouter"], capture_output=True, text=True, timeout=20)
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    return ""


def llm_world(brief: str, seed: int, base_url: str, model: str,
              key: str, out_path: Path) -> tuple[dict | None, dict]:
    """One authoring call + repair loop (<=3). Provenance stored."""
    prov = {"prompt": brief, "model": model, "base_url": base_url,
            "seed": seed, "rounds": []}
    msg = brief
    for attempt in range(3):
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": msg}],
            "reasoning": {"enabled": False},
            "seed": seed + attempt,
            "temperature": 0.7,
        }).encode()
        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions", data=body,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.load(r)
            raw = resp["choices"][0]["message"]["content"]
        except Exception as e:                      # noqa: BLE001
            prov["rounds"].append({"error": str(e)[:300]})
            break
        prov["rounds"].append({"raw": raw[:20000]})
        txt = raw.strip()
        if txt.startswith("```"):
            txt = re.sub(r"^```[a-z]*\n?|```\s*$", "", txt)
        try:
            world = json.loads(txt)
            world.setdefault("version", 2)
            world.setdefault("seed", seed)
            out_path.write_text(
                json.dumps(world, indent=1, ensure_ascii=False),
                encoding="utf-8")
            rep = _validate(out_path)
            prov["rounds"][-1]["ok"] = rep
            if rep.get("ok"):
                return world, prov
            msg = ("Your JSON failed validation. Errors:\n"
                   + json.dumps(rep.get("errors", []), indent=1)
                   + "\nReturn the corrected FULL JSON only.")
        except json.JSONDecodeError as e:
            prov["rounds"][-1]["error"] = f"not JSON: {e}"
            msg = "Output was not valid JSON. Return the FULL JSON object only."
    return None, prov


def _validate(path: Path) -> dict:
    r = subprocess.run(
        [sys.executable, str(SRC / "sculmm_validate.py"), str(path),
         "--clips", str(SRC / "clips.json")],
        capture_output=True, text=True, timeout=60)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "errors": [{"error": r.stdout[:300]}]}


# ------------------------------------------------------------------- main --
def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("author")
    a.add_argument("brief")
    a.add_argument("--out", required=True)
    a.add_argument("--mode", choices=("template", "llm"), default="template")
    a.add_argument("--seed", type=int, default=None)
    a.add_argument("--book", default="sculmm_world")
    a.add_argument("--key-file", type=Path, default=None)
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    seed = args.seed if args.seed is not None else brief_seed(args.brief)

    if args.mode == "template":
        world = template_world(args.brief, seed, args.book)
        out.write_text(json.dumps(world, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        prov = {"mode": "template", "brief": args.brief, "seed": seed}
        Path(str(out).replace(".json", ".provenance.json")).write_text(
            json.dumps(prov, indent=1), encoding="utf-8")
    else:
        key = fetch_key(args.key_file)
        if not key:
            print("[sculmm_world] no LLM key (key-file/ssh) — falling "
                  "back to template")
            world = template_world(args.brief, seed, args.book)
            out.write_text(json.dumps(world, indent=1, ensure_ascii=False),
                           encoding="utf-8")
            return 0
        base = _env("SCULMM_LLM_BASE_URL",
                    "https://openrouter.ai/api/v1")
        model = _env("SCULMM_LLM_MODEL", "openrouter/free")
        world, prov = llm_world(args.brief, seed, base, model, key, out)
        if world is None:
            print("[sculmm_world] LLM authoring failed (see provenance); "
                  "NOT falling back silently — inspect "
                  + str(out) + ".provenance.json")
            return 1
        Path(str(out).replace(".json", ".provenance.json")).write_text(
            json.dumps(prov, indent=1, ensure_ascii=False), encoding="utf-8")

    rep = _validate(out)
    print(json.dumps({"ok": rep.get("ok", False),
                      "errors": rep.get("errors", []),
                      "out": str(out)}, indent=1))
    return 0 if rep.get("ok") else 1


def _env(name: str, default: str) -> str:
    import os
    return os.environ.get(name, default)


if __name__ == "__main__":
    sys.exit(main())
