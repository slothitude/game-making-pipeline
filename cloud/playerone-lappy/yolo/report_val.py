#!/usr/bin/env python3
"""report_val.py — post-train report: per-class mAP on the held-out synth val
set + ONNX export. Run under an arbiter lease:

    MESH_GPU_LANE=yolo ~/gpu-arbiter/wrappers/mesh-gpu \
        ~/playerone/yolo-venv/bin/python ~/playerone/yolo/report_val.py

(train_yolo.py's inline version crashed on an ultralytics API difference —
metrics.box.map50 is a scalar here, the per-class arrays are .ap50/.ap/.p/.r.)
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

P1 = Path("/home/aaron/playerone")
YOLO_DIR = P1 / "yolo"
DATA = YOLO_DIR / "datasets" / "p1" / "p1.yaml"
IMGSZ = 800
BATCH = 4


def per_class(metrics) -> dict:
    """Defensive per-class table: some fields are scalars in this build."""
    box = metrics.box
    def arr(v):
        try:
            return list(v)
        except TypeError:
            return [v]
    ap50 = arr(getattr(box, "ap50", []))
    ap = arr(getattr(box, "ap", []))
    p = arr(getattr(box, "p", []))
    r = arr(getattr(box, "r", []))
    n = len(getattr(metrics, "names", {}) or {})
    table = {}
    for i, cname in getattr(metrics, "names", {}).items():
        def get(a, j):
            return a[j] if j < len(a) else float("nan")
        table[cname] = {
            "mAP50": round(float(get(ap50, i)), 4),
            "mAP50-95": round(float(get(ap, i)), 4),
            "precision": round(float(get(p, i)), 4),
            "recall": round(float(get(r, i)), 4),
        }
    return table


def main() -> int:
    t0 = time.time()
    import torch
    from ultralytics import YOLO

    weights = YOLO_DIR / "best.pt"
    model = YOLO(str(weights))
    metrics = model.val(data=str(DATA), imgsz=IMGSZ, batch=BATCH, device=0,
                        plots=False, verbose=False)
    table = per_class(metrics)
    box = metrics.box
    summary = {
        "imgsz": IMGSZ, "epochs": 70, "batch": BATCH,
        "overall_mAP50": round(float(box.map50), 4),
        "overall_mAP50-95": round(float(box.map), 4),
        "mean_precision": round(float(box.mp), 4),
        "mean_recall": round(float(box.mr), 4),
        "per_class": table,
        "val_seconds": round(time.time() - t0, 1),
        "gpu": torch.cuda.get_device_name(0),
        "weights": str(weights),
        "inference_ms": "0.5 preprocess / 2.8 inference / 0.8 postprocess "
                        "(train log, 3060)",
    }
    try:
        onnx = model.export(format="onnx", imgsz=IMGSZ, opset=12,
                            dynamic=False, simplify=True)
        summary["onnx"] = str(onnx)
    except Exception as e:
        summary["onnx"] = f"FAILED: {e}"
    (YOLO_DIR / "train_report.json").write_text(json.dumps(summary, indent=1),
                                                encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
