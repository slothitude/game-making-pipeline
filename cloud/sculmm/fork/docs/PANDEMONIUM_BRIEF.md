# The 2.5D Pandemonium System — Agent Brief

Everything an agent needs to understand what we are building, how it
works, and what the laws are. Read this before touching anything.
(Last updated 2026-09-19; check `git log` for later work.)

## 1. What we are building, and why

**Goal**: a factory technique that takes the 1984 cast's FLUX-generated
2D cutout characters and performs them with **real 3D skeletal
animation** — Mixamo bone rigs — inside **Pandemonium-style 2.5D side-
scrolling scenes**. A character that looks like flat drawn art (the
approved "totally unique" photo-cutout-on-cel film look) but runs,
jumps, lands and gestures with the motion quality of a mocap library.

Why it exists: the audiobook factory (same repo, `spec.md`, the
faithful/parody Godot pipelines) acts books out with 2D frame-cycle
sprite puppets. This 2.5D front is the next animation tier: one cutout
sheet per character + the entire Mixamo library (walk/run/jump/sneak/
capoeira/gestures — 38+ clips already staged) = unlimited performance
without generating per-frame art.

**Visual target** (user's words): "think Pandemonium — a 3D character,
bones from Mixamo, attached to cutouts." Front-facing cutout marionette
running right through parallax cel sets. The mismatch between flat art
and 3D motion is the feature (the locked film-look law, fleet-wide).

## 2. Architecture — three stages, two machines

```
[1] SAMPLE (Godot, headless)        [2] COMPOSE (PIL)         [3] ENCODE
sample_bones.gd                     compose25d.py             ffmpeg
any mixamo FBX --+                  12 cutout parts +         rawvideo
  FK at 30fps  ->| bone JSON  ->    exact per-part      ->    rgb24 pipe
  origin+global |  (rest frame     screen affine on a          -> mp4
  basis per bone|   t=-1 first)    parallax backdrop
```

- **Rog** (Windows, this repo): canonical code in
  `flux_check/pandemonium/`, art QA, git. Files are scp'd to Lappy to
  run (no shared filesystem).
- **Lappy** (ssh aaron@192.168.0.33): the Godot sampler project at
  `~/audiobook/pandemonium/` (Forward+ renderer, its own project — the
  2D `~/audiobook/video` project is the FAITHFUL/PARODY pipeline, do
  not touch), jobs under `/mnt/seagate/audiobooks/1984/cast/pandemonium/`.
  Python with cv2/PIL/numpy = `~/audiobook/venv/bin/python`.

Stage 1 — **sample_bones.gd** (SceneTree script, headless, no
rendering): loads an FBX, walks its Animation's bone tracks, forward-
kinematics the whole chain at 30 fps, writes JSON rows per frame:
`bone -> [ox,oy,oz, xx,xy,xz, yx,yy,yz, zx,zy,zz]` (origin + full
global basis). Frame 0 is the T-POSE REST (t=-1) — the compositor
diffs rotations against it.

Stage 2 — **compose25d.py**: per part, computes the exact 2D affine
(see laws below), transforms the part image with PIL, alpha-composites
in draw order onto a PIL-drawn parallax backdrop, pipes frames to
ffmpeg. **PIL owns every alpha bit** — this was the user's explicit
directive after the Godot 3D render path produced translucent parts
and a 0.6x-scale window; no engine transparency involved anywhere.

Stage 3 — ffmpeg rawvideo bgr24/rgb24 pipe (the factory's
`render_faithful.py` law), libx264 crf20.

## 3. The laws (each one was a bug — respect them)

1. **T-POSE CORRESPONDENCE**: the cut art's pose MUST be the skeleton's
   reference pose (Mixamo = T-pose, arms straight horizontal). A-pose
   art has no exact bone correspondence and forces heuristic rotations
   — the original "horrible" mapping. Sheets:
   `pandemonium_sheet.py` (FLUX, green screen, one canvas+seed family
   per character = identity lock; T-pose prompt; landscape 1216x832).
2. **EXACT AFFINE, not billboards**: per part per frame:
   `dR = B_t @ inv(B_rest)` (material rotation), then the part's two
   REST-PLANE world axes (`REST_AX` = long axis along the bone at rest;
   `REST_PERP` = width) are rotated by dR and orthographically
   projected `PJ(v) = (v_z*px, -v_y*px)` — screen-x is **z** (the run
   direction), screen-y is **-y** (up), bone x is depth. The two
   projected vectors are the affine's columns (divided by
   `ART_PX_PER_UNIT` = sheet figure px per metre). Rotation, depth
   foreshortening and shear all fall out — no heuristic angles.
3. **REST-PLANE ORIENTATION** (the marionette): the character runs
   along +z but the ART faces the camera, so body parts (torso/legs/
   head) rest spanning (y,z) = camera-facing; limbs lie ALONG their
   bone (arms = ±x at rest, briefly near-edge-on — geometrically true).
4. **AXIS-SWAP**: the bone's long axis maps onto the image's LONG
   dimension — wide parts (T-pose arms) on image-x, tall parts (legs/
   torso/head) on image-y. Swapping = every vertical part becomes a
   rotated ribbon.
5. **UNITS**: projected axes are per-WORLD-UNIT px (~464); the affine
   multiplies PART PIXELS — divide by `ART_PX_PER_UNIT` (~449) or
   parts render as ~450x zoomed fragments.
6. **MIRROR UN-FLIP**: if det<0, negate the width column (art must
   never mirror — faces).
7. **WIDTH FLOOR**: a limb swinging through the depth axis foreshortens
   toward zero width; ramp the width column norm up to a floor
   (0.30/0.45) — the standard paperdoll cheat. A runner must never
   lose a limb mid-swing.
8. **ROOT-Z REBASE PER FRAME**: clips carry metres of root travel
   (~3 m per run cycle); rebase z on the CURRENT frame's hips or the
   doll slides sideways through the loop and snaps back each wrap.
9. **FOOT-DIP CLAMP**: floor reference = min(this frame's lowest bone,
   clip-global min) — crouches never sink feet under the floor while
   airborne frames keep the global reference so jump arcs read.
10. **BONE-NAME LAW**: imports vary `mixamorig_X` / `mixamorig1_X` /
    `mixamorig:X` (Mixamo's per-export namespace counter) — normalize
    to the bare bone name everywhere.
11. **TRACK LAW (sampler)**: Godot 4 imports bone anims as
    TYPE_POSITION_3D / TYPE_ROTATION_3D tracks with the bone as the
    path SUBNAME; limb bones carry ROTATION ONLY (only Hips has
    position) — requiring both froze every limb at rest. Interpolate
    with position_track_interpolate / rotation_track_interpolate
    (the generic track_interpolate is Godot 3).
12. **CUTTING LAW** (cut25d.py): geometry is only a PARTITION; final
    part alpha = the ORIGINAL keyed alpha inside the region grown
    14px into neighbours (bounds: alpha>8 keeps the feather ring).
    Growth doubles as joint overlap — bones move, seams stay covered.
    T-pose arms are CONTIGUOUS with the shoulders: clamp arm-band rows
    to chest width, carve beyond. Kill the underfoot shadow by COLOR
    (dark green), never by row band (bands eat soles).

## 4. File map

Rog `C:\Users\aaron\audio_book\flux_check\pandemonium\`:
- `pandemonium_sheet.py` — FLUX T-pose sheets (tpose_0/1.png per char)
- `cut25d.py` — slicer -> `p25d/<part>.png` x12 + `rig25d.json`
  (parts: ul/fl/ur/fr arms, tl/sl/ftl + tr/sr/ftr legs, torso, head;
  viewer-left = character RIGHT in front view)
- `sample_bones.gd`, `compose25d.py`, `parts_sheet.py` (QA montage)
- `pandem.gd/.tscn`, `render_pandem.sh` — the RETIRED in-engine 3D
  path (kept for reference; transparency + window-scale bugs)
- `rigs/` — Locomotion FBXs; `rigs/action/`, `rigs/gestures/` — the
  user's downloaded Mixamo packs (23 action + 15 gesture clips + ref
  characters)
- this BRIEF.md

Lappy `~/audiobook/pandemonium/`: the Godot sampler project
(`assets/rigs/**` = all FBXs, imported). Job dir
`/mnt/seagate/audiobooks/1984/cast/pandemonium/`: per-char sheets +
parts + rig25d.json, sampled clip JSONs (running/jumping_up/
hard_landing/nod_yes/walk_bones), outputs pandem_*.mp4.

## 5. QA law

Pixels and numbers over eyeballs. Vision QA loop: ffmpeg frame ->
scp to Rog `_review/pandemonium/` -> Read (auto-uploads, returns CDN
URL; encode `+`->%2B, `/`->%2F in the signature) ->
`mcp__4_5v_mcp__analyze_image` with NEUTRAL wording ("describe X",
never "is this bad"). The contamination class misfires on this figure
family at any zoom; two independent framings agreeing = real defect.
Numeric checks: affine magnitudes/dets at rest (torso must be exactly
identity), bone motion ranges (a "static" result means the tracks
never bound). Max 2 rounds per visual fix.

## 6. Status & open fronts (as of writing)

Working: full timeline run -> jump -> hard landing -> run -> head-nod
composes coherently (vision-verified); julia/obrien cuts + clips in
flight via the delegated finisher agent. Open: final look gate is the
user's; stride/foot-plant polish; eventually wiring this renderer into
the audiobook factory as the goal-2 upgrade (replacing frame-cycle
sprites) — that integration is NOT started and needs the user.

## 7. Standing rules

- User gates all art and final looks; parody is YouTube-only; the
  faithful/parody pipeline and gui/tasks.json are out of pandemonium
  scope except adding a task entry.
- FLUX via NVIDIA API (key `~/.nvapi` on Lappy, never hardcode);
  llama-server on Lappy is DOWN — don't start/stop services.
- Commit on Rog; message footer:
  `Co-Authored-By: Claude Code <noreply@anthropic.com>`
