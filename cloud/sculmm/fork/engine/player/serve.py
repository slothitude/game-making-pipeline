#!/usr/bin/env python3
"""serve.py — one-command SCULMM player server.

Serves the cue_dev root so the player, the pilot scene, and book assets
(1984/...) are all fetchable. book-location asset paths in the cuec are
relative to this root.

Run:  python player/serve.py [port]      (default 8901)
Open: http://localhost:8901/player/sculmm_player.html
"""
import http.server
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8901


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


if __name__ == "__main__":
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"[sculmm-player] http://localhost:{PORT}"
              f"/player/sculmm_player.html  (root: {ROOT})")
        httpd.serve_forever()
