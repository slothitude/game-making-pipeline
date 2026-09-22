# retromonkey — Pipeline Cloud Infrastructure Hub

Oracle Cloud instance (168.138.8.0, ubuntu@, alias `retromonkey` in ~/.ssh/config).
956MB RAM + 4GB swap, Ubuntu 22.04, Docker 29.5.3. Everything runs as Docker
containers with `--restart unless-stopped`.

Deployed 2026-09-22.

## URLs

| URL | What |
|-----|------|
| https://retromonkey.com.au/ | Slothitude Games site |
| https://retromonkey.com.au/games/<slug>/ | Game builds (drop a folder, it's live) |
| https://retromonkey.com.au/drive/ | FileBrowser cloud drive |
| https://nursery.retromonkey.com.au/ | Cascade Dracaenas Nursery (legacy static site, preserved from the old native Caddy) |

## What's running

### caddy (web server + TLS + reverse proxy)
- Image: `caddy:latest`, ports `80`, `443`, network `web`
- Config: `/home/ubuntu/caddy/Caddyfile` (mounted read-only at `/etc/caddy/Caddyfile`)
- Volumes:
  - `/home/ubuntu/site` → `/srv` (the site; games go in `/home/ubuntu/site/games`)
  - `/home/ubuntu/caddy_data` → `/data` (TLS certs)
  - `/home/ubuntu/caddy_config` → `/config`
  - `/var/www/cascade-dracaenas` → same path, read-only (nursery legacy site)
- Proxy: `/drive/*` → `filebrowser:80` (container-to-container over the `web` docker
  network — NOT localhost:8080; `localhost` inside the caddy container is the container itself)

### filebrowser (cloud drive)
- Image: `filebrowser/filebrowser:latest` (v2.63 community build), port `127.0.0.1:8080` (loopback only — public access is via the `/drive` proxy), network `web`
- Volumes:
  - `/home/ubuntu/cloud` → `/srv` (drive contents)
  - `/home/ubuntu/filebrowser.db` → `/database/filebrowser.db` (users/credentials)
  - `/home/ubuntu/filebrowser_config` → `/config` (settings.json; `baseURL: "/drive"` is set there — without it the UI's asset paths 404 under the subpath)
- Login: `admin` / `7y_DNu0C_FsKHWMu` (random, from first boot — change it)
- WARNING: the filebrowser project was archived 2026-09-01 (no more releases or
  security fixes). Fine for now; replace if it ever becomes load-bearing.

## Deploying game updates

Site itself (`C:\Users\aaron\game-making-pipeline\build\site\` → live immediately, no restart):

```
scp -r C:/Users/aaron/game-making-pipeline/build/site/* retromonkey:/home/ubuntu/site/
```

A game (Godot web export → `/games/<slug>/`):

```
ssh retromonkey 'mkdir -p /home/ubuntu/site/games/<slug>'
scp -r C:/path/to/export/* retromonkey:/home/ubuntu/site/games/<slug>/
```

That's it — Caddy serves `/games/<slug>/` straight from disk, no restart needed.

Cloud drive files: upload via the FileBrowser web UI, or scp into `/home/ubuntu/cloud/`.

## Restarting services

```
ssh retromonkey 'sudo docker restart caddy'          # after Caddyfile edits
ssh retromonkey 'sudo docker restart filebrowser'    # after settings.json edits
ssh retromonkey 'sudo docker ps -a'                  # state
ssh retromonkey 'sudo docker logs --tail 50 caddy'
```

Both containers restart on boot/crash (`unless-stopped`). Certs auto-renew into
`/home/ubuntu/caddy_data`.

## Gotchas (learned the hard way)

- **ubuntu is uid 1001 on this box, not 1000.** The filebrowser container runs as
  uid 1000, so anything it must write (`filebrowser.db`, `cloud/`, `filebrowser_config/`)
  is chowned `1000:1000`. If you recreate those dirs as ubuntu, chown again or the
  container crash-loops with `permission denied`.
- **The old native Caddy is disabled, not removed.** `caddy.service` +
  `caddy-ask.service` (systemd) used to own ports 80/443. Old config backed up at
  `/etc/caddy/Caddyfile.pre-slothitude.bak`. To restore it: `sudo systemctl disable --now caddy` (docker one) then `sudo systemctl enable --now caddy caddy-ask` after removing the docker caddy.
- **/drive needs the trailing slash** — bare `/drive` 301-redirects to `/drive/`.
- **FileBrowser was placed on docker network `web`** so caddy can reach it by name.
  Recreating one container: add `--network web` or the proxy breaks.
