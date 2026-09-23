# GMP v2 — the Pipeline, fully self-hosted on retromonkey (Forgejo)

Status: plan (not yet implemented). Owner: slothitude. Written 2026-09-23.

The Pipeline today leans on GitHub for four things: git hosting, Actions CI (the brain
loops), Pages (game hosting), and API dispatch (pipeline-poll → workflow_dispatch).
GitHub Actions rate limits now throttle the factory. GMP v2 moves all four onto
retromonkey (Oracle free tier) using **Forgejo** (git + Actions-compatible CI) and a
**long-lived agent daemon** (the brain loop, no container spin-up latency). GitHub
demotes to a one-way push mirror: backup + whatever still points at it.

Nothing else changes: the brain chain (glm-5.3 → kimi-k3 → openrouter/free), the gate
wall (2x, all suites), the complete-file law, autonomy tiers, git-as-database
runtime state, Telegram intake, itch publishing, the daily remake at 10:23 UTC.

## 1. Goals and non-goals

Goals:

1. Code host on retromonkey: `https://retromonkey.com.au/git/<owner>/<repo>` via Forgejo.
2. CI on retromonkey: Forgejo Runner (act_runner) executing the existing workflows
   (build-milestone, edit-fork, games-ci, new-game, itch-publish) with minimal edits.
3. The agent loop becomes a **persistent Python process** (gmp-agent): Telegram poll →
   work order → brain → gates → deploy → playtest → notify, all in one process.
   No per-job container cold start, no 30-minute Actions poll delay, stateful between
   iterations (the brain chain's conversation context can survive across attempts).
4. Automated playtesting: after each deploy the agent drives an Android emulator on
   retromonkey (KVM), installs the APK (phase 2) or loads the web build (phase 1),
   and PlayerOne (vision brain) plays it and files a critique into `runtime/feedback/`.
5. GitHub = mirror only (push mirror from Forgejo; nothing of ours *runs* there).
6. Zero new spend. Everything fits the 956 MB + 4 GB swap box with a serialization law.

Non-goals (this round):

- Multi-runner clusters, org-level runner pools, LFS, container registry, packages.
- PostgreSQL/MySQL (SQLite is correct at this scale; see §3.2).
- Migrating anything except slothitude repos (no third-party users on the instance).
- The PLATFORM v2 hub front door (P7 in PLAN.md) — GMP v2 is its substrate, not it.
- Replacing Rog as the dev seat. Claude Code on Rog stays the way the owner works;
  it just gains a second remote (`retromonkey` git remote instead of `origin`).

## 2. Architecture

Everything lives on retromonkey. Nothing runs on GitHub any more.

```
                ┌────────────────────────────────────────────────────────────────────┐
                │ retromonkey — Oracle free tier, 168.138.8.0                        │
                │ 956 MB RAM + 4 GB swap · 39 GB disk · Ubuntu 22.04 · Docker 29.5.3 │
                │                                                                    │
 Telegram API ◀─┼─ getUpdates/ ┐                                     ports: 80, 443  │
 (owner + players)            │                                          │         │
                │      ┌──────┴──────┐                          ┌────────▼───────┐ │
                │      │  gmp-agent  │                          │     caddy      │ │
                │      │  (daemon)   │  git push (runtime/)     │ (existing)     │ │
                │      │ tg-poll     │──────────┐               └───┬────┬───┬───┘ │
                │      │ work queue  │          │                   │    │   │     │
                │      │ scheduler   │          │            /git/* │    │   │ /drive/*
                │      └──┬───┬───┬───┘          │                   │    │   │  (existing)
                │         │   │   │              ▼                   │    │   │
                │         │   │   │        ┌───────────┐  git+api  │    │   │
                │         │   │   └───────▶│  forgejo  │◀──────────┘    │   │
                │         │   │            │  SQLite   │                │   │
                │         │   │            │ /data     │──push mirror──▶ GitHub
                │         │   │            └─────▲─────┘   (one-way,     (mirror,
                │         │   │                  │        https)          backup)
                │         │   │                  │ register + fetch jobs  │
                │         │   │            ┌─────┴───────┐                │
                │         │   │            │    runner   │ capacity=1     │
                │         │   │            │ (act_runner)│ labels: godot  │
                │         │   │            └─────┬───────┘                │
                │         │   │                  │ spawns job container   │
                │         │   │                  ▼                        │
                │         │   │        ┌──────────────────┐               │
                │         │   └───────▶│ godot-ci:4.7.1   │  gate wall,   │
                │         │            │ (job container)  │  export,      │
                │         │            │ godot --headless │  butler push  │
                │         │            └──────────────────┘               │
                │         │                                               │
                │         ▼                                               │
                │  ┌─────────────────┐  adb   ┌──────────────────────┐    │
                │  │ emulator        │◀──────▶│ gmp-agent (playtest  │    │
                │  │ Android + KVM   │        │ thread): screenshot  │    │
                │  │ /dev/kvm        │        │ → vision brain → tap │    │
                │  └────────┬────────┘        └──────────────────────┘    │
                │           │ WebView                                     │
                │           ▼                                             │
                │  https://retromonkey.com.au/games/<slug>/  (phase 1)    │
                │  or installed APK /data/local (phase 2)                 │
                └────────────────────────────────────────────────────────┘
                     heavy ops serialized: ONE gate wall OR ONE emulator, never both

 external, unchanged: NVIDIA integrate.api.nvidia.com (glm-5.3, kimi-k3),
 openrouter/free (takeover + vision), itch.io (butler push + comments API),
 Telegram (intake + notify). All free tiers.
```

Component inventory (new vs existing):

| Component | Status | Notes |
|---|---|---|
| caddy | existing, +1 route | `/git/*` → `forgejo:3000` over docker network `web` |
| filebrowser | existing | untouched |
| forgejo | new | git host, SQLite, no host port (internal 3000) |
| runner | new | act_runner in Docker, capacity=1, godot label |
| gmp-agent | new (absorbs gmp-bot) | persistent daemon, owns the Telegram getUpdates loop |
| emulator | new, on demand | starts for playtests, stops after (RAM law) |
| gmp-bot | retired | its token moves to gmp-agent (two pollers on one token 409-conflict) |
| GitHub | demoted | push mirror target + legacy clone URL only |

## 3. Key decisions (and what was rejected)

### 3.1 Forgejo, not Gitea

Forgejo is the actively maintained fork (Codeberg-backed, v15 docs current, audited
runner), Gitea is the upstream it forked from when Gitea Ltd went commercial. Same
API surface, same workflow syntax, migration tooling identical. Lappy already runs
Forgejo, so tooling familiarity is free. **Rejected: Gitea** (no benefit, less
maintenance energy behind it); **rejected: bare Gogs** (no Actions).

### 3.2 SQLite, not PostgreSQL

Single instance, single writer-ish (Forgejo), tiny repo count (7 repos), no concurrent
load. Forgejo's own docs bless SQLite for small deployments and a 512 MB VPS — that is
our envelope exactly. SQLite is one file in `/data` → trivial backup (rsync the file),
trivial restore. **Rejected: Postgres** — a second always-on daemon costs 50–100 MB of
the 956 MB for zero benefit at this scale. **Trigger to revisit**: if the Forgejo
process RSS sits above ~250 MB steady-state, or action-log writes visibly contend
(`database is locked` in logs), move to Postgres. `app.ini` stays portable; the
migration is Forgejo's built-in `dump`/restore or the DB config swap + `gitea doctor`.

### 3.3 Migration order: game-making-pipeline first, then games one by one

`game-making-pipeline` moves first and goes **live** (not a mirror) immediately: it is
the keystone (workflows, runtime/ state, schemas) and the thing rate limits actually
hurt. Its GitHub side becomes a push mirror. Then the six game repos
(octogram-arcade, sonar, slime-line, star-visitor, gyro-squadron-45, cubefall)
migrate one at a time, each: migrate → gate-wall green on Forgejo → deploy path
re-pointed → GitHub flipped to push mirror. **Rejected: big-bang all repos at once**
(one bad migration would stall the whole factory); **rejected: pipeline stays on
GitHub forever** (rate limits are the reason this plan exists).

### 3.4 Telegram dispatches the daemon directly, not through Actions

Today: bot writes `runtime/orders/*.json` → pipeline-poll runs every 30 min on
GitHub → jq loop → `curl api.github.com/.../dispatches`. v2: the **daemon is the
Telegram poller** (long-lived `getUpdates`, the exact loop `cloud_editor.py` already
runs in Rog mode) and calls its own work functions directly. No dispatch API, no
30-minute latency, no second queue semantics. Actions still exists — but only for
things that are *naturally* CI (games-ci on push, itch-publish on deploy-branch push,
manual workflow_dispatch from the Forgejo UI). **Rejected: daemon → Forgejo API →
workflow_dispatch → runner → container → godot** for brain work (that is precisely
the spin-up latency and rate-limit shape we are escaping; the daemon runs the same
Python inline instead). The Forgejo-dispatch path remains as the §11 fallback.

### 3.5 Daily remake: host cron, not Actions cron

GitHub's `"23 10 * * *"` schedule becomes a real cron entry on retromonkey:

```
23 10 * * * docker exec gmp-agent python3 /app/daily/daily_remake.py >> /home/ubuntu/gmp/logs/daily.log 2>&1
```

Off-minute by law (23), UTC, same backlog-selection code, same diary. Host cron beats
a `sleep 1800` shell loop (the v1 compose hack) because it survives daemon restarts
without double-firing and is visible in `crontab -l`. The daemon also exposes the same
entry point internally so a manual Telegram "remake X today" uses identical code.

### 3.6 itch.io and webhooks point at Forgejo

itch publishing (butler push) already happens *from* CI; it keeps happening, just from
a Forgejo-triggered run: a push to any game's `deploy` branch fires Forgejo's
push-event workflow (itch-publish.yml, nearly unchanged). Forgejo's **outbound**
webhooks (push/release/tag) replace every GitHub outbound webhook we had. Any external
service that was pointed at a GitHub webhook endpoint gets re-pointed at the Forgejo
repo's webhook URL (`/git/<owner>/<repo>/hooks/...` is inbound-only for *Forgejo's*
own UI config — external inbound automation, if ever needed, lands on the daemon
instead; nothing currently needs it: itch feedback is **polled** by
`daily/itch_feedback.py`, which moves into the daemon unchanged). GitHub still
receives push mirrors, so any GitHub-side webhook we forgot about keeps firing.

### 3.7 The serialization law (one heavy op at a time)

Peak RAM math (§4) shows gate wall + emulator together do not fit 956 MB. So:

- `runner` `capacity: 1` — at most one Actions job container exists, ever.
- The daemon takes an exclusive `flock` on `/app/runtime/.heavy.lock` around its own
  gate walls, exports, and playtests.
- Emulator container is **started for the playtest and stopped after**
  (`docker compose --profile playtest up -d emulator && ... && --profile playtest down`),
  never `--restart unless-stopped`.
- The work queue is therefore **serial by construction**: one work order advances at a
  time; the rest wait in `runtime/orders/` with status pending. "Multiple games in
  parallel" at v2 means *no idle spin-up between games*, not simultaneous godot
  processes. If parallelism is ever wanted, it wants a second Oracle instance, not
  swap Roulette.

### 3.8 Deploys land on disk, not Pages

Caddy already serves `/games/<slug>/` straight from `/home/ubuntu/site/games/` —
Pages becomes redundant. The export step ends in `scp -r` (or a runner bind-mount +
copy, §7.3) into that directory. URLs change from
`slothitude.github.io/<game>/` to `retromonkey.com.au/games/<slug>/`; `new-game.yml`'s
hardcoded Pages URL and the Telegram reply text are the only string edits.

## 4. Resource budget (the 956 MB question)

Measured/estimated RSS at steady state and at peak:

| Component | Idle | Active | Always on? |
|---|---|---|---|
| OS + dockerd (existing baseline) | ~200 MB | — | yes |
| caddy | ~30 MB | ~40 MB | yes |
| filebrowser | ~40 MB | ~60 MB | yes |
| forgejo (SQLite, idle) | ~80 MB | ~150 MB (git push, web UI) | yes |
| runner daemon (capacity=1, no job) | ~30 MB | ~50 MB | yes |
| gmp-agent (python, tg long-poll) | ~50 MB | ~80 MB | yes |
| job container: godot-ci + `godot --headless` gate wall | — | ~250–400 MB | on demand |
| emulator (KVM Android, one AVD, WebView) | — | ~500–700 MB | on demand |
| **Idle total** | **~430 MB** | | |
| **Gate wall peak** | | **~800–900 MB** | fits |
| **Playtest peak** | | **~1.05–1.2 GB** | over → swap |
| **Gate wall + playtest together** | | **~1.4–1.6 GB** | forbidden (§3.7) |

Mitigations, in order:

1. **Serialization law** (§3.7) — capacity=1 + flock + emulator on demand. This is the
   whole answer; the rest is headroom.
2. 4 GB swap already present. Verify it is actually swap-on-disk with sane
   priority: `swapon --show`. Optional: `vm.swappiness=10` so the box prefers
   reclaiming page cache (godot_ci image pages) before swapping the daemon.
3. Forgejo tuning for small RAM in `app.ini`: `[server] DISABLE_SSH`, graceful
   timeouts low, `[actions] LOG_RETENTION_DAYS = 14`, `ARTIFACT_RETENTION_DAYS = 3`
   (also protects the 39 GB disk — action logs accumulate per run).
4. Cache warmers are forbidden: do NOT keep a hot godot container or a resident
   emulator "to save time". Cold start is the RAM budget's friend (godot gate wall
   cold start ≈ seconds; emulator cold boot ≈ 30–60 s with KVM — acceptable inside a
   15-minute loop).
5. If the daemon's RSS creeps (long brain conversations), it caps retained
   conversation context (last 2 attempts, per existing `messages` trimming) and logs
   its own RSS each loop to the journal.

Disk budget (39 GB): godot-ci image ~1.1 GB, node:20 ~0.4 GB, forgejo ~0.2 GB,
runner ~0.03 GB, emulator system image + SDK ~6–8 GB (phase F only), repos + exports
+ Forgejo data < 2 GB. Comfortable.

## 5. Forgejo setup

### 5.1 Container

```bash
# on retromonkey
sudo mkdir -p /home/ubuntu/forgejo && sudo chown 1000:1000 /home/ubuntu/forgejo
# uid gotcha (retromonkey.md): the container's internal git user is 1000,
# host user ubuntu is 1001 — chown the data dir to 1000:1000 like filebrowser's.

docker run -d --name gmp-forgejo \
  --restart unless-stopped \
  --network web \
  -e USER_UID=1000 -e USER_GID=1000 \
  -v /home/ubuntu/forgejo:/data \
  codeberg.org/forgejo/forgejo:15   # pin the exact tag at install time
```

No host port. It is reachable only through caddy (and by the runner over the `web`
network). Admin user created on first visit via `ROOT_URL/install`, then:

```bash
docker exec -u 1000 gmp-forgejo forgejo admin user create \
  --admin --username slothitude --password "<set>" --email <set> \
  --must-change-password=false
```

### 5.2 `app.ini` essentials (mounted at `/data/gitea/conf/app.ini`)

```ini
[server]
DOMAIN       = retromonkey.com.au
ROOT_URL     = https://retromonkey.com.au/git/
HTTP_ADDR    = 0.0.0.0
HTTP_PORT    = 3000
DISABLE_SSH  = true            # HTTPS-only git to start (see below)
ROOT         = /data/git
PROTOCOL     = http            # TLS terminates at caddy

[database]
DB_TYPE = sqlite3
PATH    = /data/forgejo.db

[service]
DISABLE_REGISTRATION = true   # single-tenant instance
REQUIRE_SIGNIN_VIEW  = false   # public game repos stay browsable

[actions]
ENABLED                = true
DEFAULT_ACTIONS_URL    = https://data.forgejo.org
LOG_RETENTION_DAYS     = 14
ARTIFACT_RETENTION_DAYS = 3

[webhook]
ALLOWED_HOST_LIST = external   # itch/telegram not needed; default is fine
```

Subpath note: `ROOT_URL` with the `/git/` suffix is natively supported (Forgejo
generates subpath-aware URLs; caddy proxies the prefix straight through). Git smart
HTTP works under the subpath:
`git clone https://retromonkey.com.au/git/slothitude/game-making-pipeline.git`.

**Git-over-SSH (optional, later):** HTTPS-only first. If the owner wants SSH remotes,
run Forgejo's embedded server on host port 2222 (`-p 2222:22`, `DISABLE_SSH=false`,
`SSH_PORT=2222`) — port 22 on the host is the real sshd and stays untouched.

### 5.3 Caddy route (the only edit to existing infra)

```
# /home/ubuntu/caddy/Caddyfile — inside the existing site block
handle_path /git/* {
    reverse_proxy forgejo:3000
}
```

Container-to-container over the `web` network, same law as `/drive`. Note
`handle_path` (strips `/git`) vs `handle` (keeps it): Forgejo expects the stripped
form when `ROOT_URL` carries the prefix. Then `sudo docker restart caddy`.

### 5.4 Post-setup checklist

- Disable open registration (done in app.ini above); owner account gets 2FA.
- Create org `slothitude` (keeps repo paths identical to GitHub: `/git/slothitude/<repo>`).
- Forgejo secrets are set **per repo** in Settings → Actions → Secrets (§10).
- Backups: nightly cron `sqlite3 /data/forgejo.db ".backup ..."` + rsync of
  `/home/ubuntu/forgejo` to `/home/ubuntu/cloud/` (filebrowser drive) — one line,
  zero new infra.

## 6. Repository migration (GitHub → Forgejo, GitHub as mirror)

### 6.1 The mechanism

Forgejo migrates **from** GitHub natively (issues, PRs, releases, labels, wiki,
milestones) via UI or API:

```bash
curl -X POST "https://retromonkey.com.au/git/api/v1/repos/migrate" \
  -H "Authorization: token $FORGEJO_TOKEN" -H "Content-Type: application/json" \
  -d '{
    "clone_addr": "https://github.com/slothitude/game-making-pipeline.git",
    "auth_token": "<GITHUB_PAT>",
    "service": "github",
    "repo_name": "game-making-pipeline",
    "repo_owner": "slothitude",
    "mirror": false,
    "private": false,
    "issues": true, "pull_requests": true, "releases": true, "milestones": true
  }'
```

(`mirror: false` = a real, live repo. `mirror: true` would make a read-only pull
mirror — wrong direction here.)

### 6.2 GitHub becomes a push mirror

After each repo goes live on Forgejo, add a one-way push mirror so GitHub stays a
current backup (and any forgotten GitHub webhook keeps working):

```bash
curl -X POST "https://retromonkey.com.au/git/api/v1/repos/slothitude/game-making-pipeline/push_mirrors" \
  -H "Authorization: token $FORGEJO_TOKEN" -H "Content-Type: application/json" \
  -d '{
    "remote_name": "github-mirror",
    "url": "https://slothitude:<GITHUB_PAT>@github.com/slothitude/game-making-pipeline.git",
    "interval": "8m0s",
    "sync_on_commit": true
  }'
```

GitHub's own Actions are then **left configured but dormant** (workflows stay in
`.github/workflows`, secrets stay set) — that is the §11 fallback, free of charge.
Do not archive the GitHub repos while the fallback is live.

### 6.3 Workflow disposition (the 7 existing workflows)

| Workflow | Disposition |
|---|---|
| `build-milestone.yml` | moves to `.forgejo/workflows/`, edits per §7.2 (runs-on, uses, URLs) |
| `edit-fork.yml` | same, and its dispatch call moves from curl → daemon function |
| `games-ci.yml` | same; becomes the push-triggered CI on every game repo's main |
| `new-game.yml` | same; Pages URL constant → `https://retromonkey.com.au/games/<slug>/`; scp deploy |
| `itch-publish.yml` | same; trigger changes to push on `deploy` branch (Forgejo push event) |
| `daily-remake.yml` | **retires** → host cron (§3.5); the yaml stays as manual-trigger docs |
| `pipeline-poll.yml` | **retires** → the daemon *is* this loop (§8); keep the file for §11 |

### 6.4 Order of operations per repo

1. Migrate (6.1), live repo, `.github/workflows/` still inside it.
2. On Forgejo: set repo secrets, enable Actions, runner picks it up (labels §7.1).
3. Run `games-ci` once via `workflow_dispatch` → gate wall green on Forgejo infra.
4. Edit deploy targets (Pages URL → retromonkey URL, scp not gh-pages).
5. Add the GitHub push mirror (6.2). Verify a test commit lands on GitHub within
   the mirror interval.
6. Only then move to the next repo.

`game-making-pipeline` additionally needs: `runtime/` state carried over (it is in
git — migration brings it), and `GAMES_TOKEN` on GitHub replaced by `FORGEJO_TOKEN`
for the daemon.

## 7. Forgejo Runner (act_runner)

### 7.1 Container + registration

```bash
docker run -d --name gmp-runner \
  --restart unless-stopped \
  --network web \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/ubuntu/runner-data:/data \
  -v /home/ubuntu/runner-config:/config \
  codeberg.org/forgejo/runner:PIN   # pin exact tag from code.forgejo.org/forgejo/runner at install

# one-time registration (token from Forgejo: Site admin → Actions → Runners → Create)
docker exec gmp-runner forgejo-runner register \
  --instance https://retromonkey.com.au/git \
  --token "<REGISTRATION_TOKEN>" \
  --name retromonkey-1 \
  --labels 'godot:docker://barichello/godot-ci:4.7.1,node:docker://node:20-bookworm'
```

Labels are the minimum required config and are **chosen here**, not in the workflow:
`runs-on: godot` in a workflow matches the label, and the label carries the container
image. The docker socket mount lets the runner spawn job containers on the host
Docker (this is the standard runner deployment; it is remote-code-execution by design
— acceptable because the instance is single-tenant, §10).

### 7.2 What actually changes in the workflows (the "minimal edits" list)

Forgejo Actions is GitHub-Actions-familiar but not byte-compatible. Per workflow:

1. **Directory**: create `.forgejo/workflows/` (Forgejo reads it; if absent it falls
   back to `.github/workflows` — which is how step-2 smoke tests can run *before* any
   edits. Deliberately move files to `.forgejo/workflows/` anyway so GitHub's dormant
   copies and Forgejo's live copies diverge visibly instead of silently).
2. **`runs-on`**: `ubuntu-latest` → `godot` (the label above). Jobs that don't need
   Godot use `node`.
3. **`container:` blocks removed** — the label's image already provides godot-ci.
   (`docker://` job-level containers do exist in act_runner, but label-imaging is
   simpler and one source of truth.)
4. **`uses:`**: `actions/checkout@v4` → `https://data.forgejo.org/actions/checkout@v4`
   (or rely on `DEFAULT_ACTIONS_URL`; explicit is safer). Most of our workflows
   barely use actions — build-milestone does its git by hand — so most files need
   none of these.
5. **Contexts**: `${{ github.* }}` mostly works; `${{ github.token }}` →
   `${{ secrets.GITHUB_TOKEN }}` (Forgejo auto-provides an instance token) or, for
   cross-repo checkout, our own `CROSS_REPO_TOKEN` PAT secret (replaces today's
   `GAMES_TOKEN` usage inside build-milestone's checkout step).
6. **GitHub API calls**: `pipeline-poll`'s dispatch curls and `gh` invocations are
   deleted (daemon does it), and `new-game.yml`'s Pages push becomes scp.
7. **Env/steps**: `GITHUB_OUTPUT`, `GITHUB_ENV`, matrix, `workflow_dispatch` inputs
   and cron `schedule:` are supported as-is.

### 7.3 Runner concurrency + the deploy mount

`/home/ubuntu/runner-config/config.yml` (generated with
`forgejo-runner generate-config`, then edited):

```yaml
runner:
  capacity: 1          # THE serialization law (§3.7)
  timeout: 30m
  labels: []           # labels live in the registration; keep in sync
container:
  valid_volumes:
    - /home/ubuntu/site   # lets a job cp exports straight onto the live site
```

With `valid_volumes`, `games-ci`'s final step is:

```yaml
- name: Deploy
  run: |
    mkdir -p /site/games/${GAME_SLUG}
    cp -r export/web/* /site/games/${GAME_SLUG}/
```

(no ssh key needed; caddy serves it instantly — the exact pattern retromonkey.md
already documents). Files land root-owned and world-readable; caddy only reads.
The `scp` variant with a `DEPLOY_SSH_KEY` secret is the fallback if bind-mount
semantics fight the runner version.

## 8. The agent loop daemon (gmp-agent)

### 8.1 What it is

One always-running Python process, built from the existing
`cloud/pipeline/Dockerfile` (python:3.12-slim + Godot 4.7.1 headless + the pipeline
code, stdlib only). It absorbs: `gmp-bot` (Telegram intake), `pipeline-poll.yml`
(work-order detection + dispatch), `daily-remake.yml` (cron trigger), and the inline
Python brain/gates code from `build-milestone.yml` / `edit-fork.yml` / `new-game.yml`.

**This is the "real agent loop" of the brief**: persistent process, no per-job
container spin-up (godot runs as a subprocess in the daemon container for brain work;
the runner path stays for CI-shaped things), stateful between iterations
(conversation context, per-game ledger), multiple games flow through one serial queue
with no idle cost.

### 8.2 Threads

```
main
 ├─ tg_poll()        — long-lived getUpdates (the Rog-mode loop cloud_editor.py already has);
 │                     owns TG_TOKEN exclusively (gmp-bot is retired — two pollers 409)
 ├─ work_queue       — consumes runtime/orders/*.json + runtime/feedback/*.json (status pending),
 │                     one at a time, flock(.heavy.lock) around heavy sections
 ├─ scheduler        — next-fire computation for 10:23 UTC daily remake (cron remains the
 │                     primary trigger; this is the in-process equivalent)
 └─ runtime_committer— every 60 s, if runtime/ is dirty: git add runtime/ && commit && push
                       to Forgejo ("git is the database", now latency-bounded by the
                       daemon, not a 30-minute Actions schedule)
```

### 8.3 The work-order state machine (per item)

```
pending → planning → brain_writing → gating ──green──▶ exporting → deploying
                ▲              │                          │
                │              └──red (≤3 attempts)──▶ failing → notify owner (T3 law:
                └──────────────────┘                     3 red gate walls → human)
                                            deploying → playtesting → notifying → done
```

- `planning`: builds the context packet exactly as build-milestone.yml does today
  (spec json, file list, feel.gd head, test list) — same prompts, same complete-file
  law, same ```path:file.gd``` parsing regex.
- `gating`: `godot --headless --import`, then every `tests/*.gd` SceneTree suite
  **twice**, `Ran N checks: X passed, 0 failed` summaries, under the heavy lock.
  Same 7-suite wall for arcade (run_tests, run_rpg_tests, smoke_battle, smoke_menu,
  run_e2e, run8_tests, run8_e2e). This is where v1's container spin-up latency used
  to be paid per job; now it is a subprocess in a warm process.
- `exporting`: Web export (phase F adds Android), then deploy to
  `/home/ubuntu/site/games/<slug>/` (bind-mounted into the daemon container).
- `playtesting`: §9.
- `notifying`: Telegram reply with the live URL; failure notices to both chat and
  owner channel, exactly as new-game.yml does today.
- `done`: order JSON flipped to done, journal append, runtime/ commit.

### 8.4 What the daemon does NOT do

- It does not run Actions jobs (the runner does) and does not reimplement the runner.
  When a request is *naturally* CI-shaped (a push landed, run the whole gate wall +
  export + itch), the daemon may hand it to Forgejo via `workflow_dispatch` — the
  dispatch curl from pipeline-poll.yml, re-targeted at
  `/git/api/v1/repos/slothitude/<repo>/actions/workflows/<wf>/dispatches` (Forgejo's
  API path), keeping the §11 fallback exercised and honest.
- It does not hold secrets in env at rest beyond what v1 already did
  (TG_TOKEN, NVAPI_KEY, OPENROUTER_KEY, CHAT_ID + new FORGEJO_TOKEN); masked `<set>`
  everywhere in logs, orders, and journal (existing law).

### 8.5 Compose service (excerpt; full file §12)

```yaml
  agent:
    build: ./pipeline            # same Dockerfile, now the daemon image
    container_name: gmp-agent
    restart: unless-stopped
    environment:
      TG_TOKEN: ${TG_TOKEN}
      NVAPI_KEY: ${NVAPI_KEY}
      OPENROUTER_KEY: ${OPENROUTER_KEY}
      CHAT_ID: ${CHAT_ID}
      FORGEJO_TOKEN: ${FORGEJO_TOKEN}
      FORGEJO_URL: https://retromonkey.com.au/git
      GODOT_BIN: /usr/local/bin/godot
      RUNTIME_DIR: /app/runtime
      SITE_DIR: /site
      CLOUD: "1"
    volumes:
      - pipeline_state:/app/runtime
      - /home/ubuntu/site:/site          # deploy target, read-write
      - /home/ubuntu/gmp-logs:/app/logs
    networks: [web]
```

## 9. PlayerOne + emulator playtest loop

### 9.1 Phase 1 — web build in the emulator's browser (ship this first)

No Android SDK, no keystore, no Gradle: the emulator's WebView/Chrome loads
`https://retromonkey.com.au/games/<slug>/`. Zero new toolchain, exercises the exact
build players get.

### 9.2 Phase 2 — APK install (the brief's end state)

Add the Android export template + SDK to a *side* image (not the gate-wall image):
`godot --headless --export-release "Android"` with a debug keystore generated once
and stored in a volume; job/daemon installs via `adb install -r`. Disk cost ~6–8 GB
(SDK + system image) is the main price; RAM cost is the emulator, not the SDK.

### 9.3 The loop (daemon `playtesting` state)

```
1. docker compose --profile playtest up -d emulator     # KVM Android, 127.0.0.1:5555 adb
2. adb wait-for-device && wait for sys.boot_completed
3. phase 1: am start -a android.intent.action.VIEW -d <game URL>
   phase 2: adb install -r build/<slug>.apk && am start -n <pkg>/.GodotApp
4. for frame in 1..N:                       # N ≈ 20–40, ~10–15 s cadence
     adb exec-out screencap -p > /tmp/f<frame>.png
     critique = vision_brain(screenshot, task card)     # openrouter/free vision model
     adb input tap X Y | input swipe ...                # from critique["next_action"]
5. critique_json → runtime/feedback/<ts>.json (status pending, meta.game=<slug>)
   — the SAME file shape a player's Telegram feedback produces, so the ordinary
   edit-fork loop picks it up next iteration. PlayerOne is just another player.
6. Telegram: "PlayerOne played <slug>: <2-line verdict>" + link
7. docker compose --profile playtest down               # RAM back (§3.7)
```

### 9.4 Emulator container

KVM-accelerated (`/dev/kvm` is available on the box). Two viable shapes; pick at
implementation by a pre-flight probe:

- **redroid** (`redroid/redroid:14.0.0-latest`): Android-in-Docker, lightest RAM.
  Requires binder modules on the host: probe
  `sudo modprobe binder_linux devices="binder,hwbinder,vndbinder"` and check
  `/dev/binderfs`; Ubuntu 22.04 generic kernels usually have it in
  `linux-modules-extra`.
- **official emulator** headless in a container
  (`emulator -avd pixel -no-window -gpu swiftshader_indirect -no-audio
  -no-boot-anim` with `/dev/kvm` mounted): heavier, but zero kernel-module risk.

Either way: `--restart no`, profile `playtest`, adb port loopback-only. Acceptance
GMP-B9 decides redroid-first, emulator-fallback (recorded in the runbook).

PlayerOne vision calls ride the existing brain-chain shape (image-capable model via
openrouter/free first, NVIDIA fallback), under the same free-tier/no-spend law.

## 10. Secrets inventory (v2)

| Secret | Where it lives | Used by |
|---|---|---|
| `TG_TOKEN`, `CHAT_ID` | daemon env (compose .env) | intake + notify |
| `NVAPI_KEY`, `OPENROUTER_KEY` | daemon env | brain chain + vision |
| `FORGEJO_TOKEN` | daemon env + Forgejo repo secrets | daemon API calls; mirror admin |
| `CROSS_REPO_TOKEN` | Forgejo repo secrets | workflows checking out sibling repos (replaces in-workflow `GAMES_TOKEN`) |
| `BUTLER_API_KEY` | Forgejo repo secrets (game repos) | itch-publish |
| `DEPLOY_SSH_KEY` | Forgejo repo secrets (only if scp variant chosen, §7.3) | deploy step |
| `GITHUB_PAT` | Forgejo push-mirror config | the mirror's push to GitHub |

GitHub's secret store keeps its copies (dormant fallback). The FileBrowser admin
credential stays in retromonkey.md only — never in this plan, in compose, or in any
order/schema/log (masking law).

## 11. Fallback plan (revert to GitHub Actions)

The escape hatch is kept warm on purpose:

- GitHub repos are **push-mirrored continuously** (§6.2), so code and `runtime/` state
  on GitHub are at most ~8 minutes stale. Nothing needs "restoring" — it is already there.
- `.github/workflows/` files remain in every repo with their secrets intact. New-game,
  edit-fork, build-milestone, games-ci, itch-publish are all `workflow_dispatch`, so
  the fallback needs no code changes: re-enable `pipeline-poll.yml`'s schedule on
  GitHub (it was never deleted), and dispatches flow again within 30 minutes.
- The only real regressions to re-point manually: Pages deploy steps (un-comment the
  gh-pages push in new-game.yml / games-ci) and the Pages URLs in Telegram replies.
- Triggering the fallback = stop the new containers
  (`docker compose stop agent runner`) and push a commit to GitHub directly (bypass
  the mirror) or flip a repo from push-mirror back to live.

Drill: GMP-B12 (§13) actually exercises this once per quarter — fall back for one
deliberate run, then return.

## 12. Docker Compose addition (final draft)

New file `cloud/docker-compose.v2.yml` (v1's stays for reference during migration):

```yaml
services:
  # === GIT + CI ===
  forgejo:
    image: codeberg.org/forgejo/forgejo:15            # pin exact tag at install
    container_name: gmp-forgejo
    restart: unless-stopped
    environment:
      - USER_UID=1000
      - USER_GID=1000
      - FORGEJO__server__DOMAIN=retromonkey.com.au
      - FORGEJO__server__ROOT_URL=https://retromonkey.com.au/git/
      - FORGEJO__server__DISABLE_SSH=true
      - FORGEJO__database__DB_TYPE=sqlite3
      - FORGEJO__database__PATH=/data/forgejo.db
      - FORGEJO__service__DISABLE_REGISTRATION=true
      - FORGEJO__actions__ENABLED=true
      - FORGEJO__actions__DEFAULT_ACTIONS_URL=https://data.forgejo.org
      - FORGEJO__actions__LOG_RETENTION_DAYS=14
      - FORGEJO__actions__ARTIFACT_RETENTION_DAYS=3
    volumes:
      - /home/ubuntu/forgejo:/data
    networks: [web]

  runner:
    image: codeberg.org/forgejo/runner:PIN            # pin exact tag at install
    container_name: gmp-runner
    restart: unless-stopped
    depends_on: [forgejo]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /home/ubuntu/runner-data:/data
      - /home/ubuntu/runner-config:/config
    networks: [web]
    # capacity: 1 lives in /home/ubuntu/runner-config/config.yml (§7.3)

  # === THE AGENT LOOP ===
  agent:
    build: ./pipeline
    container_name: gmp-agent
    restart: unless-stopped
    environment:
      - TG_TOKEN=${TG_TOKEN}
      - NVAPI_KEY=${NVAPI_KEY}
      - OPENROUTER_KEY=${OPENROUTER_KEY}
      - CHAT_ID=${CHAT_ID}
      - FORGEJO_TOKEN=${FORGEJO_TOKEN}
      - FORGEJO_URL=https://retromonkey.com.au/git
      - GODOT_BIN=/usr/local/bin/godot
      - RUNTIME_DIR=/app/runtime
      - SITE_DIR=/site
      - CLOUD=1
    volumes:
      - pipeline_state:/app/runtime
      - pipeline_repo:/app/games
      - /home/ubuntu/site:/site
      - /home/ubuntu/gmp-logs:/app/logs
    networks: [web]

  # === PLAYTEST (on demand only — never always-on, §3.7) ===
  emulator:
    image: redroid/redroid:14.0.0-latest              # or the headless-emulator image
    container_name: gmp-emulator
    profiles: [playtest]
    privileged: true
    command: >
      androidboot.redroid_width=540 androidboot.redroid_height=960
      androidboot.redroid_dpi=280 androidboot.use_memfd=1
    volumes:
      - /dev/kvm:/dev/kvm                             # redroid ignores this; emulator needs it
    ports:
      - "127.0.0.1:5555:5555"
    networks: [web]

volumes:
  pipeline_state:
  pipeline_repo:

networks:
  web:
    external: true
```

And the host crontab line (§3.5):

```
23 10 * * * docker exec gmp-agent python3 /app/daily/daily_remake.py >> /home/ubuntu/gmp-logs/daily.log 2>&1
```

## 13. Deployment steps (ordered)

Phase A — Forgejo on retromonkey (no cutover yet):

1. Create `/home/ubuntu/forgejo` (chown 1000:1000), run the forgejo container (§5.1).
2. Admin user, org `slothitude`, disable registration, 2FA.
3. Caddyfile `handle_path /git/*` route + `docker restart caddy`.
4. Verify: browse `https://retromonkey.com.au/git/`, create a scratch repo, clone/push
   over HTTPS from Rog.

Phase B — runner:

5. Runner container + registration with labels `godot`, `node` (§7.1).
6. `config.yml` with `capacity: 1` + `valid_volumes` (§7.3).
7. Verify: scratch repo with a 5-line workflow `runs-on: godot` running
   `godot --version` green.

Phase C — game-making-pipeline cutover (the keystone):

8. Migrate live (§6.1), set repo secrets (§10).
9. Smoke: run the migrated workflows via `workflow_dispatch` from the Forgejo UI;
   first `.github/` (fallback pickup), then the `.forgejo/` copies with §7.2 edits.
10. Add the GitHub push mirror; verify a commit round-trips.
11. Switch the daemon's git remote to Forgejo; build the `gmp-agent` image from the
    existing Dockerfile; start it (it takes over TG_TOKEN — **stop gmp-bot first**,
    same getUpdates token would 409-conflict).
12. Add the host cron line; delete the sleep-loop daily container if v1 compose ever ran.
13. Point `new-game.yml` (and its Telegram reply strings) at
    `https://retromonkey.com.au/games/<slug>/`; verify one order end-to-end
    (Telegram → order → brain → gates → live URL) — this is GMP-B1.

Phase D — games, one by one (§6.4 loop): octogram-arcade first (it is the template),
then sonar, slime-line, star-visitor, gyro-squadron-45, cubefall. Per repo:
migrate → secrets → games-ci green on Forgejo → deploy re-point → mirror on.

Phase E — itch + webhooks:

14. itch-publish.yml triggered by Forgejo push on `deploy`; BUTLER_API_KEY in game
    repo secrets; one real butler push verified on the itch page.
15. Re-point any external webhooks to Forgejo (expected: none needed, §3.6);
    GitHub-side webhooks keep firing via the mirror.

Phase F — playtest loop:

16. Emulator pre-flight probe (binder modules / `/dev/kvm`) → pick redroid or
    headless-emulator shape (§9.4).
17. Phase-1 web-in-browser loop (§9.1, §9.3) behind a daemon feature flag; one
    PlayerOne session filed as `runtime/feedback/<ts>.json` and closed by edit-fork.
18. Phase-2 APK path: Android SDK + export template side image, debug keystore in a
    volume, `adb install` variant of step 3.

Phase G — tidy:

19. gmp-bot container removed (token lives in gmp-agent now).
20. Runbook update: retromonkey.md gains the forgejo/runner/agent sections; this plan
    moves to "deployed" status with the GMP-B battery results appended.

## 14. Acceptance battery (GMP-B*)

| # | Check | Pass |
|---|---|---|
| GMP-B1 | Full loop on Forgejo infra: Telegram order → daemon → brain writes files → 2x gate wall green → export live at `retromonkey.com.au/games/<slug>/` → TG reply | live URL loads |
| GMP-B2 | Clone/push over `https://retromonkey.com.au/git/...` from Rog (forward slashes, no port) | push lands |
| GMP-B3 | Runner executes `runs-on: godot` workflow; `godot --version` in log | green |
| GMP-B4 | 7-suite arcade wall green on Forgejo twice (games-ci dispatch) | `0 failed` x2 |
| GMP-B5 | GitHub push mirror: Forgejo commit visible on GitHub ≤ 8 min | diff empty |
| GMP-B6 | Daily remake fires from host cron at 10:23 UTC; diary entry appended | log + diary |
| GMP-B7 | Heavy-lock law: start a gate wall and request a playtest simultaneously; the playtest waits | no OOM, serialized |
| GMP-B8 | RAM: `free -m` during gate wall peak < 950 MB used (swap may tick) | no swap storm |
| GMP-B9 | Emulator boots, WebView loads a game, vision brain taps through 10 frames, critique JSON written | feedback file pending |
| GMP-B10 | edit-fork closes a PlayerOne critique end-to-end (feedback → fix → gates → deploy → done) | status done |
| GMP-B11 | Forgejo restart (`docker restart gmp-forgejo`): daemon + runner reconnect, no lost orders | queue resumes |
| GMP-B12 | Fallback drill: GitHub Actions runs one real games-ci from the mirrored copy | green on GitHub |

## 15. Security considerations

- **The runner is remote code execution by design.** Acceptable here because the
  Forgejo instance is single-tenant: registration disabled, one admin, no public
  Actions for strangers. The docker socket it mounts is the whole host's Docker —
  treat runner compromise as host compromise; keep Forgejo + runner pinned by digest
  and upgraded on retromonkey's usual cadence.
- Instance exposed at `/git/` behind caddy TLS like everything else; registration
  off, 2FA on the admin account, and Forgejo's built-in rate limiting on auth
  endpoints left at defaults.
- Secrets never in git, never in orders/schemas/logs — masked `<set>` (existing law).
  Forgejo repo secrets are the only copy on the server beyond the daemon's env file
  (`/home/ubuntu/gmp/.env`, chmod 600).
- `valid_volumes` exposes only `/home/ubuntu/site` to job containers — not the drive,
  not the Forgejo data dir.
- The emulator runs `privileged` (binder/KVM) but only during playtests, loopback adb
  only, and the profile-down step is part of the loop itself (not optional).
- Backups: nightly sqlite `.backup` + rsync into the filebrowser drive (§5.4); GitHub
  push mirrors are the off-site copy of all code.
- PlayerOne feedback files are data, not code: they pass through the same
  complete-file parse + gate wall as human feedback, so a prompt-injected critique
  cannot ship ungated code (T3 law: 3 green attempts, else human).

## 16. Phases

| Phase | Ships | Depends on |
|---|---|---|
| A | Forgejo live at `/git/` behind caddy | nothing |
| B | Runner green on a `godot` label | A |
| C | game-making-pipeline cutover + gmp-agent daemon + cron daily remake | A, B |
| D | six game repos migrated + mirrored | C |
| E | itch publishing + webhooks from Forgejo | D |
| F | playtest loop (web build, then APK) | C, D |
| G | gmp-bot retired, runbook updated, drill scheduled | all |

C is the load-bearing phase; A and B are safe to do any evening, D–F follow the
per-repo recipe once C is proven.

## 17. What needs the most careful implementation

1. **The Telegram token handover (C, step 11).** Two processes long-polling one bot
   token = 409 Conflict storms and lost updates. gmp-bot must be *stopped before*
   gmp-agent starts, and the daemon needs a startup assertion ("I own getUpdates")
   before it starts consuming. Order matters; get it wrong and intake silently dies.
2. **The heavy-lock discipline (§3.7).** Every heavy section — daemon gates, daemon
   exports, runner job (capacity=1 covers Actions' own), emulator session — must
   respect the same lock, or the 956 MB box swaps to death under one unlucky overlap.
   The lock file lives in the shared `pipeline_state` volume so daemon-side and any
   Actions-side paths contend correctly.
3. **`runtime/` as database with a long-lived writer.** The 30-minute Actions commit
   is gone; the daemon commits every 60 s. Must handle: push conflicts with a
   simultaneous workflow commit (pull --rebase before push), and crash-with-dirty-tree
   (state survives because orders are files first, commits second).
4. **Workflow drift between `.forgejo/` (live) and `.github/` (dormant fallback).**
   The fallback is only real if the dormant copies still run. GMP-B12's quarterly
   drill is not bureaucracy — it is the test that catches drift.
5. **URL constant migration.** `new-game.yml` (and anything else) hardcodes
   `slothitude.github.io`. A missed constant ships a dead link in a Telegram reply.
   Grep the repo for `github.io` during phase D per repo; do not trust memory.

## 18. Cost

**$0/month, unchanged.**

| Thing | Cost |
|---|---|
| retromonkey (Oracle Always Free: compute, 4 GB swap on the free block volume, 39 GB disk, public IP) | $0 |
| Forgejo, runner, daemon, emulator images | $0 (OSS) |
| Brain chain: NVIDIA integrate.api.nvidia.com (glm-5.3, kimi-k3) + openrouter/free | $0 (free tiers) |
| PlayerOne vision calls | $0 (openrouter/free) |
| Telegram Bot API | $0 |
| itch.io hosting + butler | $0 |
| Domain + TLS (caddy + Let's Encrypt) | already owned/renewed |
| GitHub (mirror target, dormant Actions on public repos) | $0 |
| Egress | Oracle free tier allowance; game builds are a few MB each |

The only thing v2 spends is retromonkey's RAM, and §4 shows where every megabyte goes.
