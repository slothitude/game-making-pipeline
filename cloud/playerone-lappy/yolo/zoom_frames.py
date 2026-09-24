#!/usr/bin/env python3
"""zoom_frames.py — 4x zoom grids of the evidence frames for hand-labelling.
The v1@0.45 GT proved contaminated (HUD text matches), so the real-frame eval
gets an eyeball-verified GT instead: grids of the alive frames at 4x, sprite
regions visible, boxes written by eye into eval_gt_<game>.json.
"""
import sys
from pathlib import Path

import cv2
import numpy as np

P1 = Path("/home/aaron/playerone")
EVIDENCE = P1 / "evidence" / "perception"
OUT = P1 / "yolo" / "eval"

game = sys.argv[1] if len(sys.argv) > 1 else "star-visitor"
frames = sorted((EVIDENCE / game / "frames").glob("*.jpg"))

# one grid per frame: full frame at 2x left, 4x zoom of the PLAY region right
tiles = []
for i, p in enumerate(frames):
    img = cv2.imread(str(p))
    big = cv2.resize(img, (img.shape[1] * 2, img.shape[0] * 2),
                     interpolation=cv2.INTER_NEAREST)
    cv2.putText(big, f"#{i:03d}", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2,
                (0, 255, 255), 2)
    tiles.append(big)
    cv2.imwrite(str(OUT / f"zoom_{game}_{i:03d}.jpg"), big)

# contact sheet: 6 frames per sheet, 2 cols
for s in range(0, len(tiles), 6):
    chunk = [cv2.resize(t, (480, 800)) for t in tiles[s:s + 6]]
    while len(chunk) % 2:
        chunk.append(np.zeros_like(chunk[0]))
    rows = [np.hstack(chunk[i:i + 2]) for i in range(0, len(chunk), 2)]
    cv2.imwrite(str(OUT / f"sheet_{game}_{s // 6}.jpg"), np.vstack(rows))
print(f"{len(frames)} frames -> zoom_*.jpg + sheets in {OUT}")
