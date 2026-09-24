# The Full LLM 3D SCUMM Pipeline — v1.0

> Companion to `scumm_engine_design.md` (CUE v1.0). Where CUE defined the
> script law (closed-vocab JSON, audio-as-clock, deterministic), this doc
> defines the WORLD pipeline around it: LLM authors everything, machines
> generate everything, gates verify everything. Written 2026-09-20 from
> two days of proven (and failed) work — status marks are real.

## 0. The one-line chain

```
TEXT BRIEF
  → LLM SCREENPLAY + DIRECTION (CUE .cue.json, closed vocab)
  → ASSET GENERATION   (characters: FLUX→mesh→rig · props: text→3D · sets: 2D plates)
  → MOTION             (ARDY text-to-motion + Mixamo library → skeleton clips)
  → ASSEMBLY           (3D scene graph: actors + set plates + floor marks)
  → RENDER             (Blender/Godot film backends · browser scrub player)
  → GATES              (numeric + vision + user; nothing ungated ships)
```

Every arrow is LLM-or-script-driven; every product is gated; the same
inputs + seed give the same outputs, frame for frame.

## 1. Asset stage — characters (offline, once per character)

**Chain:** FLUX T-pose sheet → image-to-3D mesh → auto-rig → gates

1. **Sheet** (PROVEN — the pandemonium law): NVIDIA flux.2-klein endpoint
   (key `~/.nvapi`, ~500-char budget, green lead, one canvas + seed
   family per character = identity lock, T-pose, EMPTY HANDS).
   Extraction = magic-wand key (cv2.floodFill FIXED-RANGE, border-seeded,
   contiguous — interior holes unreachable by construction; +8.7%/+26%
   recovered vs the old chroma key).
2. **Mesh** (PROVEN on the free Colab T4): Hunyuan3D (`hy3dgen`):
   `HunyuanDiTPipeline` = image stage, `Hunyuan3DDiTFlowMatchingPipeline`
   = shape stage. First object (a chair) verified text→3D end-to-end
   2026-09-20. Character meshes from approved art: in flight.
   Cloud alternative: Meshy/Tripo3D (~$1/char, Mixamo-compatible naming).
3. **Auto-rig** (GAP — the missing link): Mixamo upload is a browser step
   (user); Meshy cloud rigging is an API. Output must satisfy the
   BONE-NAME LAW (normalize `mixamorig[:\d]*_` prefixes).

**Asset gates (nothing enters a scene ungated):**
- rest pose is T; left/right bone lengths agree within 15%
- 90° arm + leg rotation renders without collapsed/webbed geometry
- front render at rest matches the FLUX sheet silhouette (IoU gate)
- the user approves the look (art-first, always)

## 2. The look decision (stage-gated, not either/or)

- **Tier A — cutout puppets** (PROVEN): the approved 2D art, plane-based
  Blender engine (`blender_run2` lineage). Production NOW.
- **Tier B — rigged mesh, art projected** (RECOMMENDED BRIDGE): the mesh
  is only a skinned CARRIER; the approved sheet projects onto it from the
  front camera, unlit. Real-engine motion + depth sorting (no affine/sign
  bugs — they die with the engine doing transforms), look stays ~the
  sheet. This is the Grim Fandango lineage.
- **Tier C — full 3D character** (GOAL): the mesh's own texture, all
  views. The current 3D-character work drives here; accept look drift,
  gate with user eyes.

Production order: A ships today, B is the default target, C lands when
its art passes the user's gate.

## 3. Motion

- **Library**: the Mixamo packs (38 clips staged: locomotion, action,
  gestures, capoeira).
- **Text-to-motion** (VERIFIED): ARDY on Colab T4 (free tier; playbook at
  `~/ARDY_CLI_PLAYBOOK.md`; llama gate bypassed via NousResearch mirror;
  CoreSkeleton27 ≈ Mixamo 1:1 — converter = share rotations, copy parent).
  npz carries global rot mats + joints + root + foot contacts @20fps →
  resample 30, convert to bone JSON.
- **Retarget**: aim-based + a foot-plant pass driven by ARDY's contact
  flags. Check the engine's built-in humanoid retarget before hand-
  rolling more.
- **Motion law** (learned the hard way): weights/skinning/placement must
  always solve against TRUE REST (action detached) — the frame-1 bug
  class has now bitten planes AND weights.

## 4. Sets and props

- **Sets stay 2D plates** (Grim Fandango law): the 1984 location plates
  (16, approved) + HunyuanDiT/T2I-generated new plates as depth layers;
  3D actors in front; marks on a simple floor plane. No text-to-3D sets.
- **Props = the text-to-3D win**: unknown prop in a script →
  `missing_assets.md` backlog (same law as `missing_verbs.md`) → chair-
  style generation (one-liner) → asset gates → library.

## 5. Assembly + render

- Scene graph from the compiled cue sheet: actors (tiered), plates,
  floor marks, camera law (ortho side, up-hint = object-local +Y —
  asserted, not assumed).
- Backends: headless Blender (proven transparent-film path) and Godot
  Movie Maker reading `scene_plan.json`; browser scrub player for QA.
- Audio is the clock: word alignments drive every cut (CUE law).

## 6. Gates summary

| Gate | Method | Status |
|---|---|---|
| Script validity | cue_schema + validator + repair loop | exists |
| Art | user art-gate (max 2 redraw rounds) | law |
| Alpha | wand key + threshold 1 | proven |
| Mesh | silhouette IoU vs sheet, pose-deform render | new |
| Rig | rest-T, L/R ±15%, 90° rotation test | new |
| Motion | rest-solved weights, foot contacts | law |
| Render | numeric gates + neutral-wording vision + user | proven |

## 7. Compute map

| Machine/Service | Role |
|---|---|
| Rog | orchestration, git, QA eyes, gates |
| Lappy (3060) | TTS, local python, Blender, Godot sampler |
| Colab T4 (free, via `colab` CLI) | ARDY motion · Hunyuan3D meshes · HunyuanDiT plates |
| NVIDIA endpoint | FLUX character/scene art |

Colab quirks (law): detached kick + file-poll only (output timeout);
idle T4s get reaped; stale 412 assignments need a browser release.

## 8. First pilot (the proof)

One text brief → LLM room + blocking → tier-A puppet performs ARDY-
generated motion in front of a HunyuanDiT plate → rendered mp4, gated.
1984 room (everything downstream exists) or from-zero world — user's
call. Success = the brief-to-film loop with zero human steps between
gate and gate.

## 9. Standing laws (the two days' tuition)

Closed vocab or it didn't happen · audio is the clock · solve against
rest · never trust a gate that can't fail · pixels + numbers before
eyeballs, two framings before conviction · art-first, user owns every
look · GPU services never start/stop without a word · keys in files,
never code · nothing silent over 10 minutes.
