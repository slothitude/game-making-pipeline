#!/bin/bash
# PlayerOne evolution cycle — the critic's play distribution evolves with real play.
# Law: tiny scorers train in SECONDS on CPU; Colab units are saved for real GPU work.
# Cycle: (data already accumulated in data/<game>-evolve/) -> train -> export -> swap.
set -u
P=/home/ubuntu/playerone
VENV=$P/venv/bin/python
GAME=${1:-sonar}
DATA=$P/data/$GAME
OUT=$P/runs-evolve/$GAME-$(date +%Y%m%d-%H%M).pt
STAMP=$(date +%Y%m%d)

[ -f "$DATA/train.jsonl" ] || { echo "[evolve] no train.jsonl for $GAME — nothing to evolve from"; exit 1; }

echo "[evolve] training $GAME ($(wc -l < "$DATA/train.jsonl") rows)..."
cd $P && $VENV -m jevlike.train "$DATA/train.jsonl" \
  --validation "${VALIDATION:-$DATA/validation.jsonl}" \
  --output "$OUT" --width 64 --rank 64 --context-tokens 192 \
  --option-tokens 40 --epochs 40 2>&1 | tail -1

echo "[evolve] exporting numpy..."
cd $P && $VENV -c "
import sys, json, numpy as np, torch
sys.path.insert(0, '.')
from jevlike.model import load_checkpoint
model, coll, cfg = load_checkpoint('$OUT', torch.device('cpu'))
w = {k: v.numpy().astype(np.float32) for k, v in model.state_dict().items()}
np.savez_compressed('runs/$GAME-evolve-$STAMP.npz', **w)
open('runs/$GAME-evolve-$STAMP.json', 'w').write(json.dumps(cfg))
print('[evolve] exported runs/$GAME-evolve-$STAMP.npz')
"

echo "[evolve] cycle complete — the critic loads the newest runs/*.npz next session"
