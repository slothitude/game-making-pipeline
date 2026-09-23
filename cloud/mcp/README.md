# Pipeline MCP Server

Connect **any agent** — Claude Code, Colab, a Telegram bot, anything that
speaks MCP — to the full Game Making Pipeline. Every tool is a 1:1 front for
the GMP gateway REST API; nothing here does work itself, it just translates
MCP tool calls into gateway requests. The gateway files everything through
THE QUEUE, so heavy work never runs in the agent.

Implementation: the **official MCP Python SDK** (`mcp` 1.27.2, FastMCP path —
the `pip show mcp` check found it installed).

## Transports

| Mode | How | Who it's for |
|---|---|---|
| **stdio** (default) | `python .../server.py` — the agent spawns it as a subprocess | local agents: Claude Code, Claude Desktop, any SDK client |
| **streamable HTTP** | `python .../server.py --http` → binds `127.0.0.1:8911`, endpoint `/mcp` | remote agents: Colab, other machines, long-lived shared servers |

Environment (both modes):

| Var | Default | Meaning |
|---|---|---|
| `GATEWAY_URL` | `http://127.0.0.1:8902` | gateway base URL |
| `GATEWAY_TOKEN` | *(unset = open)* | **the gateway token doubles as the MCP token.** It is sent to the gateway as the `X-Token` header on every request, AND required as `Authorization: Bearer <token>` by the HTTP transport. One token, both doors. |
| `GATEWAY_TIMEOUT` | `60` | per-request timeout, seconds |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8911` | HTTP bind address |

One token, both doors: set `GATEWAY_TOKEN` and the MCP server (a) proves
itself to the gateway with `X-Token`, and (b) demands the same value as a
bearer token from any HTTP client. With stdio there is no inbound auth — the
subprocess is already on the agent's machine, so the token only travels
outward to the gateway. Unset `GATEWAY_TOKEN` and HTTP mode is OPEN — fine on
loopback, never expose it publicly.

## Tools

| Tool | Inputs | Fronts |
|---|---|---|
| `pipeline_status` | — | `GET /api/status` — games registry, queue depth, ladder state |
| `new_game` | `name, template, pitch, requester` | `POST /api/games` — scaffold + register a new game |
| `read_diary` | `slug` | `GET /api/games/{slug}/diary` — the in-world milestone ledger |
| `tune_tunable` | `game, tunable, new_value` | `POST /api/orders/tunable` — T1 order, clamped + gated |
| `order_art` | `game, prompt, asset_id` | `POST /api/orders/art` — T2 Flux + PIL keying order |
| `run_critique` | `game, mode="full"` | `POST /api/critique` — PlayerOne plays, sees, judges |
| `run_playtest` | `game, seconds=60` | `POST /api/playtest` — measured raw session |
| `run_gpu` | `kind, staging_dir, return_dir` | `POST /api/gpu` — offload to Lappy (TRELLIS/Blender/ComfyUI) |
| `device_test` | `game, apk` | `POST /api/device_test` — Firebase Test Lab lane |
| `deploy` | `game, target` | `POST /api/deploy` — `retromonkey` or `itch` |
| `list_jobs` | `status?, limit=20` | `GET /api/jobs` — queue rows, newest first |
| `job_status` | `id` | `GET /api/jobs/{id}` — one job in full |
| `refresh_ladder` | — | `POST /api/ladder/refresh` — force the 7-day ladder refresh |
| `pipeline_health` | — | `GET /api/health` — cheapest liveness probe |

Errors come back as `{"ok": false, "status": <code>, "error": ...}` text;
a dead gateway is `status: 0`. Successful responses are the gateway's own
JSON, passed through untouched.

## Registering — stdio mode

`claude mcp add`:

```bash
claude mcp add pipeline -e GATEWAY_URL=http://127.0.0.1:8902 -e GATEWAY_TOKEN=your-token -- python C:/Users/aaron/game-making-pipeline/cloud/mcp/server.py
```

or `.mcp.json`:

```json
{
  "mcpServers": {
    "pipeline": {
      "command": "python",
      "args": ["C:/Users/aaron/game-making-pipeline/cloud/mcp/server.py"],
      "env": {
        "GATEWAY_URL": "http://127.0.0.1:8902",
        "GATEWAY_TOKEN": "your-token"
      }
    }
  }
}
```

## Registering — HTTP mode

Start the server once (systemd/NSSM/a shell):

```bash
GATEWAY_TOKEN=your-token python C:/Users/aaron/game-making-pipeline/cloud/mcp/server.py --http
```

`claude mcp add` (Claude Code supports `--transport http` for remote servers):

```bash
claude mcp add --transport http pipeline http://127.0.0.1:8911/mcp --header "Authorization: Bearer your-token"
```

or `.mcp.json`:

```json
{
  "mcpServers": {
    "pipeline": {
      "type": "http",
      "url": "http://<host>:8911/mcp",
      "headers": {
        "Authorization": "Bearer your-token"
      }
    }
  }
}
```

`<host>` is `127.0.0.1` when the agent shares a machine with the server; for
remote agents use the server machine's LAN IP and put the whole thing behind
a tunnel (Tailscale/WireGuard) — the bearer token is required, but nothing
here should be on the open internet.

## Other MCP clients

**Colab / any Python agent** — official SDK client, six lines:

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client(
    "http://192.168.0.52:8911/mcp",
    headers={"Authorization": "Bearer your-token"},
) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool(
            "run_critique", {"game": "star-visitor", "mode": "full"})
        print(result.content[0].text)
```

**Generic JSON-RPC**: MCP is JSON-RPC 2.0 — `initialize`, then
`notifications/initialized`, then `tools/list` / `tools/call`. Any client that
can speak that (including Colab's MCP support) works. HTTP responses may come
back as SSE; the SDK client handles that for you.

**stdio from any SDK client**: spawn
`python C:/Users/aaron/game-making-pipeline/cloud/mcp/server.py` with
`GATEWAY_URL`/`GATEWAY_TOKEN` in the child env and talk JSON-RPC over
stdin/stdout.

## Tests

```bash
C:/Python313/python.exe tests/test_stdio.py   # stub gateway + full stdio handshake
C:/Python313/python.exe tests/test_http.py    # + bearer 401 check + HTTP handshake
```

Both spawn `tests/stub_gateway.py` (stdlib-only canned gateway on :8902) and
print the transcript, including the `X-Token` the gateway actually received.

## Notes for the gateway implementer (cloud/api/)

The MCP server forwards bodies verbatim, so these shapes are the contract:

- `POST /api/games` — `{name, template, pitch, requester}`
- `POST /api/orders/tunable` — `{game, tunable, new_value}` (number)
- `POST /api/orders/art` — `{game, prompt, asset_id}`
- `POST /api/critique` — `{game, mode}` where mode ∈ `full|vision|quick`
- `POST /api/playtest` — `{game, seconds}` (int)
- `POST /api/gpu` — `{kind, staging_dir, return_dir}`
- `POST /api/device_test` — `{game, apk}`
- `POST /api/deploy` — `{game, target}` where target ∈ `retromonkey|itch`
- `POST /api/ladder/refresh` — `{}` (empty body)
- `GET /api/jobs?status=&limit=` and `GET /api/jobs/{id}`
- Auth: `X-Token: $GATEWAY_TOKEN` on every request.
