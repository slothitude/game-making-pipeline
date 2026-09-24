#!/usr/bin/env python3
"""train_yolo.py — PlayerOne eyes v2 training. Runs UNDER AN ARBITER LEASE:

    MESH_GPU_LANE=yolo ~/gpu-arbiter/wrappers/mesh-gpu \
        ~/playerone/yolo-venv/bin/python ~/playerone/yolo/train_yolo.py

yolo11n (ultralytics ships it cleanly; yolov8n is its predecessor) on the
games' synthetic set. imgsz=800 keeps the 480x800 live capture at NATIVE
resolution — the gap this lane closes is 20x28 px sprites, and 640 input would
shrink them to 16x22 before the network ever sees them. If 800 does not fit
in the VRAM headroom left beside the resident ComfyUI container, retry at 640
and say so in the run log. Exports best.pt + ONNX and reports per-class mAP.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

P1 = Path("/home/aaron/playerone")
YOLO_DIR = P1 / "yolo"
DATA = YOLO_DIR / "datasets" / "p1" / "p1.yaml"

EPOCHS = 70
BATCH = 4
WORKERS = 4


def train(imgsz: int):
    from ultralytics import YOLO
    model = YOLO("yolo11n.pt")     # COCO-pretrained — small data needs it
    return model.train(
        data=str(DATA), epochs=EPOCHS, imgsz=imgsz, batch=BATCH,
        workers=WORKERS, device=0, project=str(YOLO_DIR / "runs"),
        name=f"y11n_p1_{imgsz}", exist_ok=True, patience=25,
        # the generator already owns rotation/scale/colour/walk-shift; keep
        # ultralytics' contribution to geometric + mosaic diversity
        degrees=0.0, scale=0.2, translate=0.05, fliplr=0.0, flipud=0.0,
        mosaic=0.5, close_mosaic=10, hsv_h=0.005, hsv_s=0.4, hsv_v=0.3,
        amp=True, plots=True, verbose=True,
    )


def main() -> int:
    t0 = time.time()
    from ultralytics import YOLO
    imgsz = 800
    try:
        model = YOLO("yolo11n.pt")
        model.train(data=str(DATA), epochs=EPOCHS, imgsz=imgsz, batch=BATCH,
                    workers=WORKERS, device=0, project=str(YOLO_DIR / "runs"),
                    name=f"y11n_p1_{imgsz}", exist_ok=True, patience=25,
                    degrees=0.0, scale=0.2, translate=0.05, fliplr=0.0,
                    flipud=0.0, mosaic=0.5, close_mosaic=10, hsv_h=0.005,
                    hsv_s=0.4, hsv_v=0.3, amp=True, plots=True, verbose=True)
    except RuntimeError as e:
        if "out of memory" not in str(e).lower():
            raise
        print(f"[train] imgsz=800 OOM ({e.__class__.__name__}) -> retry 640",
              flush=True)
        import torch
        torch.cuda.empty_cache()
        imgsz = 640
        model = YOLO("yolo11n.pt")
        model.train(data=str(DATA), epochs=EPOCHS, imgsz=imgsz, batch=BATCH,
                    workers=WORKERS, device=0, project=str(YOLO_DIR / "runs"),
                    name=f"y11n_p1_{imgsz}", exist_ok=True, patience=25,
                    degrees=0.0, scale=0.2, translate=0.05, fliplr=0.0,
                    flipud=0.0, mosaic=0.5, close_mosaic=10, hsv_h=0.005,
                    hsv_s=0.4, hsv_v=0.3, amp=True, plots=True, verbose=True)

    run_dir = YOLO_DIR / "runs" / f"y11n_p1_{imgsz}"
    best = run_dir / "weights" / "best.pt"
    shutil.copy(best, YOLO_DIR / "best.pt")

    # held-out synth val -> per-class mAP (the honest table)
    import torch
    torch.cuda.empty_cache()
    dmodel = YOLO(str(YOLO_DIR / "best.pt"))
    metrics = dmodel.val(data=str(DATA), imgsz=imgsz, batch=BATCH,
                         device=0, plots=False, verbose=False)
    table = {}
    names = metrics.names
    for i, cname in names.items():
        table[cname] = {
            "mAP50": round(float(metrics.box.map50[i]), 4),
            "mAP50-95": round(float(metrics.box.map[i]), 4),
            "precision": round(float(metrics.box.p[i]), 4),
            "recall": round(float(metrics.box.r[i]), 4),
        }
    summary = {
        "imgsz": imgsz, "epochs": EPOCHS, "batch": BATCH,
        "overall_mAP50": round(float(metrics.box.map50), 4),
        "overall_mAP50-95": round(float(metrics.box.map), 4),
        "per_class": table,
        "train_seconds": round(time.time() - t0, 1),
        "gpu": torch.cuda.get_device_name(0),
        "weights": str(YOLO_DIR / "best.pt"),
    }
    # ONNX export (the deployable artifact alongside the .pt)
    try:
        onnx = dmodel.export(format="onnx", imgsz=imgsz, opset=12,
                             dynamic=False, simplify=True)
        summary["onnx"] = str(onnx)
    except Exception as e:                     # export is a convenience
        summary["onnx"] = f"FAILED: {e}"
    (YOLO_DIR / "train_report.json").write_text(json.dumps(summary, indent=1),
                                                encoding="utf-8")
    print(json.dumps(summary, indent=1), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
