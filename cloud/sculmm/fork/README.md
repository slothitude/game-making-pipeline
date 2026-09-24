# cloud/sculmm/fork — the SCULMM integration fork

The full SCULMM engine, forked into the Game Making Pipeline as a
subtree. This dir is the fork's home: engine + the knowledge snapshot it
was built from. Nothing outside `fork/` is touched by SCULMM work —
the existing `cloud/sculmm/` Colab prop-order front stays as-is and is
consumed by this fork, not edited by it.

## The one-line chain

```
TEXT BRIEF
  -> CUE SCREENPLAY        (closed-vocab .cue.json, audio is the clock)
  -> ASSETS                (characters: FLUX -> mesh -> rig · props: text->3D)
  -> MOTION                (ARDY text-to-motion + Mixamo library -> skeleton clips)
  -> ASSEMBLY              (3D scene graph: actors + set plates + floor marks)
  -> TOON RENDER           (cel shader on the camera; Blender/Godot + browser scrub)
  -> GATES                 (numeric + vision + user; nothing ungated ships)
```

Every arrow is LLM-or-script-driven; every product is gated; the same
inputs + seed give the same outputs, frame for frame.

## The three-tier look decision (stage-gated, not either/or)

- **Tier A — cutout puppets** (PROVEN, production now): the approved 2D
  art as planes, Blender plane engine.
- **Tier B — rigged mesh, art projected** (RECOMMENDED BRIDGE): the mesh
  is only a skinned CARRIER; the approved sheet projects onto it from
  the front camera, unlit. Real-engine motion + depth sorting, look
  stays ~the sheet. The Grim Fandango lineage.
- **Tier C — full 3D character** (GOAL): the mesh's own texture, all
  views. Accept look drift; gate with user eyes.

Production order: A ships today, B is the default target, C lands when
its art passes the user's gate.

## What's in this fork

| Path | What |
|---|---|
| `engine/src/` | F1/F2 core — CUE schema, validator, compiler, world-gen (LLM authoring + repair loop + Colab dispatch + cache), pilot renderer. Straight copy, no edits. |
| `engine/player/` | F3 browser scrub player (3D cel, work-order law visible) + static server. Straight copy, no edits. |
| `docs/SESSION_HANDOFF.md` | Full state export 2026-09-24: done/blockers/next. |
| `docs/PIPELINE_3D_SCUMM.md` | Pipeline spec v1.0 — the world pipeline around the CUE core. |
| `docs/RESEARCH_LINKS.md` | Tool/model arsenal with links + verified facts. |
| `docs/PANDEMONIUM_BRIEF.md` | The 2.5D puppet system brief (Tier A law + the 12 laws). |
| `MESH_LANE.md` | Character-mesh decision doc — OPEN, awaiting the user's verdict. |

## Source of truth (outside this repo — do not duplicate edits)

| Machine | Dir | What |
|---|---|---|
| Rog (`C:\Users\aaron\audio_book\`) | `flux_check/cue_dev/src/`, `flux_check/cue_dev/player/` | canonical F1/F2/F3 (this fork's `engine/` is a snapshot of it) |
| Rog | `_review/sculmm/` | pilot films, stills, three.js viewer, textured winston |
| Lappy (`ssh aaron@192.168.0.33`) | `~/sculmm/` | render stack: toon cel pass (EEVEE 3-band), spring-arm camera |
| Lappy | `/mnt/seagate/sculmm/meshes/` | all generated GLBs |
| Lappy | `~/ardi/` | ARDY motion tools + clips |

When the canonical dirs move forward, refresh the snapshot here with an
explicit "sync from cue_dev" commit — never hand-edit the copies.

## Standing laws (verbatim shortlist)

- **User gates every look** (art-first — no look ships without their yes)
- **Audio is the clock** (word alignments drive every cut)
- **Solve against rest** (never against frame-1 pose)
- **Closed vocab or it didn't happen**
- **Nothing silent > 10 minutes**
- **Keys in files, never in code**

The longer list (gates that can fail, pixels + numbers before eyeballs,
Colab kick/poll, BVH root motion in the bones, ARMATURE_AUTO weights can
be inert, faithful/parody production never touched) lives in
`docs/SESSION_HANDOFF.md` and `docs/PANDEMONIUM_BRIEF.md`.

## Integration note

GMP fronts this fork the same way they front everything else: work-orders
over code, named law constants, factory fronts with green batteries.
`../prop_order.py` (existing front) is the prop-mesh lane; the character
mesh lane is pending — see `MESH_LANE.md`.
