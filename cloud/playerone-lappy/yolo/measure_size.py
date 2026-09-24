#!/usr/bin/env python3
"""measure_size.py — what size does the game ACTUALLY draw these sprites at?

roles.json says hero 20x28 / agent 19x27, but the zoomed frames read smaller.
A NCC scale sweep over an alive frame gives the answer objectively: for each
sprite, slide the alpha-flattened template over the frame at every integer
size in a range and report the (size, x, y, conf) peaks. Confirmed against
the frames the template lane itself calibrated on.
"""
import sys
from pathlib import Path

import cv2
import numpy as np

P1 = Path("/home/aaron/playerone")

def flatten(path: Path, bg=40) -> np.ndarray | None:
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    a = img[:, :, 3]
    ys, xs = np.where(a > 8)
    img = img[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    alpha = img[:, :, 3].astype(np.float32) / 255.0
    gray = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY).astype(np.float32)
    return (gray * alpha + bg * (1 - alpha)).astype(np.uint8)

def sweep(frame_gray, tmpl, wmin, wmax, hmin, hmax):
    best = []
    for w in range(wmin, wmax + 1):
        for h in range(hmin, hmax + 1):
            t = cv2.resize(tmpl, (w, h), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(frame_gray, t, cv2.TM_CCOEFF_NORMED)
            _, conf, _, loc = cv2.minMaxLoc(res)
            best.append((float(conf), w, h, loc[0], loc[1]))
    best.sort(reverse=True)
    return best[:5]

game = sys.argv[1] if len(sys.argv) > 1 else "star-visitor"
fidx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
frame = cv2.imread(str(P1 / "evidence" / "perception" / game / "frames" / f"{fidx:03d}.jpg"))
gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
bg = int(round(float(np.median(gray)) / 16) * 16)

for name, spec in (("hero_idle", (12, 30)), ("enemy_agent", (8, 28)),
                   ("bullet", (4, 20))):
    tmpl = flatten(P1 / "sprites" / game / f"{name}.png", bg)
    if tmpl is None:
        print(f"{name}: no art")
        continue
    nat_h, nat_w = tmpl.shape
    peaks = []
    for w in range(spec[0], spec[1] + 1):
        h = max(2, round(nat_h * w / nat_w))
        t = cv2.resize(tmpl, (w, h), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
        _, conf, _, loc = cv2.minMaxLoc(res)
        peaks.append((round(float(conf), 4), w, h, loc))
    peaks.sort(reverse=True)
    print(f"{name} (native {nat_w}x{nat_h}, bg {bg}): top-5 by width sweep")
    for conf, w, h, loc in peaks[:5]:
        print(f"   w={w:3d} h={h:3d} conf={conf:.3f} at {loc}")
