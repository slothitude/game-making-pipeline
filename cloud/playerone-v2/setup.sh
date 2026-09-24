#!/bin/bash
# playerone-v2 one-time setup — run ON LAPPY, as aaron:
#
#     bash ~/playerone-v2-deploy/setup.sh
#
# Expects ~/playerone-v2-deploy/ to be staged first (see README.md "Deploy"):
#
#     ~/playerone-v2-deploy/
#       setup.sh  perceive.py  brain.py  play.py  README.md    <- this repo,
#       ladder.json                                            <- cloud/ side
#       numpy_scorer.py                                        <- retromonkey pull
#       runs/*.npz                                             <- the jevlike brains
#       secrets                                 <- NVAPI_KEY=... OPENROUTER_KEY=...
#                                                 (gitignored; GEMINI stays in
#                                                 ~/.gemini_key if you have one)
#
# Installs to ~/playerone-v2/ and proves it with the offline selftests.
set -euo pipefail

DEPLOY="$HOME/playerone-v2-deploy"
ROOT="$HOME/playerone-v2"
YOLO_URL="https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt"

say() { echo "[setup] $*"; }

[ -d "$DEPLOY" ] || { echo "FATAL: no $DEPLOY — stage it first (README 'Deploy')"; exit 1; }
[ -f "$DEPLOY/secrets" ] || { echo "FATAL: no $DEPLOY/secrets (NVAPI_KEY=..., OPENROUTER_KEY=...)"; exit 1; }
for f in perceive.py brain.py play.py; do
  [ -f "$DEPLOY/$f" ] || { echo "FATAL: no $DEPLOY/$f"; exit 1; }
done

# ---------------------------------------------------------------- 1. the venv
# opencv (the eyes) + ultralytics (YOLO on the 3060) + playwright (the page)
say "venv: creating $ROOT/venv (python3: $(python3 --version 2>&1))"
mkdir -p "$ROOT"
python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install --upgrade pip
"$ROOT/venv/bin/pip" install opencv-python-headless ultralytics numpy playwright pillow
say "venv: installing chromium"
"$ROOT/venv/bin/playwright" install chromium
say "venv: installing chromium system deps (needs sudo)"
sudo "$ROOT/venv/bin/playwright" install-deps chromium

# ------------------------------------------------------- 2. the player's files
say "copying perceive/brain/play (explicit list)"
cp "$DEPLOY/perceive.py" "$DEPLOY/brain.py" "$DEPLOY/play.py" "$ROOT/"
if [ -f "$DEPLOY/ladder.json" ]; then
  cp "$DEPLOY/ladder.json" "$ROOT/ladder.json"    # discovered beside brain.py
else
  say "NOTE: ladder.json not staged — brain.py falls back to $GMP_ROOT/daily/ladder.json or code defaults"
fi

# ------------------------------------------------- 3. the jevlike brain stack
if [ -f "$DEPLOY/numpy_scorer.py" ]; then
  cp "$DEPLOY/numpy_scorer.py" "$ROOT/"
else
  say "NOTE: numpy_scorer.py not staged — reflex tier + scorer use their uniform/keyword fallbacks"
fi
mkdir -p "$ROOT/runs"
if ls "$DEPLOY"/runs/*.npz >/dev/null 2>&1; then
  cp "$DEPLOY"/runs/*.npz "$ROOT/runs/"
else
  say "NOTE: no runs/*.npz staged — scorer falls back to uniform, reflex to keyword law"
fi
say "brains installed: $(ls "$ROOT/runs"/*.npz 2>/dev/null | wc -l) .npz file(s)"

# ------------------------------------------------------------- 4. yolov8n.pt
if [ -f "$ROOT/yolov8n.pt" ]; then
  say "yolo: yolov8n.pt already present"
else
  say "yolo: downloading yolov8n.pt (~6MB, the smallest detector)"
  curl -fsSL -o "$ROOT/yolov8n.pt" "$YOLO_URL" \
    || say "NOTE: download failed — perceive degrades to OpenCV-only until yolov8n.pt lands at $ROOT/yolov8n.pt"
fi

# ----------------------------------------------------------------- 5. secrets
# shellcheck disable=SC1091
. "$DEPLOY/secrets"
for var in NVAPI_KEY OPENROUTER_KEY; do
  [ -n "${!var:-}" ] || { echo "FATAL: $var missing from $DEPLOY/secrets"; exit 1; }
done
say "secrets: ok (NVAPI_KEY, OPENROUTER_KEY present; GEMINI reads ~/.gemini_key if it exists)"

# ------------------------------------------------------------- 6. prove it
say "selftests: py_compile + perceive --selftest + brain --selftest + play --selftest"
"$ROOT/venv/bin/python" -m py_compile "$ROOT/perceive.py" "$ROOT/brain.py" "$ROOT/play.py"
NVAPI_KEY="$NVAPI_KEY" OPENROUTER_KEY="$OPENROUTER_KEY" \
  "$ROOT/venv/bin/python" "$ROOT/perceive.py" --selftest
NVAPI_KEY="$NVAPI_KEY" OPENROUTER_KEY="$OPENROUTER_KEY" \
  "$ROOT/venv/bin/python" "$ROOT/brain.py" --selftest
NVAPI_KEY="$NVAPI_KEY" OPENROUTER_KEY="$OPENROUTER_KEY" \
  "$ROOT/venv/bin/python" "$ROOT/play.py" --selftest

say "done. smoke the real loop:"
say "  source $DEPLOY/secrets"
say "  $ROOT/venv/bin/python $ROOT/play.py --game sonar --seconds 90 --brain \$(ls -t $ROOT/runs/sonar*.npz | head -1) --record"
