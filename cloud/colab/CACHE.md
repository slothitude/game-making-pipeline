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
| Server dir | `/home/ubuntu/site/cc/fbf37bb3bf14379a/` (auto-served by the site's `file_server`; unguessable path, contents are public artifacts; dir itself 404s, no listing) |
| `wheels.tar` | PyPI wheels for **Python 3.12** manylinux x86_64 (Colab's default runtime since mid-2025 — verified live: torch 2.11.0+cu128; cp311 wheels are rejected there with "from versions: none") |
| `weights.tar` | **not built** — see the verdict below |
| Untar layout | `wheels/` → `pip --no-index --find-links`; `weights/hub/models--*` → `HF_HOME` |

Measured on a real Colab T4 (2026-09-24, three probe sessions): the tar pulls
at **4.7-5.5 MB/s single-stream** (4-way parallel ranges are *slower*, 3.6 MB/s),
so the ~0.8 GB tar costs **~2.5-3 min per session**; untar 4 s. Caddy serves the
same file at 4.6 MB/s from the box itself — the box is the ceiling, not the path.

`wheels.tar` covers the union of what the runners install: `scripts/mesh.py`
(ComfyUI `requirements.txt` + kijai ComfyUI-Hunyuan3DWrapper `requirements.txt`
/ `requirements_extras.txt`) and `scripts/audio.py` (`diffusers`,
`transformers`, `accelerate`), plus their transitive closures.

Deliberately **not** in the tar (all non-fatal, the runners fall back per-line
to the normal index):

- `torch` / `torchvision` / `torchaudio` — Colab ships them (a CUDA torch wheel
  set is ~3 GB and would dwarf the useful cache).
- `pymeshlab` — **no py3.12 binary wheel exists on PyPI** (cp311 is the newest),
  so it cannot be cached for today's runtime; the wrapper's requirements line
  will not resolve on Colab py3.12 with or without the cache.
- `opencv-python` — Colab preinstalls cv2.

Resolution check (pip's own resolver, per requirement line, against the built
tar with py3.12/manylinux flags): every line of ComfyUI `requirements.txt` +
wrapper `requirements.txt`/`requirements_extras.txt` resolves except the
documented gaps above, `git+…utils3d`/`nvdiffrast` (git/sdist), and one
version-drift case — ComfyUI pins `comfyui-frontend-package==<exact>` and the
pin had already moved past the cached wheel. That drift is inherent (the
requirements file is re-fetched from the clone every session); the per-line
fallback installs it from the index, so a stale line costs seconds, not the job.

- `diso` — no py3.12 binary wheel (sdist + CUDA build) → normal pip.
- `nvdiffrast`, `utils3d` (git/sdist, CUDA-jit at import) → normal pip.
- The kijai wrapper's own `wheels/*.whl` (nvidia rasterizer/mesh .so) — those
  come with the `git clone` mesh.py already does, so they are not duplicated.
- Blender (anim/render, ~350 MB tarball from download.blender.org) — not a pip
  or HF artifact; next candidate if the box ever has the disk.

**`weights.tar` verdict: not built, and probably never worth building here.**
The three repos the runners use are `tencent/Hunyuan3D-2` (~9.5 GB),
`cvssp/audioldm2` (~3.4 GB) and `facebook/musicgen-small` (~2.3 GB) ≈ 15 GB
total. Two blockers: (a) disk — the 49 GB box swings between 0.5 GB and 14 GB
free with other agents working, and a tar needs 2x headroom; (b) bandwidth —
at the measured 5.5 MB/s ceiling, the 9.5 GB Hunyuan repo alone is a ~30 min
pull, which is no faster than Colab pulling from HF's own CDN. The runners are
already wired for it (`curl --fail || fallback` + `HF_HOME`), so dropping a
`weights.tar` into the cache dir lights it up with no code change.

Measured on the same T4 (honest A/B): installing 6 non-preinstalled mesh-lane
packages from the cache takes ~1 s vs ~5 s from PyPI, and
`diffusers`/`transformers`/`accelerate` are **preinstalled** on today's image —
so the cache's real value is decoupling setup from PyPI/HF availability and
skipping the big wheels, not shaving seconds off pure-python installs.

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
# 1. edit the NODEPS/CLOSURE lists in ~/pipeline/colab/cc_build_wheels.sh on the box (persisted
#    copy of the build script) if a runner's imports changed, then:  ssh retromonkey \
#    'cp ~/pipeline/colab/cc_build_wheels.sh /tmp/; nohup setsid bash /tmp/cc_build_wheels.sh </dev/null >/dev/null 2>&1 &'
# 2. watch /tmp/cc_wheels.log — it swaps /home/ubuntu/site/cc/<hex>/wheels.tar in atomically
#    only when the wheel count looks sane (>=60); otherwise the old tar keeps serving
# 3. weights (only if the disk/bandwidth verdict above ever flips): snapshot the repos into a
#    fresh HF_HOME, tar the hub/ dir preserving models--org--name layout -> drop it as weights.tar
```
