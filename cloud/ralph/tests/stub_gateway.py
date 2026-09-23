#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stub gateway - canned /api/status + /api/jobs + enqueue echo on :8902.

Test double for cloud/api/gateway.py so ralph.py can be verified WITHOUT
the real gateway, Postgres, or the retromonkey box. Same X-Token law,
same response shapes (mirrors gateway.py + queue_api.py):

  GET  /api/health        {"ok": true, "queue": true, ...}
  GET  /api/status        {"ok":true,"version":"stub-1.0","queue":{"reachable":true,
                            "backend":"stub","counts":{...}},"games":[...],"ladder":null}
  GET  /api/jobs          {"jobs":[...], "count": N}
  GET  /api/jobs/<id>     the one job, or 404 (this is how stale job_ids die)
  POST /api/games, /api/orders/tunable, /api/orders/art, /api/critique,
       /api/playtest, /api/gpu, /api/device_test, /api/deploy,
       /api/ladder/refresh
                          -> {"ok": true, "job_id": <next id>, "type": ...,
                              "priority": 5}   (enqueue echo)

Enqueued job ids start at 61; canned history holds #51 (done) and #52
(running) so the listing has content. Job #9999 deliberately does NOT
exist - the mini board references it to prove stale job_id detection.

Usage:
  python tests/stub_gateway.py [--port 8902] [--token stubtoken]

Clients must send X-Token: <token> (default "stubtoken"). Wrong token -> 403.
"""

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

OP_TYPES = {
    "/api/games": "new_game",
    "/api/orders/tunable": "tune_tunable",
    "/api/orders/art": "generate_art",
    "/api/critique": "critique",
    "/api/playtest": "emulator",
    "/api/gpu": "gpu.train",
    "/api/device_test": "device_test",
    "/api/deploy": "deploy",
    "/api/ladder/refresh": "ladder_refresh",
}

STATE = {
    "token": "stubtoken",
    "reject": False,  # when True, POST endpoints answer 400 (tests ralph's fail-twice law)
    "next_id": 61,
    "jobs": [
        {"id": 51, "type": "new_game", "payload": {"name": "history done"},
         "status": "done", "priority": 5, "result": {"ok": True}, "error": None,
         "attempts": 1, "created_at": "2026-09-23T08:00:00",
         "started_at": "2026-09-23T08:00:01", "finished_at": "2026-09-23T08:04:00"},
        {"id": 52, "type": "critique", "payload": {"game": "sonar", "mode": "full"},
         "status": "running", "priority": 5, "result": None, "error": None,
         "attempts": 0, "created_at": "2026-09-23T08:10:00",
         "started_at": "2026-09-23T08:10:01", "finished_at": None},
    ],
}


def say(msg):
    print("[stub-gateway] %s" % msg, flush=True)


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return self.headers.get("X-Token", "") == STATE["token"]

    def _body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)

        if parsed.path == "/api/health":
            say("GET /api/health -> ok (stub)")
            self._json(200, {"ok": True, "queue": True, "backend": "stub",
                             "counts": {"queued": 1, "running": 1, "done": 14,
                                        "failed": 1}})
            return

        if not self._authed():
            say("GET %s -> 403 (bad X-Token)" % parsed.path)
            self._json(403, {"error": "bad X-Token"})
            return

        if parsed.path == "/api/status":
            say("GET /api/status -> canned roster/queue/ladder")
            self._json(200, {
                "ok": True, "version": "stub-1.0",
                "queue": {"reachable": True, "backend": "stub",
                          "counts": {"queued": 1, "running": 1, "done": 14,
                                     "failed": 1}},
                "games": [{"slug": "sonar", "name": "Sonar", "live": {}},
                          {"slug": "octogram-arcade", "name": "Octogram Arcade",
                           "live": {}}],
                "ladder": {"updated_at": "2026-09-23T00:00:00",
                           "updated_by": "stub", "roles": {}}})
            return

        if parsed.path == "/api/jobs":
            qs = parse_qs(parsed.query)
            try:
                limit = min(max(int((qs.get("limit") or ["50"])[0]), 1), 500)
            except ValueError:
                limit = 50
            jobs = STATE["jobs"][-limit:]
            say("GET /api/jobs limit=%d -> %d jobs" % (limit, len(jobs)))
            self._json(200, {"jobs": jobs, "count": len(jobs)})
            return

        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "jobs":
            try:
                job_id = int(parts[2])
            except ValueError:
                self._json(400, {"error": "bad job id"})
                return
            job = next((j for j in STATE["jobs"] if j["id"] == job_id), None)
            if job is None:
                say("GET /api/jobs/%d -> 404 (not found - stale job_id)"
                    % job_id)
                self._json(404, {"error": "job %d not found in the stub queue"
                                          % job_id})
                return
            say("GET /api/jobs/%d -> %s" % (job_id, job["status"]))
            self._json(200, job)
            return

        say("GET %s -> 404" % parsed.path)
        self._json(404, {"error": "not found"})

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path

        if not self._authed():
            say("POST %s -> 403 (bad X-Token)" % path)
            self._json(403, {"error": "bad X-Token"})
            return

        jtype = OP_TYPES.get(path)
        if jtype is None:
            say("POST %s -> 404 (unknown op path)" % path)
            self._json(404, {"error": "not found"})
            return

        body = self._body()
        if body is None:
            say("POST %s -> 400 (body is not JSON)" % path)
            self._json(400, {"error": "body must be a JSON object"})
            return

        if STATE["reject"]:
            say("POST %s -> 400 (--reject mode, simulating gateway/queue 4xx)" % path)
            self._json(400, {"error": "rejected by stub (--reject)"})
            return

        job_id = STATE["next_id"]
        STATE["next_id"] += 1
        job = {"id": job_id, "type": jtype, "payload": body, "status": "queued",
               "priority": 5, "result": None, "error": None, "attempts": 0,
               "created_at": "2026-09-23T12:00:00", "started_at": None,
               "finished_at": None}
        STATE["jobs"].append(job)
        say("POST %s type=%s -> enqueue echo id=%d payload=%s"
            % (path, jtype, job_id, json.dumps(body, ensure_ascii=False)))
        self._json(200, {"ok": True, "job_id": job_id, "type": jtype,
                         "priority": 5})


def main():
    ap = argparse.ArgumentParser(description="stub GMP gateway for ralph tests")
    ap.add_argument("--port", type=int, default=8902)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--token", default=os.environ.get("STUB_TOKEN", "stubtoken"))
    ap.add_argument("--reject", action="store_true",
                    help="POST endpoints answer 400 (test ralph's fail-twice-then-block law)")
    args = ap.parse_args()
    STATE["token"] = args.token
    STATE["reject"] = args.reject
    server = ThreadingHTTPServer((args.host, args.port), StubHandler)
    say("listening on http://%s:%d (X-Token: %s, canned jobs #51 #52, "
        "next enqueue id #61)" % (args.host, args.port, args.token))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("shutdown")


if __name__ == "__main__":
    main()
