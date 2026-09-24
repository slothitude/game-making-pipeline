# Lappy Godot Execution Container

Godot headless builds for the Pipeline no longer run on retromonkey (956MB RAM
stalls at godot load 13). They run in a docker container on **Lappy**, driven
over the tailnet by a godot-shaped shim. Consumers call the shim exactly like
the godot CLI.

## Architecture

```
retromonkey                                    Lappy (192.168.0.33 / tailnet)
------------                                   ------------------------------
~/pipeline/queue/lappy_godot_wrapper           ~/gmp-godot/Dockerfile
  |  (pretends to be godot)                    ~/gmp-godot -> image gmp-godot:4.7.1-lappy
  |  1. rsync -a --delete --exclude .godot     ~/gmp-jobs/<game>/   <- bind mount
  |       <project>/ -> gmp-jobs/<basename>/       docker run --rm --user 1000:1000
  |  2. ssh -> docker run godot <args>             -e HOME=/tmp -e XDG_DATA_HOME=/xdg
  |  3. rsync back everything except .godot        godot --headless --path /project ...
  v
exit code == godot's exit code
```

- **Image**: `gmp-godot:4.7.1-lappy` (from `barichello/godot-ci:4.7.1`).
  Image id `c5a6af2d0819` at build time; 7.8GB disk usage / 2.58GB content.
- **godot in-image**: `/usr/local/bin/godot`, version `4.7.1.stable.official.a13da4feb`.
- **Export templates**: baked at `/root/.local/share/godot/export_templates/4.7.1.stable`
  (2.0GB). The Dockerfile `chmod 711 /root` + symlinks
  `/xdg/godot/export_templates` -> that dir so a **non-root** container can
  read them via `XDG_DATA_HOME=/xdg`.
- **Non-root on purpose**: containers run `--user 1000:1000` (= `aaron` on
  Lappy) with `HOME=/tmp`. Everything godot writes into the bind mount stays
  owned by uid 1000, so rsync can mirror, delete, and re-sync without hitting
  root-owned files. (An earlier root-run left a root-owned `.godot/` that
  neither aaron nor later uid-1000 runs could touch — do not drop the
  `--user`.)
- **Transport**: retromonkey authenticates to Lappy with
  `~/.ssh/gmp_lappy` (ed25519, no passphrase), pub key installed in Lappy's
  `~/.ssh/authorized_keys`. Host is the tailnet IP `aaron@100.123.86.14`.
  The path is a WAN route (~90ms RTT): throughput swings between ~11KB/s
  (when Lappy's home upload is saturated by other services) and ~1MB/s.

## Wrapper contract

`/home/ubuntu/pipeline/queue/lappy_godot_wrapper` (canonical copy in this
repo at `cloud/queue/lappy_godot_wrapper`):

```
lappy_godot_wrapper --headless --path /home/ubuntu/games-src/<game> --import
lappy_godot_wrapper --headless --path <dir> --export-release Web
```

- `--path <dir>` (or `--path=<dir>`) selects the project; **all other args
  pass through to godot verbatim**, order preserved. No `--path` => `$PWD`.
- Job scratch on Lappy is `~/gmp-jobs/<basename of --path>/`. Two concurrent
  jobs whose projects share a basename share a directory — don't.
- Up-sync is `--delete` (mirrors the project) so locally-deleted files are not
  resurrected by the back-sync; `.godot` is excluded both ways.
- rsync uses `-zz` (zstd) + `--whole-file` + `--partial --timeout=180`.
- Timestamped start/end/phase lines go to **stderr** (nothing-silent law);
  godot and rsync output stream through. Long runs: launch detached
  (`nohup setsid ... > out 2> err < /dev/null &`) and poll the log — an
  interactive ssh that dies takes the wrapper with it.
- **The Lappy job directory is warm across runs**: the first wrapper run for a
  game pays one full-project sync, every later run syncs only deltas.

## Measured (2026-09-24)

| Step | Time |
|---|---|
| Image build (base pull ~2.6GB) | 2370s (~40 min) |
| `--import`, empty project, in-container | 4s |
| `--export-release Web`, empty project, in-container | 4-5s (39MB out) |
| Real sonar `--import` via wrapper | 621s total — godot 7s, rest rsync (first sync + resuming an interrupted transfer on a saturated link) |
| Real sonar `--export-release Web` via wrapper | **86s total** — rsync up 46s, godot 7s, rsync back 33s (11.5MB wire for ~40MB output) |

Both proof runs exited 0; the export produced `build/web/index.html`,
`index.pck` (GDPC magic), `index.js`, `index.wasm`, synced back to retromonkey
owned by `ubuntu`.

## Rebuild the image

```bash
# on Lappy
ssh aaron@192.168.0.33        # or: ssh -i ~/.ssh/gmp_lappy aaron@100.123.86.14
nano ~/gmp-godot/Dockerfile   # edit
docker build -t gmp-godot:4.7.1-lappy ~/gmp-godot/
docker run --rm gmp-godot:4.7.1-lappy godot --version
docker run --rm gmp-godot:4.7.1-lappy ls /root/.local/share/godot/export_templates/
```

The first build is slow (base pull). Rebuilds on top of the cached base are
seconds. Do not delete the barichello base layers.

## Wiring in consumers (follow-up, not done here)

`exec_art.py` / `exec_milestone.py` / `exec_deploy.py` / `exec_tune.py` still
call godot directly; pointing them at
`/home/ubuntu/pipeline/queue/lappy_godot_wrapper` is the next lane. Nothing in
the queue executors was changed for this.
