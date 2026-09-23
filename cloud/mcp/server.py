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
        if GATEWAY_TOKEN:
            app = _BearerAuthMiddleware(app, GATEWAY_TOKEN)
        print(f"pipeline mcp: streamable HTTP on http://{MCP_HOST}:{MCP_PORT}/mcp "
              f"(auth: {'bearer GATEWAY_TOKEN' if GATEWAY_TOKEN else 'OPEN — no token set'})",
              file=sys.stderr)
        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="warning")
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
