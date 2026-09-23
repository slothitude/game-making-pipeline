"""testlab_gate — Pipeline todo #92: the Firebase Test Lab gate (free real-device farm).

  python daily/testlab_gate.py --apk C:/Users/aaron/sonar/build/sonar.apk --game sonar
  python daily/testlab_gate.py --apk build/game.apk --game sonar \
      --devices "model=Nexus5,version=30;model=Pixel2,version=30" --timeout 20
  python daily/testlab_gate.py --game sonar --dry-run     # fabricate the shape, no network

Real mode submits a Robo crawl of the game's .apk to Firebase Test Lab
(`gcloud firebase test android run --type robo ... --async`), polls
`gcloud firebase test android matrices describe` until the matrix reaches a
terminal state, then downloads the whole results tree from the tool-results
bucket URL that the describe output reports (`gsutil -m cp -r`, the gcsPath is
the source of truth — the --results-dir is only where we asked for it to land).
Everything lands in build/testlab/<game>/<stamp>/ next to summary.json:
{game, dry_run, matrix_id, state, gcs_path, per_device: [{model, version,
outcome, screenshot_count}]}. Gate is green only when the state is FINISHED and
every device's outcome is "success" — a device the farm never ran is a red
gate, not a shrug (pipeline law 6).

Dry-run mode touches nothing: no gcloud, no gsutil, no network. It writes a
plausible summary.json ("dry_run": true) plus 3 placeholder PNG notes so the
site/dashboard devs can code against the shape before anyone auths gcloud.

Env: gcloud + gsutil on PATH and already authed to a project with Test Lab
enabled (auth is a later human step — this script never logs in).
"""
import argparse
import base64
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GMP = os.path.dirname(HERE)
TESTLAB_DIR = os.path.join(GMP, "build", "testlab")
DEFAULT_DEVICES = "model=Nexus5,version=30;model=Pixel2,version=30"
TERMINAL_STATES = {"FINISHED", "ERROR", "INVALID"}
POLL_SECONDS = 30
# 1x1 transparent PNG (PIL fallback so the file shape exists without PIL)
TINY_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

# crew/gate output carries emoji; a cp1252 Windows console must not kill the lane
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg):
    print(msg, flush=True)


def tail(text, n=400):
    return "\n".join((text or "").splitlines()[-n:])


def run(cmd, timeout=1800):
    """utf-8 + replace: gcloud/gsutil emit unicode that cp1252 cannot decode."""
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def find_tool(name):
    """gcloud/gsutil are .cmd wrappers on Windows — resolve via PATHEXT."""
    return shutil.which(name) or name


# ----------------------------------------------------------------- devices --
def parse_devices(spec):
    """'model=Nexus5,version=30;model=Pixel2,version=30' -> (tokens, dicts)."""
    tokens, devices = [], []
    for group in [g.strip() for g in spec.split(";") if g.strip()]:
        entry = {}
        for part in group.split(","):
            if "=" not in part:
                raise SystemExit(f"bad --devices group {group!r}: {part!r} is not key=value")
            k, v = part.split("=", 1)
            entry[k.strip()] = v.strip()
        if "model" not in entry or "version" not in entry:
            raise SystemExit(f"bad --devices group {group!r}: needs model= and version=")
        tokens.append(group)
        devices.append(entry)
    return tokens, devices


def spread(total, n):
    """Split `total` files across n devices round-robin (3 over 2 -> [2, 1])."""
    counts = [total // n] * n
    for i in range(total % n):
        counts[i] += 1
    return counts


# ------------------------------------------------------------- real mode ----
def submit_matrix(apk, tokens, timeout_min, results_dir):
    cmd = [find_tool("gcloud"), "firebase", "test", "android", "run",
           "--type", "robo", "--app", apk, "--timeout", f"{timeout_min}m",
           "--results-dir", results_dir, "--async"]
    for tok in tokens:
        cmd += ["--device", tok]
    log("gcloud: " + " ".join(cmd))
    r = run(cmd, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"gcloud run failed rc={r.returncode}\n{tail(r.stderr)}")
    m = re.search(r"matrix-[A-Za-z0-9-]+", r.stdout or "")
    if not m:
        raise RuntimeError(f"no matrix id in gcloud output:\n{tail(r.stdout)}")
    return m.group(0)


def pick(d, *keys):
    """First present key (gcloud JSON is camelCase, some ages are snake_case)."""
    for k in keys:
        if isinstance(d, dict) and k in d:
            return d[k]
    return None


def parse_matrix(text):
    """describe output -> {state, gcs_path, outcomes: [{model, version, outcome}]}."""
    try:
        m = json.loads(text)
    except ValueError:
        m = None
    if isinstance(m, dict):
        gcs = pick(pick(m, "resultStorage") or {}, "googleCloudStorage") or {}
        outcomes = []
        for o in m.get("deviceOutcomes") or []:
            dev = o.get("device") or {}
            outcomes.append({"model": pick(dev, "androidModelId", "android_model_id"),
                             "version": pick(dev, "androidVersionId", "android_version_id"),
                             "outcome": pick(o.get("outcome") or {}, "outcome")})
        return {"state": m.get("state"), "gcs_path": pick(gcs, "gcsPath", "gcs_path"),
                "outcomes": outcomes}
    # fallback: regex the human YAML (older gcloud / --format quirk)
    outcomes = [{"model": a, "version": v, "outcome": oc} for a, v, oc in
                re.findall(r"androidModelId:\s*(\S+).*?androidVersionId:\s*(\S+)"
                           r".*?outcome:\s*(\S+)", text, re.S)]
    gcs = re.search(r"gcsPath:\s*(gs://\S+)", text)
    state = re.search(r"^state:\s*(\S+)", text, re.M)
    return {"state": state.group(1) if state else None,
            "gcs_path": gcs.group(1) if gcs else None, "outcomes": outcomes}


def describe_matrix(matrix_id):
    r = run([find_tool("gcloud"), "firebase", "test", "android", "matrices",
             "describe", matrix_id, "--format", "json"], timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"matrices describe failed rc={r.returncode}\n{tail(r.stderr)}")
    return parse_matrix(r.stdout)


def poll_matrix(matrix_id, deadline):
    """Narrate state every POLL_SECONDS (no-silent law) until terminal/deadline."""
    started = time.time()
    while True:
        matrix = describe_matrix(matrix_id)
        state = matrix.get("state")
        mins = (time.time() - started) / 60.0
        log(f"  poll {mins:4.1f}m — state {state or '?'}")
        if state in TERMINAL_STATES:
            return matrix
        if time.time() > deadline:
            raise RuntimeError(f"matrix {matrix_id} never reached a terminal state "
                               f"(last: {state}) before the poll deadline")
        time.sleep(POLL_SECONDS)


def download_results(gcs_path, dest):
    r = run([find_tool("gsutil"), "-m", "cp", "-r", gcs_path, dest], timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"gsutil download failed\n{tail(r.stderr)}")


def count_shots(dest, devices):
    """PNGs per device: robo nests results under <Model>-<Version>-<locale>-..."""
    counts = [0] * len(devices)
    total = 0
    for dirpath, _dirs, files in os.walk(dest):
        rel = os.path.relpath(dirpath, dest)
        parts = rel.split(os.sep) if rel != "." else []
        for f in files:
            if not f.lower().endswith(".png"):
                continue
            total += 1
            for i, d in enumerate(devices):
                stem = f"{d['model']}-{d['version']}"
                if any(p == stem or p.startswith(stem + "-") for p in parts):
                    counts[i] += 1
                    break
    log(f"screenshots: {total} png(s) under {dest} -> per-device {counts}")
    return counts


def write_summary(dest, summary):
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, "summary.json").replace("\\", "/")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    return path


# ------------------------------------------------------------- dry run ------
def placeholder_png(path, lines):
    """A note you can see: dark tile, one line per fact. PIL if present."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (420, 64 + 22 * len(lines)), (24, 26, 32))
        d = ImageDraw.Draw(img)
        for i, ln in enumerate(lines):
            d.text((16, 18 + 22 * i), ln,
                   fill=(240, 240, 235) if i == 0 else (150, 158, 170))
        img.save(path)
    except ImportError:
        with open(path, "wb") as f:
            f.write(base64.b64decode(TINY_PNG))


def dry_run(args, devices, stamp, dest):
    log(f"[dry-run] no gcloud/gsutil calls — fabricating the response shape for {args.game}")
    counts = spread(3, len(devices))          # exactly 3 placeholder shots, always
    os.makedirs(dest, exist_ok=True)
    for i, d in enumerate(devices):
        for j in range(counts[i]):
            path = os.path.join(dest, f"{d['model']}-{d['version']}-shot-{j + 1}.png")
            placeholder_png(path, ["DRY-RUN PLACEHOLDER — no device ran",
                                   f"game: {args.game}   gate: Test Lab (todo #92)",
                                   f"device: {d['model']} / API {d['version']}",
                                   f"stamp: {stamp}",
                                   "shape stub for dashboard devs"])
    log(f"[dry-run] wrote {sum(counts)} placeholder png(s) -> {dest.replace(chr(92), '/')}")
    return write_summary(dest, {
        "game": args.game,
        "dry_run": True,
        "matrix_id": f"matrix-dryrun-{stamp}",
        "state": "FINISHED",
        "gcs_path": f"gs://test-lab-dry-run-placeholder/gmp/{args.game}/{stamp}/",
        "per_device": [{"model": d["model"], "version": d["version"],
                        "outcome": "success", "screenshot_count": counts[i]}
                       for i, d in enumerate(devices)],
    })


# ------------------------------------------------------------------ main ----
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apk", help="path to the game's release .apk")
    ap.add_argument("--game", required=True, help="game slug (build/testlab/<game>/<stamp>)")
    ap.add_argument("--devices", default=DEFAULT_DEVICES,
                    help="'model=M,version=V;...' — one gcloud --device per ';'-group")
    ap.add_argument("--timeout", type=int, default=20,
                    help="per-device robo crawl timeout, minutes (default 20)")
    ap.add_argument("--dry-run", action="store_true",
                    help="no gcloud/gsutil — fabricate summary.json + placeholder pngs")
    args = ap.parse_args()

    if not args.dry_run and not args.apk:
        ap.error("--apk is required unless --dry-run")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.game):
        raise SystemExit(f"--game must be a slug (letters/digits/._-), got {args.game!r}")
    if args.timeout <= 0:
        raise SystemExit("--timeout wants minutes > 0")
    tokens, devices = parse_devices(args.devices)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(TESTLAB_DIR, args.game, stamp)

    if args.dry_run:
        log(f"summary -> {dry_run(args, devices, stamp, dest)}")
        return 0

    apk = args.apk.replace("\\", "/")         # windows law: forward slashes
    if not os.path.exists(apk):
        raise SystemExit(f"--apk not found: {apk}")
    results_dir = f"gmp/{args.game}/{stamp}"
    log(f"{args.game}: robo gate — {len(devices)} device(s) x {args.timeout}m")
    log(f"results-dir {results_dir} (inside the project's default tool-results bucket)")

    try:
        matrix_id = submit_matrix(apk, tokens, args.timeout, results_dir)
        log(f"matrix {matrix_id} submitted — polling every {POLL_SECONDS}s")
        deadline = time.time() + max(15, args.timeout * 3) * 60
        matrix = poll_matrix(matrix_id, deadline)
        state = matrix.get("state")
        gcs_path = matrix.get("gcs_path")
        outcomes = matrix.get("outcomes") or []
        if not gcs_path:
            raise RuntimeError(f"describe gave no gcsPath to download (state {state})")
        log(f"state {state}, results at {gcs_path} — downloading")
        download_results(gcs_path, dest)
        counts = count_shots(dest, devices)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        log(f"🛑 testlab gate could not complete for {args.game}: {exc}")
        return 3

    # farm order should match our --device order; fall back to our own list
    per_device = []
    for i, d in enumerate(devices):
        got = next((o for o in outcomes
                    if o.get("model") == d["model"] and str(o.get("version")) == str(d["version"])),
                   outcomes[i] if i < len(outcomes) else {})
        per_device.append({"model": d["model"], "version": d["version"],
                           "outcome": got.get("outcome") or "unknown",
                           "screenshot_count": counts[i] if i < len(counts) else 0})
    path = write_summary(dest, {"game": args.game, "dry_run": False,
                                "matrix_id": matrix_id, "state": state,
                                "gcs_path": gcs_path, "per_device": per_device})
    for d in per_device:
        log(f"  {d['model']}/{d['version']}: {d['outcome']} — {d['screenshot_count']} shot(s)")

    green = state == "FINISHED" and all(d["outcome"] == "success" for d in per_device)
    log(f"{'🟢 gate green' if green else '🛑 gate RED'} — {path}")
    return 0 if green else 2


if __name__ == "__main__":
    sys.exit(main())
