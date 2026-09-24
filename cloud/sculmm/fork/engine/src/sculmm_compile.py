"""sculmm_compile.py — SCULMM v2 compiler: validated .sculmm.json ->
compiled .sculmmc.json (absolute seconds + asset manifest + hashes).

Standalone by design: v1's compiler is playmod-coupled (bug-compat with
the LIVE render path — untouched); v2 scenes carry their own beats and
resolve their own clocks:

  clock=readalong   line times from align.py word timings (the v1 law —
                    one clock for audio/film; supplied per line)
  clock=placeholder durations are explicit placeholders (vertical-slice
                    mode; the audio limb lands later and re-compiles)

Asset resolution at compile time (the manifest law): every generator
slot (backdrop, prop, mesh, clip) resolves against the book dir + clip
registry to a concrete path; unresolved -> missing_assets[] (the
missing_assets.md backlog — PIPELINE_3D_SCUMM.md §4). missing assets do
NOT fail the compile; they are the work order.

Determinism law: no wall clock, no unseeded RNG, sorted iteration
everywhere, json.dumps(sort_keys). Regenerating must be byte-identical
or the build halts (v1 law, kept).

Usage:
  python sculmm_compile.py <scene.sculmm.json> [--book <dir>]
                           [--clips <clips.json>] [--out <path>]
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

SRC = Path(__file__).parent
sys.path.insert(0, str(SRC))
from sculmm_schema import (ASSET_NORM, SCULMM_VERSION)  # noqa: E402
from sculmm_validate import (discover_sprite_anims,     # noqa: E402
                             load_clip_registry, validate_script)

CUE_DUR = {          # deterministic cue durations (seconds) where the
    "room": 0.0,     # script gives no explicit timing: fixed, seed-free
    "enter": 1.2,
    "exit": 1.2,
    "walk": 2.5,
    "face": 0.3,
    "gesture": 1.5,
    "prop": 0.8,
    "camera": 0.0,
    "fx": 0.6,
    "music": 0.0,
    "sfx": 0.0,
    "perform": 3.0,  # clip length overrides when the clip carries it
    "layer": 0.0,
    "wait": None,    # takes its `s`
}


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _cue_dur(cue: dict, clip_meta: dict) -> float:
    kind = cue.get("do")
    if kind == "wait":
        return float(cue.get("s", 0.0))
    if kind == "perform":
        return float(clip_meta.get("dur_s", CUE_DUR["perform"]))
    if kind == "gesture":
        return float(cue.get("hold_s", CUE_DUR["gesture"]))
    return CUE_DUR.get(kind, 0.5)


def resolve_assets(script: dict, book: Path, clips: dict) -> tuple:
    """generator slots -> manifest entries; unresolved -> missing list."""
    manifest, missing = {}, []

    def asset(kind: str, ref: str, hint: str) -> None:
        key = f"{kind}:{ref}"
        # 1) clip registry
        if kind == "clip":
            meta = clips.get(ref)
            if meta:
                local = book / meta.get("path", "")
                entry = {
                    "kind": kind, "ref": ref, "path": str(local),
                    "location": meta.get("location", "book"),
                    "dur_s": meta.get("dur_s"), "norm": ASSET_NORM}
                if local.exists():
                    entry["sha256"] = sha(local)      # content-addressed
                manifest[key] = entry
                return
            missing.append({"kind": kind, "ref": ref, "hint": hint})
            return
        # 2) book-relative path — content-addressed when it's a local FILE
        cand = book / ref
        # 2b) absolute lappy paths (asset slots resolve to /mnt/seagate/...):
        # the compiler runs on Rog — record the path, tag lappy, hash only
        # when the file is visible from this machine
        p_abs = Path(ref)
        # "/mnt/..." is lappy-absolute; Windows Path.is_absolute() needs a
        # drive, so test the raw string too
        is_lappy_abs = p_abs.is_absolute() or ref.startswith("/")
        if not cand.exists() and is_lappy_abs and p_abs.suffix:
            entry = {"kind": kind, "ref": ref, "path": ref,
                     "location": "lappy", "norm": ASSET_NORM}
            if p_abs.exists():
                entry["sha256"] = sha(p_abs)
            manifest[key] = entry
            return
        if cand.exists():
            entry = {"kind": kind, "ref": ref, "path": str(cand),
                     "location": "book", "norm": ASSET_NORM}
            if cand.is_file():
                entry["sha256"] = sha(cand)   # dirs (banks) resolve by path
            manifest[key] = entry
            return
        # 3) un-generated slot: backdrop_prompt/model_ref = work order
        missing.append({"kind": kind, "ref": ref, "hint": hint})

    world = script.get("world", {})
    for room in world.get("rooms", []):
        if room.get("backdrop"):
            asset("backdrop", room["backdrop"],
                  f"room {room['name']} backdrop file")
        elif room.get("backdrop_prompt"):
            missing.append({"kind": "backdrop",
                            "ref": f"prompt:{room['name']}",
                            "hint": room["backdrop_prompt"][:120]})
        for lay in room.get("depth_layers", []):
            if lay.get("file"):
                asset("layer", lay["file"],
                      f"room {room['name']} layer {lay['name']}")
        # shell textures: resolved slots enter the manifest (F2 follow-up
        # hook — the stored artifact law; the compiler consumes the path)
        sh = room.get("shell", {})
        slots = [("texture:floor", sh.get("floor", {}).get("texture"))]
        for wll in sh.get("walls", []):
            slots.append((f"texture:wall:{wll.get('wall', '?')}",
                          wll.get("texture")))
        for kind, slot in slots:
            if slot and slot.get("asset"):
                entry = {"kind": kind, "ref": kind,
                         "path": slot["asset"], "location": "lappy",
                         "norm": ASSET_NORM}
                if Path(slot["asset"]).exists():
                    entry["sha256"] = sha(Path(slot["asset"]))
                manifest[f"asset:{kind}"] = entry
    for prop in world.get("props", []):
        if prop.get("asset"):                      # resolved mesh slot
            asset("model", prop["asset"], f"prop {prop['name']}")
        elif prop.get("model_ref"):
            asset("model", prop["model_ref"], f"prop {prop['name']}")
        elif prop.get("image_prompt"):
            missing.append({"kind": "prop_image",
                            "ref": prop["name"],
                            "hint": prop["image_prompt"][:120]})
    for actor in world.get("actors", []):
        a = actor.get("asset", {})
        for field in ("cutout_dir", "mesh", "rig", "art"):
            if a.get(field):
                asset(field, a[field], f"actor {actor['name']} {field}")
        for ref in a.get("clips", []):
            asset("clip", ref, f"actor {actor['name']} clip")
    for beat in script.get("beats", []):
        for cue in (beat.get("cues") or []) + (beat.get("with") or []):
            if cue.get("do") == "perform":
                asset("clip", cue.get("clip", ""), "perform cue")
    return manifest, missing


def compile_scene(script: dict, book: Path, clips: dict,
                  sprite_anims: dict | None = None) -> dict:
    manifest, missing = resolve_assets(script, book, clips)

    # ---- clock: beats -> absolute seconds ------------------------------
    audio = script.get("audio") or {}
    lines = audio.get("lines", [])
    line_at: dict[int, float] = {}
    t = 0.0
    for ln in lines:
        line_at[ln["id"]] = round(t, 3)
        dur = float(ln.get("placeholder_dur_s", 3.0))
        t += dur + 0.35                     # v1 gap law (in-scene)

    beats_out = []
    cursor = 0.0
    for bi, beat in enumerate(script.get("beats", [])):
        at = beat.get("at", "")
        if at.startswith("line:"):
            lid = int(at.split(":")[1].split("@")[0].split("#")[0])
            t0 = line_at.get(lid, cursor)
        elif at.startswith("scene:"):
            t0 = cursor                     # scenes are sequential in v2
        else:
            try:
                t0 = float(at)
            except ValueError:
                t0 = cursor
        cues_out = []
        ct = t0
        for cue in beat.get("cues") or []:
            meta = manifest.get(f"clip:{cue.get('clip')}") or {}
            d = _cue_dur(cue, meta)
            c = dict(sorted(cue.items()))
            c["t0"] = round(ct, 3)
            c["t1"] = round(ct + d, 3)
            cues_out.append(c)
            ct += d
        for cue in beat.get("with") or []:
            meta = manifest.get(f"clip:{cue.get('clip')}") or {}
            c = dict(sorted(cue.items()))
            c["t0"] = round(t0, 3)
            c["t1"] = round(t0 + _cue_dur(cue, meta), 3)
            cues_out.append(c)
        beats_out.append({"at": at, "at_seconds": round(t0, 3),
                          "cues": cues_out, "t0": round(t0, 3),
                          "t1": round(ct, 3)})
        cursor = max(cursor, ct, t0)

    duration = round(max(
        cursor,
        t + (0.9 if beats_out else 0.0),    # v1 render_len law (+tail)
        max((b["t1"] for b in beats_out), default=0.0)), 3)

    hashes = {"script": None, "clips": None}
    return {
        "version": SCULMM_VERSION,
        "book": script.get("book", ""),
        "scene": script.get("scene", "pilot"),
        "seed": script.get("seed", 0),
        "mode": "sculmm2",
        "clock": audio.get("clock", "placeholder"),
        "duration": duration,
        "beats": beats_out,
        "lines": [dict(sorted(ln.items())) for ln in lines],
        "assets": dict(sorted(manifest.items())),
        "missing_assets": sorted(missing, key=lambda m: (
            m["kind"], m["ref"])),
        "hashes": hashes,                    # filled by the writer
    }


def write_cuec(cuec: dict, script_path: Path, clips_path: Path | None,
               out: Path) -> None:
    """hashes are content-addressed AFTER assembly; then serialize
    sort_keys for byte determinism."""
    cuec["hashes"]["script"] = sha(script_path)
    if clips_path and clips_path.exists():
        cuec["hashes"]["clips"] = sha(clips_path)
    out.write_text(json.dumps(cuec, indent=1, sort_keys=True,
                              ensure_ascii=False) + "\n",
                   encoding="utf-8")


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: sculmm_compile.py <scene.sculmm.json>"
              " [--book <dir>] [--clips <clips.json>] [--out <path>]")
        return 2
    script_path = Path(args[0])
    book = Path(args[args.index("--book") + 1]) if "--book" in args \
        else script_path.parent
    clips_path = Path(args[args.index("--clips") + 1]) if "--clips" in \
        args else None
    out = Path(args[args.index("--out") + 1]) if "--out" in args \
        else script_path.parent / "build" / \
        (script_path.stem + ".sculmmc.json")
    out.parent.mkdir(parents=True, exist_ok=True)

    script = json.loads(script_path.read_text(encoding="utf-8"))
    clips = load_clip_registry(clips_path)
    sprite_anims = discover_sprite_anims(book, script.get("world", {}))

    rep = validate_script(script, clips, sprite_anims)
    if not rep.ok():
        print("[sculmmc] REFUSED — script does not validate:")
        print(json.dumps(rep.machine(), indent=1, ensure_ascii=False))
        return 1

    cuec = compile_scene(script, book, clips, sprite_anims)
    write_cuec(cuec, script_path, clips_path, out)
    print(json.dumps({"ok": True, "out": str(out),
                      "duration_s": cuec["duration"],
                      "assets": len(cuec["assets"]),
                      "missing_assets": len(cuec["missing_assets"])},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
