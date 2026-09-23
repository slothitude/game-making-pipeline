"""prop_order.py — the SCULMM prop work-order front (prompt/image -> engine prop).

Two halves, both stdlib-only:

  ORDER (local)  make_prop_order(name, prompt, target_faces=50000, image=None,
                                 seed=None) -> payload dict
                 {"kind": "mesh", "prompt": ..., "name": ..., "target_faces": ...,
                  ["seed": ..., "image_b64": ...]}
                 Ready for a queue POST; the queue router maps it onto the colab
                 lane with to_worker_job() -> colab_worker.run_job(...) -> the
                 gpu.mesh runner (cloud/colab/scripts/mesh.py, real path).

  RESOLVE (local) resolve_into_engine(glb_path, engine_assets_dir, name=None)
                  The pilot's manifest law: copy the glb to
                  <assets>/props/<name>/<name>.glb and merge into
                  <assets>/manifest.json an entry
                    {"name": ..., "glb": "props/<name>/<name>.glb",
                     "source": "colab-hy3dgen", "faces": N,
                     "created": "<UTC ISO>"}
                  Idempotent: re-resolving the same name replaces its entry.
                  Manifest-shape tolerant: a bare-list manifest stays a bare
                  list, a {"props": [...]} manifest merges in place, a missing
                  manifest is created as {"props": [...]}.

clips.json: the readable proven sculmm tools (hy_setup3/hy_gen/hy_poll/hy_all/
gen_winston — GLB export only) carry NO clips convention, so this front is
manifest-only by task law. KNOWN_GAP in README.md.

CLI:
  python prop_order.py make --name lantern --prompt "wrought-iron lantern" \
      [--image photo.png] [--target-faces 50000] [--seed 7] [--out payload.json]
  python prop_order.py resolve --glb done/out/lantern.glb --assets-dir <engine>/assets [--name lantern]
  python prop_order.py --selftest     # temp dir, fake glb, resolve, show manifest
"""
import argparse
import base64
import json
import re
import shutil
import struct
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SOURCE_TAG = "colab-hy3dgen"


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name)).strip("_").lower()
    if not s or not re.match(r"[a-z]", s[0]):
        s = "prop_" + s
    return s


# ---------------------------------------------------------------------------
# ORDER
# ---------------------------------------------------------------------------
def make_prop_order(name: str, prompt: str, target_faces: int = 50000,
                    image=None, seed=None, real: bool = True) -> dict:
    """Build the gpu.mesh payload. `image` is a path to a PNG/JPG — inlined as
    base64 (the proven chain is image-conditioned; prompt-only orders are valid
    but the real mesh path will refuse them — see mesh.py module docstring).
    real=True rides the payload as the gate: worker env vars do NOT cross to the
    Colab runtime, so `real` in job.json is the only reliable way to arm the
    real path (GMP_MESH_REAL=1 is for direct/manual exec runs)."""
    name = _slug(name)
    if not (prompt or image):
        raise ValueError("a prop order needs a prompt and/or an image")
    target_faces = int(target_faces)
    if target_faces <= 0:
        raise ValueError(f"target_faces must be positive, got {target_faces}")

    payload = {"kind": "mesh", "prompt": str(prompt or ""), "name": name,
               "target_faces": target_faces, "real": bool(real)}
    if seed is not None:
        payload["seed"] = int(seed)
    if image is not None:
        blob = Path(image).read_bytes()
        payload["image_b64"] = base64.b64encode(blob).decode("ascii")
        payload["image_name"] = Path(image).name
    return payload


def to_worker_job(payload: dict, staging_dir, return_dir, gpu: str = "T4",
                  session: str = None) -> dict:
    """Map a prop payload onto the colab lane (cloud/colab/colab_worker.run_job)."""
    if payload.get("kind") != "mesh":
        raise ValueError(f"not a mesh payload: kind={payload.get('kind')!r}")
    job = {"type": "gpu.mesh", "staging_dir": str(staging_dir),
           "return_dir": str(return_dir), "gpu": gpu, "payload": payload}
    if session:
        job["session"] = session
    return job


# ---------------------------------------------------------------------------
# RESOLVE — GLB face counting (stdlib GLB parse)
# ---------------------------------------------------------------------------
def _glb_stats(path: Path) -> dict:
    blob = path.read_bytes()
    if len(blob) < 20 or blob[:4] != b"glTF":
        raise ValueError(f"not a GLB: {path}")
    json_len = struct.unpack_from("<I", blob, 12)[0]
    gltf = json.loads(blob[20:20 + json_len].decode("utf-8"))
    accs = gltf.get("accessors", [])
    faces = verts = 0
    for mesh in gltf.get("meshes", []):
        for prim in mesh.get("primitives", []):
            idx = prim.get("indices")
            if isinstance(idx, int) and idx < len(accs):
                faces += accs[idx].get("count", 0) // 3
            pos = prim.get("attributes", {}).get("POSITION")
            if isinstance(pos, int) and pos < len(accs):
                verts += accs[pos].get("count", 0)
    return {"faces": faces, "verts": verts}


def _merge_manifest(assets_dir: Path, entry: dict) -> Path:
    """Idempotent manifest merge, shape-tolerant (law not derivable from the
    readable proven tools — see module docstring + README KNOWN_GAPS)."""
    mf = assets_dir / "manifest.json"
    raw = None
    if mf.exists():
        raw = json.loads(mf.read_text(encoding="utf-8"))

    if raw is None:                      # fresh manifest
        doc, key = {"props": [entry]}, "props"
    elif isinstance(raw, list):          # bare-list manifest: keep the shape
        doc, key = None, None
    elif isinstance(raw, dict) and isinstance(raw.get("props"), list):
        doc, key = raw, "props"
    else:                                # unknown dict shape: add/replace props
        doc, key = raw, "props"

    if doc is None:  # list path
        doc = [e for e in raw if not (isinstance(e, dict) and e.get("name") == entry["name"])]
        doc.append(entry)
    else:
        doc[key] = [e for e in doc[key]
                    if not (isinstance(e, dict) and e.get("name") == entry["name"])]
        doc[key].append(entry)

    mf.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return mf


def resolve_into_engine(glb_path, engine_assets_dir, name: str = None) -> dict:
    """Copy the glb into assets/props/<name>/ and merge the manifest entry.
    Returns the entry written."""
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
    entry = {"name": name,
             "glb": Path("props") / name / f"{name}.glb",  # posix str via json below
             "source": SOURCE_TAG,
             "faces": stats["faces"],
             "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    entry["glb"] = entry["glb"].as_posix()
    entry["verts"] = stats["verts"]  # informational; the law fields are above
    manifest = _merge_manifest(assets, entry)
    return {"entry": entry, "glb": str(dest), "manifest": str(manifest)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_make(a) -> int:
    payload = make_prop_order(a.name, a.prompt, a.target_faces, a.image, a.seed)
    doc = {"kind": "mesh", "payload": payload} if a.full else payload
    text = json.dumps(doc, indent=2)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
        print(f"payload -> {a.out}")
    print(text)
    return 0


def _cmd_resolve(a) -> int:
    res = resolve_into_engine(a.glb, a.assets_dir, a.name)
    print(json.dumps(res["entry"], indent=2))
    print(f"glb      -> {res['glb']}")
    print(f"manifest -> {res['manifest']}")
    return 0


def _selftest() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    tmp = Path(tempfile.mkdtemp(prefix="sculmm_selftest_"))
    assets = tmp / "engine" / "assets"
    print(f"SELFTEST tmp={tmp}")

    # fake glb: minimal valid cube (24 verts / 12 faces) via the stub math
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
    fake_glb = (struct.pack("<III", 0x46546C67, 2, total)
                + struct.pack("<II", len(js), 0x4E4F534A) + js
                + struct.pack("<II", len(bin_chunk), 0x004E4942) + bin_chunk)
    src = tmp / "done" / "lantern_mk2.glb"   # stem == slug: resolve derives the name from it
    src.parent.mkdir(parents=True)
    src.write_bytes(fake_glb)

    # 1. order
    payload = make_prop_order("Lantern Mk2", "wrought-iron lantern", 50000, seed=7)
    checks = [("make_prop_order payload", payload == {
        "kind": "mesh", "prompt": "wrought-iron lantern", "name": "lantern_mk2",
        "target_faces": 50000, "seed": 7, "real": True})]
    print("payload: " + json.dumps(payload))

    # 2. resolve
    res = resolve_into_engine(src, assets)
    entry = res["entry"]
    dest = Path(res["glb"])
    checks += [
        ("glb copied to props/<name>/", dest == assets / "props" / "lantern_mk2" / "lantern_mk2.glb"
         and dest.is_file() and dest.read_bytes() == fake_glb),
        ("entry law fields", entry["name"] == "lantern_mk2"
         and entry["glb"] == "props/lantern_mk2/lantern_mk2.glb"
         and entry["source"] == "colab-hy3dgen"
         and entry["faces"] == 12 and entry["verts"] == 24
         and "T" in entry["created"]),
    ]

    # 3. idempotent re-resolve
    resolve_into_engine(src, assets, "lantern_mk2")
    doc = json.loads((assets / "manifest.json").read_text(encoding="utf-8"))
    checks += [("re-resolve replaces, no duplicate", len(doc["props"]) == 1)]

    print("--- manifest.json ---")
    print((assets / "manifest.json").read_text(encoding="utf-8"))
    print("---------------------")

    ok = all(p for _, p in checks)
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    print(f"SELFTEST {'GREEN' if ok else 'RED'}")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SCULMM prop work-order front")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    m = sub.add_parser("make", help="build a gpu.mesh prop payload")
    m.add_argument("--name", required=True)
    m.add_argument("--prompt", default="")
    m.add_argument("--image", default=None, help="path to a reference PNG/JPG (inlined b64)")
    m.add_argument("--target-faces", type=int, default=50000)
    m.add_argument("--seed", type=int, default=None)
    m.add_argument("--out", default=None, help="also write the JSON here")
    m.add_argument("--full", action="store_true",
                   help="emit the colab_worker.run_job envelope around the payload")
    m.set_defaults(fn=_cmd_make)

    r = sub.add_parser("resolve", help="copy a done glb into an engine assets dir + manifest")
    r.add_argument("--glb", required=True)
    r.add_argument("--assets-dir", required=True)
    r.add_argument("--name", default=None)
    r.set_defaults(fn=_cmd_resolve)

    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if getattr(a, "fn", None):
        return a.fn(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
