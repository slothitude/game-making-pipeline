#!/usr/bin/env python3
"""eval_real.py — the honest test: fused eyes over the template lane's own
evidence frames.

Ground truth: the frames are unlabelled captures, so GT comes from the v1
template matcher at a RELAXED threshold (0.45 — below the 0.75 production law,
above noise) and is then verified by eye on the annotated montages this script
writes. For the classes v1 is known-good on (sonar player 0.96, star-visitor
player 0.87) that is solid GT; for enemy agents (v1 caps at 0.70) the relaxed
threshold is exactly how their GT is reachable at all, and the montage is the
check. Bullets have no template GT at all (v1 dropped them): they are judged
by zoomed crops a human/agent eye counts.

Compared per class over all frames:
  - v1-only   (the 0.75 template law, production behaviour)
  - fused     (templates primary + YOLO filling the gaps)
  - yolo-only (raw detector, conf 0.25) for reference
plus ms/frame template-only vs fused.

    yolo-venv/bin/python eval_real.py [--game star-visitor] [--conf 0.25]
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

P1 = Path("/home/aaron/playerone")
YOLO_DIR = P1 / "yolo"
EVIDENCE = P1 / "evidence" / "perception"
GT_CONF = 0.45          # relaxed template threshold -> GT candidates
EVAL_IOU = 0.4          # GT box vs detection box match


def iou(a, b) -> float:
    ax0, ay0, ax1, ay1 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    iw = min(ax1, bx1) - max(ax0, bx0)
    ih = min(ay1, by1) - max(ay0, by0)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / float(a[2] * a[3] + b[2] * b[3] - inter)


def det_boxes(dets):
    """v1 det tuples -> [(cls, x, y, w, h, conf)] in frame pixels."""
    return [(d[0], d[1], d[2], d[3], d[4], d[5]) for d in dets]


def yolo_boxes(model, frame_bgr, game: str, conf: float, imgsz: int):
    """YOLO detections for THIS game's classes, frame pixels + role prefix."""
    res = model.predict(frame_bgr, imgsz=imgsz, conf=conf, verbose=False)[0]
    out = []
    names = res.names
    for b in res.boxes:
        cname = names[int(b.cls)]
        if not cname.startswith(f"{game}."):
            continue
        x0, y0, x1, y1 = (float(v) for v in b.xyxy[0])
        role_cls = cname.split(".", 1)[1]        # "threats:enemy_agent"
        out.append((role_cls, x0, y0, x1 - x0, y1 - y0, float(b.conf)))
    return out


def recall_vs(gt: list, dets: list, cls: str) -> tuple[float, int, int, float]:
    """Recall of `dets` against GT for one class (greedy IoU match)."""
    g = [t for t in gt if t[0] == cls]
    d = [t for t in dets if t[0] == cls]
    if not g:
        return (float("nan"), 0, 0, float("nan"))
    used = set()
    hits = 0
    confs = []
    for cls_, gx, gy, gw, gh, _ in g:
        best, bi = 0.0, None
        for i, (c2, x, y, w, h, cf) in enumerate(d):
            if i in used:
                continue
            v = iou((gx, gy, gw, gh), (x, y, w, h))
            if v > best:
                best, bi = v, i
        if best >= EVAL_IOU and bi is not None:
            used.add(bi)
            hits += 1
            confs.append(d[bi][5])
    return (hits / len(g), hits, len(g),
            float(np.mean(confs)) if confs else float("nan"))


def main(argv: list[str]) -> int:
    args = argv[1:]
    game = args[args.index("--game") + 1] if "--game" in args else "star-visitor"
    conf = float(args[args.index("--conf") + 1]) if "--conf" in args else 0.25
    imgsz = int(args[args.index("--imgsz") + 1]) if "--imgsz" in args else 800

    sys.path.insert(0, str(P1))
    import perception
    from ultralytics import YOLO

    weights = os.environ.get("P1_YOLO_WEIGHTS", str(YOLO_DIR / "best.pt"))
    model = YOLO(weights)

    frames = sorted((EVIDENCE / game / "frames").glob("*.jpg"))
    # GT: either a hand-labelled JSON (the eyeball truth, --gt) or the v1
    # matcher at a relaxed threshold (cheap, but contaminated — the v1@0.45
    # heatmap fires on HUD text; documented in the JSON's note)
    gt_path = Path(args[args.index("--gt") + 1]) if "--gt" in args else None
    hand: dict[str, list] = {}
    if gt_path is not None and gt_path.is_file():
        hand = json.loads(gt_path.read_text(encoding="utf-8"))["frames"]
    gt_all, v1_all, fused_all, yolo_all, src_all = [], [], [], [], []
    annotated = []
    for p in frames:
        frame = cv2.imread(str(p))
        if hand:
            key = p.stem.split("_")[-1] if not p.stem.isdigit() else p.stem
            key = p.stem[-3:] if key not in hand else key
            gt = [(c, float(x), float(y), float(w), float(h), 1.0)
                  for c, x, y, w, h in hand.get(key, [])]
        else:
            gt = det_boxes(perception.perceive(frame, game,
                                               threshold=GT_CONF)["all"])
        v1 = det_boxes(perception.perceive(frame, game)["all"])
        yb = yolo_boxes(model, frame, game, conf, imgsz)
        fused_state = perception.perceive(frame, game, yolo_model=model,
                                          yolo_conf=conf, yolo_imgsz=imgsz)
        fused = det_boxes(fused_state["all"])
        src = fused_state.get("source", {})
        gt_all.append(gt)
        v1_all.append(v1)
        fused_all.append(fused)
        yolo_all.append(yb)
        src_all.append(src)
        # annotated frame: GT green / v1 blue / fused orange (yolo-sourced =
        # orange-yellow) / raw yolo magenta
        vis = frame.copy()
        for cls_, x, y, w, h, _ in gt:
            cv2.rectangle(vis, (int(x), int(y)), (int(x + w), int(y + h)),
                          (0, 255, 0), 1)
        for cls_, x, y, w, h, cf in v1:
            cv2.rectangle(vis, (int(x), int(y)), (int(x + w), int(y + h)),
                          (255, 128, 0), 1)
        for i, (cls_, x, y, w, h, cf) in enumerate(fused):
            colour = (0, 255, 255) if src.get(str(i)) == "yolo" else (0, 160, 255)
            cv2.rectangle(vis, (int(x), int(y)), (int(x + w), int(y + h)),
                          colour, 1)
        for cls_, x, y, w, h, cf in yb:
            cv2.rectangle(vis, (int(x), int(y)), (int(x + w), int(y + h)),
                          (255, 0, 255), 1)
        annotated.append(vis)

    # ---- per-class table -------------------------------------------------
    classes = sorted({t[0] for gt in gt_all for t in gt}
                     | {t[0] for yb in yolo_all for t in yb})
    flat_gt = [t for gt in gt_all for t in gt]
    table = {}
    for cls in classes:
        row = {}
        for name, dets in (("v1", v1_all), ("fused", fused_all),
                           ("yolo", yolo_all)):
            r, hits, n, mc = recall_vs(flat_gt, [t for d in dets for t in d],
                                       cls)
            if not np.isnan(r):
                row[name] = {"recall": round(r, 3), "hits": hits, "gt": n,
                             "mean_conf": round(mc, 3) if not np.isnan(mc)
                             else None}
        if row:
            table[cls] = row

    # ---- ms/frame --------------------------------------------------------
    f0 = cv2.imread(str(frames[0]))
    perception.configure(enabled=False)             # template-only
    for _ in range(3):
        perception.perceive(f0, game)               # warmup (caches)
    t0 = time.perf_counter()
    for _ in range(10):
        perception.perceive(f0, game)
    ms_v1 = (time.perf_counter() - t0) * 100.0
    perception.configure(enabled=True)
    for _ in range(3):
        perception.perceive(f0, game, yolo_model=model, yolo_conf=conf,
                            yolo_imgsz=imgsz)
    t0 = time.perf_counter()
    for _ in range(10):
        perception.perceive(f0, game, yolo_model=model, yolo_conf=conf,
                            yolo_imgsz=imgsz)
    ms_fused = (time.perf_counter() - t0) * 100.0
    t0 = time.perf_counter()
    for _ in range(10):
        yolo_boxes(model, f0, game, conf, imgsz)
    ms_yolo = (time.perf_counter() - t0) * 100.0

    out = {
        "game": game, "frames": len(frames),
        "gt_source": f"hand:{gt_path.name}" if hand else f"v1@{GT_CONF}",
        "eval_iou": EVAL_IOU, "yolo_conf": conf, "imgsz": imgsz,
        "per_class": table,
        "ms_per_frame": {"template_only": round(ms_v1, 1),
                         "fused": round(ms_fused, 1),
                         "yolo_alone": round(ms_yolo, 1)},
        "v1_detections_total": sum(len(d) for d in v1_all),
        "fused_detections_total": sum(len(d) for d in fused_all),
        "fused_yolo_sourced": sum(
            1 for s in src_all for v in s.values() if v == "yolo"),
        "yolo_raw_detections_total": sum(len(d) for d in yolo_all),
    }

    # montages for the human eye: 6 frames in a 2x3 grid
    sdir = YOLO_DIR / "eval"
    sdir.mkdir(parents=True, exist_ok=True)
    picks = np.linspace(0, len(annotated) - 1, 6).astype(int)
    tiles = [cv2.resize(annotated[i], (240, 400)) for i in picks]
    rows = [np.hstack(tiles[i:i + 2]) for i in range(0, len(tiles), 2)]
    cv2.imwrite(str(sdir / f"annotated_{game}.jpg"), np.vstack(rows))
    for i in picks:
        cv2.imwrite(str(sdir / f"frame_{i:03d}.jpg"), annotated[i])

    (YOLO_DIR / f"eval_real_{game}.json").write_text(json.dumps(out, indent=1),
                                                     encoding="utf-8")
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
