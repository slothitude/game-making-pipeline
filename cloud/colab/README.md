# cloud/colab — the GPU offload lane

The receiving end of PIPELINE.md law 9: "GPU jobs -> the official Colab CLI."
The queue router never talks to Colab itself — it calls `run_job(job) -> dict`
here. The worker provisions a T4 runtime, ships the job, fetches artifacts,
and **always tears the runtime down** (try/finally — free-tier discipline),
with a 15-minute hard cap per job.

## Files

| File | Runs where | What |
|---|---|---|
| `colab_worker.py` | queue host | `run_job(job: dict) -> dict` + `--selftest` |
| `scripts/train.py` | Colab runtime | gpu.train: tiny-transformer trainer (real loop, stub scale) |
| `scripts/mesh.py` | Colab runtime | gpu.mesh: valid-GLB stub + gated proven hy3dgen chain |
| `scripts/render.py` | Colab runtime | gpu.render: placeholder-frames stub + gated blender headless |

## The queue wiring (all of it)

```python
from cloud.colab.colab_worker import run_job

result = run_job({
    "type": "gpu.train",              # gpu.train | gpu.mesh | gpu.render
    "staging_dir": "C:/jobs/42/stage",# inputs: data/*.jsonl, config.json, images/*, *.blend
    "return_dir":  "C:/jobs/42/done", # receives out/ + result.json
    "gpu": "T4",                      # optional; T4 is the free default
    "session": "gmp-train-42",        # optional runtime session name
    "payload": {"game": "sonar"},     # embedded verbatim into the remote job.json
})
# result == the result.json the runtime script wrote (scripts/*.py define the
# shape), or a structured error: {"error": ..., "hint": ...}. run_job never
# raises for operational failures and never leaves a runtime behind.
```

Per-job flow: stage staging dir + job.json -> `in.zip` -> `colab new -s S --gpu T4`
-> `colab upload` -> `colab exec -f scripts/<type>.py` -> poll-download
`result.json` + `out.zip` -> unzip into `return_dir/out/` -> `colab stop -s S`.

## AUTH SETUP — the one-time human step

The CLI's default auth is **ADC** (Application Default Credentials), and Colab's
API rejects plain gcloud ADC tokens: `userinfo.email` must be in the token or
session calls return HTTP 401. On the queue host run exactly:

```bash
gcloud auth application-default login \
    --scopes=openid,\
https://www.googleapis.com/auth/cloud-platform,\
https://www.googleapis.com/auth/userinfo.email,\
https://www.googleapis.com/auth/colaboratory
```

That's the whole login. Notes:

- **`colab auth` is NOT the login.** It authenticates the *runtime VM* for GCP
  services (BigQuery/GCS) mid-session. The prompt's "run colab auth" hint means
  "go do the auth setup" — the real command is the gcloud one above.
- Debug any auth weirdness with `colab whoami` (prints email, scopes, expiry)
  and `colab usage` (compute-unit balance).
- Tokens cache at `~/.config/colab-cli/token.json`; stale tokens from before a
  scope change must be deleted to re-trigger consent.
- The OAuth2 alternative (`--auth oauth2`) uses an installed-app copy-paste flow
  against the bundled cloud-SDK client (`~/.colab-cli-oauth-config.json`
  overrides). ADC is what this lane assumes.

## Free-tier notes

- **T4 is the default and the cheap/free shape.** L4/G4/H100/A100 exist but
  burn compute units; `--high-mem` shapes require Pro/Pro+ (never requested).
- `colab usage` shows the compute-unit balance — the router should treat a
  zero balance as a queue pause, not a retry storm.
- The CLI runs a keep-alive daemon per session so an idle runtime doesn't die
  mid-job — which makes **teardown discipline load-bearing**: every `run_job`
  ends with `colab stop -s`, success or failure.
- A 15-minute hard cap (`JOB_TIMEOUT_SEC`) exists because a wedged free-tier
  slot is worse than a failed job.

## KNOWN_GAPS (read before the first real run)

1. **Windows is not supported by the CLI** (README: "Linux and macOS only";
   WSL is never mentioned). On Rog the worker returns
   `{"error": "cli_missing", "hint": "run colab auth"}` instead of hanging.
   Production home is the retromonkey queue host (Ubuntu); on a Windows dev box
   use `--selftest` or run inside WSL.
2. **Whether a no-Pro account can provision runtimes through the CLI at all is
   unverified** — the README states no blanket free-tier rule, only the
   `--high-mem` entitlement. The old plan note ("Colab can't be headlessly
   started on free tier", `cloud/free-google-plan.md` G4) predates the CLI;
   first real run confirms or kills this lane.
3. **`colab exec` blocking semantics unverified** — docs imply exec then
   download works back-to-back (README example 1), but a long train may return
   early. The worker polls for `result.json` every 15s until the cap, which
   converges under both sync and async semantics.
4. **Directory upload/download is not documented** — the worker zips on purpose
   (`in.zip` / `out.zip`), so only single-file transfers are ever exercised.
5. **Remote working dir assumed `/content`** (the classic Colab content dir;
   the docs only ever name `/content/drive`). Scripts resolve paths through the
   `GMP_COLAB_BASE` fallback chain, so a different cwd is a one-env fix.
6. **Default auth provider contradiction**: README global flag says
   "default: adc"; docs/04 heading says "oauth2 (default)". This lane pins ADC
   via the scopes login above; `colab whoami` settles it on first run.
7. **Blender on Colab (gpu.render real path)**: blender is not preinstalled and
   there is no `bpy` wheel for Colab's python. The script downloads the release
   tarball at `GMP_BLENDER_VERSION` (default 4.2.3) — the URL pattern is
   standard but **the pinned version must be confirmed on first real run**
   (~350MB per session).
8. **The hy3dgen block in scripts/mesh.py is carried, not proven here** — it is
   the sculmm pilot v2 shape chain (Colab T4, normalized m/floor/+z, ~50k
   faces) pasted behind `GMP_MESH_REAL=1`. The default stub path (hand-rolled
   valid GLB cube) is what actually runs until someone watches a real one.
9. **Auth-failure detection is a string heuristic** (401/403/credential/scope
   in `colab new` output). A provision failure mentioning "permission" could be
   misfiled as auth — the error always carries the CLI's own output tail for a
   human to overrule.
10. **`train.py`'s loop is real but stub-scale** (char-level tiny transformer,
    one window per step). The proven per-game jevlike trainer plugs in at the
    `PROVEN PLUG-IN` marker without touching the worker contract.

## Verify (no network, no Colab account needed)

```bash
python cloud/colab/colab_worker.py --selftest
```

Fakes the `colab` CLI with a stub shim (session lifecycle, real file
up/downloads, synchronous exec) and runs one `gpu.train` job end-to-end:
staging zip -> provision -> upload -> exec -> artifact downloads -> teardown,
plus the auth-failure path asserting the `{"error": "auth_failed",
"hint": "run colab auth"}` shape.

## Research sources (2026-09-23)

- Repo + README: https://github.com/googlecolab/google-colab-cli
- Auth deep dive: https://github.com/googlecolab/google-colab-cli/blob/main/docs/04_automation_and_utility.md
- MCP server (in-notebook agents, not used by this lane): https://github.com/googlecolab/colab-mcp
