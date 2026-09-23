#!/usr/bin/env python3
"""Stub GMP job queue for gateway tests — canned answers, zero state.

Mimics cloud/queue/queue_api.py's contract just enough for the gateway to
front it locally:
  POST /jobs   -> {"id": N}   (counts up from 99)
  GET  /jobs   -> {"jobs": [...canned...], "count": 2}
  GET  /health -> {"ok": true, "backend": "stub", "counts": {...}}
No auth, no storage, runs anywhere. Start it on 8901, point the gateway at it,
and the whole gateway surface is testable without Postgres or the box.
"""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = 8901

JOBS = [
    {"id": 97, "type": "emulator",
     "payload": {"game": "sonar", "seconds": 60},
     "status": "queued", "priority": 1},
    {"id": 96, "type": "deploy",
     "payload": {"game": "slime-line", "target": "itch"},
     "status": "queued", "priority": 5},
]
COUNTS = {"queued": 2, "running": 1, "done": 7, "failed": 1}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def say(msg):
    print(f"[stub_queue] {msg}", flush=True)


class StubHandler(BaseHTTPRequestHandler):
    next_id = 98  # first POST /jobs answers {"id": 99}

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            say("GET /health -> canned counts")
            self._json(200, {"ok": True, "backend": "stub", "counts": COUNTS})
            return
        if path == "/jobs":
            say("GET /jobs -> canned list")
            self._json(200, {"jobs": JOBS, "count": len(JOBS)})
            return
        say(f"GET {path} -> 404")
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/jobs":
            body = self._body()
            StubHandler.next_id += 1
            say(f"POST /jobs type={body.get('type')!r} -> id={StubHandler.next_id}")
            self._json(200, {"id": StubHandler.next_id})
            return
        say(f"POST {path} -> 404")
        self._json(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description="stub GMP job queue (gateway tests)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--host", default=HOST)
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), StubHandler)
    say(f"listening on http://{args.host}:{args.port} (no auth, canned answers)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("shutdown (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
