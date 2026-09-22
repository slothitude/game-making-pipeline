"""daily_remake — the one-game-a-day cloud builder (GitHub Actions native).

Picks the next unbuilt entry from journal/BACKLOG.md and builds its M1 entirely
in the cloud: boss LLM writes the spec + scaffold + tests (workers draft), the
gate wall judges (green twice), Flux makes the art, git gets the repo, Telegram
gets the announcement. No local machine required.

Env: NVAPI_KEY, OPENROUTER_KEY, TG_TOKEN, CHAT_ID, GAMES_TOKEN, GODOT_BIN (default: godot)
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GMP = os.path.dirname(HERE)
BACKLOG = os.path.join(GMP, "journal", "BACKLOG.md")
WORK_ROOT = os.environ.get("WORK_ROOT", "/tmp/remakes")
LLM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
BOSS = "z-ai/glm-5.3"
BACKUP = "moonshotai/kimi-k3"
WORKER_URL = "https://openrouter.ai/api/v1/chat/completions"
WORKER_MODEL = "openrouter/free"
GATE_ATTEMPTS = 3


def llm(messages, max_tokens=8192):
    """Three-deep brain chain: boss -> kimi-k3 -> openrouter takeover."""
    for model in (BOSS, BACKUP):
        payload = {"model": model, "messages": messages,
                   "temperature": 0.3, "max_tokens": max_tokens}
        if model != BOSS:
            payload.pop("chat_template_kwargs", None)
        for _ in range(2):
            try:
                return api(LLM_URL, payload, key=os.environ["NVAPI_KEY"])
            except Exception as exc:
                print(f"llm {model} fail: {exc}", flush=True)
                time.sleep(10)
    takeover = dict(payload)
    takeover["model"] = WORKER_MODEL
    return api(WORKER_URL, takeover, key=os.environ.get("OPENROUTER_KEY", ""))


def api(url, payload, key, timeout=400):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    content = body["choices"][0]["message"].get("content") or ""
    if not content.strip():
        raise RuntimeError("empty llm content")
    return content


def tg(text):
    try:
        api(f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
            {"chat_id": os.environ.get("CHAT_ID"), "text": text[:3900]}, key="")
    except Exception:
        pass


def next_entry():
    text = open(BACKLOG, encoding="utf-8").read()
    for line in text.splitlines():
        m = re.match(r"\|\s*(\d+)\s*\|\s*([^|]+)\|", line)
        if m:
            return m.group(1), m.group(2).strip()
    return None, None


def write_files(repo, llm_output):
    """Parse ```path:file.gd blocks and ``` blocks after '### <path>' markers."""
    made = []
    pattern = re.compile(r"```(?:[a-zA-Z0-9_./-]+:)?\s*([a-zA-Z0-9_./-]+\.(?:gd|tscn|godot|cfg|txt|json))\s*\n(.*?)```", re.DOTALL)
    for m in pattern.finditer(llm_output):
        rel, code = m.group(1), m.group(2)
        dest = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "w", encoding="utf-8", newline="\n").write(code)
        made.append(rel)
    return made


def run_gates(repo, godot):
    fails = []
    r = subprocess.run([godot, "--headless", "--path", repo, "--import"],
                       capture_output=True, text=True, timeout=600)
    suites = sorted({f[:-3] for f in os.listdir(os.path.join(repo, "tests"))
                     if f.endswith(".gd")} if os.path.isdir(os.path.join(repo, "tests")) else [])
    for suite in suites:
        for run in range(2):
            r = subprocess.run([godot, "--headless", "--path", repo,
                                "--script", f"res://tests/{suite}.gd"],
                               capture_output=True, text=True, timeout=900)
            out = r.stdout + r.stderr
            if r.returncode != 0 or re.search(r"\d+ failed", out) and not re.search(r"0 failed", out):
                fails.append(f"{suite} run{run + 1}:\n" + "\n".join(out.splitlines()[-10:]))
    return fails, suites


def main():
    num, study = next_entry()
    if not study:
        tg("daily remake: backlog empty — add entries to BACKLOG.md")
        return 0
    name = os.environ.get("GAME_NAME") or f"remake-{num}"
    repo = os.path.join(WORK_ROOT, name)
    os.makedirs(repo, exist_ok=True)
    print(f"building: {study} -> {name}", flush=True)
    tg(f"📅 Daily remake #{num}: {study} — the crew is building {name.upper()} M1")

    system = ("You are the Game Making Pipeline's game builder. You write COMPLETE, "
              "runnable Godot 4.7 GDScript projects. Output ONLY files as fenced blocks "
              "labeled ```path:scripts/foo.gd — every file complete, tabs, no placeholders. "
              "Laws: every tunable is a named const in scripts/feel.gd; tests are SceneTree "
              "scripts (deferred start: _summary.call_deferred(); await pattern; PASS/FAIL "
              "lines + 'Ran N checks: X passed, 0 failed' summary; quit(1/0)); 2D games are "
              "phone-portrait 540x960 canvas_items/expand gl_compatibility; code-drawn art "
              "only (no image files); gl_compatibility safe.")
    spec_prompt = (f"Build milestone 1 of an original systems-study of: {study}.\n"
                   "Files required: project.godot (name the game something ORIGINAL, main scene "
                   "res://scenes/main.tscn, portrait phone settings), .gitignore (.godot/, build/), "
                   "scripts/feel.gd (all consts), the core gameplay scripts for the FIRST playable "
                   "loop only (one mechanic working end to end), scenes/main.tscn (you may build the "
                   "tree from code in main.gd _ready), tests/m1_tests.gd (12-20 checks incl. the "
                   "core mechanic math) and tests/m1_replay.gd (scripted 10s, no soft-locks).")
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": spec_prompt}]
    for attempt in range(1, GATE_ATTEMPTS + 1):
        try:
            out = llm(messages)
        except Exception as exc:
            tg(f"daily remake {name}: brain chain dead — {exc}")
            return 1
        made = write_files(repo, out)
        if not made:
            messages.append({"role": "user", "content": "No labeled files parsed. Output ```path:file blocks only."})
            continue
        fails, suites = run_gates(repo, os.environ.get("GODOT_BIN", "godot"))
        if not fails and suites:
            tg(f"✅ daily remake {name} ({study}): M1 GREEN — {len(suites)} suite(s) x2 on attempt {attempt}")
            print("M1 GREEN", flush=True)
            return 0
        messages += [{"role": "assistant", "content": out[-3000:]},
                     {"role": "user", "content": "Gates failed:\n" + "\n".join(fails)[:2500] +
                      "\nRegenerate the FULL corrected files."}]
    tg(f"🛑 daily remake {name}: M1 not green after {GATE_ATTEMPTS} attempts — parked in the backlog")
    return 2


if __name__ == "__main__":
    sys.exit(main())
