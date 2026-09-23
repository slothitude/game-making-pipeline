"""mesh.py — the gpu.mesh runner (image -> .glb). EXECUTES ON THE COLAB RUNTIME.

Contract with cloud/colab/colab_worker.py (the only thing that ships it here):
  - in.zip contains work/job.json ({"payload": {"prompt": ...}}) and work/images/*
  - this script writes out/mesh.glb + result.json, zips out/ -> out.zip
  - the worker downloads out.zip + result.json and tears the runtime down.

STATUS OF THE CODE
  DEFAULT PATH = STUB: emits a valid, textured-nothing glTF 2.0 cube
  (stdlib-only, runs anywhere) so the downstream pipeline always gets a
  well-formed .glb and a truthful result.json. Never fake-claims a mesh.
  REAL PATH = the proven hy3dgen shape chain from the sculmm pilot v2
  (Colab T4: telescope / star_table / lantern / chair, normalized
  meters / floor / +z, ~50k faces), carried over and gated behind
  GMP_MESH_REAL=1 or payload.real=true. That block is proven IN THE SCULMM
  REPO, not in this file — first real run must be watched (VRAM ~6GB, fits T4).

Local sanity run:
  python scripts/mesh.py --base ./selftest_base
"""
import json
import os
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path

BASE = Path(os.environ.get("GMP_COLAB_BASE", "/content/gmp_job"))


def log(msg):
    print(f"[mesh] {msg}", flush=True)


def _locate_base() -> Path:
    for cand in (BASE, Path.cwd()):
        if (cand / "in.zip").exists():
            return cand
    raise FileNotFoundError(f"in.zip not found under {BASE} or {Path.cwd()}")


def _load_work(base: Path) -> dict:
    work = base / "work"
    if work.exists():
        import shutil
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
# REAL PATH — PROVEN PIPELINE PLUG-IN (carried from sculmm pilot v2, Colab T4).
# UNTESTED IN THIS FILE. Gate: env GMP_MESH_REAL=1 or payload.real == true.
# ---------------------------------------------------------------------------
def _real_mesh(images_dir: Path, out: Path, prompt: str) -> dict:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                    "hy3dgen", "trimesh", "pyglet"], check=True)
    from PIL import Image  # noqa: PLC0415  (present on Colab)
    from hy3dgen.shapegen import (DegenerateFaceRemover, FaceReducer,  # noqa: PLC0415
                                  FloaterRemover, Hunyuan3DDiTFlowMatchingPipeline)
    import trimesh  # noqa: PLC0415

    src = sorted(images_dir.glob("*")) if images_dir.exists() else []
    if not src:
        raise FileNotFoundError("real mesh path needs work/images/* (an input image)")
    image = Image.open(src[0]).convert("RGBA")

    # shape-only chain, as proven in the sculmm repo (no tex stage)
    pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained("tencent/Hunyuan3D-2")
    mesh = pipeline(image=image, prompt=prompt or None)[0]
    mesh = FloaterRemover()(mesh)
    mesh = DegenerateFaceRemover()(mesh)
    mesh = FaceReducer()(mesh, max_facenum=int(os.environ.get("GMP_MESH_FACES", "50000")))

    tm = trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces)
    # sculmm normalization law: meters, sitting on the floor, +z up
    tm.apply_scale(1.0 / max(tm.extents.max(), 1e-6))  # unit-ish first
    tm.apply_translation([0, 0, -tm.bounds[0][2]])     # floor: min z -> 0
    tm.export(str(out / "mesh.glb"))
    return {"verts": int(len(tm.vertices)), "faces": int(len(tm.faces)),
            "source_image": src[0].name}


def main():
    t0 = time.time()
    base = _locate_base()
    job = _load_work(base)
    payload = job.get("payload", {})
    prompt = payload.get("prompt", "")
    out = base / "out"
    out.mkdir(parents=True, exist_ok=True)

    want_real = os.environ.get("GMP_MESH_REAL") == "1" or payload.get("real") is True
    real_err = None
    if want_real:
        try:
            stats = _real_mesh(base / "work" / "images", out, prompt)
            result = {"ok": True, "type": "gpu.mesh", "stub": False,
                      "pipeline": "hy3dgen shape (tencent/Hunyuan3D-2), sculmm chain",
                      "prompt": prompt, **stats}
        except Exception as exc:  # fall through to the stub, never lose the slot
            log(f"real path FAILED ({exc}) -> writing stub mesh so the job still returns")
            real_err = str(exc)

    if not want_real or real_err:
        glb = _cube_glb()
        (out / "mesh.glb").write_bytes(glb)
        result = {"ok": True, "type": "gpu.mesh", "stub": True,
                  "stub_note": "valid glTF 2.0 cube; no image conditioning",
                  "real_path": "GMP_MESH_REAL=1 -> hy3dgen sculmm chain",
                  "real_error": real_err,
                  "prompt": prompt,
                  "verts": 24, "faces": 12,
                  "glb_bytes": len(glb),
                  "seconds": round(time.time() - t0, 2)}

    (base / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    with zipfile.ZipFile(base / "out.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(out).as_posix())
    log(f"done: stub={result['stub']} verts={result.get('verts')}")


if __name__ == "__main__":
    main()
