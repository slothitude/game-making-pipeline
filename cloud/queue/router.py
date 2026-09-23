#!/usr/bin/env python3
"""GMP queue router — the dispatcher loop. One job at a time (the serialize law).

Claims a single job from the queue API, routes it by type through the executor
registry, reports the result back. Retry accounting lives server-side: the API
requeues a job on its first error and fails it permanently on the second.

  --once   drain exactly ONE job, then exit (systemd timer mode)
  --loop   poll forever, 5s between polls
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ALL_TYPES = ["llm", "device_test", "gpu", "gate", "deploy", "emulator", "critique"]
DEFAULT_API = "http://127.0.0.1:8901"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def say(msg):
    print(f"[router] {msg}", flush=True)


class NotWired(Exception):
    """Executor stub for a type this build doesn't implement yet."""


# --------------------------------------------------------------------------
# Executors — registry is the whole routing table
# --------------------------------------------------------------------------

def exec_llm(job):
    """Real stub: logs the payload shape, reports done. The brain plugs in here."""
    payload = job.get("payload") or {}
    shape = {k: type(v).__name__ for k, v in sorted(payload.items())}
    say(f"exec llm id={job['id']}: payload keys/shapes: "
        f"{json.dumps(shape) if shape else '{}'}")
    return {"ok": True, "note": "llm stub — payload shape logged", "payload_shape": shape}


def exec_not_wired(job):
    raise NotWired(f"executor {job['type']!r} not wired yet")


EXECUTORS = {
    "llm": exec_llm,
    "device_test": exec_not_wired,
    "gpu": exec_not_wired,
    "gate": exec_not_wired,
    "deploy": exec_not_wired,
    "emulator": exec_not_wired,
    "critique": exec_not_wired,
}


# --------------------------------------------------------------------------
# Queue API client (stdlib urllib)
# --------------------------------------------------------------------------

class QueueClient:
    def __init__(self, base_url, token):
        self.base = base_url.rstrip("/")
        self.token = token

    def _call(self, method, path, body=None):
        req = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=method,
            headers={"X-Token": self.token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def health(self):
        return self._call("GET", "/health")

    def claim(self, types):
        return self._call("POST", "/jobs/claim", {"types": types}).get("job")

    def complete(self, job_id, result):
        return self._call("POST", f"/jobs/{job_id}/result", {"result": result})

    def fail(self, job_id, error, permanent=False):
        body = {"error": error}
        if permanent:
            body["permanent"] = True  # skip the server-side retry law
        return self._call("POST", f"/jobs/{job_id}/result", body)


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def drain_one(client, types):
    """Claim ONE job, run it, report. Returns 'done' | 'empty' | 'unreachable'."""
    try:
        job = client.claim(types)
    except urllib.error.HTTPError as exc:
        say(f"queue rejected claim ({exc.code} {exc.reason})")
        return "unreachable"
    except (urllib.error.URLError, OSError) as exc:
        say(f"queue unreachable at {client.base} ({exc})")
        return "unreachable"
    if not job:
        say("nothing queued — queue drained")
        return "empty"

    jid, jtype = job["id"], job["type"]
    executor = EXECUTORS.get(jtype)
    if executor is None:
        say(f"id={jid} type={jtype!r} has no executor — failing permanently")
        client.fail(jid, f"executor {jtype!r} not wired yet", permanent=True)
        return "done"

    say(f"id={jid} type={jtype} priority={job.get('priority')} -> executing")
    try:
        result = executor(job)
    except NotWired as exc:
        say(f"id={jid} type={jtype} -> FAILED permanently ({exc})")
        client.fail(jid, str(exc), permanent=True)
        return "done"
    except Exception as exc:  # noqa: BLE001 — the retry law lives server-side
        say(f"id={jid} type={jtype} -> error: {exc} (server decides retry/fail)")
        try:
            outcome = client.fail(jid, f"{type(exc).__name__}: {exc}")
            if outcome and outcome.get("status") == "queued":
                say(f"id={jid} requeued for retry "
                    f"(attempt {outcome.get('attempts')})")
        except (urllib.error.URLError, OSError) as report_exc:
            say(f"id={jid} COULD NOT report failure ({report_exc}) — "
                f"job left running, will need manual /result")
        return "done"

    try:
        client.complete(jid, result)
        say(f"id={jid} type={jtype} -> done")
    except (urllib.error.URLError, OSError) as exc:
        say(f"id={jid} COULD NOT report result ({exc}) — job left running")
    return "done"


def main():
    ap = argparse.ArgumentParser(description="GMP queue router")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true",
                      help="drain one job and exit (systemd timer mode)")
    mode.add_argument("--loop", action="store_true",
                      help="poll forever")
    ap.add_argument("--api", default=DEFAULT_API, help=f"queue API (default {DEFAULT_API})")
    ap.add_argument("--types", default=",".join(ALL_TYPES),
                    help="comma-separated job types this router serves")
    ap.add_argument("--poll", type=float, default=5.0,
                    help="seconds between polls in --loop (default 5)")
    args = ap.parse_args()

    token = os.environ.get("QUEUE_TOKEN", "").strip()
    if not token:
        say("QUEUE_TOKEN is not set — refusing to start (fail closed)")
        sys.exit(1)

    types = [t.strip() for t in args.types.split(",") if t.strip()]
    client = QueueClient(args.api, token)

    try:
        health = client.health()
        say(f"api={args.api} backend={health.get('backend')} "
            f"counts={health.get('counts')} types={types}")
    except (urllib.error.URLError, OSError) as exc:
        say(f"queue unreachable at {args.api} ({exc})")
        sys.exit(1)

    if args.once:
        outcome = drain_one(client, types)  # empty queue is fine for a timer;
        sys.exit(0 if outcome != "unreachable" else 1)  # a dead API is not

    say(f"loop mode: polling every {args.poll}s — Ctrl-C to stop")
    while True:
        try:
            drain_one(client, types)
        except KeyboardInterrupt:
            say("shutdown (KeyboardInterrupt)")
            break
        except Exception as exc:  # noqa: BLE001 — the loop must survive anything
            say(f"loop iteration failed ({exc}) — continuing")
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
