#!/usr/bin/env python3
"""Startup sweep: settle 'running' jobs whose worker is gone.

Router restarts orphan jobs in running state forever (hit 3x on 2026-09-23).
This runs at router boot: any running job older than ORPHAN_GRACE (the
process that claimed it can't survive a restart) is requeued once; if it's
already been requeued beyond attempts, it settles failed with the honest
reason. Call from router.py's main() before the loop starts.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ORPHAN_GRACE = 120  # seconds: a running job younger than this is left alone
                   # (the claiming process may still be shutting down)


def sweep(base_url: str, token: str) -> int:
    """Settle orphaned running jobs. Returns the count swept."""
    req = urllib.request.Request(
        f"{base_url}/jobs?status=running&limit=50",
        headers={"X-Token": token})
    with urllib.request.urlopen(req, timeout=15) as resp:
        jobs = json.load(resp).get("jobs") or []
    swept = 0
    now = time.time()
    for job in jobs:
        started = job.get("started_at")
        if not started:
            continue
        try:  # started_at format: "2026-09-23T21:58:09+00:00"
            from datetime import datetime, timezone
            st = datetime.fromisoformat(started)
            age = now - st.timestamp()
        except (ValueError, TypeError):
            age = ORPHAN_GRACE + 1  # unparseable -> treat as old
        if age < ORPHAN_GRACE:
            continue
        jid = job["id"]
        attempts = job.get("attempts") or 0
        if attempts < 2:
            body = json.dumps({"error": "orphaned by restart - requeued by "
                              f"sweep (age {int(age)}s)"}).encode()
            req = urllib.request.Request(
                f"{base_url}/jobs/{jid}/result", data=body,
                headers={"X-Token": token,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                out = json.load(resp)
            print(f"[sweep] job {jid} (age {int(age)}s) -> "
                  f"{out.get('status', '?')} (attempt {attempts + 1})",
                  flush=True)
        else:
            body = json.dumps({"error": "orphaned by restart - attempts "
                              "exhausted", "permanent": True}).encode()
            req = urllib.request.Request(
                f"{base_url}/jobs/{jid}/result", data=body,
                headers={"X-Token": token,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                out = json.load(resp)
            print(f"[sweep] job {jid} -> failed permanently "
                  f"(attempts exhausted)", flush=True)
        swept += 1
    if swept:
        print(f"[sweep] {swept} orphaned job(s) settled", flush=True)
    return swept


if __name__ == "__main__":
    base = os.environ.get("QUEUE_URL", "http://127.0.0.1:8901")
    tok = os.environ.get("QUEUE_TOKEN", "")
    if not tok:
        raise SystemExit("QUEUE_TOKEN env required")
    sweep(base, tok)
