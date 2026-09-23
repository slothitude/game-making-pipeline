"""update_tunables — the T1 auto-deploy lane: crew tuning values, no code changes.

  python daily/update_tunables.py --order order.json
  python daily/update_tunables.py --game sonar --tunable AIR_SECONDS --new-value 90
  order.json: {"game": "sonar", "tunable": "AIR_SECONDS", "new_value": 90}

Validates the new value against <game>/data/tunables.json (exists, type, range),
patches the const line in the game's feel file, runs the gate wall (every
tests/*.gd suite must stay green), and only then deploys the game to retromonkey.
A red gate wall reverts the change — a tuning lane that can ship a broken game
is not an auto-deploy lane. Every green deploy is also a local git commit, so
every change stays revertible (pipeline law).

Env: GODOT_BIN (default: the pinned 4.7.1), NVAPI_KEY not needed here,
TG_TOKEN + CHAT_ID for the crew notification.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAMES = {
    "sonar": "C:/Users/aaron/sonar",
    "slime-line": "C:/Users/aaron/slime-line",
    "octogram-arcade": "C:/Users/aaron/octogram-arcade",
}
GODOT_DEFAULTS = [
    "C:/Users/aaron/AppData/Local/Godot/Godot_v4.7.1-stable_win64.exe",
    "C:/Users/aaron/Tools/godot/Godot_v4.7.1-stable_win64.exe",
]
REMOTE = "retromonkey"          # ssh alias (retromonkey.md)
GAMES_DIR = "/home/ubuntu/site/games"
CONST_RE = re.compile(
    r"^(\s*const\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*([^=\n]+?))?\s*(?::=|=)\s*)([^\n#]*?)(\s*#.*)?$"
)

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
        import urllib.request
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


# --------------------------------------------------------------- gate wall --
def gate_wall(root, godot, runs=1):
    """Every tests/*.gd suite must exit 0 with a '0 failed' summary."""
    tests = os.path.join(root, "tests")
    suites = sorted(f[:-3] for f in os.listdir(tests) if f.endswith(".gd")) \
        if os.path.isdir(tests) else []
    if not suites:
        return [], suites
    r = run([godot, "--headless", "--path", root, "--import"])
    if r.returncode != 0 and "ERROR" in (r.stderr or ""):
        log(f"import pass unhappy: {(r.stderr or '')[-300:]}")
    fails = []
    for suite in suites:
        for attempt in range(runs):
            r = run([godot, "--headless", "--path", root,
                     "--script", f"res://tests/{suite}.gd"])
            out = (r.stdout or "") + (r.stderr or "")
            m = re.search(r"(\d+) failed", out)
            if r.returncode != 0 or (m and int(m.group(1)) > 0):
                tail = "\n".join(out.splitlines()[-8:])
                fails.append(f"{suite} (run {attempt + 1}/{runs}) rc={r.returncode}:\n{tail}")
                break
    return fails, suites


# ------------------------------------------------------------------ deploy --
def web_export_path(root):
    """The 'Web' preset's export_path from export_presets.cfg (default build/web)."""
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
    """Export the Web preset and scp it to retromonkey (drop a folder, it's live)."""
    rel = web_export_path(root)
    out_dir = os.path.dirname(rel) or "."
    r = run([godot, "--headless", "--path", root, "--export-release", "Web", rel], timeout=1200)
    index = os.path.join(root, rel)
    if not os.path.exists(index):
        raise RuntimeError(f"web export produced no {rel}\n{(r.stdout or '')[-400:]}\n{(r.stderr or '')[-400:]}")
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


# ------------------------------------------------------------------ tuning --
def fmt_value(entry, value):
    if entry["type"] == "int":
        return str(int(value))
    if entry["type"] == "bool":
        return "true" if value else "false"
    s = f"{float(value):g}"
    if "." not in s and "e" not in s and "E" not in s:
        s += ".0"
    return s


def validate(entry, name, new_value):
    if not entry.get("editable"):
        return f"{name} is not crew-editable (type {entry.get('type')})"
    t = entry.get("type")
    if t == "int":
        if isinstance(new_value, bool) or not isinstance(new_value, int):
            return f"{name} wants an integer, got {new_value!r}"
    elif t == "float":
        if isinstance(new_value, bool) or not isinstance(new_value, (int, float)):
            return f"{name} wants a number, got {new_value!r}"
    elif t == "bool":
        if not isinstance(new_value, bool):
            return f"{name} wants true/false, got {new_value!r}"
    else:
        return f"{name} is a {t} — only int/float/bool tunables are editable"
    rng = entry.get("range")
    if rng and (new_value < rng[0] or new_value > rng[1]):
        return f"{name}={new_value} outside its range {rng[0]}..{rng[1]}"
    return None


def patch_const(source, const, new_text):
    """Rewrite one const line's value, preserving the type annotation + comment."""
    for line in source.splitlines(True):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        m = CONST_RE.match(body)
        if m and m.group(2) == const:
            return source.replace(line, f"{m.group(1)}{new_text}{m.group(5) or ''}{eol}", 1)
    raise RuntimeError(f"const {const} not found in feel file")


def load_order(args):
    if args.order:
        raw = sys.stdin.read() if args.order == "-" else open(args.order, encoding="utf-8").read()
        order = json.loads(raw)
        return (order.get("game"), order.get("tunable"), order.get("new_value"),
                order.get("wo", ""))
    return args.game, args.tunable, args.new_value, ""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--order", help="work-order JSON file ('-' = stdin)")
    ap.add_argument("--game")
    ap.add_argument("--tunable")
    ap.add_argument("--new-value", type=float)
    ap.add_argument("--root", help="override the game repo directory")
    ap.add_argument("--runs", type=int, default=1, help="gate wall passes per suite")
    ap.add_argument("--no-deploy", action="store_true")
    ap.add_argument("--no-commit", action="store_true")
    args = ap.parse_args()

    game, tunable, new_value, wo_id = load_order(args)
    if not (game and tunable and new_value is not None):
        ap.error("need --order {game, tunable, new_value} or --game/--tunable/--new-value")

    try:
        roots = dict(GAMES)
        roots.update(json.loads(os.environ.get("GAME_ROOTS", "{}")))
    except ValueError:
        pass
    root = args.root or roots.get(game)
    if not root or not os.path.isdir(root):
        raise SystemExit(f"unknown game {game!r}: no repo dir at {root}")

    tpath = os.path.join(root, "data", "tunables.json")
    if not os.path.exists(tpath):
        raise SystemExit(f"{tpath} missing — run daily/extract_tunables.py first")
    tunables = json.load(open(tpath, encoding="utf-8"))
    entry = (tunables.get("tunables") or {}).get(tunable)
    if entry is None:
        known = ", ".join(sorted((tunables.get("tunables") or {}).keys()))
        raise SystemExit(f"{game}: no tunable {tunable!r}. Known: {known}")

    bad = validate(entry, tunable, new_value)
    if bad:
        tg(f"🛑 tunables order rejected: {bad}")
        raise SystemExit(f"REJECTED: {bad}")

    const = entry.get("const", tunable)
    if entry["type"] == "int" and isinstance(new_value, float) and new_value.is_integer():
        new_value = int(new_value)          # CLI passes floats; ints stay ints
    feel_path = os.path.join(root, tunables.get("source", "scripts/feel.gd"))
    # newline="" keeps CRLF working-tree files byte-identical outside the patch
    feel_src = open(feel_path, encoding="utf-8", newline="").read()
    tjson_src = open(tpath, encoding="utf-8", newline="").read()
    old_value = entry["value"]
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    label = f"{wo_id} " if wo_id else ""
    log(f"{game}: {tunable} ({const}) {old_value} -> {new_value}")

    # 1. patch the const + the JSON (both revertible from the saved bytes)
    open(feel_path, "w", encoding="utf-8", newline="").write(
        patch_const(feel_src, const, fmt_value(entry, new_value)))
    entry["value"] = new_value
    entry["updated"] = stamp
    with open(tpath, "w", encoding="utf-8", newline="\n") as f:
        json.dump(tunables, f, indent=2)
        f.write("\n")

    # 2. gate wall or revert
    godot = find_godot()
    log(f"gate wall: {godot}")
    fails, suites = gate_wall(root, godot, runs=args.runs)
    if fails:
        open(feel_path, "w", encoding="utf-8", newline="").write(feel_src)
        open(tpath, "w", encoding="utf-8", newline="").write(tjson_src)
        msg = (f"🛑 {label}tunables {game}/{tunable}={new_value} REVERTED — gate wall red "
               f"({len(fails)} suite fail):\n" + "\n".join(fails)[:800])
        log(msg)
        tg(msg)
        return 2
    log(f"gates green: {len(suites)} suite(s) x{args.runs}")

    # 3. commit (every deploy is a git commit) + deploy
    if not args.no_commit and os.path.isdir(os.path.join(root, ".git")):
        subprocess.run(["git", "-C", root, "add", "data/tunables.json",
                        os.path.relpath(feel_path, root).replace("\\", "/")])
        c = subprocess.run(["git", "-C", root, "commit", "-q", "-m",
                            f"tunables {label}{tunable} {old_value} -> {new_value} ({stamp})"])
        if c.returncode == 0:
            log("committed on the game repo")
    if args.no_deploy:
        log("--no-deploy: change is in the working tree, not on retromonkey")
        return 0
    try:
        shipped = deploy_web(root, game, godot)
    except Exception as exc:
        msg = f"⚠️ {label}tunables {game}/{tunable}={new_value} live in git, deploy FAILED: {exc}"
        log(msg)
        tg(msg)
        return 3
    msg = (f"🎚 {label}{game} tuned: {tunable} {old_value} → {new_value} — gates green "
           f"({len(suites)} suites), deployed to retromonkey ({len(shipped)} files)")
    log(msg)
    tg(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
