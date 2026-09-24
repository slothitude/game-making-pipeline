# cloud/colab CACHE.md — the cold-start cache

Colab runtimes are throwaway, so every `gpu.*` job re-paid the same setup tax:
pip resolving/downloading the ComfyUI + wrapper + diffusers stack, and the HF
hub re-downloading multi-GB weights onto a fresh VM. The cold-start cache moves
both to the retromonkey box and serves them over HTTPS so a session pulls one
tar instead.

## The cache

| | |
|---|---|
| Capability URL | `https://retromonkey.com.au/cc/fbf37bb3bf14379a/` |
| Server dir | `/home/ubuntu/site/cc/fbf37bb3bf14379a/` (auto-served by the site's `file_server`; unguessable path, contents are public artifacts) |
| Files | `wheels.tar` (PyPI wheels, py3.11 manylinux x86_64), `weights.tar` (HF cache layout, **not built yet — see below**) |
| `wheels.tar` size | ~250 MB (see the size line in the refresh log) |
| Untar layout | `wheels/` → `pip --no-index --find-links`; `weights/hub/models--*` → `HF_HOME` |

`wheels.tar` covers the union of what the runners install: `scripts/mesh.py`
(ComfyUI `requirements.txt` + kijai ComfyUI-Hunyuan3DWrapper `requirements.txt`
/ `requirements_extras.txt`) and `scripts/audio.py` (`diffusers`,
`transformers`, `accelerate`), plus their transitive closures.

Deliberately **not** in the tar (all non-fatal, the runners fall back per-line
to the normal index):

- `torch` / `torchvision` / `torchaudio` — Colab ships them (a CUDA torch wheel
  set is ~3 GB and would dwarf the useful cache).
- `opencv-python`, `numpy`, `scipy` — Colab preinstalls them (~145 MB saved).
- `diso` — no py3.11 binary wheel on PyPI (sdist + CUDA build) → normal pip.
- `nvdiffrast`, `utils3d` (git/sdist, CUDA-jit at import) → normal pip.
- The kijai wrapper's own `wheels/*.whl` (nvidia rasterizer/mesh .so) — those
  come with the `git clone` mesh.py already does, so they are not duplicated.
- Blender (anim/render, ~350 MB tarball from download.blender.org) — not a pip
  or HF artifact; next candidate if the box ever has the disk.

`weights.tar` is **not built**: the three repos the runners use are
`tencent/Hunyuan3D-2` (~9.5 GB), `cvssp/audioldm2` (~3.4 GB) and
`facebook/musicgen-small` (~2.3 GB) ≈ 15 GB total, and the 49 GB retromonkey
disk sits at 95-99% full. Even one repo does not fit. The runners are already
wired for it (`curl --fail || fallback` + `HF_HOME`), so dropping a
`weights.tar` into the cache dir lights it up with no code change.

## How the runners consume it

`scripts/mesh.py` + `scripts/audio.py` each carry the same bootstrap
(`CC_BASE`, `_cc_bootstrap`, `_pip`): `curl` the tars into `/content/gmp_cc`,
untar, then install `--no-index --find-links` when the cache satisfies the
whole set, else per-requirement-line cache-first with the normal index as the
last resort. When `weights.tar` lands, `HF_HOME` points at the untarred cache
so `snapshot_download` / `from_pretrained` go offline. A cache miss only logs —
the job then pays the old download path.

## Refresh (3 lines)

```bash
# 1. rebuild the wheels into /tmp/wheels (edit the CLOSURE/NODEPS lists if a runner's imports changed)
ssh retromonkey 'nohup bash /tmp/cc_build_wheels.sh >/dev/null 2>&1 & sleep 2; tail -f /tmp/cc_wheels.log'
# 2. weights, only once the box has ~20 GB free (df -h /): snapshot each repo into a fresh
#    HF_HOME dir, tar the *hub/* dir preserving models--org--name layout -> weights.tar
# 3. both tars land in /home/ubuntu/site/cc/fbf37bb3bf14379a/ and are served instantly; bump
#    CC_BASE in scripts/*.py ONLY if you regenerate the random path segment
```
