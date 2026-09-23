#!/usr/bin/env python
"""Pipeline MCP server — connect any agent to the full Game Making Pipeline.

Every tool is a 1:1 front for the GMP gateway REST API. The gateway is the only
thing this server talks to; all heavy work runs behind it through THE QUEUE.

Transports:
  stdio (default)   : local agents, e.g. Claude Code via `claude mcp add`
  --http            : streamable HTTP on 127.0.0.1:8911 at /mcp for remote agents

Environment:
  GATEWAY_URL       : gateway base URL      (default http://127.0.0.1:8902)
  GATEWAY_TOKEN     : shared secret. Sent to the gateway as the X-Token header,
                      AND required as `Authorization: Bearer <token>` by the
                      HTTP transport. One token, both doors.
  GATEWAY_TIMEOUT   : per-request timeout in seconds (default 60)

Auth (HTTP mode): if GATEWAY_TOKEN is unset the /mcp endpoint is open — fine on
loopback, never expose it publicly without a token.
"""

import json
import os
from pathlib import Path
import sys
from typing import Any

# --- UTF-8 reconfigure law (Windows consoles are cp1252 by default) ---------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from mcp.server.fastmcp import FastMCP

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8902").rstrip("/")
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
if not GATEWAY_TOKEN:
    # fall back to the .token file next to this script — keeps secrets out of
    # Desktop's JSON config; rotations only touch the file, never the config
    _token_path = Path(__file__).resolve().parent / ".token"
    try:
        _raw = _token_path.read_text(encoding="utf-8").strip()
        if _raw.startswith("GATEWAY_TOKEN="):
            _raw = _raw[len("GATEWAY_TOKEN="):]
        GATEWAY_TOKEN = _raw
    except OSError:
        pass
try:
    GATEWAY_TIMEOUT = float(os.environ.get("GATEWAY_TIMEOUT", "60"))
except ValueError:
    GATEWAY_TIMEOUT = 60.0

MCP_NAME = "pipeline"
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8911"))

mcp = FastMCP(MCP_NAME, host=MCP_HOST, port=MCP_PORT)


# --- gateway client ----------------------------------------------------------

def _call(method: str, path: str, body: dict | None = None,
          params: dict | None = None) -> str:
    """One request to the gateway; returns the JSON response as text.

    2xx  -> the gateway's JSON, passed through untouched (1:1 with the REST API)
    else -> {"ok": false, "status": <code>, "error": <body>} envelope
    transport failure -> {"ok": false, "status": 0, "error": "..."} envelope
    """
    import httpx

    url = f"{GATEWAY_URL}{path}"
    headers = {"Accept": "application/json"}
    if GATEWAY_TOKEN:
        headers["X-Token"] = GATEWAY_TOKEN
    try:
        with httpx.Client(timeout=GATEWAY_TIMEOUT) as client:
            resp = client.request(method, url, json=body, params=params,
                                  headers=headers)
    except httpx.HTTPError as exc:
        return json.dumps({
            "ok": False,
            "status": 0,
            "error": f"gateway unreachable at {GATEWAY_URL}: {exc}",
        }, indent=2)

    if 200 <= resp.status_code < 300:
        try:
            return json.dumps(resp.json(), indent=2)
        except ValueError:
            return json.dumps({"ok": True, "status": resp.status_code,
                               "raw": resp.text}, indent=2)
    return json.dumps({
        "ok": False,
        "status": resp.status_code,
        "error": resp.text[:2000],
    }, indent=2)


def _get(path: str, params: dict | None = None) -> str:
    return _call("GET", path, params=params)


def _post(path: str, body: dict) -> str:
    return _call("POST", path, body=body)


# --- tools (1:1 with the gateway) --------------------------------------------

@mcp.tool()
def pipeline_status() -> str:
    """Snapshot of the whole factory: registered games, queue depth, last
    builds, ladder state. Call this first — it tells you what the pipeline is
    doing right now. Returns the gateway's status JSON as text."""
    return _get("/api/status")


@mcp.tool()
def new_game(name: str, template: str, pitch: str,
             requester: str = "mcp-agent") -> str:
    """Instantiate a brand-new game in the pipeline. Files a work-order that
    scaffolds the Godot project from <template> and registers the game.
      name:      human title, e.g. "Cube Fall"
      template:  scaffold id, e.g. "arcade", "platformer", "3d"
      pitch:     one-sentence design pitch (goes in the ledger)
      requester: who is asking — recorded in the diary
    Returns JSON with the new game's slug and the queued build job."""
    return _post("/api/games", {"name": name, "template": template,
                                "pitch": pitch, "requester": requester})


@mcp.tool()
def read_diary(slug: str) -> str:
    """Read a game's DIARY — the in-world ledger every green milestone is
    written into (builds, critiques, art, tunables). slug is the game id, e.g.
    "word-poker". Returns the diary entries JSON, newest last."""
    return _get(f"/api/games/{slug}/diary")


@mcp.tool()
def tune_tunable(game: str, tunable: str, new_value: float) -> str:
    """File a T1 tunable order: change ONE gameplay constant on a game — the
    lane for "make the timer longer" / "double the player speed".
      game:      game slug, e.g. "word-poker"
      tunable:   const name as declared in the game, e.g. "round_seconds"
      new_value: numeric new value (the gateway clamps to the tunable's range,
                 runs the 2x gate wall, auto-reverts on red, deploys on green)
    Returns the queued job JSON."""
    return _post("/api/orders/tunable", {"game": game, "tunable": tunable,
                                         "new_value": new_value})


@mcp.tool()
def order_art(game: str, prompt: str, asset_id: str) -> str:
    """File a T2 art order: generate or re-key an art asset (Flux.1-dev + PIL
    magic-wand keying).
      game:     game slug the asset belongs to
      prompt:   art direction, e.g. "mossy stone tiles, top-down, 32px look"
      asset_id: the asset slot to fill, e.g. "tileset_overworld"
    Returns the queued job JSON."""
    return _post("/api/orders/art", {"game": game, "prompt": prompt,
                                     "asset_id": asset_id})


@mcp.tool()
def run_critique(game: str, mode: str = "full") -> str:
    """Send The Critic at a game: PlayerOne PLAYS it on the emulator/Playwright
    lane, the vision model scores screenshots, the LLM ladder writes the
    verdict (loves / issues / work-orders).
      game: game slug
      mode: "full" (play + see + judge), "vision" (screenshots only), or
            "quick" (play only, no vision)
    Returns the queued critique job JSON; read_diary once it lands."""
    return _post("/api/critique", {"game": game, "mode": mode})


@mcp.tool()
def run_playtest(game: str, seconds: int = 60) -> str:
    """Queue a timed playtest of a game through the queue — a raw measured
    session (inputs, frames survived, score) without the judgement layer.
      game:    game slug
      seconds: wall-clock length of the session (default 60)
    Returns the queued job JSON."""
    return _post("/api/playtest", {"game": game, "seconds": seconds})


@mcp.tool()
def run_gpu(kind: str, staging_dir: str, return_dir: str) -> str:
    """Offload a heavyweight job to the GPU machine (Lappy, RTX 3060) through
    the queue: TRELLIS.2 3D meshes, Blender renders, ComfyUI passes.
      kind:        job type, e.g. "trellis", "blender", "comfyui"
      staging_dir: forward-slash path holding the job's inputs
      return_dir:  forward-slash path the outputs land in
    Returns the queued job JSON."""
    return _post("/api/gpu", {"kind": kind, "staging_dir": staging_dir,
                              "return_dir": return_dir})


@mcp.tool()
def device_test(game: str, apk: str) -> str:
    """Queue a real-device Android test of a built APK (Firebase Test Lab lane
    offloaded to free Google tiers).
      game: game slug
      apk:  forward-slash path to the built .apk
    Returns the queued job JSON."""
    return _post("/api/device_test", {"game": game, "apk": apk})


@mcp.tool()
def deploy(game: str, target: str) -> str:
    """Ship a green-gated build. Only builds that passed the 2x clear gates go
    out; red gates auto-revert upstream.
      game:  game slug
      target: "retromonkey" (site, /games/<slug>/) or "itch" (itch.io, butler)
    Returns the queued deploy job JSON."""
    return _post("/api/deploy", {"game": game, "target": target})


@mcp.tool()
def list_jobs(status: str | None = None, limit: int = 20) -> str:
    """List jobs in THE QUEUE (Postgres-backed; one job at a time locally —
    serialization is how the little box survives).
      status: optional filter — "queued", "running", "done" or "failed"
      limit:  max rows to return (default 20)
    Returns the jobs JSON, newest first. Poll with job_status for detail."""
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    if limit:
        params["limit"] = limit
    return _get("/api/jobs", params=params or None)


@mcp.tool()
def job_status(id: str) -> str:
    """One queue job in full: state, timing, and result payload. Use this to
    poll anything any other tool queued (deploys, critiques, GPU jobs...).
      id: the job id returned when the job was filed.
    Returns the job JSON as text."""
    return _get(f"/api/jobs/{id}")


@mcp.tool()
def refresh_ladder() -> str:
    """Refresh the model ladder (daily/ladder.json): benchmark what the catalog
    offers and promote the strongest models to the brain rungs. Runs on
    retromonkey every 7 days — call this to force it early.
    Returns the queued refresh job JSON."""
    return _post("/api/ladder/refresh", {})


@mcp.tool()
def pipeline_health() -> str:
    """Liveness probe for the gateway itself — the cheapest way to check the
    pipeline is up and which version is serving. Returns ok + version JSON."""
    return _get("/api/health")


# =============================================================================
# FULL DEV CYCLE — read, build, test, iterate, deploy, monitor
# (these read/write the server filesystem + Forgejo directly; the MCP server
#  runs on the same box as the game repos, so no extra gateway hops)
# =============================================================================

GAMES_SRC = os.environ.get("GMP_GAMES_SRC", "/home/ubuntu/games-src")
FORGEJO_BASE = os.environ.get("FORGEJO_HOST", "127.0.0.1:3001")
FORGEJO_ORG = "slothitude"
FORGEJO_TOKEN = os.environ.get("FORGEJO_TOKEN",
                               "${FORGEJO_TOKEN}")  # basic-auth token


def _game_path(game: str) -> Path:
    return Path(GAMES_SRC) / game


def _forgejo(path: str) -> dict | list:
    import urllib.request, base64
    url = f"http://{FORGEJO_BASE}/api/v1/repos/{FORGEJO_ORG}/{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " +
                   base64.b64encode(f"{FORGEJO_ORG}:{FORGEJO_TOKEN}".encode()
                                    ).decode())
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


@mcp.tool()
def read_spec(game: str) -> str:
    """Read a game's design spec — the milestones, art list, constants law.
    This is the PLAN: what M1-M4 define, what each milestone's scope and tests
    are. Any agent building on a game starts here.
      game: game slug (e.g. 'sonar')"""
    for rel in ("spec/study_spec.json", "spec/jam_spec.json"):
        p = _game_path(game) / rel
        if p.is_file():
            return p.read_text(encoding="utf-8")[:12000]
    return json.dumps({"error": f"no spec found for {game}"})


@mcp.tool()
def read_code(game: str, path: str) -> str:
    """Read a source file from a game repo on the server.
      game: game slug
      path: relative path (e.g. 'scripts/feel.gd', 'scripts/main.gd')"""
    p = _game_path(game) / path
    if not p.is_file():
        return json.dumps({"error": f"not found: {p}"})
    return p.read_text(encoding="utf-8")[:16000]


@mcp.tool()
def write_code(game: str, path: str, content: str,
               message: str = "code change via MCP") -> str:
    """Write a file in a game repo, commit, push to Forgejo (the Actions wall
    judges it). This is how agents ship code changes — the wall catches red.
      game: game slug
      path: relative path
      content: the complete file content (never a diff)
      message: commit message"""
    import subprocess
    gd = _game_path(game)
    p = gd / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    for cmd in (["git", "add", "-A"],
                ["git", "-c", "user.name=mcp-agent",
                 "-c", "user.email=mcp@pipeline", "commit", "-m", message],
                ["git", "push",
                 f"http://{FORGEJO_ORG}:{FORGEJO_TOKEN}@{FORGEJO_BASE}/"
                 f"{FORGEJO_ORG}/{game}.git", "HEAD:main"]):
        r = subprocess.run(cmd, cwd=gd, capture_output=True, text=True,
                           timeout=120)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout or ""):
            return json.dumps({"error": f"git {cmd[1]}: {(r.stderr or '')[:200]}"})
    return json.dumps({"ok": True, "game": game, "path": path,
                       "message": message})


@mcp.tool()
def build_milestone(game: str, milestone: str, directive: str = "") -> str:
    """Trigger the pi milestone builder with the red-wall learner. pi builds
    the milestone from the spec, pushes, the Actions wall judges; on red the
    failure log is fed back to pi for up to 3 attempts. On green, the chain
    enqueues the next milestone automatically.
      game: game slug
      milestone: e.g. 'M2', 'M3'
      directive: optional extra instruction for pi"""
    body = {"game": game, "milestone": milestone}
    if directive:
        body["directive"] = directive
    return _post("/api/jobs", {"type": "milestone", "payload": body,
                               "priority": 4})


@mcp.tool()
def wall_status(game: str) -> str:
    """Check the Actions gate wall verdicts for a game's recent pushes.
    Returns each run's status (success/failure) and commit message."""
    try:
        runs = _forgejo(f"{game}/actions/tasks")
        out = [{"commit": r.get("display_title", "")[:60],
                "status": r.get("status"),
                "sha": r.get("head_sha", "")[:7]}
               for r in (runs if isinstance(runs, list) else
                         runs.get("workflow_runs", []))[:5]]
        return json.dumps(out)
    except Exception as exc:
        return json.dumps({"error": str(exc)[:120]})


@mcp.tool()
def read_wall_logs(game: str) -> str:
    """Get the failing test output from the most recent red wall run —
    the learner's food. Shows which suite failed and WHY."""
    import urllib.request, base64
    try:
        runs = _forgejo(f"{game}/actions/tasks")
        wf = (runs if isinstance(runs, list) else
              runs.get("workflow_runs", []))
        if not wf:
            return json.dumps({"note": "no runs yet"})
        run = wf[0]
        if run.get("status") != "failure":
            return json.dumps({"note": f"latest run: {run.get('status')}"})
        # pull the job logs
        url = (f"http://{FORGEJO_BASE}/api/v1/repos/{FORGEJO_ORG}/{game}/"
               f"actions/jobs/{run.get('id')}/logs")
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Basic " +
                       base64.b64encode(f"{FORGEJO_ORG}:{FORGEJO_TOKEN}"
                                        .encode()).decode())
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines()
                 if any(k in ln.lower() for k in
                        ("fail", "error", "assert", "RESULT"))]
        return "\n".join((lines or text.splitlines())[-30:])[-4000:]
    except Exception as exc:
        return json.dumps({"error": str(exc)[:120]})


@mcp.tool()
def game_files(game: str, subpath: str = "") -> str:
    """List files in a game repo (optionally under a subpath like 'scripts/')."""
    p = _game_path(game) / subpath if subpath else _game_path(game)
    if not p.is_dir():
        return json.dumps({"error": f"not found: {p}"})
    entries = sorted(str(f.relative_to(_game_path(game)))
                     for f in p.rglob("*") if f.is_file()
                     and ".git" not in f.parts and ".godot" not in f.parts)
    return json.dumps({"game": game, "files": entries[:80]})


@mcp.tool()
def git_log(game: str, count: int = 5) -> str:
    """Recent commits for a game — what shipped, when, by whom."""
    import subprocess
    r = subprocess.run(["git", "log", f"--oneline", f"-{count}"],
                       cwd=_game_path(game), capture_output=True, text=True,
                       timeout=15)
    return r.stdout or "(no commits)"


@mcp.tool()
def player_reviews(game: str) -> str:
    """Read what players actually said — reviews, star ratings, and help
    session transcripts from the in-game portal. The build memory's voice."""
    rev = Path("/home/ubuntu/pipeline/reviews") / f"{game}.jsonl"
    if not rev.is_file():
        return json.dumps({"note": f"no reviews for {game} yet"})
    rows = [json.loads(ln) for ln in rev.read_text(encoding="utf-8").splitlines()
            if ln.strip()][-10:]
    return json.dumps(rows, indent=1)[:6000]


@mcp.tool()
def run_selfplay(game: str, sessions: int = 2) -> str:
    """Trigger PlayerOne self-play: the evolved brain plays the live game,
    recording every decision as training data for the next evolution cycle."""
    import subprocess
    r = subprocess.run(
        ["/home/ubuntu/playerone/venv/bin/python",
         "/home/ubuntu/playerone/cloud/playerone/selfplay.py",
         "--game", game, "--sessions", str(sessions),
         "--scripts-dir", "/home/ubuntu/playerone/scripts"],
        capture_output=True, text=True, timeout=600,
        cwd="/home/ubuntu/playerone")
    tail = (r.stdout or "").strip().splitlines()[-3:]
    return "\n".join(tail) or (r.stderr or "")[-200:]


@mcp.tool()
def chain_status(game: str) -> str:
    """Where a game is in its dev cycle: spec milestones vs shipped milestones
    vs wall verdicts vs live status. The one-call cycle report."""
    spec = {}
    for rel in ("spec/study_spec.json", "spec/jam_spec.json"):
        p = _game_path(game) / rel
        if p.is_file():
            spec = json.loads(p.read_text(encoding="utf-8"))
            break
    milestones = spec.get("milestones", [])
    wall = json.loads(wall_status(game))
    commits = git_log(game, 8)
    diary = read_diary(game)[:300]
    return json.dumps({
        "milestones_defined": len(milestones),
        "milestone_names": [m if isinstance(m, str)
                            else m.get("id", m.get("name", "?"))
                            for m in milestones],
        "recent_walls": wall if isinstance(wall, list) else str(wall)[:200],
        "recent_commits": commits,
        "diary_head": diary,
    }, indent=1)[:4000]


@mcp.tool()
def export_game(game: str) -> str:
    """Export a game's web build (Godot headless) and deploy it live to
    retromonkey.com.au/games/<game>/. The ship button."""
    import subprocess
    gd = _game_path(game)
    godot = "/home/ubuntu/Godot_v4.7.1-stable_linux.x86_64"
    webdir = gd / "build" / "web"
    webdir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME="/home/ubuntu")
    r = subprocess.run(
        [godot, "--headless", "--path", str(gd),
         "--export-release", "Web", str(webdir / "index.html")],
        capture_output=True, text=True, timeout=300, env=env)
    if r.returncode != 0:
        return json.dumps({"error": (r.stderr or r.stdout)[-300:]})
    import shutil
    dest = Path(f"/home/ubuntu/site/games/{game}")
    dest.mkdir(parents=True, exist_ok=True)
    for f in webdir.iterdir():
        shutil.copy2(f, dest / f.name)
    return json.dumps({"ok": True, "game": game,
                       "url": f"https://retromonkey.com.au/games/{game}/",
                       "files": len(list(webdir.iterdir()))})


# --- transports ---------------------------------------------------------------

class _BearerAuthMiddleware:
    """Pure-ASGI gate: require `Authorization: Bearer <GATEWAY_TOKEN>` on every
    HTTP request before it reaches the MCP app."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self.token:
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        supplied = headers.get(b"authorization", b"").decode("latin-1")
        if supplied != f"Bearer {self.token}":
            body = json.dumps(
                {"ok": False, "error": "unauthorized: bad or missing bearer"}).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def main() -> None:
    if "--http" in sys.argv:
        import uvicorn

        app = mcp.streamable_http_app()
        # MCP_OPEN=1: the server is the auth boundary — it holds the gateway
        # token internally, clients connect with just the URL (no Bearer).
        # This is the one-paste setup: add https://<host>/mcp in any Claude.
        mcp_open = os.environ.get("MCP_OPEN", "") == "1"
        if GATEWAY_TOKEN and not mcp_open:
            app = _BearerAuthMiddleware(app, GATEWAY_TOKEN)
        print(f"pipeline mcp: streamable HTTP on http://{MCP_HOST}:{MCP_PORT}/mcp "
              f"(auth: {'OPEN — url-only' if mcp_open or not GATEWAY_TOKEN else 'bearer GATEWAY_TOKEN'})",
              file=sys.stderr)
        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="warning")
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
