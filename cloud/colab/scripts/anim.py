"""anim.py — the gpu.anim runner (static GLB + BVH -> animated asset).
EXECUTES ON THE COLAB RUNTIME.

Contract with cloud/colab/colab_worker.py (the only thing that ships it here):
  - in.zip contains work/job.json whose payload carries
    {name, glb, bvh, frames=48, fps=24, render=false, real}
    plus the staged <glb> and <bvh> files themselves (filenames in the payload,
    bytes in the staging dir -> work/)
  - this script writes out/<name>.glb (baked animation) or out/frames/*.png
    (render=true) + result.json, zips out/ -> out.zip
  - the worker downloads out.zip + result.json and tears the runtime down.
  - NOTE: colab_worker's runner map does not know "gpu.anim" yet (one line,
    deliberate scope decision — see cloud/sculmm/README.md KNOWN_GAPS).

STATUS OF THE CODE
  DEFAULT PATH = STUB: writes a valid STATIC glTF 2.0 cube GLB (stdlib-only,
  self-contained — exec -f ships one file, so mesh.py's stub is not on the
  runtime) so the downstream pipeline always gets a well-formed artifact and a
  truthful result.json. Never fake-claims an animation.
  REAL PATH = blender headless, gated behind GMP_ANIM_REAL=1 or
  payload.real=true (payload is the reliable arm — worker env vars do NOT
  cross to the runtime). Binary install follows scripts/render.py's tarball
  pattern (GMP_BLENDER_VERSION, default 4.2.3 — the pin is a KNOWN_GAP until
  the first real run confirms it). Stages:
    1. install  — blender release tarball (~350MB, fresh runtime every job)
    2. bind     — RIGID nearest-segment bind: every mesh vertex gets weight
                  1.0 on its nearest bone segment of the BVH armature. This is
                  the pilot v3 law — ARMATURE_AUTO weights were inert (the
                  actor stood at origin while the rig drove), so the bind is
                  computed by hand, no falloff, no automatic weights.
    3. retarget — the BVH import IS the retarget: the BVH armature drives, the
                  prop mesh is skinned onto it via vertex groups + an ARMATURE
                  modifier (no second rig to map).
    4. bake     — export_scene.gltf (GLB, export_animation) -> one animated
                  GLB; or render=true -> Cycles CUDA PNG frames (T4 has CUDA,
                  not OPTIX; EEVEE needs a GL context headless Colab lacks).
  result.json carries {anim_seconds, frames, bind: "rigid-nearest-segment"}.
  The bpy calls are NOT yet proven on Colab — first real run must be watched.

Local sanity run (stub path):
  python scripts/anim.py   # after pointing GMP_COLAB_BASE at a dir with in.zip
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

BASE = Path(os.environ.get("GMP_COLAB_BASE", "/content/gmp_job"))
BLENDER_VERSION = os.environ.get("GMP_BLENDER_VERSION", "4.2.3")


def log(msg):
    print(f"[anim] {msg}", flush=True)


# Windows console law: never let a cp1252 stream kill the lane
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _locate_base() -> Path:
    for cand in (BASE, Path.cwd()):
        if (cand / "in.zip").exists():
            return cand
    raise FileNotFoundError(f"in.zip not found under {BASE} or {Path.cwd()}")


def _load_work(base: Path) -> dict:
    work = base / "work"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    with zipfile.ZipFile(base / "in.zip") as z:
        z.extractall(work)
    jf = work / "job.json"
    return json.loads(jf.read_text(encoding="utf-8")) if jf.exists() else {}


def _slug(name: str) -> str:
    import re
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name)).strip("_").lower()
    return s or "anim"


def _find_input(work: Path, filename: str, suffix: str) -> Path:
    """Payload names the file; tolerate a subdir or a missing exact name by
    taking the (sorted) first match on the suffix — one glb/bvh per job."""
    if filename:
        cand = work / filename
        if cand.is_file():
            return cand
    hits = sorted(p for p in work.rglob("*" + suffix) if p.is_file())
    if not hits:
        raise FileNotFoundError(f"no *{suffix} staged in {work}")
    return hits[0]


# ---------------------------------------------------------------------------
# STUB: static glTF 2.0 cube GLB — 24 verts / 36 indices, stdlib only.
# (Same hand-rolled math as the mesh stub; duplicated because exec -f ships
# exactly one script and mesh.py is not on the runtime.)
# ---------------------------------------------------------------------------
def _cube_glb() -> bytes:
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
        "asset": {"version": "2.0", "generator": "gmp gpu.anim stub"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "gmp_anim_stub_cube"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "buffers": [{"byteLength": len(bin_chunk)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bin), "target": 34962},
            {"buffer": 0, "byteOffset": len(pos_bin), "byteLength": len(idx_bin),
             "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(positions),
             "type": "VEC3", "min": [-0.5] * 3, "max": [0.5] * 3},
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
# REAL PATH — blender headless. Binary install per scripts/render.py's
# tarball pattern; the bpy bind/bake script below is NOT yet proven on Colab.
# ---------------------------------------------------------------------------
def _ensure_blender() -> str:
    found = shutil.which("blender") or os.environ.get("GMP_BLENDER_PATH")
    if found:
        return found
    url = (f"https://download.blender.org/release/Blender{BLENDER_VERSION.rsplit('.', 1)[0]}/"
           f"blender-{BLENDER_VERSION}-linux-x64.tar.xz")
    tgz = Path("/tmp/blender.tar.xz")
    log(f"downloading blender {BLENDER_VERSION} (~350MB) from {url}")
    urllib.request.urlretrieve(url, tgz)
    with tarfile.open(tgz) as t:
        t.extractall("/tmp")
    return str(next(Path("/tmp").glob(f"blender-{BLENDER_VERSION}-linux-x64/blender")))


# The inner bpy program runs under blender's own python (CFG arrives as the
# argv JSON after "--"). Written as one constant: no outer interpolation, so
# the braces and quotes stay honest.
_INNER = r'''
import json, os, sys, traceback
import bpy
from mathutils import Vector

CFG = json.loads(sys.argv[sys.argv.index("--") + 1])


def _seg_dist(p, a, b):
    ab = b - a
    t = ab.dot(p - a) / max(ab.length_squared, 1e-12)
    t = max(0.0, min(1.0, t))
    return (p - (a + ab * t)).length


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=CFG["glb"])
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        raise RuntimeError("no MESH objects in " + CFG["glb"])

    # the BVH import IS the retarget: it brings its own armature + action
    bpy.ops.import_anim.bvh(filepath=CFG["bvh"])
    arm = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
    s = float(CFG.get("bvh_scale", 0.01))   # BVH cm -> engine meters (law knob)
    arm.scale = (s, s, s)
    bpy.context.view_layer.objects.active = arm
    bpy.context.view_layer.update()

    # RIGID nearest-segment bind — pilot v3 law. ARMATURE_AUTO weights were
    # inert, so: every vertex, weight 1.0, nearest bone segment, no falloff.
    segs = [(b.name, arm.matrix_world @ b.head_local,
             arm.matrix_world @ b.tail_local) for b in arm.data.bones]
    if not segs:
        raise RuntimeError("BVH produced no bones: " + CFG["bvh"])
    bound = 0
    for obj in meshes:
        mw = obj.matrix_world
        for v in obj.data.vertices:
            co = mw @ v.co
            bname, ha, ta = min(segs, key=lambda sg: _seg_dist(co, sg[1], sg[2]))
            vg = obj.vertex_groups.get(bname)
            if vg is None:
                vg = obj.vertex_groups.new(name=bname)
            vg.add([v.index], 1.0, "REPLACE")
            bound += 1
        mod = obj.modifiers.new("Armature", "ARMATURE")
        mod.object = arm
    report = {"bones": len(segs), "verts_bound": bound, "meshes": len(meshes),
              "bind": "rigid-nearest-segment"}
    with open(CFG["bind_report"], "w", encoding="utf-8") as f:
        json.dump(report, f)
    print("BIND_REPORT " + json.dumps(report), flush=True)

    sc = bpy.context.scene
    sc.render.fps = int(CFG.get("fps", 24))
    f0 = int(sc.frame_start)             # BVH action start
    f1 = f0 + int(CFG["frames"]) - 1
    sc.frame_start, sc.frame_end = f0, f1

    if CFG.get("render"):
        # frame range as PNGs: Cycles via CUDA (T4 has no OPTIX; EEVEE needs a
        # GL context headless Colab lacks). Low samples: free-tier discipline.
        sc.render.engine = "CYCLES"
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "CUDA"
        prefs.get_devices()
        for d in prefs.devices:
            d.use = d.type != "CPU"
        sc.cycles.device = "GPU"
        sc.cycles.samples = int(CFG.get("samples", 32))
        sc.render.resolution_x = sc.render.resolution_y = int(CFG.get("resolution", 512))
        os.makedirs(CFG["out_frames"], exist_ok=True)
        sc.render.filepath = os.path.join(CFG["out_frames"], CFG["name"] + "_")
        for f in range(f0, f1 + 1):
            sc.frame_set(f)
            bpy.ops.render.render(write_still=True)
        n = len([p for p in os.listdir(CFG["out_frames"]) if p.endswith(".png")])
        print("FRAMES_DONE " + str(n), flush=True)
    else:
        # one baked animated GLB: armature + skinned meshes + the BVH action
        bpy.ops.export_scene.gltf(filepath=CFG["out_glb"], export_format="GLB",
                                  export_animation=True)
        print("GLB_ANIM_DONE", flush=True)


try:
    main()
except Exception:
    traceback.print_exc()
    sys.exit(1)
'''


def _real_anim(base: Path, work: Path, out: Path, payload: dict) -> dict:
    glb = _find_input(work, payload.get("glb", ""), ".glb")
    bvh = _find_input(work, payload.get("bvh", ""), ".bvh")
    name = _slug(payload.get("name") or glb.stem)
    frames = int(payload.get("frames", 48))
    fps = int(payload.get("fps", 24))
    render = bool(payload.get("render"))

    blender = _ensure_blender()
    cfg = {"glb": str(glb), "bvh": str(bvh), "name": name,
           "frames": frames, "fps": fps, "render": render,
           "bvh_scale": float(payload.get("bvh_scale", 0.01)),
           "samples": int(payload.get("samples", 32)),
           "resolution": int(payload.get("resolution", 512)),
           "out_glb": str(out / f"{name}.glb"),
           "out_frames": str(out / "frames"),
           "bind_report": str(out / "_bind_report.json")}
    script = base / "anim_inner.py"
    script.write_text(_INNER, encoding="utf-8")

    t0 = time.time()
    cmd = [blender, "-b", "-noaudio", "-P", str(script), "--", json.dumps(cfg)]
    log("blender bake: " + " ".join(cmd[:6]) + " ...")
    subprocess.run(cmd, check=True)          # streams like render.py; worker cap governs
    anim_seconds = round(time.time() - t0, 1)

    bind_report = {}
    if Path(cfg["bind_report"]).is_file():
        bind_report = json.loads(Path(cfg["bind_report"]).read_text(encoding="utf-8"))

    if render:
        pngs = sorted(p.name for p in (out / "frames").glob("*.png"))
        if not pngs:
            raise RuntimeError("blender baked no frames (see exec log)")
        return {"ok": True, "type": "gpu.anim", "stub": False,
                "source": "colab-anim", "name": name,
                "output": "frames/" + pngs[0] + ".." + pngs[-1],
                "rendered_pngs": True, "frames": len(pngs), "fps": fps,
                "bind": "rigid-nearest-segment", "bind_stats": bind_report,
                "anim_seconds": anim_seconds,
                "blender": BLENDER_VERSION,
                "seconds": round(time.time() - t0, 1)}

    glb_out = Path(cfg["out_glb"])
    if not glb_out.is_file():
        raise RuntimeError("blender wrote no animated glb (see exec log)")
    return {"ok": True, "type": "gpu.anim", "stub": False,
            "source": "colab-anim", "name": name, "output": glb_out.name,
            "rendered_pngs": False, "frames": frames, "fps": fps,
            "bind": "rigid-nearest-segment", "bind_stats": bind_report,
            "anim_seconds": anim_seconds,
            "blender": BLENDER_VERSION,
            "seconds": round(time.time() - t0, 1)}


def main():
    t0 = time.time()
    base = _locate_base()
    job = _load_work(base)
    payload = job.get("payload", {})
    name = _slug(payload.get("name") or "anim")
    frames = int(payload.get("frames", 48))
    out = base / "out"
    out.mkdir(parents=True, exist_ok=True)

    want_real = os.environ.get("GMP_ANIM_REAL") == "1" or payload.get("real") is True
    real_err = None
    result = None
    if want_real:
        try:
            result = _real_anim(base, base / "work", out, payload)
        except Exception as exc:  # fall through to the stub, never lose the slot
            log(f"real anim FAILED ({exc}) -> stub glb so the job still returns")
            real_err = str(exc)

    if result is None:
        glb = _cube_glb()
        (out / f"{name}.glb").write_bytes(glb)
        result = {"ok": True, "type": "gpu.anim", "stub": True,
                  "stub_note": "static cube placeholder — no armature, no animation",
                  "real_path": ("GMP_ANIM_REAL=1 + staged glb+bvh -> blender -b "
                                "rigid-nearest-segment bind + BVH bake"),
                  "real_error": real_err,
                  "source": "colab-anim", "name": name,
                  "output": f"{name}.glb", "rendered_pngs": False,
                  "frames": frames, "fps": int(payload.get("fps", 24)),
                  "bind": None,
                  "glb_bytes": len(glb),
                  "seconds": round(time.time() - t0, 2)}

    (base / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    with zipfile.ZipFile(base / "out.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(out).as_posix())
    log(f"done: stub={result['stub']} name={result.get('name')} "
        f"frames={result.get('frames')} bind={result.get('bind')}")


if __name__ == "__main__":
    main()
