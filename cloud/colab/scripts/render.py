"""render.py — the gpu.render runner (Blender headless frames). EXECUTES ON COLAB.

Contract with cloud/colab/colab_worker.py (the only thing that ships it here):
  - in.zip contains work/job.json ({"payload": {"frames": 12, "width": 1280,
    "height": 720, ...}}) and, for real renders, work/scene.blend
  - this script writes out/frame_*.png + result.json, zips out/ -> out.zip
  - the worker downloads out.zip + result.json and tears the runtime down.

STATUS OF THE CODE
  DEFAULT PATH = STUB: writes solid-color placeholder PNGs (PIL on Colab,
  stdlib fallback elsewhere) so shape/flow is proven end-to-end for free.
  REAL PATH = blender headless (`blender -b scene.blend -o //out/frame_ -s N -e M -a`),
  gated behind GMP_RENDER_REAL=1 or payload.real=true. The render command is
  the proven headless invocation; what is NOT settled on Colab is the blender
  binary itself (not preinstalled, no bpy wheel for Colab's python) — the
  tarball install below is the standard approach but the pinned URL is a
  KNOWN_GAP until the first real run confirms it (see cloud/colab/README.md).

Local sanity run:
  python scripts/render.py --base ./selftest_base
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
    print(f"[render] {msg}", flush=True)


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


# ---------------------------------------------------------------------------
# STUB PNG writer: PIL when present, stdlib raw-PNG otherwise.
# ---------------------------------------------------------------------------
def _png_bytes(width, height, rgb) -> bytes:
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", __import__("zlib").crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width  # filter 0 + RGB
    raw = row * height
    idat = __import__("zlib").compress(raw, 6)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def _stub_frames(out: Path, n: int, w: int, h: int, rgb):
    try:
        from PIL import Image
        names = []
        for i in range(n):
            shade = tuple(min(255, int(c * (0.75 + 0.25 * (i + 1) / n))) for c in rgb)
            img = Image.new("RGB", (w, h), shade)
            p = out / f"frame_{i + 1:04d}.png"
            img.save(p, "PNG")
            names.append(p.name)
        return names
    except ImportError:
        names = []
        for i in range(n):
            shade = tuple(min(255, int(c * (0.75 + 0.25 * (i + 1) / n))) for c in rgb)
            p = out / f"frame_{i + 1:04d}.png"
            p.write_bytes(_png_bytes(w, h, shade))
            names.append(p.name)
        return names


# ---------------------------------------------------------------------------
# REAL PATH — blender headless. Render command is the proven invocation; the
# Colab-side binary install is the unsettled part (KNOWN_GAP in the README).
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
    binp = next(Path("/tmp").glob(f"blender-{BLENDER_VERSION}-linux-x64/blender"))
    return str(binp)


def _real_render(blend: Path, out: Path, blender: str, first: int, last: int):
    cmd = [blender, "-b", str(blend), "-o", str(out / "frame_"),
           "-F", "PNG", "-x", "1", "-s", str(first), "-e", str(last), "-a"]
    log("blender: " + " ".join(cmd))
    subprocess.run(cmd, check=True)
    return sorted(p.name for p in out.glob("frame_*.png"))


def main():
    t0 = time.time()
    base = _locate_base()
    job = _load_work(base)
    payload = job.get("payload", {})
    n = int(payload.get("frames", 12))
    w = int(payload.get("width", 1280))
    h = int(payload.get("height", 720))
    rgb = tuple(int(payload.get(k, d)) for k, d in
                (("r", 47), ("g", 62), ("b", 104)))
    out = base / "out"
    out.mkdir(parents=True, exist_ok=True)

    blends = sorted((base / "work").glob("*.blend"))
    want_real = os.environ.get("GMP_RENDER_REAL") == "1" or payload.get("real") is True
    real_err = None
    if want_real and blends:
        try:
            names = _real_render(blends[0], out, _ensure_blender(),
                                 int(payload.get("first", 1)),
                                 int(payload.get("last", n)))
            result = {"ok": True, "type": "gpu.render", "stub": False,
                      "blender": True, "blend": blends[0].name,
                      "frames": names, "frames_written": len(names)}
        except Exception as exc:
            log(f"real render FAILED ({exc}) -> stub frames so the job still returns")
            real_err = str(exc)
            names = None
    else:
        names = None

    if names is None:
        names = _stub_frames(out, n, w, h, rgb)
        result = {"ok": True, "type": "gpu.render", "stub": True,
                  "stub_note": "solid-color placeholder frames (real path: .blend + blender)",
                  "real_path": "GMP_RENDER_REAL=1 + work/scene.blend -> blender -b headless",
                  "real_error": real_err,
                  "frames": names, "frames_written": len(names),
                  "width": w, "height": h,
                  "seconds": round(time.time() - t0, 2)}

    (base / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    with zipfile.ZipFile(base / "out.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(out).as_posix())
    log(f"done: stub={result['stub']} frames={result['frames_written']}")


if __name__ == "__main__":
    main()
