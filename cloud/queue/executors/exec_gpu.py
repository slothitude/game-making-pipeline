#!/usr/bin/env python3
"""exec_gpu — the Colab T4 offload lane: hand a gpu job to the deployed
colab worker (provision -> run -> retrieve -> teardown).

  run(job)   job payload: {kind: train|mesh|render, staging_dir, return_dir, ...}

sys.path-inserts /home/ubuntu/pipeline/colab, imports colab_worker.run_job,
and hands it {id, type, payload} with `kind` guaranteed present. The worker is
Linux-only and lives on the queue host — anything else raises the clear
"colab lane not deployed" error (which the router treats as retryable, so a
job filed before the lane is deployed just waits for it).

The gpu.train / gpu.mesh / gpu.render entries in executors.REGISTRY all land
here with `kind` injected (see __init__.py).
"""

from __future__ import annotations

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

COLAB_DIR = "/home/ubuntu/pipeline/colab"
NOT_DEPLOYED = (
    f"colab lane not deployed: expected {COLAB_DIR}/colab_worker.py on the "
    "queue host (Linux only) — deploy cloud/colab/colab_worker.py there first")


def say(msg):
    print(f"[exec_gpu] {msg}", flush=True)


def run(job: dict) -> dict:
    payload = dict(job.get("payload") or {})
    kind = payload.get("kind")
    if not kind:
        jtype = str(job.get("type") or "")
        kind = jtype.split(".", 1)[1] if "." in jtype else None
    if kind not in ("train", "mesh", "render"):
        raise ValueError(f"gpu job needs kind train|mesh|render, got {kind!r}")
    if not payload.get("staging_dir") or not payload.get("return_dir"):
        raise ValueError("gpu job payload needs 'staging_dir' and 'return_dir'")

    sys.path.insert(0, COLAB_DIR)
    try:
        from colab_worker import run_job
    except ImportError as exc:
        raise RuntimeError(f"{NOT_DEPLOYED} ({exc})") from exc

    worker_job = {"id": job.get("id"), "type": job.get("type") or f"gpu.{kind}",
                  "payload": payload}
    say(f"id={job.get('id')} gpu kind={kind} staging={payload['staging_dir']} "
        f"return={payload['return_dir']} -> colab_worker.run_job")
    result = run_job(worker_job)
    say(f"colab_worker returned: {str(result)[:200]}")
    return result


if __name__ == "__main__":
    print("usage: import me — REGISTRY['gpu.*'] are this module's run() with "
          "kind injected")
    sys.exit(2)
