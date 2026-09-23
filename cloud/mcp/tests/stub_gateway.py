#!/usr/bin/env python
"""Stub GMP gateway for MCP server tests — stdlib only, zero deps.

Listens on STUB_PORT (default 8902, the gateway's real port) and returns
canned JSON for every endpoint the MCP server fronts. Every response echoes
the X-Token header it received so tests can verify the MCP server is
propagating GATEWAY_TOKEN correctly.

Usage:  python tests/stub_gateway.py
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

PORT = int(os.environ.get("STUB_PORT", "8902"))

# (method, path) -> canned JSON body (as python object)
ROUTES = {
    ("GET", "/api/status"): {
        "ok": True,
        "pipeline": "gmp",
        "games": ["octogram-arcade", "sonar", "slime-line", "star-visitor",
                  "gyro-squadron-45", "cubefall"],
        "queue": {"queued": 2, "running": 1, "done": 47, "failed": 1},
        "ladder": {"brain": "glm-5.3", "worker": "openrouter/free",
                   "refreshed_days_ago": 3},
    },
    ("GET", "/api/health"): {"ok": True, "version": "stub-1.0.0"},
    ("GET", "/api/games/word-poker/diary"): {
        "ok": True,
        "slug": "word-poker",
        "entries": [
            {"ts": "2026-09-22T10:14:00Z", "kind": "build",
             "text": "M4 green twice: hint debounce + keyboard lane."},
            {"ts": "2026-09-22T18:41:00Z", "kind": "critique",
             "text": "PlayerOne: fun 0.72, polish 0.55 — tile contrast issue "
                     "filed as T2 art order."},
        ],
    },
    ("GET", "/api/jobs"): {
        "ok": True,
        "jobs": [
            {"id": "j-1007", "kind": "deploy", "state": "running",
             "game": "word-poker"},
            {"id": "j-1006", "kind": "critique", "state": "queued",
             "game": "star-visitor"},
        ],
    },
    ("GET", "/api/jobs/j-1007"): {
        "ok": True,
        "id": "j-1007",
        "kind": "deploy",
        "state": "running",
        "game": "word-poker",
        "target": "retromonkey",
        "started": "2026-09-23T09:12:00Z",
    },
    ("POST", "/api/games"): {
        "ok": True, "slug": "cube-fall", "job_id": "j-1008",
        "state": "queued", "template": "3d",
    },
    ("POST", "/api/orders/tunable"): {
        "ok": True, "job_id": "j-1009", "kind": "tunable",
        "game": "word-poker", "tunable": "round_seconds",
        "clamped": True, "value": 45.0, "state": "queued",
    },
    ("POST", "/api/orders/art"): {
        "ok": True, "job_id": "j-1010", "kind": "art",
        "game": "word-poker", "asset_id": "tileset_overworld",
        "state": "queued",
    },
    ("POST", "/api/critique"): {
        "ok": True, "job_id": "j-1011", "kind": "critique",
        "game": "star-visitor", "mode": "full", "state": "queued",
    },
    ("POST", "/api/playtest"): {
        "ok": True, "job_id": "j-1012", "kind": "playtest",
        "game": "sonar", "seconds": 90, "state": "queued",
    },
    ("POST", "/api/gpu"): {
        "ok": True, "job_id": "j-1013", "kind": "gpu",
        "gpu_kind": "trellis", "staging_dir": "C:/staging/chair",
        "return_dir": "C:/games/cubefall/assets", "state": "queued",
    },
    ("POST", "/api/device_test"): {
        "ok": True, "job_id": "j-1014", "kind": "device_test",
        "game": "word-poker", "lane": "firebase_test_lab", "state": "queued",
    },
    ("POST", "/api/deploy"): {
        "ok": True, "job_id": "j-1007", "kind": "deploy",
        "game": "word-poker", "target": "retromonkey", "state": "running",
        "gates": "2x clear",
    },
    ("POST", "/api/ladder/refresh"): {
        "ok": True, "job_id": "j-1015", "kind": "ladder_refresh",
        "state": "queued", "note": "refreshes daily/ladder.json",
    },
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str) -> None:
        token = self.headers.get("X-Token", "")
        path = self.path.split("?")[0]
        route = ROUTES.get((method, path))
        if route is None:
            self._send(404, {"ok": False,
                             "error": f"stub has no route {method} {path}"})
            return
        if method == "POST":
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                received = json.loads(raw or b"{}")
            except ValueError:
                received = {"unparseable": raw[:200].decode("utf-8", "replace")}
        else:
            received = None
        # Echo auth + what the client sent so tests can assert on both.
        response = dict(route)
        response["_stub"] = {"x_token_received": token,
                             "body_received": received}
        self._send(200, response)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[stub-gateway] {self.address_string()} {fmt % args}",
              file=sys.stderr)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[stub-gateway] canned GMP gateway on http://127.0.0.1:{PORT} "
          f"({len(ROUTES)} routes)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
