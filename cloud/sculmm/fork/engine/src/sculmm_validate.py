"""sculmm_validate.py — SCULMM v2 validator: JSON Schema gate + semantic
rules, every rejection carrying a suggested fix (repair-loop contract,
design §6; ≤3 LLM retries upstream).

Layers:
  L1  jsonschema    structural + closed fields (additionalProperties
                    False everywhere — a freeform field cannot validate)
  L2  semantic      registry refs (rooms/actors/props/clips), v1 rule
                    engine reused beat-by-beat (imported read-only),
                    v2 additions: clip-ref closure, tier-asset sanity,
                    gen-slot XOR (image_prompt XOR model_ref),
                    audio-clock placeholder discipline.

Usage:
  python sculmm_validate.py <scene.sculmm.json> [--clips <clips.json>]
Exit 0 ok / 1 rejections; report is machine JSON (repair loop food).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

from sculmm_schema import (ASSET_TIERS, CLIP_LIBRARIES, CLIP_REF,
                           SCULMM_CUE_VOCAB, SCULMM_VERSION,
                           sculmm_json_schema)

SRC = Path(__file__).parent
FACTORY_SRC = SRC.parents[2] / "src"
sys.path.insert(0, str(FACTORY_SRC))
from cue_validate import Report as V1Report          # noqa: E402
from cue_validate import validate_beat as v1_validate_beat  # noqa: E402
from cue_schema import Registry as V1Registry        # noqa: E402
from cue_schema import nearest                       # noqa: E402

RULES = {
    10: "schema violation (structural / closed fields)",
    11: "unknown clip library",
    12: "clip id not in the clip registry",
    13: "gen slot must set exactly one of image_prompt / model_ref",
    14: "actor tier outside A/B/C",
    15: "perform clip ref malformed (library:id)",
    16: "audio clock discipline",
    17: "world ref dangling (room/prop/actor used but not declared)",
}


class Report(V1Report):
    """v1 report + the v2 rule numbers; machine() unchanged in shape."""

    def __init__(self) -> None:
        super().__init__()
        self.missing_assets: list = []   # feeds the backlog law

    def machine(self) -> dict:
        m = super().machine()
        m["missing_assets"] = self.missing_assets
        return m


def load_clip_registry(path: Path | None) -> dict:
    """clip registry: {id: {path, location}} — one place to add clips.
    Default: src/clips.json beside this module (the F2 hook — without it
    every compile ran registry-empty and all perform clips read missing)."""
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    default = Path(__file__).parent / "clips.json"
    if default.exists():
        return json.loads(default.read_text(encoding="utf-8"))
    return {}


def discover_sprite_anims(book: Path, world: dict) -> dict:
    """tier-A actor -> sprite-bank anims (walk_0.png -> 'walk'), so v2
    actors keep BOTH sprite anims and clip refs in their registry."""
    anims: dict = {}
    for actor in world.get("actors", []):
        d = actor.get("asset", {}).get("cutout_dir")
        if not d:
            continue
        p = book / d
        if p.is_dir():
            anims[actor["name"]] = sorted({
                f.name.rsplit("_", 1)[0] for f in p.glob("*_0.png")
                if not f.name.startswith("_")})
    return anims


def validate_script(script: dict, clips: dict | None = None,
                    sprite_anims: dict | None = None) -> Report:
    rep = Report()
    clips = clips or {}

    # ---- L1: JSON Schema (closed fields law) ------------------------------
    schema = sculmm_json_schema()
    validator = jsonschema.Draft202012Validator(schema)
    for e in sorted(validator.iter_errors(script), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in e.path) or "<root>"
        rep.err(10, loc, e.message[:200])

    if rep.errors:                      # structure broken: semantics can't
        return rep                      # run meaningfully — fail fast

    world = script.get("world", {})
    rooms = {r["name"]: r for r in world.get("rooms", [])}
    props = {p["name"]: p for p in world.get("props", [])}
    actors = {a["name"]: a for a in world.get("actors", [])}

    # ---- L2: semantic rules ----------------------------------------------
    # clip refs closure (actors' banks + perform cues)
    def check_clip(ref: str, path: str) -> None:
        m = CLIP_REF.match(ref)
        if not m:
            rep.err(15, path, f"clip ref {ref!r} malformed"
                    " (want library:id, e.g. ardy:walk)")
            return
        lib, cid = m.groups()
        if lib not in CLIP_LIBRARIES:
            rep.err(11, path, f"unknown clip library {lib!r}"
                    f" (closed set {CLIP_LIBRARIES})")
        if ref not in clips:
            rep.missing_assets.append({"kind": "clip", "ref": ref})
            rep.err(12, path, f"clip {ref!r} not in the clip registry"
                    " (add a registry entry — the one-place law)")

    for name, actor in actors.items():
        for ref in actor.get("asset", {}).get("clips", []):
            check_clip(ref, f"world.actors[{name}].asset.clips")
        tier = actor.get("asset", {}).get("tier")
        if tier and tier not in ASSET_TIERS:
            rep.err(14, f"world.actors[{name}]",
                    f"tier {tier!r} outside {ASSET_TIERS}")

    # gen-slot XOR (semantic; schema oneOf is intentionally loose here)
    for name, prop in props.items():
        has_img = "image_prompt" in prop
        has_model = "model_ref" in prop
        if has_img == has_model:        # both or neither
            rep.err(13, f"world.props[{name}]",
                    "set exactly one of image_prompt / model_ref")

    # audio clock discipline
    audio = script.get("audio")
    if audio:
        if audio.get("clock") == "placeholder":
            for ln in audio.get("lines", []):
                if "placeholder_dur_s" not in ln:
                    rep.err(16, f"audio.lines[{ln.get('id')}]",
                            "placeholder clock requires placeholder_dur_s")

    # beats: reuse the v1 rule engine (closed vocab, parallel-state,
    # serial-audio...) against a registry built from the v2 world
    reg = V1Registry(book=script.get("book", ""))
    reg.rooms = set(rooms)
    reg.actors = set(actors) | {"narrator"}
    for name, room in rooms.items():
        reg.anchors[name] = set()
        reg.props[name] = list(room.get("props", []))
    for name, actor in actors.items():
        clip_ids = [CLIP_REF.match(c).group(2)
                    for c in actor.get("asset", {}).get("clips", [])
                    if CLIP_REF.match(c)]
        reg.actor_anims[name] = sorted(
            set(clip_ids) | set((sprite_anims or {}).get(name, [])))

    v1rep = V1Report()
    for bi, beat in enumerate(script.get("beats", [])):
        # v2-only cues first (v1's walker doesn't know perform/layer)
        for key in ("cues", "with"):
            for k, cue in enumerate(beat.get(key) or []):
                path = f"beats[{bi}].{key}[{k}]"
                kind = cue.get("do")
                if kind == "perform":
                    check_clip(cue.get("clip", ""), path)
                    if cue.get("actor") not in reg.actors:
                        rep.err(17, path,
                                "perform actor %r not declared in"
                                " world.actors" % cue.get("actor"))
                elif kind == "layer":
                    room = rooms.get(_current_room(script, bi))
                    names = [l["name"] for l in
                             (room or {}).get("depth_layers", [])]
                    if cue.get("name") not in names:
                        rep.err(17, path, f"layer {cue.get('name')!r} not"
                                f" declared in room"
                                f" {_current_room(script, bi)!r}"
                                " depth_layers")
        v1_validate_beat(beat, bi, reg, v1rep)
    # fold v1 rejections that are v2-legal (perform/layer unknown-cue
    # false positives) back out; keep everything else.
    for e in v1rep.errors:
        if "unknown cue 'perform'" in e["error"] or \
                "unknown cue 'layer'" in e["error"]:
            continue
        rep.errors.append(e)
    rep.warnings.extend(v1rep.warnings)
    return rep


def _current_room(script: dict, beat_index: int) -> str:
    """latest room cue at or before beat_index (layer closure helper)."""
    room = ""
    for beat in script.get("beats", [])[: beat_index + 1]:
        for cue in (beat.get("cues") or []) + (beat.get("with") or []):
            if cue.get("do") == "room":
                room = cue.get("room", "")
    return room


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: sculmm_validate.py <scene.sculmm.json>"
              " [--clips <clips.json>]")
        return 2
    script = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    clips = {}
    if "--clips" in sys.argv:
        clips = load_clip_registry(
            Path(sys.argv[sys.argv.index("--clips") + 1]))
    book = Path(sys.argv[sys.argv.index("--book") + 1]) if "--book" in \
        sys.argv else Path(sys.argv[1]).parent.parent
    rep = validate_script(script, clips,
                          discover_sprite_anims(book,
                                                script.get("world", {})))
    print(json.dumps(rep.machine(), indent=1, ensure_ascii=False))
    return 0 if rep.ok() else 1


if __name__ == "__main__":
    sys.exit(main())
