"""anim_order.py — the SCULMM animation work-order front (static GLB + BVH ->
animated engine asset). Companion to prop_order.py; SAME manifest law — the
merge/count/slug helpers are imported from it so there is exactly one manifest
writer convention in the lane (cloud/sculmm/prop_order.py, landed).

Two halves, both stdlib-only:

  ORDER (local)   make_anim_order(name, glb, bvh, frames=48, fps=24,
                                  render=False, real=True) -> payload dict
                  {"kind": "anim", "name": ..., "glb": "<basename>",
                   "bvh": "<basename>", "frames": N, "fps": N,
                   "render": bool, "real": true}
                  glb/bvh are SOURCE paths at order time (validated to exist);
                  the payload carries only their basenames — the bytes travel
                  in the job's staging dir (in.zip -> work/ on the runtime).
                  to_anim_job(payload, staging, return_dir, glb_src, bvh_src)
                  stages the files and wraps the payload in the colab_worker
                  envelope {"type": "gpu.anim", ...}.

  RESOLVE (local) resolve_anim_into_engine(glb_path, engine_assets_dir, name=None)
                  Same copy/merge law as prop_order.resolve_into_engine, with
                  the animation entry shape:
                    {"name": ..., "glb": "props/<name>/<name>.glb",
                     "source": "colab-anim", "animated": true,
                     "bind": "rigid-nearest-segment", "faces": N,
                     "frames": N, "created": "<UTC ISO>"}
                  frames/bind are enriched from the sibling result.json when it
                  is present; `animated` downgrades to false if that result
                  says stub (a static stub cube must never claim animation).
                  Re-resolving a name REPLACES its entry — the animated GLB
                  supersedes the static mesh entry for the same prop name.

CLI:
  python anim_order.py --make winston --glb winston.glb --bvh walk.bvh \
      [--frames 48] [--fps 24] [--render] [--out payload.json]
  python anim_order.py --resolve done/out/winston.glb --assets-dir <engine>/assets
  python anim_order.py --selftest     # temp dir, fake glb, resolve, show manifest
"""
import argparse
import json
import shutil
import struct
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from prop_order import _glb_stats, _merge_manifest, _slug  # one manifest law

SOURCE_TAG = "colab-anim"
BIND_LAW = "rigid-nearest-segment"   # pilot v3: ARMATURE_AUTO weights were inert


# ---------------------------------------------------------------------------
# ORDER
# ---------------------------------------------------------------------------
def make_anim_order(name: str, glb, bvh, frames: int = 48, fps: int = 24,
                    render: bool = False, real: bool = True) -> dict:
    """Build the gpu.anim payload. glb/bvh are local SOURCE paths, validated
    here; their basenames ride the payload, the bytes are staged by
    to_anim_job(). real=True rides the payload as the gate: worker env vars do
    NOT cross to the Colab runtime, so `real` is the reliable arm for
    anim.py's real path (GMP_ANIM_REAL=1 is for direct/manual exec runs)."""
    glb, bvh = Path(glb), Path(bvh)
    if not glb.is_file():
        raise FileNotFoundError(f"glb not found: {glb}")
    if not bvh.is_file():
        raise FileNotFoundError(f"bvh not found: {bvh}")
    frames, fps = int(frames), int(fps)
    if frames <= 0:
        raise ValueError(f"frames must be positive, got {frames}")
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    return {"kind": "anim", "name": _slug(name), "glb": glb.name, "bvh": bvh.name,
            "frames": frames, "fps": fps, "render": bool(render), "real": bool(real)}


def to_anim_job(payload: dict, staging_dir, return_dir, glb_src=None, bvh_src=None,
                gpu: str = "T4", session: str = None) -> dict:
    """Stage the glb/bvh bytes into staging_dir and wrap the payload in the
    colab_worker envelope. glb_src/bvh_src default to the payload basenames
    resolved against the current directory.
    NOTE: colab_worker's runner map does not know "gpu.anim" yet — run_job
    returns bad_job until that one line lands (README KNOWN_GAPS)."""
    if payload.get("kind") != "anim":
        raise ValueError(f"not an anim payload: kind={payload.get('kind')!r}")
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    for key, src in (("glb", glb_src), ("bvh", bvh_src)):
        src = Path(src) if src is not None else Path(payload[key])
        if not src.is_file():
            raise FileNotFoundError(f"{key} source not found: {src}")
        shutil.copy2(src, staging / src.name)
    for key in ("glb", "bvh"):
        if not (staging / payload[key]).is_file():
            raise FileNotFoundError(f"staging missing {key}: {staging / payload[key]}")
    job = {"type": "gpu.anim", "staging_dir": str(staging),
           "return_dir": str(return_dir), "gpu": gpu, "payload": payload}
    if session:
        job["session"] = session
    return job


# ---------------------------------------------------------------------------
# RESOLVE
# ---------------------------------------------------------------------------
def _sibling_result(glb: Path):
    """The worker writes result.json into the return dir; the glb lands in
    return_dir/out/. Check both neighbourhoods."""
    for cand in (glb.parent / "result.json", glb.parent.parent / "result.json"):
        if cand.is_file():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
    return None


def resolve_anim_into_engine(glb_path, engine_assets_dir, name: str = None) -> dict:
    """Copy the glb into assets/props/<name>/ and merge the animation entry.
    Returns {"entry", "glb", "manifest"} like prop_order's resolve."""
    glb = Path(glb_path)
    if not glb.is_file():
        raise FileNotFoundError(f"glb not found: {glb}")
    name = _slug(name or glb.stem)
    assets = Path(engine_assets_dir)
    dest_dir = assets / "props" / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{name}.glb"
    shutil.copy(glb, dest)

    stats = _glb_stats(dest)
    result = _sibling_result(glb)
    stub = bool(result.get("stub")) if isinstance(result, dict) else False
    entry = {"name": name,
             "glb": (Path("props") / name / f"{name}.glb").as_posix(),
             "source": SOURCE_TAG,
             "animated": not stub,
             "bind": (result or {}).get("bind") or BIND_LAW,
             "faces": stats["faces"],
             "frames": (result or {}).get("frames"),
             "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    manifest = _merge_manifest(assets, entry)
    return {"entry": entry, "glb": str(dest), "manifest": str(manifest)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_make(a) -> int:
    payload = make_anim_order(a.name, a.glb, a.bvh, a.frames, a.fps, a.render)
    text = json.dumps(payload, indent=2)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
        print(f"payload -> {a.out}")
    print(text)
    return 0


def _cmd_resolve(a) -> int:
    res = resolve_anim_into_engine(a.glb, a.assets_dir, a.name)
    print(json.dumps(res["entry"], indent=2))
    print(f"glb      -> {res['glb']}")
    print(f"manifest -> {res['manifest']}")
    return 0


def _fake_glb() -> bytes:
    """24-vert / 12-face cube — same math the landed stubs use."""
    positions, indices = [], []
    axes = [((1, 0, 0), (0, 1, 0), (0, 0, 1)), ((-1, 0, 0), (0, 1, 0), (0, 0, -1)),
            ((0, 1, 0), (0, 0, 1), (1, 0, 0)), ((0, -1, 0), (0, 0, 1), (-1, 0, 0)),
            ((0, 0, 1), (1, 0, 0), (0, 1, 0)), ((0, 0, -1), (1, 0, 0), (0, -1, 0))]
    for n, u, v in axes:
        b = len(positions)
        for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            positions.extend(0.5 * (n[k] + su * u[k] + sv * v[k]) for k in range(3))
        indices += [b, b + 1, b + 2, b, b + 2, b + 3]
    pos_bin = struct.pack(f"<{len(positions)}f", *positions)
    idx_bin = struct.pack(f"<{len(indices)}H", *indices)
    bin_chunk = pos_bin + idx_bin + b"\x00" * ((4 - len(pos_bin + idx_bin) % 4) % 4)
    gltf = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}], "meshes": [{"primitives": [
                {"attributes": {"POSITION": 0}, "indices": 1}]}],
            "buffers": [{"byteLength": len(pos_bin + idx_bin)}],
            "bufferViews": [
                {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bin), "target": 34962},
                {"buffer": 0, "byteOffset": len(pos_bin), "byteLength": len(idx_bin), "target": 34963}],
            "accessors": [
                {"bufferView": 0, "componentType": 5126, "count": 24, "type": "VEC3",
                 "min": [-0.5] * 3, "max": [0.5] * 3},
                {"bufferView": 1, "componentType": 5123, "count": 36, "type": "SCALAR"}]}
    js = json.dumps(gltf).encode("utf-8") + b" " * ((4 - len(json.dumps(gltf)) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(bin_chunk)
    return (struct.pack("<III", 0x46546C67, 2, total)
            + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(bin_chunk), 0x004E4942) + bin_chunk)


def _selftest() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sculmm_anim_selftest_"))
    assets = tmp / "engine" / "assets"
    src = tmp / "src"
    src.mkdir(parents=True)
    (src / "winston.glb").write_bytes(_fake_glb())
    (src / "walk.bvh").write_text("HIERARCHY\nROOT Hips\n{\n}\nMOTION\nFrames: 1\n"
                                  "Frame Time: 0.0416667\n0 0 0\n", encoding="utf-8")
    print(f"SELFTEST tmp={tmp}")

    # 1. order
    payload = make_anim_order("Winston Walk", src / "winston.glb", src / "walk.bvh",
                              frames=48, fps=24)
    checks = [("make_anim_order payload", payload == {
        "kind": "anim", "name": "winston_walk", "glb": "winston.glb",
        "bvh": "walk.bvh", "frames": 48, "fps": 24, "render": False, "real": True})]
    print("payload: " + json.dumps(payload))

    # 2. envelope + staging
    stage, ret = tmp / "stage", tmp / "done"
    job = to_anim_job(payload, stage, ret, src / "winston.glb", src / "walk.bvh",
                      session="selftest-anim")
    checks += [
        ("to_anim_job envelope", job["type"] == "gpu.anim" and job["gpu"] == "T4"
         and job["payload"] is payload),
        ("inputs staged", (stage / "winston.glb").is_file()
         and (stage / "walk.bvh").is_file()
         and (stage / "winston.glb").read_bytes() == (src / "winston.glb").read_bytes()),
    ]

    # 3. resolve with a real-shaped result.json beside the glb
    out = ret / "out"
    out.mkdir(parents=True)
    done_glb = out / "winston_walk.glb"
    done_glb.write_bytes(_fake_glb())
    (ret / "result.json").write_text(json.dumps(
        {"ok": True, "type": "gpu.anim", "stub": False, "name": "winston_walk",
         "bind": BIND_LAW, "frames": 48, "fps": 24, "anim_seconds": 61.4}),
        encoding="utf-8")
    res = resolve_anim_into_engine(done_glb, assets)
    entry = res["entry"]
    dest = Path(res["glb"])
    checks += [
        ("glb copied to props/<name>/", dest == assets / "props" / "winston_walk"
         / "winston_walk.glb" and dest.is_file()),
        ("entry law fields", entry["source"] == "colab-anim"
         and entry["animated"] is True and entry["bind"] == BIND_LAW
         and entry["glb"] == "props/winston_walk/winston_walk.glb"
         and entry["faces"] == 12 and entry["frames"] == 48
         and "T" in entry["created"]),
    ]

    # 4. idempotent re-resolve + a second name merges
    resolve_anim_into_engine(done_glb, assets)
    resolve_anim_into_engine(done_glb, assets, "winston_idle")
    doc = json.loads((assets / "manifest.json").read_text(encoding="utf-8"))
    names = [e["name"] for e in doc["props"]]
    checks += [("re-resolve replaces, second name merges",
                names == ["winston_walk", "winston_idle"])]

    # 5. stub honesty: a stub result must not claim animation
    stub_dir = tmp / "stubdone" / "out"
    stub_dir.mkdir(parents=True)
    (stub_dir / "cube.glb").write_bytes(_fake_glb())
    (stub_dir.parent / "result.json").write_text(json.dumps(
        {"ok": True, "type": "gpu.anim", "stub": True, "frames": 48}),
        encoding="utf-8")
    stub_entry = resolve_anim_into_engine(stub_dir / "cube.glb", assets, "cube")["entry"]
    checks += [("stub result -> animated: false", stub_entry["animated"] is False)]

    print("--- manifest.json ---")
    print((assets / "manifest.json").read_text(encoding="utf-8"))
    print("---------------------")

    ok = all(p for _, p in checks)
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    print(f"SELFTEST {'GREEN' if ok else 'RED'}")
    if ok:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ap = argparse.ArgumentParser(description="SCULMM animation work-order front")
    ap.add_argument("--make", dest="name", metavar="NAME",
                    help="build a gpu.anim payload (needs --glb and --bvh)")
    ap.add_argument("--resolve", dest="glb", metavar="GLB",
                    help="copy a done animated glb into an engine assets dir")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--glb-src", dest="glb_src", default=None,
                    help="(with --make) source glb path staged into the job")
    ap.add_argument("--bvh-src", dest="bvh_src", default=None,
                    help="(with --make) source bvh path staged into the job")
    ap.add_argument("--frames", type=int, default=48)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--render", action="store_true",
                    help="order PNG frames instead of a baked animated glb")
    ap.add_argument("--out", default=None, help="also write the payload JSON here")
    ap.add_argument("--assets-dir", default=None, help="(with --resolve)")
    ap.add_argument("--name", default=None, help="(with --resolve) override the prop name")

    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.name:
        if not (a.glb_src and a.bvh_src):
            ap.error("--make needs --glb-src and --bvh-src")
        return _cmd_make(a)
    if a.glb:
        if not a.assets_dir:
            ap.error("--resolve needs --assets-dir")
        return _cmd_resolve(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
