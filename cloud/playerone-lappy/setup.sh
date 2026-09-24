#!/bin/bash
# playerone-lappy one-time setup — run ON LAPPY, as aaron:
#
#     bash ~/playerone-deploy/setup.sh
#
# Expects ~/playerone-deploy/ to be staged first (see README.md "Deploy"):
#
#     ~/playerone-deploy/
#       setup.sh  worker.py  playerone.service  README.md     <- this repo,
#               collect_evidence.py  selfplay.py                 cloud/ side
#               exec_critique.py
#       secrets                                  <- GATEWAY_TOKEN/NVAPI_KEY/
#                                               OPENROUTER_KEY=... (gitignored)
#       retro/                                   <- pulled off retromonkey:
#         runs/*.npz                               the brains (one per game:
#         llm_critic.py vision_critic.py           <game>-evolve-<stamp>.npz)
#         numpy_scorer.py canvas_recorder.py       the critic + scorer stack
#         scripts/<game>_playthrough.py            the playthrough modules
#         ladder.json                              the LLM ladder (daily/)
#
# Installs to ~/playerone/ and brings up the systemd service.
set -euo pipefail

DEPLOY="$HOME/playerone-deploy"
ROOT="$HOME/playerone"

say() { echo "[setup] $*"; }

[ -d "$DEPLOY" ] || { echo "FATAL: no $DEPLOY — stage it first (README 'Deploy' step 1-3)"; exit 1; }
[ -f "$DEPLOY/secrets" ] || { echo "FATAL: no $DEPLOY/secrets (GATEWAY_TOKEN=..., NVAPI_KEY=..., OPENROUTER_KEY=...)"; exit 1; }
[ -f "$DEPLOY/worker.py" ] || { echo "FATAL: no $DEPLOY/worker.py"; exit 1; }

# ---------------------------------------------------------------- 1. the venv
# playwright + pillow + numpy — NOT torch (numpy brains only; the .pt->.npz
# evolve step stays on the brain lane)
say "venv: creating $ROOT/venv (python3: $(python3 --version 2>&1))"
mkdir -p "$ROOT"
python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install --upgrade pip
"$ROOT/venv/bin/pip" install playwright pillow numpy
say "venv: installing chromium"
"$ROOT/venv/bin/playwright" install chromium
say "venv: installing chromium system deps (needs sudo)"
sudo "$ROOT/venv/bin/playwright" install-deps chromium

# ------------------------------------------------- 2. the worker's own files
say "copying worker + lane modules (explicit list)"
cp "$DEPLOY/worker.py"           "$ROOT/"
cp "$DEPLOY/collect_evidence.py" "$ROOT/"   # the eyes (playwright feed)
cp "$DEPLOY/selfplay.py"         "$ROOT/"   # the self-play recorder
cp "$DEPLOY/exec_critique.py"    "$ROOT/"   # issues_to_jobs — the order wire
cp "$DEPLOY/input_bridge.py"     "$ROOT/"   # the hands (P1_INPUT=bridge actuator)
if [ -d "$DEPLOY/profiles" ]; then          # per-game gesture profiles
  mkdir -p "$ROOT/profiles"
  cp "$DEPLOY/profiles/"*.json "$ROOT/profiles/"
fi

# --------------------------------------- 3. the retromonkey pull (brain side)
RETRO="$DEPLOY/retro"
for f in llm_critic.py vision_critic.py numpy_scorer.py canvas_recorder.py; do
  if [ -f "$RETRO/$f" ]; then
    cp "$RETRO/$f" "$ROOT/"
  else
    say "NOTE: retro/$f not staged — the lane that needs it will degrade loudly"
  fi
done
if [ -f "$RETRO/ladder.json" ]; then
  mkdir -p "$ROOT/daily"
  cp "$RETRO/ladder.json" "$ROOT/daily/ladder.json"   # GMP_ROOT discovery law
else
  say "NOTE: retro/ladder.json not staged — llm_critic falls back to code defaults"
fi
if [ -d "$RETRO/scripts" ]; then
  mkdir -p "$ROOT/scripts"
  cp "$RETRO/scripts/"*.py "$ROOT/scripts/" 2>/dev/null || true
  say "scripts: $(ls "$ROOT/scripts" | wc -l) playthrough module(s) installed"
else
  say "NOTE: retro/scripts not staged — selfplay jobs will fail honestly"
fi
mkdir -p "$ROOT/runs" "$ROOT/critiques" "$ROOT/evidence"
if ls "$RETRO"/runs/*.npz >/dev/null 2>&1; then
  cp "$RETRO"/runs/*.npz "$ROOT/runs/"
else
  say "NOTE: no retro/runs/*.npz staged — selfplay jobs will fail honestly"
fi
say "brains installed: $(ls "$ROOT/runs"/*.npz 2>/dev/null | wc -l) .npz file(s):"
ls -la "$ROOT/runs"/*.npz 2>/dev/null || true

# ------------------------------------------------------- 4. secrets + service
# shellcheck disable=SC1091
. "$DEPLOY/secrets"   # must define GATEWAY_TOKEN, NVAPI_KEY, OPENROUTER_KEY
for var in GATEWAY_TOKEN NVAPI_KEY OPENROUTER_KEY; do
  [ -n "${!var:-}" ] || { echo "FATAL: $var missing from $DEPLOY/secrets"; exit 1; }
done
say "secrets: writing systemd unit with substituted values"
sed -e "s|@VENV_PY@|$ROOT/venv/bin/python|g" \
    -e "s|@WORKER@|$ROOT/worker.py|g" \
    -e "s|@GATEWAY_TOKEN@|$GATEWAY_TOKEN|g" \
    -e "s|@NVAPI_KEY@|$NVAPI_KEY|g" \
    -e "s|@OPENROUTER_KEY@|$OPENROUTER_KEY|g" \
    "$DEPLOY/playerone.service" > /tmp/playerone.service
sudo cp /tmp/playerone.service /etc/systemd/system/playerone.service
rm -f /tmp/playerone.service

# ------------------------------------------------------------ 5. bring it up
say "systemd: daemon-reload + enable --now playerone"
sudo systemctl daemon-reload
sudo systemctl enable --now playerone
sleep 3
systemctl status playerone --no-pager | head -6 || true
say "last log lines (Ctrl-C to stop watching):"
journalctl -u playerone -n 10 --no-pager || true
say "done. the worker polls the retromonkey queue every 10s —"
say "watch it:    journalctl -u playerone -f"
say "smoke it:    curl -s -X POST https://retromonkey.com.au/api/jobs/claim -H \"X-Token: \$GATEWAY_TOKEN\" -d '{\"types\":[\"playtest\"]}'   # (from a box with the token)"
