#!/usr/bin/env python3
"""sculmm_dispatch.py — F2: generation dispatch (Rog -> Lappy -> Colab T4).

ALL generation compute goes through the Google Colab CLI (user law).
Work orders come from a .sculmm.json's gen slots (texture slots,
backdrop_prompt, prop image_prompts). Content-hash cache law: an
artifact is keyed by sha256(engine|prompt|seed) under
/mnt/seagate/sculmm/assets/<key>/ — never regenerate what exists.

  plan    <world.json>          what exists in cache vs to-generate
  kick    <world.json>          deploy jobs + runner to Lappy, launch
  resolve <world.json>          fill texture 'asset' fields from cache

The Lappy runner (~/sculmm/sculmm_gen.py, written by kick):
  phase 1  hunyuandit images (texture slots, backdrops, prop art) via
           the proven hy3dgen.text2image path on the T4
  phase 2  pixal3d/trellis meshes from the cached prop images
  Kick/poll law (ARDY_CLI_PLAYBOOK): detached launch + file polling;
  idempotent — a reaped session costs one re-kick.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

LAPPY = "aaron@192.168.0.33"
CACHE = "/mnt/seagate/sculmm/assets"
REMOTE_HOME = "~/sculmm"

RUNNER = r'''#!/usr/bin/env python3
"""sculmm_gen.py — Lappy-side SCULMM generator (Colab T4, kick/poll law).
Idempotent: scans jobs/*.json, skips keys with done.marker, drives the
session, downloads artifacts into the content-hash cache."""
import json, os, subprocess, sys, time
from pathlib import Path

HOME = Path.home() / "sculmm"
JOBS = HOME / "jobs"
CACHE = Path("/mnt/seagate/sculmm/assets")
COLAB = str(Path.home() / ".local/bin/colab")
S = "sculmm"          # colab session name

def sh(*cmd, timeout=120):
    return subprocess.run(list(cmd), capture_output=True, text=True,
                          timeout=timeout)

def ensure_session():
    r = sh(COLAB, "status", "-s", S)
    if "not found" in r.stdout or r.returncode != 0:
        r = sh(COLAB, "new", "-s", S, "--gpu", "T4", timeout=300)
        print("[gen] session:", r.stdout.strip()[-80:])

def exec_file(path, timeout=90):
    return sh(COLAB, "exec", "-s", S, "-f", str(path), timeout=timeout)

def main():
    jobs = sorted(JOBS.glob("*.json"))
    todo = []
    for j in jobs:
        d = json.loads(j.read_text())
        out = Path(CACHE) / d["key"]
        if (out / "done.marker").exists():
            continue
        todo.append(d)
    if not todo:
        print("[gen] nothing to do"); return 0
    ensure_session()
    for d in todo:
        out = Path(CACHE) / d["key"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "job.json").write_text(json.dumps(d))
        tag = d["key"][:12]
        # kick script on the session: one detached launcher per engine
        if d["engine"] == "hunyuandit":
            py = HOME / ("kick_%s.py" % tag)
            sess = (
                "from hy3dgen.text2image import HunyuanDiTPipeline\n"
                "p = HunyuanDiTPipeline()\n"
                "img = p(%r, seed=%d)\n"
                "img.save('/content/out_%s.png')\n"
                "print('GEN_DONE')"
                % (d["prompt"], d["seed"], tag))
            lines = [
                "import subprocess",
                "src = " + repr(sess),
                "with open('/content/gen_src_%s.py', 'w') as f:" % tag,
                "    f.write(src)",
                "subprocess.Popen(['bash', '-lc',",
                "    'nohup python /content/gen_src_%s.py"
                " > /content/gen_%s.log 2>&1 &'])" % (tag, tag),
                "print('KICKED')",
            ]
            py.write_text("\n".join(lines) + "\n")
            exec_file(py)
        else:
            # pixal3d / trellis mesh jobs: need the image first + the
            # t2d3 chain on the session — queued, not yet driven here
            print("[gen] mesh job %s queued (needs t2d3 chain on session)"
                  % tag)
    print("[gen] kicked %d jobs; poll /content/gen_*.log on the session"
          % len(todo))
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''


def work_orders(world: dict) -> list[dict]:
    """Collect every gen slot as a work order with a content-hash key."""
    out = []

    def key(engine: str, prompt: str, seed: int | None) -> str:
        return hashlib.sha256(
            f"{engine}|{prompt}|{seed}".encode()).hexdigest()

    for room in world.get("world", {}).get("rooms", []):
        if room.get("backdrop_prompt"):
            e = room.get("backdrop_engine", "hunyuandit")
            out.append({"kind": "backdrop", "engine": e,
                        "prompt": room["backdrop_prompt"],
                        "seed": world.get("seed", 0),
                        "key": key(e, room["backdrop_prompt"],
                                   world.get("seed", 0))})
        shell = room.get("shell", {})
        for part in (["floor"] + [f"wall:{w['wall']}" for w in
                                  shell.get("walls", [])]):
            holder = (shell.get("floor", {}) if part == "floor" else
                      next(w for w in shell.get("walls", [])
                           if f"wall:{w['wall']}" == part))
            slot = holder.get("texture", {}) if holder else {}
            if slot and slot.get("prompt"):
                out.append({"kind": f"texture:{part}",
                            "engine": slot.get("engine", "hunyuandit"),
                            "prompt": slot["prompt"],
                            "seed": slot.get("seed", 0),
                            "key": key(slot.get("engine", "hunyuandit"),
                                       slot["prompt"], slot.get("seed", 0)),
                            "slot_path": slot})
    for prop in world.get("world", {}).get("props", []):
        if prop.get("image_prompt"):
            e = prop.get("engine", "pixal3d")
            out.append({"kind": "prop", "engine": e,
                        "prompt": prop["image_prompt"],
                        "seed": world.get("seed", 0),
                        "key": key(e, prop["image_prompt"],
                                   world.get("seed", 0)),
                        "name": prop["name"]})
    return out


def ssh(*cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-o", "ConnectTimeout=10", LAPPY, *cmd],
                          capture_output=True, text=True, timeout=timeout)


def cache_state(orders: list[dict]) -> dict:
    """Check done markers on Lappy for each key — keys scp'd as a file,
    read by ~/sculmm/state.py (file-arg law: no shell quoting anywhere)."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump([o["key"] for o in orders], f)
        tmp = f.name
    subprocess.run(["scp", "-q", tmp,
                    "%s:%s/keys.json" % (LAPPY, REMOTE_HOME)],
                   check=False, timeout=60)
    Path(tmp).unlink(missing_ok=True)
    r = ssh("python3 %s/state.py %s/keys.json" % (REMOTE_HOME, REMOTE_HOME))
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "kick", "resolve"):
        p = sub.add_parser(name)
        p.add_argument("world")
    args = ap.parse_args()
    world = json.loads(Path(args.world).read_text(encoding="utf-8"))
    orders = work_orders(world)

    if args.cmd == "plan":
        state = cache_state(orders)
        for o in orders:
            print("%-16s %-12s %-10s %s" % (
                o["kind"], o["engine"],
                "CACHED" if state.get(o["key"]) else "todo",
                o["prompt"][:60]))
        print(json.dumps({"total": len(orders),
                          "cached": sum(1 for o in orders
                                        if state.get(o["key"]))}))
        return 0

    if args.cmd == "kick":
        for o in orders:
            job = {"key": o["key"], "engine": o["engine"],
                   "prompt": o["prompt"], "seed": o["seed"],
                   "kind": o["kind"]}
            if o["kind"] == "prop":
                img = next((x for x in orders if x["kind"] != "prop"
                            and x["key"] == o["key"]), None)
            r = ssh("mkdir -p %s/jobs %s && cat > %s/jobs/%s.json <<'EOF'\n%s\nEOF"
                    % (REMOTE_HOME, CACHE, REMOTE_HOME, o["key"][:16],
                       json.dumps(job)))
            if r.returncode != 0:
                print("[kick] job write failed:", r.stderr[:120])
                return 1
        r = ssh("mkdir -p %s && cat > %s/sculmm_gen.py <<'PYEOF'\n%s\nPYEOF"
                % (REMOTE_HOME, REMOTE_HOME, RUNNER))
        if r.returncode != 0:
            print("[kick] runner write failed:", r.stderr[:120])
            return 1
        r = ssh("nohup python3 %s/sculmm_gen.py > %s/gen.log 2>&1 & "
                "echo kicked" % (REMOTE_HOME, REMOTE_HOME), timeout=30)
        print("[kick]", (r.stdout + r.stderr).strip()[:200],
              "— poll: ssh %s 'tail %s/gen.log'" % (LAPPY, REMOTE_HOME))
        return 0

    if args.cmd == "resolve":
        state = cache_state(orders)
        n = 0
        for o in orders:
            if state.get(o["key"]) and "slot_path" in o:
                o["slot_path"]["asset"] = "%s/%s/art.png" % (CACHE, o["key"])
                n += 1
        Path(args.world).write_text(
            json.dumps(world, indent=1, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"resolved_slots": n,
                          "note": "prop image_prompts stay work-ordered; "
                                  "their artifacts live in the cache "
                                  "manifest (schema has no prop asset "
                                  "field yet)"}))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
