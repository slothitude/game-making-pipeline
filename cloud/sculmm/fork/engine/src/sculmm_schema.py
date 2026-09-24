"""sculmm_schema.py — SCULMM script format v2 (CUE-2): closed vocabulary,
JSON-Schema-validatable, world + motion + generator slots.

SCULMM = the LLM 3D SCUMM engine (user-named, 2026-09-20). v2 extends
CUE v1 (src/cue_schema.py — LIVE read-only, imported not modified) with:

  world primitives   rooms with generated-asset slots (backdrop_prompt,
                     prop refs, floor marks, depth layers); props with
                     gen slots (image prompt OR model ref); actors with
                     tiered asset refs (A cutout / B projected-mesh /
                     C full-3D) + motion clip refs.
  motion vocabulary  `perform` cue: clip references (mixamo:<name>,
                     ardy:<id>, trellis:<id>) as first-class verbs
                     alongside v1 blocking verbs.
  generator slots    resolve at COMPILE time to an asset manifest; the
                     compiled sculmmc carries resolved paths + a
                     missing_assets list (the missing_assets.md backlog
                     law — PIPELINE_3D_SCUMM.md §4).

Laws kept from v1: closed vocabulary (no freeform escape hatches),
audio-as-clock, determinism (no wall clock, seed-driven residuals).
v1 compiled sheets are unaffected (version discriminates).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

FACTORY_SRC = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(FACTORY_SRC))
from cue_schema import CUE_VOCAB as CUE_VOCAB_V1       # noqa: E402
from cue_schema import FX_KINDS, MOODS, PACES, SHOTS   # noqa: E402

SCULMM_VERSION = 2

# ------------------------------------------------------- motion vocabulary --
# clip refs: <library>:<id> — library in CLIP_LIBRARIES, id closed against
# the clip registry at validate time (registry entry = one place to add).
CLIP_LIBRARIES = ("mixamo", "ardy", "trellis")
CLIP_REF = re.compile(r"^(mixamo|ardy|trellis):([A-Za-z0-9_.-]+)$")

# v2 cue vocabulary = v1 + perform (clip ref as first-class verb) +
# layer (depth-layer control for plate rooms). Fields stay closed.
SCULMM_CUE_VOCAB: dict = dict(CUE_VOCAB_V1)
SCULMM_CUE_VOCAB["perform"] = (("actor", "clip"), {
    "pace": PACES, "loop": ("once", "hold"), "fade_s": None})
SCULMM_CUE_VOCAB["layer"] = (("name",), {
    "depth": ("far", "mid", "near"), "file": None, "parallax_k": None})

# ------------------------------------------------------- asset tiers (§2) --
ASSET_TIERS = ("A", "B", "C")     # A cutout · B projected-mesh · C full-3D

GEN_ENGINES = ("flux", "hunyuandit", "pixal3d", "trellis", "mixamo",
               "ardy", "none")    # pipeline's generator registry


def prop_gen_schema() -> dict:
    """oneOf gen slot: exactly one of image prompt or model ref."""
    return {
        "oneOf": [
            {"required": ["image_prompt"], "not": {"required": ["model_ref"]}},
            {"required": ["model_ref"], "not": {"required": ["image_prompt"]}},
        ],
        "properties": {
            "engine": {"enum": list(GEN_ENGINES)},
            "image_prompt": {"type": "string", "maxLength": 500},
            "model_ref": {"type": "string"},
        },
    }


def actor_asset_schema() -> dict:
    """per-tier asset refs; one per tier max, path refs are strings."""
    return {
        "type": "object",
        "properties": {
            "tier": {"enum": list(ASSET_TIERS)},
            "cutout_dir": {"type": "string"},          # tier A: sprites dir
            "mesh": {"type": "string"},                # tier B/C: glb path
            "rig": {"type": "string"},                 # skeleton fbx/glb
            "art": {"type": "string"},                 # projection source
            "clips": {"type": "array", "items": {
                "type": "string", "pattern": CLIP_REF.pattern}},
        },
        "required": ["tier"],
        "additionalProperties": False,
    }


# --------------------------------------------- room shells + placement (§4) --
# rooms are procedural 3D SHELLS: floor/walls/ceiling params + generated
# texture slots; props place via a CLOSED ANCHOR VOCABULARY.
WALL_DIRS = ("north", "south", "east", "west")
ANCHOR_VOCAB = tuple(["center"] + [f"against_wall:{d}" for d in WALL_DIRS])
# plus object-relative anchors, expressed as {"left_of": "<prop>"} /
# {"right_of": "<prop>"} — the only compound form allowed (closed set).

# asset normalization contract (manifest law): every resolved asset
# declares its expected normalization; verification is the asset-gate
# front's job, the manifest records the contract.
ASSET_NORM = {"units": "m", "pivot": "floor", "facing": "+z",
              "poly_budget": 60000}


def place_schema() -> dict:
    """closed anchor vocabulary: string enum | left_of/right_of object."""
    return {
        "oneOf": [
            {"enum": list(ANCHOR_VOCAB)},
            {"type": "object", "properties": {
                "left_of": {"type": "string"}}, "required": ["left_of"],
             "additionalProperties": False},
            {"type": "object", "properties": {
                "right_of": {"type": "string"}}, "required": ["right_of"],
             "additionalProperties": False},
        ],
    }


def texture_slot_schema() -> dict:
    """generated texture slot: STORED-ARTIFACT provenance — the LLM world
    JSON records prompt+engine+seed; generation happens offline; the
    compiler consumes the stored artifact, never re-calls an LLM."""
    return {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "maxLength": 500},
            "engine": {"enum": list(GEN_ENGINES)},
            "seed": {"type": "integer"},
            "asset": {"type": "string"},   # resolved path once generated
        },
        "additionalProperties": False,
    }


def shell_schema() -> dict:
    wall = {
        "type": "object",
        "properties": {
            "wall": {"enum": list(WALL_DIRS)},
            "width_m": {"type": "number"},
            "height_m": {"type": "number"},
            "texture": texture_slot_schema(),
        },
        "required": ["wall", "height_m"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "floor": {"type": "object", "properties": {
                "size_m": {"type": "array", "items": {"type": "number"},
                           "minItems": 2, "maxItems": 2},
                "texture": texture_slot_schema()},
                "additionalProperties": False},
            "walls": {"type": "array", "items": wall},
            "ceiling": {"type": "object", "properties": {
                "height_m": {"type": "number"},
                "texture": texture_slot_schema()},
                "additionalProperties": False},
        },
        "additionalProperties": False,
    }


def sculmm_json_schema() -> dict:
    """Full JSON Schema (draft 2020-12) for a .sculmm.json script.
    Every field closed; additionalProperties False everywhere — a
    freeform field is a validation error, by law."""
    cue_props = {}
    for kind, (req, allowed) in SCULMM_CUE_VOCAB.items():
        p = {"properties": {"do": {"const": kind}}}
        for f, values in allowed.items():
            prop = {} if values is None else {"enum": list(values)}
            p["properties"][f] = prop
        for f in req:
            if f != "do":
                p["properties"].setdefault(f, {})
        cue_props[kind] = p

    beat = {
        "type": "object",
        "properties": {
            "at": {"type": "string"},          # v1 time exprs (§3.1)
            "cues": {"type": "array", "items": {
                "type": "object",
                "oneOf": [sub for sub in cue_props.values()],
            }},
            "with": {"type": "array", "items": {
                "type": "object",
                "oneOf": [sub for sub in cue_props.values()],
            }},
        },
        "required": ["at"],
        "additionalProperties": False,
    }

    room = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "backdrop": {"type": "string"},           # existing asset path
            "backdrop_prompt": {"type": "string", "maxLength": 500},
            "backdrop_engine": {"enum": list(GEN_ENGINES)},
            "shell": shell_schema(),                  # procedural 3D room
            "props": {"type": "array", "items": {"type": "string"}},
            "floor_marks": {"type": "array", "items": {
                "type": "object",
                "properties": {"x": {"type": "number"},
                               "name": {"type": "string"}},
                "required": ["x"], "additionalProperties": False}},
            "depth_layers": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": {"type": "string"},
                               "depth": {"enum": ["far", "mid", "near"]},
                               "file": {"type": "string"},
                               "parallax_k": {"type": "number"}},
                "required": ["name", "depth"],
                "additionalProperties": False}},
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    prop = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "room": {"type": "string"},
            "place": place_schema(),                  # closed anchors (§4)
            "asset": {"type": "string"},   # resolved mesh path once generated
            **prop_gen_schema()["properties"],
        },
        "required": ["name"],        # image/model XOR is rule 13 (semantic)
        "additionalProperties": False,
    }
    # exactly-one-of enforced at semantic level (oneOf with additional-
    # Properties False interacts badly; the validator owns the XOR rule).

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "SCULMM script v2",
        "type": "object",
        "properties": {
            "version": {"const": SCULMM_VERSION},
            "seed": {"type": "integer"},
            "book": {"type": "string"},
            "scene": {"type": "string"},
            "world": {
                "type": "object",
                "properties": {
                    "rooms": {"type": "array", "items": room},
                    "props": {"type": "array", "items": prop},
                    "actors": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "asset": actor_asset_schema(),
                        },
                        "required": ["name", "asset"],
                        "additionalProperties": False}},
                },
                "required": ["rooms"],
                "additionalProperties": False,
            },
            "beats": {"type": "array", "items": beat},
            "audio": {"type": "object", "properties": {
                "clock": {"enum": ["readalong", "placeholder"]},
                "lines": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "speaker": {"type": "string"},
                        "text": {"type": "string"},
                        "placeholder_dur_s": {"type": "number"},
                    },
                    "required": ["id", "speaker", "text"],
                    "additionalProperties": False}}},
                "required": ["clock", "lines"],
                "additionalProperties": False},
        },
        "required": ["version", "seed", "book", "world", "beats"],
        "additionalProperties": False,
    }


# -------------------------------------------------- compiled sculmmc format --
# build/<name>.sculmmc.json — v1 cuec fields it keeps (version, book, seed,
# beats, lines) plus the asset manifest. Reproducible evidence law: same
# script + registries + seed -> byte-identical, or the build halts.
SCULMMC_REQUIRED = ("version", "book", "seed", "mode", "duration",
                    "beats", "lines", "assets", "missing_assets", "hashes")
