#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ralph Wiggum - the Pipeline's persistence daemon (memento loop).

Ralph keeps working the task board until everything is done, then idles.
He is the FIRST agent built on the new gateway: he drives the Pipeline
EXCLUSIVELY through its REST API (default http://127.0.0.1:8902, X-Token
from GATEWAY_TOKEN) and never touches services directly. See CONNECT.md.

THE MEMENTO LOOP (canonical Ralph Wiggum pattern, ghuntley.com/loop) -
the memo file RALPH.md is the heart. Every pass:

  (a) START by reading RALPH.md (where I am, in-flight job ids, blockers,
      next) and ralph_tasks.json (the board);
  (b) do EXACTLY ONE increment of work (inventory sync + one pick:
      dispatch a gateway task, or file a manual task as blocked, or idle);
  (c) REWRITE RALPH.md so the next pass - which starts with zero memory -
      knows exactly where it left off. Same-prompt philosophy: every pass
      behaves as if told "keep going, consult your memo".

Fixed memo sections: WHERE I AM / IN FLIGHT (job ids) / BLOCKED (reasons) /
NEXT / LOG (append-only, timestamped, capped to last 50 lines).

Env:
  GATEWAY_TOKEN   X-Token for the gateway (required; refuses without it)
  GATEWAY_URL     gateway base URL (default http://127.0.0.1:8902)
  TG_TOKEN        optional Telegram bot token (progress posts, silent-fail)
  TG_CHAT_ID      optional Telegram chat id
  RALPH_HOME      Ralph's home dir (default: this script's directory);
                  holds ralph_tasks.json (board) and RALPH.md (memo)
  NVAPI_KEY       optional - enables --brain via the NVIDIA ladder
  LADDER_PATH     optional - ladder.json location (default repo daily/ladder.json)
  OPENROUTER_API_KEY  optional - --brain openrouter fallback

CLI:
  --once              one pass, then exit (systemd timer friendly)
  (default)           loop forever, sleeping --idle-seconds between passes
  --idle-seconds N    sleep between passes (default 120)
  --max-inflight N    never have more than N jobs in flight (default 3)
  --brain             consult the LLM ladder for ambiguous picks (off by
                      default; without it Ralph is fully deterministic)
  --home PATH         override RALPH_HOME
  --board PATH        override board path (default HOME/ralph_tasks.json)

Windows laws honored: UTF-8 stdout reconfigure, forward slashes, no tab
characters in string literals.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# constants / windows laws
# ---------------------------------------------------------------------------

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))  # cloud/ralph -> repo
DEFAULT_LADDER = os.path.join(REPO_ROOT, "daily", "ladder.json").replace(os.sep, "/")

TASK_STATUSES = ("todo", "queued", "running", "done", "blocked")
JOB_TO_TASK = {"queued": "queued", "running": "running", "done": "done",
               "failed": "blocked"}  # task enum has no 'failed' -> blocked
LOG_CAP = 50  # memo LOG section keeps the last 50 timestamped lines

# op name -> (endpoint, allowed payload keys). Mirrors CONNECT.md 1:1.
OPS = {
    "new_game":    ("/api/games", ("name", "template", "pitch", "requester")),
    "tunable":     ("/api/orders/tunable", ("game", "tunable", "new_value")),
    "art":         ("/api/orders/art", ("game", "prompt", "asset_id")),
    "critique":    ("/api/critique", ("game", "mode")),
    "playtest":    ("/api/playtest", ("game", "seconds")),
    "gpu":         ("/api/gpu", ("kind", "staging_dir", "return_dir", "payload")),
    "device_test": ("/api/device_test", ("game", "apk")),
    "deploy":      ("/api/deploy", ("game", "target")),
    "ladder":      ("/api/ladder/refresh", ()),
}
# param keys whose string values must use forward slashes (Windows law)
PATH_LIKE_KEYS = ("staging_dir", "return_dir", "apk")


def now():
    return datetime.now().isoformat(timespec="seconds")


def p(*parts):
    """Join a path and return it with forward slashes (Windows law)."""
    return os.path.join(*parts).replace(os.sep, "/")


# ---------------------------------------------------------------------------
# narration - one line per action, to stdout, collected for the memo LOG
# ---------------------------------------------------------------------------

def make_narrator(cfg):
    def narrate(msg):
        line = "[%s] %s" % (now(), msg)
        print(line, flush=True)
        cfg["lines"].append(msg)
    return narrate


def notify(cfg, msg):
    """Telegram progress post. Silent-fail: never let it break the loop."""
    token, chat = cfg.get("tg_token"), cfg.get("tg_chat")
    if not token or not chat:
        return
    try:
        url = "https://api.telegram.org/bot%s/sendMessage" % token
        body = json.dumps({"chat_id": chat, "text": "ralph: %s" % msg}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass  # silent fail, by design


# ---------------------------------------------------------------------------
# THE MEMENTO - RALPH.md is the heart
# ---------------------------------------------------------------------------

SECTIONS = ("WHERE I AM", "IN FLIGHT", "BLOCKED", "NEXT", "LOG")


def memo_path(cfg):
    return p(cfg["home"], "RALPH.md")


def read_memo(cfg, narrate):
    """(a) every pass starts here: read the memo, recover state context.

    Returns a dict {pass_no, where, inflight_ids, blocked, next, log}.
    A missing/unreadable memo = first pass (zero memory is expected)."""
    memo = {"pass_no": 0, "where": [], "inflight_ids": [],
            "blocked": [], "next": [], "log": []}
    try:
        with open(memo_path(cfg), "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        narrate("memo: none found (first pass - zero memory, fresh start)")
        return memo
    section, found_any = None, False
    for line in raw.splitlines():
        if line.startswith("## "):
            header = line[3:].strip()
            # headers carry annotations ("IN FLIGHT (job ids)") - prefix match
            section = next((s for s in SECTIONS
                            if header == s or header.startswith(s + " ")), None)
            if section:
                found_any = True
            continue
        if not line.strip() or section is None:
            continue  # skip blank separators between sections
        if section == "WHERE I AM":
            memo["where"].append(line)
        elif section == "IN FLIGHT":
            memo["inflight_ids"] += [int(n) for n in re.findall(r"#(\d+)", line)]
        elif section == "BLOCKED":
            memo["blocked"].append(line)
        elif section == "NEXT":
            memo["next"].append(line)
        elif section == "LOG":
            memo["log"].append(line)
    m = re.search(r"pass #(\d+)", raw)
    if m:
        memo["pass_no"] = int(m.group(1))
    if found_any:
        first_where = next((l for l in memo["where"] if l.strip()), "")
        narrate("memo: pass #%d read - %s | in-flight=%s blocked=%d log=%d lines"
                % (memo["pass_no"], first_where.strip("- ").strip() or "?",
                   memo["inflight_ids"] or "none", len(memo["blocked"]),
                   len(memo["log"])))
    else:
        narrate("memo: present but no known sections (treating as fresh)")
    return memo


def write_memo(cfg, memo, tasks, action, inv):
    """(c) rewrite the memo so the next pass (zero memory) knows the state.
    LOG is append-only across passes, capped to the last 50 lines."""
    counts = {s: sum(1 for t in tasks if t.get("status") == s) for s in TASK_STATUSES}
    inflight = [t for t in tasks if t.get("status") in ("queued", "running")]
    blocked = [t for t in tasks if t.get("status") == "blocked"]
    todo = [t for t in tasks if t.get("status") == "todo"]

    new_log = list(memo.get("log", []))
    for line in cfg["lines"]:
        new_log.append("- %s %s" % (now(), line))
    new_log = new_log[-LOG_CAP:]

    out = []
    out.append("# RALPH - memento (rewritten every pass; a fresh pass reads this first)")
    out.append("")
    out.append("_pass #%d - generated %s - gateway %s - brain %s_"
               % (memo["pass_no"] + 1, now(), cfg["gateway_url"],
                  "on" if cfg.get("brain") else "off"))
    out.append("")
    out.append("## WHERE I AM")
    out.append("- board: %d tasks (todo %d, queued %d, running %d, done %d, blocked %d)"
               % (len(tasks), counts["todo"], counts["queued"], counts["running"],
                  counts["done"], counts["blocked"]))
    out.append("- gateway: %s" % (inv.get("summary") or "unreachable this pass"))
    out.append("- last action: %s" % action)
    out.append("")
    out.append("## IN FLIGHT (job ids)")
    if inflight:
        for t in inflight:
            out.append("- #%s - %s (%s)" % (t.get("job_id"), t.get("title"), t.get("status")))
    else:
        out.append("- (none)")
    out.append("")
    out.append("## BLOCKED (reasons)")
    if blocked:
        for t in blocked:
            out.append("- %s - %s" % (t.get("title"), (t.get("notes") or "?").splitlines()[0]))
    else:
        out.append("- (none)")
    out.append("")
    out.append("## NEXT")
    if todo:
        out.append("- next pick: %s (%s)" % (todo[0].get("title"), todo[0].get("kind")))
        if len(inflight) >= cfg["max_inflight"]:
            out.append("- in-flight full (%d/%d): poll jobs first, dispatch later"
                       % (len(inflight), cfg["max_inflight"]))
    elif inflight:
        out.append("- no todo tasks; poll in-flight job(s) %s"
                   % ", ".join("#%s" % t.get("job_id") for t in inflight))
    else:
        out.append("- board clear: keep idling (I'm learning!)")
    out.append("")
    out.append("## LOG")
    out.extend(new_log if new_log else ["- (empty)"])
    out.append("")

    tmp = memo_path(cfg) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out))
    os.replace(tmp, memo_path(cfg))


# ---------------------------------------------------------------------------
# the board - ralph_tasks.json (local state, gitignored-style)
# ---------------------------------------------------------------------------

def board_path(cfg):
    return cfg.get("board") or p(cfg["home"], "ralph_tasks.json")


def load_board(cfg, narrate):
    """Read the board; first run seeds it from ralph_tasks.seed.json."""
    bp = board_path(cfg)
    if not os.path.isfile(bp):
        for seed in (p(os.path.dirname(bp), "ralph_tasks.seed.json"),
                     p(SCRIPT_DIR, "ralph_tasks.seed.json")):
            if os.path.isfile(seed):
                narrate("board: %s missing - seeding from %s" % (bp, seed))
                with open(seed, "r", encoding="utf-8") as fh:
                    tasks = json.load(fh)
                save_board(cfg, tasks)
                return tasks
        narrate("board: %s missing and no seed found - starting empty" % bp)
        return []
    with open(bp, "r", encoding="utf-8") as fh:
        tasks = json.load(fh)
    return tasks if isinstance(tasks, list) else []


def save_board(cfg, tasks):
    for t in tasks:
        t["updated_at"] = now()
    tmp = board_path(cfg) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(tasks, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, board_path(cfg))


# ---------------------------------------------------------------------------
# gateway client - the ONLY door to the pipeline (never touch services)
# ---------------------------------------------------------------------------

def gw_call(cfg, method, path, body=None, timeout=20):
    """One HTTP round-trip. Returns (status_code, parsed_body).
    code 0 = unreachable (connection-level failure, not an HTTP answer)."""
    url = cfg["gateway_url"] + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Token", cfg["token"])
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"error": raw[:300]}
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": "gateway unreachable: %s" % exc}


def norm_paths(params):
    """Forward-slash law: normalize path-like param values before sending."""
    out = {}
    for k, v in params.items():
        if isinstance(v, str) and (k in PATH_LIKE_KEYS or k.endswith(("_dir", "_path"))):
            v = v.replace("\\", "/")
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# BRAIN (optional, off by default) - one function, deterministic without it
# ---------------------------------------------------------------------------

def ask_brain(question, context):
    """Consult the LLM ladder for an ambiguous pick. Returns text or None.

    Boss model comes from LADDER_PATH ladder.json (roles -> big NVIDIA),
    called via NVIDIA integrate API with NVAPI_KEY; falls back to
    openrouter/chat-completions with OPENROUTER_API_KEY. No keys -> None
    (Ralph stays fully deterministic)."""
    def ladder_boss():
        path = os.environ.get("LADDER_PATH", DEFAULT_LADDER)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                ladder = json.load(fh)
        except (OSError, ValueError):
            return None
        roles = ladder.get("roles") or {}
        for name, spec in roles.items():
            if any(k in str(name).lower() for k in ("boss", "brain", "planner")):
                return spec.get("model") or None
        for spec in roles.values():
            if isinstance(spec, dict) and spec.get("model"):
                return spec.get("model")
        return None

    def chat(url, key, model):
        body = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": context},
                         {"role": "user", "content": question}],
            "max_tokens": 200, "temperature": 0,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", "Bearer %s" % key)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            obj = json.loads(resp.read().decode("utf-8") or "{}")
        return (obj.get("choices") or [{}])[0].get("message", {}).get("content")

    model = ladder_boss()
    nv, orr = os.environ.get("NVAPI_KEY"), os.environ.get("OPENROUTER_API_KEY")
    try:
        if nv:
            answer = chat("https://integrate.api.nvidia.com/v1/chat/completions",
                          nv, model or "openai/gpt-oss-120b")
            if answer:
                return answer.strip()
        if orr:
            return (chat("https://openrouter.ai/api/v1/chat/completions",
                         orr, model or "openrouter/auto") or "").strip() or None
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# loop steps
# ---------------------------------------------------------------------------

def sync_inventory(cfg, narrate, tasks):
    """(a) INVENTORY: GET /api/status + GET /api/jobs?limit=50, then refresh
    every task that carries a job_id from the job's real status."""
    inv = {"summary": None, "ok": False}
    code, status = gw_call(cfg, "GET", "/api/status")
    if code == 0:
        narrate("inventory: gateway UNREACHABLE at %s - skipping this pass" % cfg["gateway_url"])
        return inv
    if code != 200:
        narrate("inventory: /api/status -> %s (%s)" % (code, status.get("error", "?")))
        return inv
    counts = (status.get("queue") or {}).get("counts") or {}
    inv["ok"] = True
    inv["summary"] = ("queue %s | %d games | ladder %s"
                      % (json.dumps(counts, sort_keys=True),
                         len(status.get("games") or []),
                         "present" if status.get("ladder") else "none"))
    narrate("inventory: %s" % inv["summary"])

    code, obj = gw_call(cfg, "GET", "/api/jobs?limit=50")
    jobs = {}
    if code == 200:
        raw = obj.get("jobs") if isinstance(obj, dict) else obj
        for job in raw or []:
            if isinstance(job, dict) and job.get("id") is not None:
                jobs[job["id"]] = job
        narrate("inventory: %d recent jobs fetched (limit 50)" % len(jobs))
    else:
        narrate("inventory: /api/jobs -> %s (%s)" % (code, obj.get("error", "?")))

    for task in tasks:
        job_id = task.get("job_id")
        if not job_id or task.get("status") in ("done", "blocked"):
            continue
        job = jobs.get(job_id)
        if job is None:
            code, job = gw_call(cfg, "GET", "/api/jobs/%s" % job_id)
            if code != 200:
                old = task.get("status")
                task["status"] = "blocked"
                task["notes"] = ("job #%s not found in queue (stale job_id, "
                                 "gateway said %s)" % (job_id, code or "unreachable"))
                narrate("sync: '%s' job #%s gone from queue (%s) - %s -> blocked: %s"
                        % (task.get("title"), job_id, code or "unreachable", old,
                           task["notes"]))
                notify(cfg, "'%s' blocked: job #%s vanished from the queue"
                       % (task.get("title"), job_id))
                continue
        new_status = JOB_TO_TASK.get(job.get("status"))
        if new_status is None:
            continue
        if new_status != task.get("status"):
            old = task.get("status")
            task["status"] = new_status
            if new_status == "blocked":
                task["notes"] = ("job #%s failed (attempts %s): %s"
                                 % (job_id, job.get("attempts"),
                                    (job.get("error") or "unknown error")[:300]))
                narrate("sync: '%s' job #%s %s -> failed -> task blocked: %s"
                        % (task.get("title"), job_id, old, task["notes"]))
                notify(cfg, "'%s' FAILED (job #%s): %s"
                       % (task.get("title"), job_id, job.get("error")))
            else:
                narrate("sync: '%s' job #%s %s -> %s"
                        % (task.get("title"), job_id, old, new_status))
                if new_status == "done":
                    notify(cfg, "'%s' done (job #%s)" % (task.get("title"), job_id))
    return inv


def dispatch(cfg, narrate, task):
    """(c) DISPATCH one gateway task: map params.op -> endpoint per CONNECT.md.
    Returns True if the pick was consumed (dispatched or blocked)."""
    params = norm_paths(task.get("params") or {})
    op = params.get("op")
    if op not in OPS:
        resolved = None
        if cfg.get("brain"):
            answer = ask_brain(
                "Task '%s'. Which ONE pipeline operation fits it? Answer with "
                "exactly one token from: %s, or NONE." % (task.get("title"),
                                                          ", ".join(sorted(OPS))),
                "You are Ralph, a persistence daemon for a game-making pipeline. "
                "Operations: new_game (build a new game), tunable (tune a game "
                "constant), art (generate art), critique (PlayerOne judges), "
                "playtest (emulator plays), gpu (colab offload), device_test "
                "(firebase test lab), deploy (ship to retromonkey|itch), ladder "
                "(refresh llm ladder).")
            if answer:
                for token in re.findall(r"[a-z_]+", answer.lower()):
                    if token in OPS:
                        resolved = token
                        break
            narrate("brain: ambiguous op for '%s' -> %s"
                    % (task.get("title"), resolved or "NONE"))
        if resolved:
            op = resolved
        else:
            task["status"] = "blocked"
            task["notes"] = ("no dispatch mapping (params.op=%r is not one of %s)"
                             % (op, ", ".join(sorted(OPS))))
            narrate("blocked: '%s' - %s" % (task.get("title"), task["notes"]))
            return True

    path, allowed = OPS[op]
    payload = {k: v for k, v in params.items() if k in allowed}
    code, body = gw_call(cfg, "POST", path, body=payload)
    if code == 0:
        narrate("dispatch: '%s' -> POST %s FAILED (gateway unreachable, "
                "transient - will retry next pass)" % (task.get("title"), path))
        return True
    job_id = body.get("job_id") or body.get("id") if isinstance(body, dict) else None
    if code == 200 and job_id is not None:
        task["status"] = "queued"
        task["job_id"] = job_id
        task["notes"] = ""
        task["fails"] = 0
        narrate("dispatch: '%s' -> POST %s -> job #%s queued (payload %s)"
                % (task.get("title"), path, job_id,
                   json.dumps(payload, ensure_ascii=False)))
        notify(cfg, "dispatched '%s' -> job #%s (%s)" % (task.get("title"), job_id, op))
        return True
    # 4xx / 5xx: retry law of our own - twice then blocked
    task["fails"] = int(task.get("fails") or 0) + 1
    reason = "%s -> HTTP %s: %s" % (path, code, str(body.get("error", body))[:200])
    if task["fails"] >= 2:
        task["status"] = "blocked"
        task["notes"] = "dispatch failed twice. %s" % reason
        narrate("blocked: '%s' after %d failed dispatch attempts - %s"
                % (task.get("title"), task["fails"], reason))
        notify(cfg, "'%s' blocked: dispatch failed twice (%s)" % (task.get("title"), reason))
    else:
        narrate("dispatch: '%s' attempt %d failed - %s (retry next pass)"
                % (task.get("title"), task["fails"], reason))
    return True


def file_manual(cfg, narrate, task):
    """(d) MANUAL kind: never dispatched - filed as blocked with the exact
    human step it needs."""
    task["status"] = "blocked"
    if not task.get("notes"):
        task["notes"] = "manual task: no reason given - needs a human decision"
    narrate("manual: '%s' filed as blocked - needs human step: %s"
            % (task.get("title"), task["notes"]))
    notify(cfg, "'%s' filed as blocked (manual): %s" % (task.get("title"), task["notes"]))
    return True


def run_pass(cfg):
    """One full memento pass: (a) read memo -> (b) one increment -> (c) rewrite."""
    cfg["lines"] = []
    narrate = make_narrator(cfg)

    memo = read_memo(cfg, narrate)                     # (a) the memo first
    tasks = load_board(cfg, narrate)

    inv = sync_inventory(cfg, narrate, tasks)

    if not inv["ok"]:
        action = "gateway unreachable - no work attempted this pass"
        narrate(action)
        save_board(cfg, tasks)
        write_memo(cfg, memo, tasks, action, inv)      # (c) rewrite memo
        return "down"

    # memo drift check: job ids the memo remembers but the board does not know
    board_job_ids = {t.get("job_id") for t in tasks if t.get("job_id")}
    for lost_id in memo.get("inflight_ids", []):
        if lost_id not in board_job_ids:
            narrate("drift: memo remembers job #%s but no board task references "
                    "it (board edited between passes?)" % lost_id)

    inflight = [t for t in tasks if t.get("status") in ("queued", "running")]
    todo = [t for t in tasks if t.get("status") == "todo"]

    if not todo:
        action = "I'm learning! (no todo tasks - idling)"
        narrate("I'm learning! (no todo tasks - %d in flight, %d blocked - idling)"
                % (len(inflight), sum(1 for t in tasks if t.get("status") == "blocked")))
    elif len(inflight) >= cfg["max_inflight"]:
        action = ("in-flight guard: %d/%d jobs running - waiting before picking '%s'"
                  % (len(inflight), cfg["max_inflight"], todo[0].get("title")))
        narrate(action)
    else:
        task = todo[0]                                  # (b) one increment
        narrate("pick: '%s' (kind=%s, op=%s)"
                % (task.get("title"), task.get("kind"),
                   (task.get("params") or {}).get("op")))
        if task.get("kind") == "manual":
            file_manual(cfg, narrate, task)
            action = "filed manual task '%s' as blocked" % task.get("title")
        else:
            dispatch(cfg, narrate, task)
            action = "worked on '%s' -> job #%s" % (task.get("title"),
                                                    task.get("job_id") or "n/a")

    save_board(cfg, tasks)
    write_memo(cfg, memo, tasks, action, inv)           # (c) rewrite memo
    return "acted"


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Ralph Wiggum - pipeline persistence daemon")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--idle-seconds", type=int, default=120)
    ap.add_argument("--max-inflight", type=int, default=3)
    ap.add_argument("--brain", action="store_true", help="consult LLM ladder on ambiguous picks")
    ap.add_argument("--home", default=os.environ.get("RALPH_HOME", SCRIPT_DIR))
    ap.add_argument("--board", default=None, help="board json path (default HOME/ralph_tasks.json)")
    args = ap.parse_args()

    token = os.environ.get("GATEWAY_TOKEN", "").strip()
    if not token:
        print("[ralph] GATEWAY_TOKEN is not set - refusing to start (fail closed)")
        sys.exit(1)

    cfg = {
        "home": args.home.replace("\\", "/"),
        "board": args.board.replace("\\", "/") if args.board else None,
        "gateway_url": os.environ.get("GATEWAY_URL", "http://127.0.0.1:8902").rstrip("/"),
        "token": token,
        "tg_token": os.environ.get("TG_TOKEN", "").strip(),
        "tg_chat": os.environ.get("TG_CHAT_ID", "").strip(),
        "brain": args.brain,
        "max_inflight": args.max_inflight,
        "lines": [],
    }
    print("[ralph] home=%s board=%s memo=%s gateway=%s inflight<=%d idle=%ss brain=%s"
          % (cfg["home"], board_path(cfg), memo_path(cfg), cfg["gateway_url"],
             cfg["max_inflight"], args.idle_seconds, "on" if cfg["brain"] else "off"))

    if args.once:
        run_pass(cfg)
        return
    try:
        while True:
            run_pass(cfg)
            time.sleep(args.idle_seconds)
    except KeyboardInterrupt:
        print("[ralph] shutdown (KeyboardInterrupt) - memo is up to date")


if __name__ == "__main__":
    main()
