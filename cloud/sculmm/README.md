# cloud/sculmm — the SCULMM prop lane (Colab ONLY)

The SCULMM engine eats normalized GLB props (image/text prompt → mesh). The
proven chain is hy3dgen/Hunyuan3D-2 on a Colab T4 (sculmm pilot v2: telescope /
star_table / lantern / chair — normalized meters / floor / +z, ~50k faces).

## THE LAW

**Colab CLI ONLY. Lappy/ComfyUI is OUT.** The flow is always
`colab new → upload → exec → download → stop` through
`cloud/colab/colab_worker.py` — never a Lappy inference server, never a ComfyUI
server. (Nuance: the Colab job *clones* the Kijai ComfyUI repo on the runtime —
purely as a code vehicle for the precompiled rasterizer wheels and the vendored
`hy3dshape`/`hy3dgen` packages. No ComfyUI server ever runs; the gen calls are
headless python, exactly as proven in `_review/sculmm/gen_winston.py`.)

## Files

| File | What |
|---|---|
| `prop_order.py` | order front: `make_prop_order()` payload + `resolve_into_engine()` manifest law + CLI + `--selftest` |
| `../colab/scripts/mesh.py` | the gpu.mesh runner that executes on the runtime: stub default, real path = the proven chain (gate `real: true` in the payload or `GMP_MESH_REAL=1` on the runtime) |
| `../colab/colab_worker.py` | `run_job()`: provision/upload/exec/download/**stop**, 15-min cap |

## Flow

```
prop_order.make_prop_order(name, prompt, image?, target_faces=50000, seed?)
      payload {"kind":"mesh","prompt","name","target_faces","real":true,["image_b64","seed"]}
        |
        v
colab_worker.run_job(to_worker_job(payload, staging, done))     [queue host]
      colab new -s S --gpu T4 -> upload in.zip -> exec -f scripts/mesh.py
      -> poll result.json -> download out.zip -> colab stop        [Colab T4]
        |                                            |
        |                                            mesh.py real path: wrapper
        |                                            wheels + Hunyuan3D-2 snapshot,
        |                                            shape(256,20) -> paint -> glb
        v
done/out/<name>.glb + done/result.json
        |
        v
prop_order.resolve_into_engine(glb, <engine>/assets)
      copies to assets/props/<name>/<name>.glb
      merges manifest.json entry {"name","glb","source":"colab-hy3dgen","faces","created"}
```

Normalization law (mesh.py real path): meters-unit scale → min-z to floor →
frame kept as-generated (pilot "+z up" law). Face target from
`target_faces` (default 50000) via a best-effort `FaceReducer` (see gaps).
Gate safety: the real path arms ONLY on a Colab runtime (posix + `/content`
present) — an armed run anywhere else fails fast into the stub with
`real_error` instead of touching the host.

## First burn (when Colab auth lands)

The CLI is **Linux/macOS only** — run on the Ubuntu queue host (retromonkey) or
WSL, not bare Rog Windows.

```bash
# 0. one-time auth (human step) — the four Colab scopes, per cloud/colab/README.md
gcloud auth application-default login \
    --scopes=openid,\
https://www.googleapis.com/auth/cloud-platform,\
https://www.googleapis.com/auth/userinfo.email,\
https://www.googleapis.com/auth/colaboratory
colab whoami        # scopes present?
colab usage         # compute units left?

# 1. order a prop (repo root: game-making-pipeline/)
mkdir -p jobs/lantern/{stage,done}
python cloud/sculmm/prop_order.py make --name lantern \
    --prompt "wrought-iron lantern" --image reference.png \
    --target-faces 50000 --seed 7 --out jobs/lantern/payload.json

# 2. burn — the exact call the queue router makes
python - <<'PY'
import json
from cloud.sculmm.prop_order import make_prop_order, to_worker_job
from cloud.colab.colab_worker import run_job
payload = make_prop_order("lantern", "wrought-iron lantern",
                          image="reference.png", target_faces=50000, seed=7)
result = run_job(to_worker_job(payload, "jobs/lantern/stage", "jobs/lantern/done",
                               session="gmp-mesh-firstburn"))
print(json.dumps(result, indent=2))
PY
# expect result.json: {"ok": true, "stub": false, "faces": ~<=50000, "textured": true, ...}
# run_job tears the runtime down itself (colab stop) — nothing left running.

# 3. resolve into the engine assets
python cloud/sculmm/prop_order.py resolve \
    --glb jobs/lantern/done/out/lantern.glb \
    --assets-dir /path/to/engine/assets

# 4. smoke the offline halves any time (no network, no Colab):
python cloud/sculmm/prop_order.py --selftest
python cloud/colab/colab_worker.py --selftest
python cloud/colab/scripts/mesh.py --base <tmpdir-with-in.zip>   # stub path
```

Watch the first burn end-to-end. If `exec` output is queryable, the proven
progress markers are `SHAPE_LOADED / SHAPE_DONE / PAINT_LOADED / PAINT_DONE /
GLB_SAVED` plus this script's `[mesh] ...` lines.

## KNOWN_GAPS

1. **Nothing here has touched real Colab** — no auth from the dev box. The
   worker contract is proven by its fake-CLI selftest; the real path is a
   faithful rebuild of the proven sculmm scripts, not a watched run. First burn
   must be watched.
2. **15-min cap vs a cold chain.** `colab_worker.JOB_TIMEOUT_SEC = 900` covers
   pip installs (~3-5 min) + a ~5GB model snapshot + shape + paint. The proven
   session's timings were not preserved, so whether a cold job fits is
   unmeasured. Runtimes are fresh per job (always `colab stop`), so there is no
   warm-start; if the first burn trips `timeout_or_no_result`, the cap needs
   raising in `colab_worker.py` (one line — deliberate user decision, this task
   did not touch that file).
3. **Text-prompt-only generation is UNVERIFIED.** Every proven call is
   image-conditioned (`shape(image=img, ...)` — no prompt kwarg appears in
   `gen_winston.py`/`gen2.py`). Prompt-only orders are accepted by the order
   front but the real path REFUSES them and the job falls back to the stub with
   `real_error` explaining. If Hunyuan3D-2's text conditioning is wanted, plug
   it in at `_load_image`/the `shape(...)` call in mesh.py and re-prove.
4. **`FaceReducer` is unverified surface.** No readable proven script calls it
   (the pilot's ~50k faces are commit-message law). mesh.py tries
   `hy3dgen.shapegen.FaceReducer` then `hy3dshape.FaceReducer`; if both fail the
   mesh ships unreduced and `result.json` says `reduced: false`.
5. **"+z up" vs glTF Y-up.** The pilot law is +z up; the normalization carries
   no axis rotation. glTF-standard consumers that convert Y-up→Z-up will tilt
   these props. Fine inside the engine (which defined the law); flagged for
   anything else that reads these GLBs.
6. **clips.json: not derivable.** The readable proven tools export GLBs only —
   no clips convention survives in them, so `resolve_into_engine` is
   manifest-only. If the engine needs clip entries per prop, that law must come
   from the engine repo itself (outside this task's read scope).
7. **manifest.json shape assumed tolerant, not proven.** A bare-list manifest
   stays a bare list; a `{"props": [...]}` manifest merges in place; a fresh
   manifest is created as `{"props": [...]}`. Entry fields are the task law:
   `name, glb, source:"colab-hy3dgen", faces, created` (+ informational verts).
   Re-resolving a name replaces its entry (idempotent).
8. **`colab exec` semantics / free-tier provisioning / directory transfer** —
   inherited from `cloud/colab/README.md` KNOWN_GAPS (blocking semantics
   unverified, free-tier CLI provisioning unverified, only single-file
   transfers exercised — hence in.zip/out.zip).
9. **Windows dev box**: the `colab` CLI itself won't run on Rog; the worker
   returns `cli_missing`. Everything local (order front, resolve, stub, both
   selftests) is stdlib-only and runs anywhere.

## Verify (no network, no Colab)

```bash
python -m py_compile cloud/sculmm/prop_order.py cloud/colab/scripts/mesh.py
python cloud/sculmm/prop_order.py --selftest          # resolve + manifest law
python cloud/colab/scripts/mesh.py --base /tmp/smoke  # stub glb + result.json
python cloud/colab/colab_worker.py --selftest         # worker contract regression
```

---

# Animation lane (gpu.anim) — static prop + BVH -> animated asset

The third SCULMM lane. SCULMM = **S**cript **C**reation **U**nity for
**L**L**M**s — and an engine whose script acts needs rigged actors, so a
finished mesh-lane GLB plus a BVH motion file become an animated asset: one
baked animated GLB (default) or a PNG frame range (`render: true`). Same lane
law: **Colab CLI ONLY, T4, never Lappy.**

## Flow (the mesh -> anim -> render chain)

```
prop_order.resolve_into_engine(glb, assets)        mesh lane lands a prop
        |
        v
anim_order.make_anim_order(name, glb, bvh, frames) source paths validated here
      payload {"kind":"anim","name","glb","bvh","frames","fps","render","real":true}
        |
        v
anim_order.to_anim_job(payload, stage, done, glb_src, bvh_src)
        |   copies the glb+bvh bytes into staging, wraps the envelope
        v
colab_worker.run_job({"type": "gpu.anim", ...})    [queue host]
      colab new -s S --gpu T4 -> upload in.zip -> exec -f scripts/anim.py
      -> poll result.json -> download out.zip -> colab stop   [Colab T4]
        |                                         |
        |                                         anim.py real path: blender
        |                                         tarball -> import GLB + BVH
        |                                         -> RIGID bind -> bake/frames
        v
done/out/<name>.glb (animated) | done/out/frames/*.png   + done/result.json
        |                     {anim_seconds, frames, bind: "rigid-nearest-segment"}
        v
anim_order.resolve_anim_into_engine(glb, <engine>/assets)
      copies to assets/props/<name>/<name>.glb
      merges manifest entry {"name","glb","source":"colab-anim","animated":true,
                             "bind","faces","frames","created"}
        |
        v
gpu.render (scripts/render.py) can take the animated asset next — the lanes
chain through the same queue envelope, never through each other's internals.
```

## Files (animation lane)

| File | What |
|---|---|
| `anim_order.py` | order front: `make_anim_order()` payload + `to_anim_job()` staging/envelope + `resolve_anim_into_engine()` manifest law + CLI + `--selftest` |
| `../colab/scripts/anim.py` | the gpu.anim runner on the runtime: stub default (static cube GLB), real path = blender headless (gate `real: true` in the payload or `GMP_ANIM_REAL=1` on the runtime) |

`anim_order.py` imports `_glb_stats / _merge_manifest / _slug` from
`prop_order.py` — one manifest writer convention for the whole lane. Run it as
a direct script (`python cloud/sculmm/anim_order.py ...`), like prop_order.

## gpu.anim payload (job.json)

| key | default | meaning |
|---|---|---|
| `name` | — | asset name (slugged; names the output `<name>.glb`) |
| `glb` | — | basename of the staged static GLB (mesh-lane output) |
| `bvh` | — | basename of the staged BVH motion file |
| `frames` | 48 | frames to bake/render from the BVH action start |
| `fps` | 24 | playback rate written into the bake |
| `render` | false | false -> baked animated GLB; true -> Cycles PNG frames in `out/frames/` |
| `bvh_scale` | 0.01 | BVH cm -> engine meters (law knob; BVH units vary) |
| `samples` | 32 | Cycles samples per frame (render=true only) |
| `resolution` | 512 | square render resolution (render=true only) |
| `real` | — | the gate: worker env vars do NOT cross to the runtime, so `real: true` in the payload is the reliable arm |

## The anim laws

1. **RIGID nearest-segment bind** — every mesh vertex gets weight 1.0 on its
   nearest bone segment of the BVH armature. No falloff, no `ARMATURE_AUTO`:
   pilot v3 proved those weights inert (the actor stood at origin while the
   rig drove — the whole camera-off symptom). The bind is computed by hand in
   bpy, then a plain ARMATURE modifier carries it.
2. **The BVH import IS the retarget** — the BVH armature drives, the prop is
   skinned onto it. There is no second rig and no retarget map to maintain.
3. **Stub never fake-claims an animation** — the stub writes a static cube GLB
   and `bind: null`; a resolve of a stub artifact records `animated: false`.
4. **Blender arrives the render.py way** — release tarball at
   `GMP_BLENDER_VERSION` (default 4.2.3, ~350MB per fresh runtime), after
   `shutil.which("blender")` / `GMP_BLENDER_PATH`.
5. **Cycles CUDA, not OPTIX, not EEVEE** — T4 is Turing (no OPTIX) and EEVEE
   needs a GL context headless Colab lacks. Low sample counts: free-tier law.
6. **Never lose the slot** — any real-path failure falls through to the stub
   with `real_error`; the worker still gets its result.json and tears down.

## First burn (animation, when Colab auth lands)

```bash
# 0. auth + whoami + usage — identical to the mesh lane above.

# 1. order + stage + burn (repo root; Ubuntu host or WSL — the CLI is not Windows-native)
python cloud/sculmm/anim_order.py --make winston_walk \
    --glb-src jobs/winston/winston.glb --bvh-src jobs/winston/walk.bvh \
    --frames 48 --fps 24 --out jobs/winston/payload.json
python - <<'PY'
from cloud.sculmm.anim_order import make_anim_order, to_anim_job
from cloud.colab.colab_worker import run_job   # NOTE: needs the gpu.anim map line, see gaps
payload = make_anim_order("winston_walk", "jobs/winston/winston.glb",
                          "jobs/winston/walk.bvh", frames=48, fps=24)
result = run_job(to_anim_job(payload, "jobs/winston/stage", "jobs/winston/done",
                             "jobs/winston/winston.glb", "jobs/winston/walk.bvh",
                             session="gmp-anim-firstburn"))
print(result)
PY
# watch for BIND_REPORT {...} / GLB_ANIM_DONE (or FRAMES_DONE n) in the exec log

# 2. resolve into the engine assets
python cloud/sculmm/anim_order.py --resolve jobs/winston/done/out/winston_walk.glb \
    --assets-dir /path/to/engine/assets
```

## KNOWN_GAPS (animation lane)

1. **The real path has never executed.** No Colab auth from the dev box. The
   tarball install is render.py's carried pattern; every bpy call (glTF/BVH
   import, the hand bind, the GLB animation export, Cycles-on-CUDA) is written
   to API, not to a watched run. First burn must be watched end-to-end.
2. **`colab_worker.py` does not know `gpu.anim` yet.** Its runner map covers
   gpu.train/gpu.mesh/gpu.render only, so `run_job` returns `bad_job` for an
   anim envelope until one line lands there (`"gpu.anim": "anim.py"`). That
   file is outside this lane's edit scope — deliberate; reported to the user.
3. **`GMP_BLENDER_VERSION` 4.2.3 pin unconfirmed** (inherited render.py gap):
   the URL pattern is standard but the pin must be confirmed on first real
   run (~350MB per session — fresh runtime every job, no warm cache).
4. **`bvh_scale` default 0.01 (cm -> m) is a guess** at BVH units; first burn
   confirms against the actual motion files, knob fixes.
5. **15-min worker cap vs tarball + bake** — ~350MB download plus a bake may
   crowd `JOB_TIMEOUT_SEC` on a cold runtime; the stub fallback keeps the job
   honest either way.
6. **Animated resolve replaces the same-name mesh entry** — that is the
   idempotent merge law working as designed (the rigged GLB supersedes the
   static one); resolve under a different name to keep both.
7. **Exported frame is glTF Y-up** — the anim lane does not re-normalize; the
   input GLB is expected mesh-lane-normalized already.
8. **`anim_order.py` leans on `prop_order.py` internals** (`_glb_stats`,
   `_merge_manifest`, `_slug`) so the manifest law stays single-sourced; if
   prop_order refactors those names, anim_order follows.

## Verify (animation lane, no network, no Colab)

```bash
python -m py_compile cloud/colab/scripts/anim.py cloud/sculmm/anim_order.py
python cloud/sculmm/anim_order.py --selftest     # order + staging + resolve + manifest law
GMP_COLAB_BASE=<dir-with-in.zip> python cloud/colab/scripts/anim.py   # stub glb + result.json
```

---

# Audio lane (gpu.audio) — prompt/text -> engine sfx / music (voice TBD)

The fourth lane, after mesh / anim / render. A text prompt becomes a 44.1 kHz
wav in the engine's `assets/audio/<name>/`, loudness-normalized, plus an ogg
sibling when the runtime has ffmpeg (Godot-friendly). Same lane law:
**Colab CLI ONLY, T4, never Lappy.** Stub default, gated real path — identical
shape to the mesh lane.

## Flow

```
audio_order.make_audio_order(name, prompt|text, kind="sfx"|"music"|"voice",
                             seconds?, seed?)
      payload {"kind":"sfx","name","prompt","seconds","real":true,["seed"]}
              (voice carries "text" instead of "prompt")
        |
        v
audio_order.to_audio_job(payload, stage, done)     [no staged bytes; empty staging dir]
        |
        v
colab_worker.run_job({"type": "gpu.audio", ...})   [queue host]
      colab new -s S --gpu T4 -> upload in.zip -> exec -f scripts/audio.py
      -> poll result.json -> download out.zip -> colab stop   [Colab T4]
        |                                      |
        |                                      audio.py real path: pip diffusers/
        |                                      transformers/accelerate -> fp16 gen
        |                                      -> del + empty_cache -> normalize
        |                                      -> 44.1 kHz wav (+ ogg if ffmpeg)
        v
done/out/<name>.wav (+ <name>.ogg) + done/result.json
        |
        v
audio_order.resolve_into_engine(wav, <engine>/assets)
      copies to assets/audio/<name>/<name>.wav (+ the ogg sibling)
      merges manifest.json entry {"name","file","source":"colab-audio","kind",
                                  "seconds","created"} (+ informational
                                  sample_rate/channels/stub/ogg)
```

## Files (audio lane)

| File | What |
|---|---|
| `audio_order.py` | order front: `make_audio_order()` payload + `to_audio_job()` envelope + `resolve_into_engine()` manifest law + CLI + `--selftest` |
| `../colab/scripts/audio.py` | the gpu.audio runner on the runtime: stub default (0.5 s 440 Hz sine), real path = AudioLDM2 / MusicGen small (gate `real: true` in the payload or `GMP_AUDIO_REAL=1` on the runtime) |

`audio_order.py` imports `_merge_manifest / _slug` from `prop_order.py` (with a
relative-import and an isolated-copy fallback, so it imports in every mode) —
one manifest writer convention for the whole lane. Audio entries share the
manifest's single `props` list with mesh/anim entries; re-resolving a name
REPLACES its entry (idempotency law — resolve under a different name to keep
both).

## gpu.audio payload (job.json)

| key | default | meaning |
|---|---|---|
| `kind` | — | `sfx` \| `music` \| `voice` — the payload kind IS the audio kind |
| `prompt` | — | sfx/music conditioning text (`text` for voice; the runner reads either) |
| `name` | — | asset name (slugged; names the output `<name>.wav`) |
| `seconds` | sfx 5 / music 15 / voice 10 | clip length, hard-capped sfx 10 / music 30 for T4 (front raises; runner clamps) |
| `seed` | — | torch generator seed before the gen call |
| `real` | — | the gate: worker env vars do NOT cross to the runtime, so `real: true` in the payload is the reliable arm |

## The audio laws

1. **Models are picked, not proven** — sfx: `cvssp/audioldm2` via diffusers
   (fp16, 16 kHz native); music: `facebook/musicgen-small` via transformers
   (fp16, 32 kHz native, 50 audio tokens per second). Both are model-card
   call forms, never run — see KNOWN_GAPS.
2. **LOW-RAM discipline like the mesh lane** — fp16 everywhere, load ->
   generate -> `del` + `torch.cuda.empty_cache()` inside each gen fn (never
   two models resident), seconds capped per kind, idempotent pip setup under
   a `/content` marker, pin loop against idle reaping (hy_all law).
3. **Stub never fake-claims audio** — the stub writes a 0.5 s 440 Hz sine and
   `stub: true`; resolve records `stub: true` on the entry so a placeholder
   can't pass as generated.
4. **Output normalization law** — loudness-normalize to a ~ -18 dBFS RMS
   target (peak-capped at 0.98), linear resample to 44.1 kHz, mono int16 wav
   via stdlib `wave`. Ogg (libvorbis) sibling only when ffmpeg exists on the
   runtime; `result.json.ogg` is null otherwise.
5. **Voice refuses rather than fakes** — no proven free TTS is picked yet, so
   the real path raises and the job returns a stub sine with `real_error`
   explaining (bark is the candidate).

## First burn (audio, when Colab auth lands)

```bash
# 0. auth + whoami + usage — identical to the mesh lane above.
#    ALSO needs the colab_worker map line — see KNOWN_GAPS 2 (human's edit).

# 1. order (repo root; Ubuntu host or WSL — the CLI is not Windows-native)
python cloud/sculmm/audio_order.py --make coin_pickup \
    --prompt "retro coin pickup blip" --kind sfx --seconds 3 --seed 7 \
    --out jobs/audio/payload.json

# 2. burn — the exact call the queue router makes
python - <<'PY'
import json
from cloud.sculmm.audio_order import make_audio_order, to_audio_job
from cloud.colab.colab_worker import run_job   # needs the gpu.audio map line, see gaps
payload = make_audio_order("coin_pickup", "retro coin pickup blip",
                           kind="sfx", seconds=3, seed=7)
result = run_job(to_audio_job(payload, "jobs/audio/stage", "jobs/audio/done",
                              session="gmp-audio-firstburn"))
print(json.dumps(result, indent=2))
PY
# expect result.json: {"ok": true, "stub": false, "audio_seconds": ~3.0,
#                      "sr_out": 44100, "ogg": "coin_pickup.ogg"|null, ...}

# 3. resolve into the engine assets
python cloud/sculmm/audio_order.py --resolve jobs/audio/done/out/coin_pickup.wav \
    --assets-dir /path/to/engine/assets

# 4. smoke the offline halves any time (no network, no Colab):
python cloud/sculmm/audio_order.py --selftest
GMP_COLAB_BASE=<dir-with-in.zip> python cloud/colab/scripts/audio.py   # stub path
```

Watch the first burn end-to-end. Progress markers in the exec log:
`SFX_LOADED / SFX_DONE / MUSIC_LOADED / MUSIC_DONE / WAV_SAVED / OGG_DONE`
plus this script's `[audio] ...` lines.

## KNOWN_GAPS (audio lane)

1. **The real path has never executed.** No Colab auth from the dev box. The
   diffusers/transformers calls are written to the model cards' documented API
   (kwarg names `audio_length_in_s`, `max_new_tokens`, fp16 dtype, native
   rates) but unwatched. First burn must be watched end-to-end.
2. **`colab_worker.py` does not know `gpu.audio` yet.** Its runner map covers
   gpu.train/gpu.mesh/gpu.render only, so `run_job` returns `bad_job` for an
   audio envelope until the map line lands — and the same statement's
   `bad_job` message lists the allowed types in a second literal set, so the
   human's addition is two tokens in the one statement at `colab_worker.py`
   (`"gpu.audio": "audio.py"` in the dict + `"gpu.audio"` in the sorted set).
   That file is outside this lane's edit scope — deliberate; reported to the
   user.
3. **Voice is TBD.** No proven free TTS picked. Candidate: bark
   (`suno/bark`) — heavy for T4 (the small variant fits fp16, untested) and
   slower than the sfx/music paths, which may butt against the 15-min worker
   cap. Until a pick lands, `kind: "voice"` orders are valid on the wire and
   the real path refuses into the stub with `real_error`.
4. **Resample is linear interpolation, not sinc** — no scipy on the runtime
   by design (stdlib `wave` writes the file). Fine for game sfx/music, not
   for mastering; swap in `soxr`/scipy at `_resample_np` if fidelity ever
   matters.
5. **Model fp16 behavior on T4 is unverified** — musicgen-small fp16
   generation and audioldm2 fp16 are the documented recipes, but VRAM peaks
   and step counts (200 default for audioldm2, `GMP_AUDIO_STEPS` knob) are
   unmeasured until the first burn.
6. **Cold pip + model weights vs the 15-min cap** — audioldm2 is a multi-GB
   snapshot per fresh runtime (no warm cache, always `colab stop`); a cold
   sfx job may crowd `JOB_TIMEOUT_SEC` the same way the mesh lane's does.
   The stub fallback keeps the job honest either way.
7. **`GMP_AUDIO_REAL=1` only works for direct/manual exec runs** — worker env
   vars do not cross to the Colab runtime; `real: true` in the payload is the
   reliable arm (same law as mesh/anim).
8. **`audio_order.py` leans on `prop_order.py` internals** (`_merge_manifest`,
   `_slug`) so the manifest law stays single-sourced; if prop_order refactors
   those names, audio_order follows (its isolated-copy fallback would then
   silently diverge — watch for that on refactor).

## Verify (audio lane, no network, no Colab)

```bash
python -m py_compile cloud/colab/scripts/audio.py cloud/sculmm/audio_order.py
python cloud/sculmm/audio_order.py --selftest     # order + envelope + resolve + manifest law
GMP_COLAB_BASE=<dir-with-in.zip> python cloud/colab/scripts/audio.py   # stub wav + result.json
```
