"""mesh.py — the gpu.mesh runner (image -> normalized .glb prop). EXECUTES ON THE COLAB RUNTIME.

Contract with cloud/colab/colab_worker.py (the only thing that ships it here):
  - in.zip contains work/job.json — job.json payload:
      {"prompt": str?,           # recorded; NOT a proven conditioning input (see below)
       "image_b64": str?,        # primary input: PNG/JPG, raw base64 or data URL
       "name": "lantern",        # out glb name -> out/lantern.glb (default mesh.glb)
       "target_faces": 50000,    # face-count target (pilot v2 law)
       "seed": int?,             # torch.manual_seed before the shape call
       "real": true?}            # alt gate to env GMP_MESH_REAL=1
    plus any staged files (work/images/* also accepted as the image source)
  - this script writes out/<name>.glb + result.json, zips out/ -> out.zip
  - the worker downloads out.zip + result.json and tears the runtime down.

STATUS OF THE CODE
  DEFAULT PATH = STUB: emits a valid glTF 2.0 cube (stdlib-only, runs anywhere)
  so the downstream pipeline always gets a well-formed .glb and a truthful
  result.json. Never fake-claims a mesh.
  REAL PATH = rebuilt on the PROVEN sculmm chain (C:/Users/aaron/audio_book/
  _review/sculmm/ — hy_setup3.py, hy_all.py, gen_winston.py, gen2.py), Colab T4:
    setup : ComfyUI clone (code vehicle only, no server) -> kijai
            ComfyUI-Hunyuan3DWrapper -> pip -r requirements.txt ->
            pip wheels/*.whl (precompiled nvidia rasterizer/mesh .so) ->
            pip -r requirements_extras.txt -> snapshot_download("tencent/Hunyuan3D-2")
            copied flat to /content/models/hunyuan3d
    gen   : from hy3dshape import Hunyuan3DDiTFlowMatchingPipeline
            from hy3dgen.texgen import Hunyuan3DPaintPipeline
            shape(image=img, octree_resolution=256, num_inference_steps=20)[0]
            [del + cuda.empty_cache] paint(image=img, mesh=mesh) -> export
  Everything not evidenced by those scripts is marked UNVERIFIED inline and in
  cloud/sculmm/README.md KNOWN_GAPS. In particular:
    - text-prompt conditioning is NOT in the proven calls (image= only); the
      prompt rides along as metadata and the real path REFUSES prompt-only jobs.
    - face reduction (FaceReducer) and the m/floor normalization are pilot-v2
      law carried from the draft, not from the readable proven scripts.
  First real run must be watched (VRAM fits T4; cold setup may butt against the
  worker's 15-min cap — see README KNOWN_GAPS).

Local sanity run (stub, no network):
  python scripts/mesh.py --base ./selftest_base     # in.zip with job.json inside
"""
import argparse
import base64
import glob as _glob
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# proven constants (hy_setup3.py / hy_all.py / gen_winston.py, sculmm rounds)
COMFYUI_URL = "https://github.com/comfyanonymous/ComfyUI.git"
WRAPPER_URL = "https://github.com/kijai/ComfyUI-Hunyuan3DWrapper.git"
COMFYUI_DIR = Path("/content/ComfyUI")
WRAPPER_DIR = COMFYUI_DIR / "custom_nodes" / "ComfyUI-Hunyuan3DWrapper"
MODEL_REPO = "tencent/Hunyuan3D-2"
MODEL_DIR = Path("/content/models/hunyuan3d")
ENV_DONE_MARKER = Path("/content/.hy_env_done")
OCTREE_RESOLUTION = int(os.environ.get("GMP_MESH_OCTREE", "256"))   # proven value
NUM_INFERENCE_STEPS = int(os.environ.get("GMP_MESH_STEPS", "20"))   # proven value
DEFAULT_FACES = int(os.environ.get("GMP_MESH_FACES", "50000"))      # pilot v2 law


def log(msg):
    print(f"[mesh] {msg}", flush=True)


def _locate_base(explicit=None) -> Path:
    for cand in ([Path(explicit)] if explicit else []) + \
                [Path(os.environ.get("GMP_COLAB_BASE", "/content/gmp_job")), Path.cwd()]:
        if (cand / "in.zip").exists():
            return cand
    raise FileNotFoundError(f"in.zip not found (base={explicit} env={os.environ.get('GMP_COLAB_BASE')} cwd={Path.cwd()})")


def _load_work(base: Path) -> dict:
    work = base / "work"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    with zipfile.ZipFile(base / "in.zip") as z:
        z.extractall(work)
    jf = work / "job.json"
    return json.loads(jf.read_text(encoding="utf-8")) if jf.exists() else {}


# ---------------------------------------------------------------------------
# STUB: minimal valid glTF 2.0 binary (GLB) — one cube, stdlib only.
# ---------------------------------------------------------------------------
def _cube_glb() -> bytes:
    """Hand-rolled GLB: 24-vert cube, 36 indices, POSITION accessor only."""
    # 6 faces x 4 verts
    positions, indices = [], []
    axes = [((1, 0, 0), (0, 1, 0), (0, 0, 1)), ((-1, 0, 0), (0, 1, 0), (0, 0, -1)),
            ((0, 1, 0), (0, 0, 1), (1, 0, 0)), ((0, -1, 0), (0, 0, 1), (-1, 0, 0)),
            ((0, 0, 1), (1, 0, 0), (0, 1, 0)), ((0, 0, -1), (1, 0, 0), (0, -1, 0))]
    for n, u, v in axes:
        base_i = len(positions)
        for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            positions.append(tuple(0.5 * (n[k] + su * u[k] + sv * v[k]) for k in range(3)))
        indices += [base_i, base_i + 1, base_i + 2, base_i, base_i + 2, base_i + 3]
    pos_bin = struct.pack(f"<{len(positions) * 3}f", *[c for p in positions for c in p])
    idx_bin = struct.pack(f"<{len(indices)}H", *indices)
    bin_chunk = pos_bin + idx_bin

    gltf = {
        "asset": {"version": "2.0", "generator": "gmp gpu.mesh stub"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "gmp_stub_cube"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "buffers": [{"byteLength": len(bin_chunk)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bin), "target": 34962},
            {"buffer": 0, "byteOffset": len(pos_bin), "byteLength": len(idx_bin),
             "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(positions),
             "type": "VEC3",
             "min": [-0.5] * 3, "max": [0.5] * 3},
            {"bufferView": 1, "componentType": 5123, "count": len(indices),
             "type": "SCALAR"},
        ],
    }
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * ((4 - len(js) % 4) % 4)
    bin_chunk += b"\x00" * ((4 - len(bin_chunk) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(bin_chunk)
    header = struct.pack("<III", 0x46546C67, 2, total)
    return (header + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(bin_chunk), 0x004E4942) + bin_chunk)


# ---------------------------------------------------------------------------
# REAL PATH — the proven sculmm chain (hy_setup3/hy_all/gen_winston), Colab T4.
# Gate: env GMP_MESH_REAL=1 or payload.real == true. UNVERIFIED parts marked.
# ---------------------------------------------------------------------------
def _pin_loop():
    """Proven anti-reap trick (hy_all.py: idle T4s get reaped — ARDY playbook):
    a background no-op touching a log every 60s for the job's lifetime."""
    try:
        p = subprocess.Popen(
            ["bash", "-lc",
             "( while true; do date +\"PIN %s\" >> /content/gmp_pin.log; sleep 60; done )"],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("pin loop up (anti-reap, hy_all law)")
        return p
    except Exception as exc:  # a dead pin loop must never kill the job
        log(f"pin loop skipped: {exc}")
        return None


def _sh(cmd, **kw):
    log(f"$ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    return subprocess.run(cmd, check=True, **kw)


def _pip(*args):
    _sh([sys.executable, "-m", "pip", "install", "-q", *args])


def _setup_env() -> None:
    """Idempotent proven setup (hy_setup3.py / hy_all.py law, order preserved).
    NOTE: the proven scripts never `pip install hy3dgen` or torch — Colab ships
    torch and the wrapper vendors the hy3dshape/hy3dgen packages. Kept that way."""
    if ENV_DONE_MARKER.exists():
        log("setup: env marker found — skipping (idempotent re-entry)")
        return
    t0 = time.time()

    import torch  # runtime-provided (launch_gen.py proves this is the check that matters)
    log(f"torch {torch.__version__} cuda={torch.cuda.is_available()}")

    if not COMFYUI_DIR.exists():
        _sh(["git", "clone", "-q", COMFYUI_URL, str(COMFYUI_DIR)])
    _pip("-r", str(COMFYUI_DIR / "requirements.txt"))

    if not WRAPPER_DIR.exists():
        _sh(["git", "clone", "-q", WRAPPER_URL, str(WRAPPER_DIR)])
    _pip("-r", str(WRAPPER_DIR / "requirements.txt"))
    wheels = sorted(_glob.glob(str(WRAPPER_DIR / "wheels" / "*.whl")))
    if not wheels:
        raise FileNotFoundError(f"no precompiled wheels in {WRAPPER_DIR}/wheels "
                                "(the wrapper's nvidia rasterizer/mesh .so builds)")
    _pip(*wheels)                                   # proven: pip install wheels/*.whl
    _pip("-r", str(WRAPPER_DIR / "requirements_extras.txt"))

    # model snapshot -> flat /content/models/hunyuan3d (proven path + max_workers=16)
    if not (MODEL_DIR / "model_index.json").exists():
        from huggingface_hub import snapshot_download
        snap = snapshot_download(MODEL_REPO, max_workers=16)
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        for root, _dirs, files in os.walk(snap):
            for f in files:
                src = os.path.join(root, f)
                rel = os.path.relpath(src, snap)
                dst = MODEL_DIR / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dst)
    ENV_DONE_MARKER.write_text("ok\n", encoding="utf-8")
    log(f"setup done in {time.time() - t0:.0f}s")


def _load_image(payload: dict, work: Path):
    """Proven input is an RGBA PIL image (gen_winston.py). Primary source: the
    job's image_b64; fallback: staged work/images/*."""
    import io
    from PIL import Image
    b64 = payload.get("image_b64") or ""
    if b64:
        if "," in b64 and b64.strip().startswith("data:"):  # data URL form
            b64 = b64.split(",", 1)[1]
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA"), "payload.image_b64"
    staged = sorted((work / "images").glob("*")) if (work / "images").exists() else []
    if staged:
        return Image.open(staged[0]).convert("RGBA"), f"staged:{staged[0].name}"
    raise FileNotFoundError(
        "real mesh path is IMAGE-conditioned (proven law): job needs payload.image_b64 "
        "or staged work/images/* — text-prompt-only generation is UNVERIFIED in the "
        "sculmm chain and is refused here")


def _reduce_faces(mesh, target: int):
    """UNVERIFIED surface: FaceReducer lives in the hy3dgen package family but no
    readable proven script calls it (pilot law says ~50k faces out). Best-effort
    cascade; if unavailable the mesh ships unreduced and result.json says so."""
    import importlib
    for mod, attr in (("hy3dgen.shapegen", "FaceReducer"), ("hy3dshape", "FaceReducer")):
        try:
            reducer = getattr(importlib.import_module(mod), attr)()
            mesh = reducer(mesh, max_facenum=int(target))
            return mesh, True, mod
        except Exception as exc:
            log(f"face reducer via {mod}.{attr} unavailable: {type(exc).__name__}: {exc}")
    return mesh, False, None


def _normalize(mesh):
    """Pilot v2 normalization law (carried from the draft block; the readable
    proven scripts export raw): meters-scale unit, min-z -> floor, frame kept
    as-generated (recorded as +z up). NOTE glTF convention is Y-up — see
    cloud/sculmm/README.md KNOWN_GAPS before consuming this outside the engine."""
    ext = float(max(mesh.extents.max(), 1e-6))
    mesh.apply_scale(1.0 / ext)
    mesh.apply_translation([0.0, 0.0, -float(mesh.bounds[0][2])])
    return mesh


def _require_colab_runtime() -> None:
    """The real path provisions pip packages, clones repos into /content and
    needs CUDA torch — it may ONLY run on the Colab runtime. (Found the hard
    way: a dev box with cpu-torch sails past an import check and starts
    cloning into C:\\content.) Gated runs elsewhere fail fast into the stub."""
    if os.name != "posix" or not Path("/content").exists():
        raise RuntimeError(
            "real mesh path requires the Colab Linux runtime (/content present); "
            f"refusing on {sys.platform} — leave the gate off on dev boxes, the "
            "stub path is the default")


def _real_mesh(payload: dict, work: Path, out: Path, glb_name: str) -> dict:
    _require_colab_runtime()
    """Full proven chain; writes out/<glb_name> (normalized, reduced) and returns
    the stats that go verbatim into result.json."""
    t0 = time.time()
    timings = {}

    pin = _pin_loop()
    try:
        t = time.time()
        _setup_env()
        timings["setup_s"] = round(time.time() - t, 1)

        sys.path.insert(0, str(WRAPPER_DIR))        # proven: wrapper on sys.path
        img, image_source = _load_image(payload, work)

        seed = payload.get("seed")
        if seed is not None:
            import torch
            torch.manual_seed(int(seed))            # generic; pipeline kwarg UNVERIFIED

        # proven shape-stage import (gen_winston.py form first, gen2.py fallback)
        try:
            from hy3dshape import Hunyuan3DDiTFlowMatchingPipeline
        except ImportError:
            from hy3dshape.pipelines import Hunyuan3DDiTFlowMatchingPipeline
    except Exception:
        if pin:
            pin.kill()
        raise

    import torch
    try:
        shape = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(str(MODEL_DIR))
        log("SHAPE_LOADED %.0fs" % (time.time() - t0))
        mesh = shape(image=img, octree_resolution=OCTREE_RESOLUTION,
                     num_inference_steps=NUM_INFERENCE_STEPS)[0]
        timings["shape_s"] = round(time.time() - t0, 1)
        log("SHAPE_DONE faces=%d VRAM %.1fGB" % (
            len(mesh.faces), torch.cuda.max_memory_allocated() / 1e9))
        shape_only = out / "shape_only.glb"
        mesh.export(str(shape_only))                # proven secondary output
        del shape
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
    except Exception:
        if pin:
            pin.kill()
        raise

    textured = True
    try:
        t = time.time()
        from hy3dgen.texgen import Hunyuan3DPaintPipeline   # proven import + call
        paint = Hunyuan3DPaintPipeline.from_pretrained(str(MODEL_DIR))
        log("PAINT_LOADED %.0fs" % (time.time() - t))
        mesh = paint(image=img, mesh=mesh)                  # proven call
        timings["paint_s"] = round(time.time() - t, 1)
        log("PAINT_DONE VRAM %.1fGB" % (torch.cuda.max_memory_allocated() / 1e9))
        del paint
        torch.cuda.empty_cache()
    except Exception as exc:
        # shape-only glb is also a proven output (gen_winston.py) — degrade, don't die
        log(f"paint stage FAILED ({exc}) -> shipping the shape-only glb")
        textured = False
        mesh = None
    finally:
        if pin:
            pin.kill()

    if mesh is None:  # paint failed: re-load the proven shape-only export
        import trimesh
        mesh = trimesh.load(str(out / "shape_only.glb"), force="mesh", process=False)

    target = int(payload.get("target_faces") or DEFAULT_FACES)
    mesh, reduced, reducer_mod = _reduce_faces(mesh, target)
    mesh = _normalize(mesh)

    glb = out / glb_name
    mesh.export(str(glb))
    return {"image_source": image_source,
            "faces": int(len(mesh.faces)), "verts": int(len(mesh.vertices)),
            "target_faces": target, "reduced": reduced, "reducer": reducer_mod,
            "textured": textured, "octree_resolution": OCTREE_RESOLUTION,
            "steps": NUM_INFERENCE_STEPS, "seed": seed,
            "timings": timings, "vram_peak_gb": round(
                float(torch.cuda.max_memory_allocated()) / 1e9, 2)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="gpu.mesh runner (stub default, gated real path)")
    ap.add_argument("--base", default=None,
                    help="job base dir holding in.zip (default $GMP_COLAB_BASE, /content/gmp_job, cwd)")
    args = ap.parse_args(argv)

    t0 = time.time()
    base = _locate_base(args.base)
    job = _load_work(base)
    payload = job.get("payload", {})
    prompt = payload.get("prompt", "")
    name = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(payload.get("name") or "")) or "mesh"
    out = base / "out"
    out.mkdir(parents=True, exist_ok=True)
    glb_name = f"{name}.glb"

    want_real = os.environ.get("GMP_MESH_REAL") == "1" or payload.get("real") is True
    real_err = None
    result = None
    if want_real:
        try:
            stats = _real_mesh(payload, base / "work", out, glb_name)
            glb = out / glb_name
            result = {"ok": True, "type": "gpu.mesh", "stub": False,
                      "pipeline": "hy3dshape + hy3dgen.texgen (Kijai wrapper, tencent/Hunyuan3D-2), sculmm chain",
                      "name": name, "glb": f"out/{glb_name}", "prompt": prompt,
                      "glb_bytes": glb.stat().st_size, **stats}
            log(f"GLB_SAVED {glb}")
        except Exception as exc:  # fall through to the stub, never lose the slot
            log(f"real path FAILED ({type(exc).__name__}: {exc}) -> stub mesh so the job still returns")
            real_err = f"{type(exc).__name__}: {exc}"

    if not want_real or real_err:
        glb = _cube_glb()
        (out / glb_name).write_bytes(glb)
        result = {"ok": True, "type": "gpu.mesh", "stub": True,
                  "stub_note": "valid glTF 2.0 cube; no image conditioning",
                  "real_path": "GMP_MESH_REAL=1 -> proven sculmm hy3dgen chain (see module docstring)",
                  "real_error": real_err,
                  "name": name, "glb": f"out/{glb_name}",
                  "prompt": prompt,
                  "verts": 24, "faces": 12,
                  "glb_bytes": len(glb),
                  "seconds": round(time.time() - t0, 2)}
    else:
        result["seconds"] = round(time.time() - t0, 2)
        result["total_s"] = round(time.time() - t0, 1)

    (base / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    with zipfile.ZipFile(base / "out.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(out).as_posix())
    log(f"done: stub={result['stub']} glb={result['glb']} verts={result.get('verts')} "
        f"faces={result.get('faces')} total={result['seconds']}s")


if __name__ == "__main__":
    main()
