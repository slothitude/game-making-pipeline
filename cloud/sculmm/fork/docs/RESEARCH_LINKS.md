# Research Links — the LLM 3D SCUMM arsenal

Every external tool/model surfaced for the pipeline, organized by role.
Verified facts marked; the rest as reported. (Compiled 2026-09-20.)

## Motion — text to skeletal animation

| Link | What | Facts |
|---|---|---|
| https://github.com/nv-tlabs/kimodo | **Kimodo** (NVIDIA Toronto) — kinematic motion diffusion, ~700h commercial mocap | Apache-2.0 code; ~17GB VRAM full-GPU, <3GB with TEXT_ENCODER_DEVICE=cpu; text prompts + **kinematic constraints** (pose keyframes, end-effector targets, 2D root paths/waypoints); NPZ out (posed_joints, rot mats, foot contacts) — same shape as ARDY's |
| (successor, in use) | **ARDY** — Kimodo's real-time successor; SIGGRAPH 2026, v0.2.0 | PROVEN end-to-end on free Colab T4 (see ~/ARDY_CLI_PLAYBOOK.md on Lappy); CoreSkeleton27 ≈ Mixamo 1:1; converter done (`ardy_to_bones.py`) |
| https://github.com/localai-org/kimodo | Kimodo CPP port (community) | unexamined — candidate for local low-VRAM motion gen on Lappy |
| https://research.nvidia.com/labs/sil/... | NVIDIA Research page for the lab | background |

## Characters — image to 3D mesh

| Link | What | Facts |
|---|---|---|
| https://huggingface.co/microsoft/TRELLIS.2-4B | **TRELLIS.2-4B** (Microsoft) — image → textured 3D | search-verified: full asset in ~7 min on a 6GB CUDA GPU (Lappy 3060 qualifies); ComfyUI-native (no compiled CUDA extensions); GLB export; PBR materials per community reports |
| https://huggingface.co/Comfy-Org/TRELLIS-2 | ComfyUI repackaging of TRELLIS 2 | 8GB min low-res / 16GB+ full-res textured (one guide's numbers; the 6GB claim above is the community low-res path) |
| https://huggingface.co/Comfy-Org/Pixal3D | **Pixal3D** (TencentARC, ComfyUI repack) — image → 3D | MIT license; DINOv3 image conditioning; shape + TEXTURE VAEs included; **multiview variants** (bf16 + int8) — multi-image conditioning; 404k downloads/mo |
| (in use) | Hunyuan3D-2 (`hy3dgen`) | shape PROVEN on T4; paint pipeline broken for us (VAE load hard-kill; loader suspects logged) — superseded by TRELLIS.2/Pixal3D for textured output |
| https://www.mixamo.com | Mixamo — rig + motion library | browser-only upload (the rig gap workaround); 38 clips already staged locally |
| https://actorcore.reallusion.com/auto_rig | **AccuRIG** (Reallusion) — free auto-rigger | Windows desktop (Rog!): guide markers → skeleton + skin weights → rigged FBX; solves the rig gap without browser uploads; needs Reallusion account on first run |
| https://3daistudio.lemonsqueezy.com/ | 3D AI Studio Flow | paid; unexamined fallback |

## Art — 2D generation

| Link | What | Facts |
|---|---|---|
| NVIDIA flux.2-klein endpoint (ai.api.nvidia.com) | FLUX character/scene sheets | PROVEN factory law (sprites.py: seed-family identity lock, 500-char budget, green lead); key at ~/.nvapi on Lappy |
| HunyuanDiTPipeline (hy3dgen.text2image) | Tencent 2D image gen | PROVEN on T4 (the chair smoke test) — also the image stage of the t2d3 chain |

## Engine / tooling

| Link | What | Facts |
|---|---|---|
| https://www.blender.org/download | Blender | 5.0 on Rog, 4.3.2 on Lappy — both proven headless in this pipeline |
| https://colab.research.google.com + `colab` CLI (v0.6.0) | Free T4 compute | oauth stored on Lappy; kick/poll law documented; sessions reaped ~hourly under load — idempotent chains are mandatory |
| ComfyUI (blog.comfy.org) | Node runtime hosting TRELLIS.2 + Pixal3D natively | "No custom nodes, no compiled CUDA extensions" (Aug 2026) — the deployment path for the 3D stack |

## Reading / background

- https://www.earngenix.com/workflows/trellis2-image-to-3d-comfyui — TRELLIS 2 ComfyUI workflow guide
- https://trellis2.app/blog/trellis-2-comfyui — install + workflow (GLB export notes)
- https://www.nextdiffusion.ai/tutorials/trellis-2-pixal3d-native-image-to-3d-generation-inside-comfyui — TRELLIS.2 + Pixal3D tutorial (VRAM notes)
- https://blog.comfy.org/p/trellis2-and-pixal3d-are-now-native — official native-support announcement
- https://www.reddit.com/r/StableDiffusion/comments/1v4k3je/ — the 6GB / 7-min TRELLIS.2 claim
- https://www.reddit.com/r/StableDiffusion/comments/1pr2anl/ — ComfyUI-TRELLIS2 release thread (PBR materials)
