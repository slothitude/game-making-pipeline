# SCULMM Session Handoff — 2026-09-24

> Full state export for any agent picking up this work. Read BRIEF.md,
> PIPELINE_3D_SCUMM.md, and RESEARCH_LINKS.md first — this doc covers
> what happened SINCE those were written, current blockers, and the
> exact state of every artifact.

## The north star

**SCULMM** — an LLM 3D SCUMM engine where LLMs author everything and
machines generate everything. User constraints (2026-09-20, overriding):
1. Everything 3D (no 2D plates for sets)
2. Toon cel shader on the camera (fleet-locked look)
3. Always the Google Colab CLI for compute (Lappy 3060 as fallback)

## What's DONE and working

| Deliverable | Location | Status |
|---|---|---|
| **1984 faithful play** | Lappy `/mnt/seagate/audiobooks/1984/episodes/` | ✅ 24/24 chapters QA-green, multi-voice, pushed to Forgejo |
| **SCULMM core (F1)** | `flux_check/cue_dev/src/` | ✅ schema/validator/compiler, 15/15 gates, byte-deterministic |
| **World-gen (F2)** | `flux_check/cue_dev/src/sculmm_world.py` | ✅ LLM authoring + repair loop + Colab dispatch + cache |
| **Browser player (F3)** | `flux_check/cue_dev/player/` | ✅ 3D cel scrub player, work-order law visible |
| **ARDY motion** | Lappy `~/ardi/t2d3/ardy_to_bones.py` | ✅ text→skeleton, converter to bone JSON + BVH |
| **Text-to-3D shape** | Multiple paths proven | ✅ Hunyuan3D (chair/rocket/winston), TRELLIS (props) |
| **Toon cel pass** | Lappy `~/sculmm/` | ✅ EEVEE 3-band, 0.7s/frame, ink hulls |
| **Spring-arm camera** | Lappy `~/sculmm/` | ✅ damped follow, DOF, room-clamped |
| **From-zero pilot** | `_review/sculmm/` | ✅ v1+v2 delivered (LLM world + assets + film) |
| **Textured winston (local)** | `_review/sculmm/viewer/winston_textured_final.glb` | ✅ 6.58MB, 2048×2048 texture, GP pipeline on 3060 |
| **Hunyuan3D-2GP setup** | Lappy `~/Hunyuan3D-2GP` + `/mnt/seagate/h3dgp` | ✅ fully installed, venv, models cached, reusable |
| **3D viewer** | `_review/sculmm/viewer/viewer.html` | ✅ three.js, orbit controls, GLB loading |

## Current blocker: character mesh quality

The user has rejected every 3D character mesh so far:
- Hunyuan3D 2.0 shape: flat (0.33m depth), bas-relief
- TRELLIS 512-pass: blocky cube
- GP textured result: texture works but underlying mesh is still Hunyuan 2.0 shape quality
- User verdict: "he doesn't look good"

**Root cause:** single-image-to-3D models produce poor character geometry.
They work for objects (chair, rocket were decent) but characters need depth,
volume, and anatomy that these models can't infer from one flat view.

## Upgrade paths (researched, not yet tried)

### 1. Hunyuan3D 2.1 (recommended next step)
- GitHub: `Tencent-Hunyuan/Hunyuan3D-2.1`
- "Significantly improved texture quality with finer surface details and
  enhanced three-dimensional depth perception"
- Production-ready PBR materials (albedo, metallic, roughness)
- Better geometry from single images (addresses the flat-mesh problem)
- Same setup pattern as our working 2.0 GP (Python 3.10, torch 2.5.1,
  same CUDA extensions)
- Can run locally on the 3060 with the GP offloading

### 2. Meshy AI (cloud, best quality)
- Image-to-3D in 20-30 seconds, good humanoid geometry
- Built-in auto-rigging (0 credits = free)
- 500+ animations pre-built
- REST API — callable from the factory
- Free credits to start, then ~$0.20/character
- Exports GLB/FBX with textures
- Sign up at meshy.ai

### 3. TRELLIS.2 full resolution (needs T4)
- Our 512-pass was low-res because of 6GB VRAM
- Full resolution needs 12GB (T4 has 16GB)
- Colab free tier can't hold sessions long enough (see below)

## Colab free tier reality (learned the hard way)

- Session lifetimes: 50-70 minutes (reaped by backend)
- Setup + models + generation needs ~50 minutes total
- This means the session dies right as generation starts
- OAuth token expires every ~2 days (user must re-auth in browser)
- 503 outages can last hours
- **Verdict:** unusable for the full pipeline. Use for individual steps
  (ARPY generation, prop meshes) but not for character generation that
  needs setup + models + generation in one session

## Lappy disk state (2026-09-24)

- Root: ~11GB free (Qwen2.5-7B 15GB was deleted to free space)
- Seagate: ~1.4GB free (tight)
- Hunyuan3D-2GP models: 8.4GB on root ~/.cache/huggingface
- venv at /mnt/seagate/h3dgp
- Further cleanup candidates (user's call): higgs-tts 8.7GB, ~/talu_* 42GB, log_space 13GB

## Key file locations

### Rog (this repo, C:\Users\aaron\audio_book\)
- `flux_check/handoff/BRIEF.md` — SCULMM architecture brief
- `flux_check/handoff/PIPELINE_3D_SCUMM.md` — pipeline spec v1.0
- `flux_check/handoff/RESEARCH_LINKS.md` — tool/model arsenal with links
- `flux_check/cue_dev/src/` — F1 core (schema/validate/compile)
- `flux_check/cue_dev/player/` — F3 browser player
- `flux_check/pandemonium/` — puppet/2.5D work + ARDY converter
- `_review/sculmm/` — pilot films, stills, viewer
- `_review/sculmm/viewer/winston_textured_final.glb` — textured winston
- `_review/pandemonium/t2d3/` — 3D mesh work, TRELLIS chains

### Lappy (ssh aaron@192.168.0.33)
- `~/Hunyuan3D-2GP/` — GP fork installed and working
- `/mnt/seagate/h3dgp/` — Python 3.10 venv for GP
- `~/.cache/huggingface/hub/models--tencent--Hunyuan3D-2` — 8.4GB models
- `~/sculmm/` — render scripts, pilot files
- `/mnt/seagate/sculmm/meshes/` — all generated GLBs
- `/mnt/seagate/sculmm/assets/` — content-hash cache
- `~/ardi/` — ARDY tools and clips
- `/mnt/seagate/audiobooks/1984/` — the faithful factory (production)

### Forgejo (Lappy :3000)
- Remote: `http://192.168.0.33:3000/aaron/pet.git`
- Credentials: user=aaron, pass=REDACTED-FOR-GIT (original in the Rog
  source copy of this doc / your Forgejo credential store — keys in
  files, never in code, never in a pushed repo)
- Last push: `ae77f510` (faithful complete)

## Colab CLI auth
- Token at `~/.config/colab-cli/` on Lappy
- Expires frequently; user must run:
  `ssh -t aaron@192.168.0.33 ~/.local/bin/colab new -s <name> --gpu T4`
  and complete the OAuth flow in their browser

## Immediate next steps (for the picking-up agent)

1. **Try Hunyuan3D 2.1 locally on Lappy** (same pattern as the working
   2.0 GP setup — clone, install, generate). This is the most likely
   path to a good character mesh without external services.

2. **If 2.1 isn't good enough: Meshy API** (cloud service, best quality,
   built-in rigging, solves two problems at once). Sign up, get API key,
   generate winston from the tpose_n1.png art.

3. **If good mesh achieved:** rig it (Mixamo upload or Meshy auto-rig),
   apply ARDY walk motion, render through the toon cel pass, deliver the
   SCULMM pilot v6 with a character the user approves.

4. **Parallel:** the parody front (goal 3) is fully unblocked — all the
   faithful pipeline machinery exists, parody scripts just need to run
   through the same path.

## Standing laws (the hard-won rules)

- Closed vocab or it didn't happen
- Audio is the clock
- Solve against rest (never against frame-1 pose)
- Gates must be able to fail (fault-injection tested)
- Pixels + numbers before eyeballs, two framings before conviction
- Art-first: user owns every look decision
- Keys in files, never in code
- Colab: kick/poll law (detached subprocess + file polling)
- Nothing silent >10 minutes
- Commit footer: `Co-Authored-By: Claude Code <noreply@anthropic.com>`
- Faithful/parody production pipelines are never touched by SCULMM work
- BVH root motion lives in the bones (not the armature object)
- ARMATURE_AUTO weights can be inert — verify deformation, not just binding
- Single-image-to-3D produces poor character meshes (proven repeatedly)
