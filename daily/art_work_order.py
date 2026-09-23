"""art_work_order — the T2 auto-deploy lane: Flux renders, PIL keys, gates judge.

  python daily/art_work_order.py --order order.json
  python daily/art_work_order.py --game sonar --asset-id wreck_beacon_v2 \
      --prompt "sunken ship beacon, glowing, game sprite" --mean-color "#3fa8c8"
  order.json (schemas/art_work_order.md):
    {"game": "sonar", "asset_id": "wreck_beacon", "prompt": "...",
     "style": "flat-vector",
     "acceptance_bounds": {"opaque_min": 0.15, "opaque_max": 0.97,
                           "mean_color_hint": "#3fa8c8", "color_tol": 60}}

Per attempt (max 3 before the order fails): NVIDIA Flux.1-dev renders the
prompt (content filter + all-black frames are real — memory law), PIL magic-wand
keys the background to transparency for sprites, and the acceptance bounds are
checked against the actual pixels (opaque fraction, mean color). The first
attempt to pass acceptance runs the gate wall, then the game is exported and
scp'd to retromonkey. Rejected after 3 attempts: nothing is written to the
game's assets — report failure and get out.

Env: NVAPI_KEY (Flux), GODOT_BIN (default: the pinned 4.7.1),
TG_TOKEN + CHAT_ID for the crew notification.
"""
import argparse
import base64
import datetime
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import deque

GAMES = {
    "sonar": "C:/Users/aaron/sonar",
    "slime-line": "C:/Users/aaron/slime-line",
    "octogram-arcade": "C:/Users/aaron/octogram-arcade",
}
GODOT_DEFAULTS = [
    "C:/Users/aaron/AppData/Local/Godot/Godot_v4.7.1-stable_win64.exe",
    "C:/Users/aaron/Tools/godot/Godot_v4.7.1-stable_win64.exe",
]
FLUX_API = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev"
# the endpoint takes exactly these fields — no aspect_ratio/n/mode (memory law)
FLUX_BODY = {"cfg_scale": 3.5, "steps": 30, "width": 1024, "height": 1024}
REMOTE = "retromonkey"                     # ssh alias (retromonkey.md)
GAMES_DIR = "/home/ubuntu/site/games"
ATTEMPTS = 3
DEFAULTS = {"opaque_min": 0.15, "opaque_max": 0.97, "color_tol": 60.0}

# crew messages carry emoji; a cp1252 Windows console must not kill the lane
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg):
    print(msg, flush=True)


def tg(text):
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("CHAT_ID")
    if not tok or not chat:
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=json.dumps({"chat_id": chat, "text": text[:3900]}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as exc:
        log(f"telegram notify failed: {exc}")


def find_godot():
    if os.environ.get("GODOT_BIN"):
        return os.environ["GODOT_BIN"]
    for cand in GODOT_DEFAULTS:
        if os.path.exists(cand):
            return cand
    return "godot"


def run(cmd, timeout=900):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ------------------------------------------------------------- generation --
def flux_render(prompt, seed, key):
    body = dict(FLUX_BODY)
    body["prompt"] = prompt
    body["seed"] = seed % 999999
    req = urllib.request.Request(FLUX_API, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {key}", "Accept": "application/json",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode())
    raw = base64.b64decode(data["artifacts"][0]["base64"])
    from PIL import Image
    return Image.open(io.BytesIO(raw)).convert("RGBA")


def magic_wand_key(img, tol=42):
    """Flood-fill the border background away (classic wand from the edges).

    Seeds on the four corner colors; any border-connected pixel within `tol`
    of its seed goes fully transparent. Sprites keep hard edges; full-bleed
    assets should be ordered with key_background=false.
    """
    w, h = img.size
    px = img.load()
    seeds = [px[x, y][:3] for x, y in
             ((1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2))]

    def close(a, b):
        return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2]) <= tol * 3

    seen = bytearray(w * h)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            q.append((x, y))
    killed = 0
    while q:
        x, y = q.popleft()
        if x < 0 or y < 0 or x >= w or y >= h:
            continue
        i = y * w + x
        if seen[i]:
            continue
        p = px[x, y]
        if not any(close(p[:3], s) for s in seeds):
            continue
        seen[i] = 1
        px[x, y] = (p[0], p[1], p[2], 0)
        killed += 1
        q.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    return img, killed / (w * h)


def hex_rgb(hint):
    m = re.match(r"^#?([0-9a-fA-F]{6})$", str(hint or "").strip())
    return tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)) if m else None


def check_acceptance(img, bounds):
    """-> (ok, report). opaque fraction + mean color of what survived keying."""
    from PIL import ImageStat
    w, h = img.size
    total = w * h
    alpha = img.getchannel("A")
    hist = alpha.histogram()
    opaque = sum(hist[129:])                       # pixels with alpha > 128
    frac = opaque / total if total else 0.0
    mean = (0, 0, 0)
    lum = 255.0
    if opaque:
        mask = alpha.point(lambda a: 255 if a > 128 else 0)
        rgb = img.convert("RGB")
        stat = ImageStat.Stat(rgb, mask)
        mean = tuple(int(v) for v in stat.mean)
        lum = (stat.mean[0] + stat.mean[1] + stat.mean[2]) / 3.0
    report = {"opaque": round(frac, 4), "mean_rgb": list(mean),
              "mean_hex": "#%02x%02x%02x" % mean}
    if opaque == 0:
        return False, {**report, "why": "nothing survived keying"}
    if frac > 0.9 and lum < 8:
        return False, {**report, "why": "image is near-all-black (Flux black-frame failure)"}
    if frac < bounds["opaque_min"]:
        return False, {**report, "why": f"opaque {frac:.3f} < min {bounds['opaque_min']}"}
    if frac > bounds["opaque_max"]:
        return False, {**report, "why": f"opaque {frac:.3f} > max {bounds['opaque_max']}"}
    hint = hex_rgb(bounds.get("mean_color_hint"))
    if hint:
        dist = sum(abs(a - b) for a, b in zip(mean, hint))
        report["color_dist"] = dist
        if dist > bounds["color_tol"] * 3:
            return False, {**report, "why": (f"mean color {report['mean_hex']} too far from "
                                             f"hint {bounds['mean_color_hint']} (dist {dist})")}
    return True, report


# ------------------------------------------------- gates + deploy (T1/T2) --
def gate_wall(root, godot, runs=1):
    tests = os.path.join(root, "tests")
    suites = sorted(f[:-3] for f in os.listdir(tests) if f.endswith(".gd")) \
        if os.path.isdir(tests) else []
    if not suites:
        return [], suites
    run([godot, "--headless", "--path", root, "--import"])
    fails = []
    for suite in suites:
        for attempt in range(runs):
            r = run([godot, "--headless", "--path", root,
                     "--script", f"res://tests/{suite}.gd"])
            out = (r.stdout or "") + (r.stderr or "")
            m = re.search(r"(\d+) failed", out)
            if r.returncode != 0 or (m and int(m.group(1)) > 0):
                fails.append(f"{suite}:\n" + "\n".join(out.splitlines()[-6:]))
                break
    return fails, suites


def web_export_path(root):
    cfg = os.path.join(root, "export_presets.cfg")
    if os.path.exists(cfg):
        text = open(cfg, encoding="utf-8").read()
        for block in re.split(r"\[preset\.\d+\]", text)[1:]:
            if re.search(r'^name="Web"\s*$', block, re.M):
                m = re.search(r'export_path="([^"]+)"', block)
                if m:
                    return m.group(1)
    return "build/web/index.html"


def deploy_web(root, slug, godot):
    rel = web_export_path(root)
    out_dir = os.path.dirname(rel) or "."
    r = run([godot, "--headless", "--path", root, "--export-release", "Web", rel], timeout=1200)
    if not os.path.exists(os.path.join(root, rel)):
        raise RuntimeError(f"web export produced no {rel}\n{(r.stderr or '')[-400:]}")
    files = sorted(f for f in os.listdir(os.path.join(root, out_dir))
                   if os.path.isfile(os.path.join(root, out_dir, f)))
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", REMOTE,
             f"mkdir -p {GAMES_DIR}/{slug}"])
    if r.returncode != 0:
        raise RuntimeError(f"ssh mkdir failed: {(r.stderr or '').strip()}")
    for f in files:
        r = run(["scp", "-o", "BatchMode=yes", os.path.join(root, out_dir, f),
                 f"{REMOTE}:{GAMES_DIR}/{slug}/"])
        if r.returncode != 0:
            raise RuntimeError(f"scp {f} failed: {(r.stderr or '').strip()}")
    return [f"{out_dir}/{f}" for f in files]


# ------------------------------------------------------------------ order --
def load_order(args):
    if args.order:
        raw = sys.stdin.read() if args.order == "-" else open(args.order, encoding="utf-8").read()
        order = json.loads(raw)
    else:
        order = {}
    order["game"] = order.get("game") or args.game
    order["asset_id"] = order.get("asset_id") or args.asset_id
    order["prompt"] = order.get("prompt") or args.prompt
    order["style"] = order.get("style") or args.style or "flat-vector"
    bounds = dict(DEFAULTS)
    bounds.update(order.get("acceptance_bounds") or order.get("acceptance") or {})
    if args.opaque_min is not None:
        bounds["opaque_min"] = args.opaque_min
    if args.opaque_max is not None:
        bounds["opaque_max"] = args.opaque_max
    if args.mean_color:
        bounds["mean_color_hint"] = args.mean_color
    order["acceptance_bounds"] = bounds
    order.setdefault("key_background", True)
    order.setdefault("key_tolerance", 42)
    order["wo"] = order.get("wo") or (f"wo-art-{datetime.datetime.now():%Y%m%d}-{int(time.time()) % 10000}")
    return order


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--order", help="work-order JSON file ('-' = stdin)")
    ap.add_argument("--game")
    ap.add_argument("--asset-id", help="file name the game resolves (no .png)")
    ap.add_argument("--prompt")
    ap.add_argument("--style", help="flat-vector | pixel | clay")
    ap.add_argument("--opaque-min", type=float)
    ap.add_argument("--opaque-max", type=float)
    ap.add_argument("--mean-color", help="expected mean color #rrggbb")
    ap.add_argument("--root", help="override the game repo directory")
    ap.add_argument("--no-deploy", action="store_true")
    ap.add_argument("--no-gates", action="store_true")
    args = ap.parse_args()

    order = load_order(args)
    game, asset_id, prompt = order["game"], order["asset_id"], order["prompt"]
    if not (game and asset_id and prompt):
        ap.error("need --order or --game/--asset-id/--prompt")
    key = os.environ.get("NVAPI_KEY")
    if not key:
        raise SystemExit("NVAPI_KEY not set — Flux renders need it")

    try:
        roots = dict(GAMES)
        roots.update(json.loads(os.environ.get("GAME_ROOTS", "{}")))
    except ValueError:
        pass
    root = args.root or roots.get(game)
    if not root or not os.path.isdir(root):
        raise SystemExit(f"unknown game {game!r}: no repo dir at {root}")

    bounds, wo = order["acceptance_bounds"], order["wo"]
    style = order["style"]
    suffix = {"pixel": "crisp pixel art, limited palette", "clay": "soft clay render, matte"}.get(style, "flat vector game art, clean shapes, no text")
    full_prompt = f"{prompt}, {suffix}, centered, plain solid background"
    dest = os.path.join(root, "assets", "generated", f"{asset_id}.png")
    log(f"{wo}: {game}/{asset_id} — {prompt!r} ({style}, bounds {bounds})")

    accepted = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            img = flux_render(full_prompt, seed=int(time.time()) + attempt, key=key)
        except Exception as exc:
            log(f"attempt {attempt}: flux render failed: {exc}")
            time.sleep(5)
            continue
        if order["key_background"]:
            img, killed = magic_wand_key(img, tol=order["key_tolerance"])
        else:
            killed = 0.0
        ok, report = check_acceptance(img, bounds)
        report["keyed"] = round(killed, 3)
        log(f"attempt {attempt}: {'PASS' if ok else 'reject'} {report}")
        if ok:
            accepted = (img, attempt, report)
            break
        time.sleep(2)

    if not accepted:
        msg = (f"🛑 {wo}: art order FAILED after {ATTEMPTS} attempts — {game}/{asset_id} "
               f"never passed acceptance. Nothing written, nothing deployed.")
        log(msg)
        tg(msg)
        return 2

    img, attempt, report = accepted
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    img.save(dest)
    log(f"accepted on attempt {attempt} -> {dest}")

    godot = find_godot()
    if not args.no_gates:
        log(f"gate wall: {godot}")
        fails, suites = gate_wall(root, godot)
        if fails:
            os.remove(dest)                      # a rejected asset never ships
            msg = f"🛑 {wo}: {game}/{asset_id} passed pixels but FAILED the gate wall — asset removed"
            log(msg + "\n" + "\n".join(fails)[:600])
            tg(msg)
            return 3
        log(f"gates green: {len(suites)} suite(s)")
    if args.no_deploy:
        log("--no-deploy: asset written, game not deployed")
        return 0
    try:
        shipped = deploy_web(root, game, godot)
    except Exception as exc:
        msg = f"⚠️ {wo}: {game}/{asset_id} written but deploy FAILED: {exc}"
        log(msg)
        tg(msg)
        return 4
    msg = (f"🎨 {wo}: {game} new art — {asset_id} (attempt {attempt}, opaque {report['opaque']}, "
           f"mean {report['mean_hex']}) — gates green, deployed to retromonkey ({len(shipped)} files)")
    log(msg)
    tg(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
