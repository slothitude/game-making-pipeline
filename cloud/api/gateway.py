#!/usr/bin/env python3
"""GMP pipeline gateway — one REST door any agent walks through.

stdlib-only HTTP server (http.server), same shape as cloud/queue/queue_api.py.
Reads answer directly (roster, diaries, ladder); anything that does work is
ENQUEUED into the job queue (POST <QUEUE_URL>/jobs, X-Token from QUEUE_TOKEN).
The gateway never runs work itself — the serialize law holds.

Env:
  GATEWAY_TOKEN  X-Token clients must present (fail closed if unset)
  QUEUE_TOKEN    X-Token the gateway presents to the queue
  QUEUE_URL      queue base URL (default http://127.0.0.1:8901)
  GAME_ROOTS     os.pathsep list of roots holding game checkouts; a game's
                 diary lives at <root>/<diary from games.json>
                 (default C:/Users/aaron)

Endpoints (all JSON, prefix /api):
  GET  /api/health              {ok, queue} — no token, mirrors the queue
  GET  /api/status              roster + queue counts + ladder + version
  POST /api/games               {name, template, pitch, requester} -> new_game
  GET  /api/games/<slug>/diary  that game's DIARY.md (entry count + 40-line tail)
  POST /api/orders/tunable      {game, tunable, new_value}     -> tune_tunable
  POST /api/orders/art          {game, prompt, asset_id}       -> generate_art
  POST /api/critique            {game, mode: llm|vision|full}  -> critique
  POST /api/playtest            {game, seconds}                -> emulator
  POST /api/gpu                 {kind: train|mesh|render, staging_dir,
                                return_dir, payload}           -> gpu.<kind>
  POST /api/device_test         {game, apk}                    -> device_test
  POST /api/deploy              {game, target: retromonkey|itch} -> deploy
  GET  /api/jobs                proxy -> queue GET /jobs (same query params)
  GET  /api/jobs/<id>           proxy -> queue GET /jobs, filtered to one id
  POST /api/ladder/refresh      {}                             -> ladder_refresh

The public /api/jobs route belongs to the QUEUE (Caddy sends it to :8901).
This gateway's /api/jobs + /api/jobs/<id> are local-only proxies for agents
that only know the gateway — Caddy never routes public traffic to them.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlencode, urlparse

HOST = "127.0.0.1"
PORT = 8902
API_DIR = os.path.dirname(os.path.abspath(__file__))
GAMES_FILE = os.path.join(API_DIR, "games.json")
REPO_ROOT = os.path.dirname(os.path.dirname(API_DIR))  # cloud/api -> repo root
LADDER_FILE = os.path.join(REPO_ROOT, "daily", "ladder.json")
QUEUE_URL = os.environ.get("QUEUE_URL", "http://127.0.0.1:8901").rstrip("/")
VERSION = "0.1.0"
QUEUE_TIMEOUT = 10  # seconds — the queue is local; slow means down
TAIL_LINES = 40     # diary tail law
CRITIQUE_MODES = ("llm", "vision", "full")
GPU_KINDS = ("train", "mesh", "render")
DEPLOY_TARGETS = ("retromonkey", "itch")
JOB_STATUSES = ("queued", "running", "done", "failed")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def say(msg):
    """No-silent-jobs law: one line per event, always."""
    print(f"[gateway] {msg}", flush=True)


# --------------------------------------------------------------------------
# Data the gateway READS directly (no work, no queue round-trip)
# --------------------------------------------------------------------------

def load_games():
    with open(GAMES_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return {g["slug"]: g for g in data["games"]}


def load_ladder():
    """daily/ladder.json if present, trimmed to what agents need (no key routing)."""
    if not os.path.isfile(LADDER_FILE):
        return None
    try:
        with open(LADDER_FILE, "r", encoding="utf-8") as fh:
            ladder = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"error": f"ladder unreadable: {exc}"}
    roles = {}
    for role, spec in (ladder.get("roles") or {}).items():
        roles[role] = {
            "model": spec.get("model"),
            "url": spec.get("url"),
            "pinned": bool(spec.get("pinned")),
        }
    return {
        "updated_at": ladder.get("updated_at"),
        "updated_by": ladder.get("updated_by"),
        "roles": roles,
    }


def game_roots():
    raw = os.environ.get("GAME_ROOTS", "C:/Users/aaron")
    roots = [r.strip().rstrip("/") for r in raw.split(os.pathsep) if r.strip()]
    return roots or ["C:/Users/aaron"]


# --------------------------------------------------------------------------
# Queue client — the gateway's only way of making work happen
# --------------------------------------------------------------------------

class QueueDown(Exception):
    pass


def queue_call(method, path, query="", body=None, token=""):
    """One HTTP round-trip to the queue. Returns (status_code, parsed_body).
    Raises QueueDown when the queue itself is unreachable."""
    url = QUEUE_URL + path + (("?" + query) if query else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Token", token or "")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=QUEUE_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:  # the queue answered — pass it through
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"error": raw[:500]}
    except (urllib.error.URLError, OSError) as exc:
        raise QueueDown(f"{method} {path}: {exc}") from exc


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class GatewayHandler(BaseHTTPRequestHandler):
    queue_token = None  # set in main()
    games = {}          # slug -> roster entry, set in main()

    def log_message(self, fmt, *args):  # narration is one line per handler
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        token = os.environ.get("GATEWAY_TOKEN", "").strip()
        return token and self.headers.get("X-Token", "") == token

    def _body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return obj if isinstance(obj, dict) else None

    def _enqueue(self, jtype, path, payload, priority=5):
        """The one door to doing work: POST /jobs on the queue, narrate the outcome."""
        try:
            code, obj = queue_call(
                "POST", "/jobs",
                body={"type": jtype, "payload": payload, "priority": priority},
                token=self.queue_token,
            )
        except QueueDown as exc:
            say(f"POST {path} type={jtype} -> 503 (queue unreachable: {exc})")
            self._json(503, {"error": "queue unreachable", "detail": str(exc)})
            return
        job_id = obj.get("id") if isinstance(obj, dict) else None
        if code != 200 or job_id is None:
            say(f"POST {path} type={jtype} -> {code} (queue said: {obj})")
            self._json(502, {"error": "queue rejected the work-order",
                             "queue_status": code, "queue_body": obj})
            return
        say(f"POST {path} type={jtype} -> queued id={job_id} priority={priority}")
        self._json(200, {"ok": True, "job_id": job_id, "type": jtype,
                         "priority": priority})

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/health":
            try:
                _, health = queue_call("GET", "/health")
                say("GET /api/health -> ok, queue up")
                self._json(200, {"ok": True, "queue": True,
                                 "queue_backend": health.get("backend"),
                                 "queue_counts": health.get("counts")})
            except QueueDown as exc:
                say(f"GET /api/health -> ok, queue DOWN ({exc})")
                self._json(200, {"ok": True, "queue": False, "detail": str(exc)})
            return

        if parsed.path == "/api/public/status":  # no token — site dashboard feed
            try:
                _, health = queue_call("GET", "/health")
                counts, queue_up = health.get("counts"), True
            except QueueDown:
                counts, queue_up = None, False
            roster = [{"slug": g["slug"], "name": g.get("name", g["slug"]),
                       "live": g.get("live") or {}}
                      for g in self.games.values()]
            ladder = load_ladder()
            say(f"GET /api/public/status -> {len(roster)} games (public feed)")
            self._json(200, {"ok": True, "version": VERSION,
                             "queue": {"reachable": queue_up, "counts": counts},
                             "games": roster, "ladder": ladder})
            return

        if not self._authed():
            say(f"GET {parsed.path} -> 403 (bad token from {self.client_address[0]})")
            self._json(403, {"error": "bad X-Token"})
            return

        if parsed.path == "/api/status":
            counts, backend = None, None
            try:
                _, health = queue_call("GET", "/health")
                counts, backend = health.get("counts"), health.get("backend")
                queue_up = True
            except QueueDown as exc:
                backend = f"unreachable: {exc}"
                queue_up = False
            roster = [{"slug": g["slug"], "name": g.get("name", g["slug"]),
                       "live": g.get("live") or {}}
                      for g in self.games.values()]
            ladder = load_ladder()
            say(f"GET /api/status -> {len(roster)} games, queue="
                f"{'up' if queue_up else 'DOWN'}, "
                f"ladder={'present' if ladder else 'none'}")
            self._json(200, {"ok": True, "version": VERSION,
                             "queue": {"reachable": queue_up, "backend": backend,
                                       "counts": counts},
                             "games": roster, "ladder": ladder})
            return

        if parsed.path == "/api/jobs":
            qs = parse_qs(parsed.query)
            status = (qs.get("status") or [None])[0]
            if status is not None and status not in JOB_STATUSES:
                say(f"GET /api/jobs -> 400 (unknown status {status!r})")
                self._json(400, {"error": "status must be queued|running|done|failed"})
                return
            try:
                limit = min(max(int((qs.get("limit") or ["50"])[0]), 1), 500)
            except ValueError:
                limit = 50
            query = urlencode({k: v for k, v in (("status", status), ("limit", limit))
                               if v is not None})
            try:
                code, obj = queue_call("GET", "/jobs", query=query,
                                       token=self.queue_token)
            except QueueDown as exc:
                say(f"GET /api/jobs -> 503 (queue unreachable: {exc})")
                self._json(503, {"error": "queue unreachable", "detail": str(exc)})
                return
            say(f"GET /api/jobs status={status or 'any'} limit={limit} -> queue "
                f"said {code} ({obj.get('count', '?')} jobs)")
            self._json(code, obj)
            return

        parts = parsed.path.strip("/").split("/")

        if len(parts) == 3 and parts[0] == "api" and parts[1] == "jobs":
            try:
                job_id = int(parts[2])
            except ValueError:
                say(f"GET {parsed.path} -> 400 (bad job id)")
                self._json(400, {"error": "bad job id"})
                return
            try:
                code, obj = queue_call("GET", "/jobs", query="limit=500",
                                       token=self.queue_token)
            except QueueDown as exc:
                say(f"GET /api/jobs/{job_id} -> 503 (queue unreachable: {exc})")
                self._json(503, {"error": "queue unreachable", "detail": str(exc)})
                return
            if code != 200:
                say(f"GET /api/jobs/{job_id} -> {code} (queue list failed)")
                self._json(code, obj)
                return
            job = next((j for j in obj.get("jobs") or []
                        if j.get("id") == job_id), None)
            if job is None:
                n = len(obj.get("jobs") or [])
                say(f"GET /api/jobs/{job_id} -> 404 (not in the most recent {n})")
                self._json(404, {"error": f"job {job_id} not found in the queue's "
                                          f"most recent {n} jobs"})
                return
            say(f"GET /api/jobs/{job_id} -> {job.get('status')}")
            self._json(200, job)
            return

        if (len(parts) == 4 and parts[0] == "api" and parts[1] == "games"
                and parts[3] == "diary"):
            self._diary(unquote(parts[2]))
            return

        say(f"GET {parsed.path} -> 404")
        self._json(404, {"error": "not found"})

    def _diary(self, slug):
        entry = self.games.get(slug)
        if entry is None:
            say(f"GET /api/games/{slug}/diary -> 404 (unknown game)")
            self._json(404, {"error": f"unknown game '{slug}'"})
            return
        candidates = [os.path.join(root, *entry["diary"].split("/"))
                      for root in game_roots()]
        diary_path = next((c for c in candidates if os.path.isfile(c)), None)
        if diary_path is None:
            say(f"GET /api/games/{slug}/diary -> 404 (no diary at any root)")
            self._json(404, {"slug": slug, "error": "diary not found",
                             "looked_in": [c.replace(os.sep, "/")
                                           for c in candidates]})
            return
        try:
            with open(diary_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            say(f"GET /api/games/{slug}/diary -> 500 ({exc})")
            self._json(500, {"slug": slug, "error": f"diary unreadable: {exc}"})
            return
        entries = sum(1 for ln in lines if ln.startswith("## "))
        tail = lines[-TAIL_LINES:]
        say(f"GET /api/games/{slug}/diary -> {entries} entries, "
            f"tail {len(tail)} lines ({diary_path.replace(os.sep, '/')})")
        self._json(200, {"slug": slug, "entries_count": entries, "tail": tail})

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._authed():
            say(f"POST {parsed.path} -> 403 (bad token from {self.client_address[0]})")
            self._json(403, {"error": "bad X-Token"})
            return

        body = self._body()
        if body is None:
            say(f"POST {parsed.path} -> 400 (body is not a JSON object)")
            self._json(400, {"error": "body must be a JSON object"})
            return

        if parsed.path == "/api/jobs/claim":  # remote workers (Lappy) claim lane
            _, code, payload = queue_call("POST", "/jobs/claim", body)
            job = (payload or {}).get("job") or {}
            say(f"POST /api/jobs/claim types={body.get('types')} -> "
                f"job {job.get('id', 'none')}")
            self._json(code if code else 200, payload)
            return

        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/result"):
            jid = parsed.path.split("/")[3]
            _, code, payload = queue_call("POST", f"/jobs/{jid}/result", body)
            say(f"POST /api/jobs/{jid}/result -> {str(body)[:80]}")
            self._json(code if code else 200, payload)
            return

        if parsed.path == "/api/games":
            name = body.get("name")
            if not isinstance(name, str) or not name.strip():
                say("POST /api/games -> 400 (missing 'name')")
                self._json(400, {"error": "'name' is required"})
                return
            payload = {"name": name.strip()}
            for key in ("template", "pitch", "requester"):
                if body.get(key) is not None:
                    payload[key] = str(body[key])
            self._enqueue("new_game", "/api/games", payload)
            return

        if parsed.path == "/api/orders/tunable":
            game, tunable, new_value = (body.get("game"), body.get("tunable"),
                                        body.get("new_value"))
            if (not isinstance(game, str) or not game.strip()
                    or not isinstance(tunable, str) or not tunable.strip()
                    or new_value is None):
                say("POST /api/orders/tunable -> 400 (need game, tunable, new_value)")
                self._json(400, {"error": "'game', 'tunable' and 'new_value' are required"})
                return
            self._enqueue("tune_tunable", "/api/orders/tunable",
                          {"game": game.strip(), "tunable": tunable.strip(),
                           "new_value": new_value})
            return

        if parsed.path == "/api/orders/art":
            game, prompt = body.get("game"), body.get("prompt")
            if (not isinstance(game, str) or not game.strip()
                    or not isinstance(prompt, str) or not prompt.strip()):
                say("POST /api/orders/art -> 400 (need game, prompt)")
                self._json(400, {"error": "'game' and 'prompt' are required"})
                return
            payload = {"game": game.strip(), "prompt": prompt.strip()}
            if body.get("asset_id") is not None:
                payload["asset_id"] = str(body["asset_id"])
            self._enqueue("generate_art", "/api/orders/art", payload)
            return

        if parsed.path == "/api/critique":
            game, mode = body.get("game"), body.get("mode", "full")
            if not isinstance(game, str) or not game.strip():
                say("POST /api/critique -> 400 (missing 'game')")
                self._json(400, {"error": "'game' is required"})
                return
            if mode not in CRITIQUE_MODES:
                say(f"POST /api/critique -> 400 (unknown mode {mode!r})")
                self._json(400, {"error": "mode must be llm|vision|full"})
                return
            self._enqueue("critique", "/api/critique",
                          {"game": game.strip(), "mode": mode})
            return

        if parsed.path == "/api/playtest":
            game = body.get("game")
            if not isinstance(game, str) or not game.strip():
                say("POST /api/playtest -> 400 (missing 'game')")
                self._json(400, {"error": "'game' is required"})
                return
            try:
                seconds = min(max(int(body.get("seconds", 60)), 1), 7200)
            except (TypeError, ValueError):
                say("POST /api/playtest -> 400 ('seconds' must be an int)")
                self._json(400, {"error": "'seconds' must be an int"})
                return
            self._enqueue("emulator", "/api/playtest",
                          {"game": game.strip(), "seconds": seconds})
            return

        if parsed.path == "/api/gpu":
            kind = body.get("kind")
            if kind not in GPU_KINDS:
                say(f"POST /api/gpu -> 400 (unknown kind {kind!r})")
                self._json(400, {"error": "kind must be train|mesh|render"})
                return
            staging_dir, return_dir = body.get("staging_dir"), body.get("return_dir")
            if (not isinstance(staging_dir, str) or not staging_dir.strip()
                    or not isinstance(return_dir, str) or not return_dir.strip()):
                say(f"POST /api/gpu kind={kind} -> 400 (need staging_dir, return_dir)")
                self._json(400, {"error": "'staging_dir' and 'return_dir' are required"})
                return
            payload = body.get("payload") or {}
            if not isinstance(payload, dict):
                say(f"POST /api/gpu kind={kind} -> 400 ('payload' must be an object)")
                self._json(400, {"error": "'payload' must be an object"})
                return
            self._enqueue(f"gpu.{kind}", "/api/gpu",
                          {"staging_dir": staging_dir.strip(),
                           "return_dir": return_dir.strip(), "payload": payload})
            return

        if parsed.path == "/api/device_test":
            game = body.get("game")
            if not isinstance(game, str) or not game.strip():
                say("POST /api/device_test -> 400 (missing 'game')")
                self._json(400, {"error": "'game' is required"})
                return
            payload = {"game": game.strip()}
            if body.get("apk") is not None:
                payload["apk"] = str(body["apk"])
            self._enqueue("device_test", "/api/device_test", payload)
            return

        if parsed.path == "/api/deploy":
            game, target = body.get("game"), body.get("target")
            if not isinstance(game, str) or not game.strip():
                say("POST /api/deploy -> 400 (missing 'game')")
                self._json(400, {"error": "'game' is required"})
                return
            if target not in DEPLOY_TARGETS:
                say(f"POST /api/deploy -> 400 (unknown target {target!r})")
                self._json(400, {"error": "target must be retromonkey|itch"})
                return
            self._enqueue("deploy", "/api/deploy",
                          {"game": game.strip(), "target": target})
            return

        if parsed.path == "/api/ladder/refresh":
            self._enqueue("ladder_refresh", "/api/ladder/refresh", {})
            return

        say(f"POST {parsed.path} -> 404")
        self._json(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description="GMP pipeline gateway")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--host", default=HOST,
                    help="bind address (loopback; Caddy fronts it publicly)")
    args = ap.parse_args()

    token = os.environ.get("GATEWAY_TOKEN", "").strip()
    if not token:
        say("GATEWAY_TOKEN is not set — refusing to start (fail closed)")
        sys.exit(1)

    try:
        games = load_games()
    except (OSError, ValueError) as exc:
        say(f"games.json unreadable ({exc})")
        sys.exit(1)

    GatewayHandler.queue_token = os.environ.get("QUEUE_TOKEN", "").strip()
    GatewayHandler.games = games
    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    say(f"listening on http://{args.host}:{args.port} token=***/ "
        f"games={len(games)} queue={QUEUE_URL}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("shutdown (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
