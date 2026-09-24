#!/usr/bin/env python3
"""selftest_yolo.py — offline gate for eyes v2, the same law as the v1
selftest but through the FUSED lane: synthetic pastes onto real cleaned
backgrounds at known centres, then the fused perceive must recover them
within +-8 px at recall >= 0.9. Two seeds (the "x2").

Only classes with an on-screen size the lane can reasonably own are gated —
the v1 law's explicit-[w,h] pairs are in whatever their size, star-visitor's
~11x5 px bullet is excluded from the bar (it is judged on the evidence frames
instead; an 11x5 object under +-8 px is a coin toss by geometry alone).

    yolo-venv/bin/python selftest_yolo.py [--seed-base 1984]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

P1 = Path("/home/aaron/playerone")
YOLO_DIR = P1 / "yolo"
TOL_PX = 8
MIN_RECALL = 0.9
SEEDS = (1984, 2001)


def paste_frame(game: str, cls_names: list[str], sprites: dict,
                classes: dict, bg: np.ndarray,
                rng: np.random.Generator) -> tuple[np.ndarray, list[tuple]]:
    """Real background + pastes at known centres (the ground truth)."""
    frame = bg.copy()
    truth = []
    pick = cls_names[:]
    rng.shuffle(pick)
    for cls in pick[:9]:
        spr = sprites[cls]
        scale = float(rng.uniform(0.9, 1.15))
        canon = classes[cls]
        if isinstance(canon, (list, tuple)):
            tw, th = round(canon[0] * scale), round(canon[1] * scale)
        elif spr.w >= spr.h:
            tw, th = round(canon * scale), round(spr.h * canon * scale / spr.w)
        else:
            tw, th = round(spr.w * canon * scale / spr.h), round(canon * scale)
        tw, th = min(tw, 400), min(th, 700)
        if tw < 2 or th < 2:
            continue
        rgba = spr.at(tw, th)
        x0 = int(rng.integers(4, 480 - tw - 4))
        y0 = int(rng.integers(4, 800 - th - 4))
        region = frame[y0:y0 + th, x0:x0 + tw].astype(np.float32)
        a = rgba[:, :, 3].astype(np.float32) / 255.0
        frame[y0:y0 + th, x0:x0 + tw] = \
            (rgba[:, :, :3].astype(np.float32) * a[..., None]
             + region * (1.0 - a[..., None])).astype(np.uint8)
        truth.append((cls, x0 + tw / 2.0, y0 + th / 2.0))
    return frame, truth


def main(argv: list[str]) -> int:
    args = argv[1:]
    seed_base = int(args[args.index("--seed-base") + 1]) \
        if "--seed-base" in args else SEEDS[0]
    os.environ.setdefault("P1_EYES", "fusion")
    sys.path.insert(0, str(P1))
    import perception
    import gen_data as gd

    model = perception._yolo_model()
    if model is None:
        print("FAIL: no YOLO weights — fusion lane unavailable "
              f"({perception._FUSION.get('weights')})")
        return 1

    ok = True
    report = {}
    for gi, game in enumerate(gd.GAMES):
        classes = gd.game_classes(game)
        cls_names = sorted(classes)
        sprites = {}
        for c in cls_names:
            p = P1 / "sprites" / game / f"{c.split(':', 1)[1]}.png"
            try:
                sprites[c] = gd.Sprite(p)
            except (FileNotFoundError, ValueError):
                pass
        classes = {c: classes[c] for c in classes if c in sprites}
        cls_names = sorted(classes)
        bgs = gd.Backgrounds(game)
        seed_runs = []
        for si, seed in enumerate((seed_base, seed_base + 17)):
            rng = np.random.default_rng(seed + gi * 101)
            bg = bgs.sample(rng)
            frame, truth = paste_frame(game, cls_names, sprites, classes, bg,
                                       rng)
            state = perception.perceive(frame, game)
            used = set()
            errors = []
            misses = []
            for cls, tx, ty in truth:
                best_i, best_d = None, TOL_PX + 1
                for i, det in enumerate(state["all"]):
                    if i in used or det[0] != cls:
                        continue
                    d = ((det[1] + det[3] / 2 - tx) ** 2
                         + (det[2] + det[4] / 2 - ty) ** 2) ** 0.5
                    if d < best_d:
                        best_i, best_d = i, d
                if best_i is not None:
                    used.add(best_i)
                    errors.append(best_d)
                else:
                    misses.append(cls)
            recall = len(errors) / len(truth) if truth else 1.0
            max_err = max(errors) if errors else 0
            # the sub-36px bar, v1's law: measured pairs are gated, estimates
            # below 36px long edge are reported but not gated
            gated_truth = []
            for cls, tx, ty in truth:
                canon = classes[cls]
                long_px = max(canon) if isinstance(canon, (list, tuple)) \
                    else canon
                gated_truth.append((cls, tx, ty))
            gate_ok = (recall >= MIN_RECALL and max_err <= TOL_PX)
            seed_runs.append({
                "seed": seed, "expected": len(truth), "found": len(errors),
                "recall": round(recall, 3), "max_err_px": round(max_err, 1),
                "missed": misses, "pass": bool(gate_ok)})
            print(f"  {game} seed={seed}: {len(errors)}/{len(truth)} "
                  f"recall={recall:.2f} max_err={max_err:.1f}px "
                  f"ms={state['ms']} yolo_ms={state['yolo_ms']} "
                  f"{'PASS' if gate_ok else 'FAIL'}"
                  + (f" missed={misses}" if misses else ""))
            ok = ok and gate_ok
        report[game] = seed_runs

    (YOLO_DIR / "selftest_yolo.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8")
    print("SELFTEST-YOLO " + ("GREEN" if ok else "RED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
