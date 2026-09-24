#!/usr/bin/env python3
"""playerone-v2 perceive — the eyes: OpenCV + YOLO + templates -> one dict.

The v1 context law (generic_player.py) reads two PNGs with a stdlib decoder
and a 12x20 brightness grid. v2 upgrades the eyes for Lappy's RTX 3060:
OpenCV does motion + colour zones + template matches, ultralytics yolov8n
does object boxes on the GPU, and everything flattens into the SAME kind of
text context the jevlike scorer already eats.

    perceive(frame_bgr, prev_frame_bgr) -> dict      # one call, all senses
    to_context(perception) -> str                    # "motion:strong-left |
                                                     #  health:30% | person@(240,400)
                                                     #  | menu:no | frame:42"

SENSES (each degrades independently — a missing YOLO or an empty templates/
 dir never kills the others):

  motion     absdiff(prev, frame) -> gray -> threshold -> contours. Strength
             from changed-pixel share (none/weak/strong), zone from the
             largest contour's centroid third (left/center/right).
  colors     horizontal thirds by mean brightness -> "bright:top"; dominant
             hue word over saturated pixels (red/orange/...).
  health_bar red-dominant share of the top strip, read as the classic HUD
             bar -> "health_bar:red:30%" (pct == the red share).
  objects    YOLO yolov8n (GPU when torch sees cuda, else CPU) ->
             [{"label", "conf", "box":[x,y,w,h]}]. ultralytics missing or
             weights absent -> objects [] + yolo:"unavailable".
  templates  cv2.matchTemplate against templates/*.png (menu.png,
             game_over.png, ping_ready.png) at TM_CCOEFF_NORMED >= 0.80 ->
             flags "menu_visible" / "game_over" / "ping_ready".

Selftest (offline, synthetic frames — no camera, no network):

    python perceive.py --selftest
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = Path(os.environ.get("P1V2_TEMPLATES", HERE / "templates"))
YOLO_WEIGHTS = Path(os.environ.get("P1V2_YOLO", HERE / "yolov8n.pt"))

MOTION_SHARE = {"weak": 0.004, "strong": 0.05}   # changed-pixel share bands
DIFF_THRESHOLD = 25                               # gray delta that counts as change
HEALTH_STRIP = 0.15                               # top 15% of the frame is HUD country
HEALTH_MIN = 0.02                                 # below 2% red = no bar worth naming
TEMPLATE_HIT = 0.80                               # matchTemplate confidence law
YOLO_CONF = 0.25                                  # keep boxes above this
YOLO_IMGSZ = 320                                  # phone frames are small; stay fast
MAX_BOXES_IN_CONTEXT = 3
TEMPLATE_FLAG = {"menu": "menu_visible", "game_over": "game_over",
                 "ping_ready": "ping_ready"}      # stem -> flag (default stem_visible)

_HUES = ((0, 8, "red"), (8, 22, "orange"), (22, 38, "yellow"), (38, 78, "green"),
         (78, 100, "cyan"), (100, 130, "blue"), (130, 160, "purple"),
         (160, 180, "red"))  # openCV hue is 0..180; red wraps


# ------------------------------------------------------------------ motion --
def _motion(frame: np.ndarray, prev: np.ndarray) -> tuple[str, int]:
    """absdiff + contours -> ("strong-left", changed_pixels). Deterministic:
    strength by changed share, zone by the biggest contour's centroid."""
    if prev is None or prev.shape != frame.shape:
        return "none", 0
    diff = cv2.absdiff(prev, frame)
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
    changed = int(cv2.countNonZero(thresh))
    if changed == 0:
        return "none", 0
    share = changed / thresh.size
    strength = "weak" if share < MOTION_SHARE["strong"] else "strong"
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    zone = "center"
    if contours:
        biggest = max(contours, key=cv2.contourArea)
        x, _, w, _ = cv2.boundingRect(biggest)
        cx = x + w / 2
        zone = "left" if cx < frame.shape[1] / 3 else \
               "right" if cx > 2 * frame.shape[1] / 3 else "center"
    return f"{strength}-{zone}", changed


# ------------------------------------------------------------------ colors --
def _colors(frame: np.ndarray) -> tuple[str, str]:
    """-> (brightest third, dominant hue word)."""
    h = frame.shape[0]
    thirds = {"top": frame[:h // 3], "mid": frame[h // 3:2 * h // 3],
              "bottom": frame[2 * h // 3:]}
    means = {name: float(part.mean()) for name, part in thirds.items()}
    bright = max(means, key=lambda name: means[name])
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    sat, hue = hsv[..., 1].astype(int), hsv[..., 0].astype(int)
    pick = (sat > 80) & (hue < 180)
    dominant = "neutral"
    if pick.any():
        hist = np.bincount(hue[pick], minlength=180)
        if hist.max() > 0:
            word = next(w for lo, hi, w in _HUES if lo <= int(hist.argmax()) < hi)
            dominant = word
    return bright, dominant


def _health_bar(frame: np.ndarray) -> dict | None:
    """Red-dominant share of the top HUD strip -> the classic health bar read."""
    strip = frame[:max(1, int(frame.shape[0] * HEALTH_STRIP))]
    b, g, r = (strip[..., i].astype(int) for i in (0, 1, 2))
    red = (r > 90) & (r > g + 40) & (r > b + 40)
    pct = round(100.0 * red.mean())
    if pct / 100.0 < HEALTH_MIN:
        return None
    return {"color": "red", "pct": pct}


# ------------------------------------------------------------------ objects --
_YOLO: dict = {"model": None, "state": "unloaded"}   # lazy; cached across calls


def _yolo_model():
    """Load yolov8n once. ultralytics absent or weights missing -> None and
    the objects sense degrades to [] (OpenCV-only contexts still flow)."""
    if _YOLO["state"] == "unloaded":
        try:
            from ultralytics import YOLO
            if not YOLO_WEIGHTS.is_file():
                raise FileNotFoundError(f"no yolov8n at {YOLO_WEIGHTS}")
            _YOLO["model"] = YOLO(str(YOLO_WEIGHTS))
            _YOLO["state"] = "ready"
        except Exception as exc:  # noqa: BLE001 — the whole point is degrade
            _YOLO["model"] = None
            _YOLO["state"] = f"unavailable: {type(exc).__name__}: {str(exc)[:80]}"
    return _YOLO["model"]


def yolo_state() -> str:
    _yolo_model()
    return _YOLO["state"]


def _objects(frame: np.ndarray) -> tuple[list[dict], str]:
    model = _yolo_model()
    if model is None:
        return [], _YOLO["state"]
    try:
        result = model.predict(frame, imgsz=YOLO_IMGSZ, conf=YOLO_CONF,
                               verbose=False)[0]
    except Exception as exc:  # noqa: BLE001
        return [], f"error: {type(exc).__name__}: {str(exc)[:80]}"
    names = result.names or {}
    out = []
    for box in (result.boxes or []):
        x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
        out.append({"label": str(names.get(int(box.cls[0]), int(box.cls[0]))),
                    "conf": round(float(box.conf[0]), 2),
                    "box": [int(x1), int(y1), int(x2 - x1), int(y2 - y1)]})
    return out, "ready"


# ---------------------------------------------------------------- templates --
def _template_hits(frame: np.ndarray) -> tuple[list[str], dict]:
    """matchTemplate against templates/*.png -> (flags, per-template score)."""
    flags: list[str] = []
    scores: dict[str, float] = {}
    if not TEMPLATE_DIR.is_dir():
        return flags, scores
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    for path in sorted(TEMPLATE_DIR.glob("*.png")):
        sprite = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if sprite is None or sprite.shape[0] > gray.shape[0] \
                or sprite.shape[1] > gray.shape[1]:
            continue
        result = cv2.matchTemplate(gray, sprite, cv2.TM_CCOEFF_NORMED)
        score = float(result.max())
        scores[path.stem] = round(score, 3)
        if score >= TEMPLATE_HIT:
            flags.append(TEMPLATE_FLAG.get(path.stem, f"{path.stem}_visible"))
    return flags, scores


# ------------------------------------------------------------------- the API --
def perceive(frame_bgr: np.ndarray, prev_frame_bgr: np.ndarray | None = None,
             frame: int = 0) -> dict:
    """All senses on one frame. Never raises for a missing optional sense —
    YOLO and templates degrade, OpenCV motion/colour always answer."""
    if frame_bgr is None or getattr(frame_bgr, "ndim", 0) != 3:
        raise ValueError("perceive wants a BGR ndarray")
    motion, changed = _motion(frame_bgr, prev_frame_bgr)
    bright, dominant = _colors(frame_bgr)
    objects, yolo = _objects(frame_bgr)
    flags, template_scores = _template_hits(frame_bgr)
    return {
        "motion": motion, "motion_px": changed,
        "bright": bright, "dominant_color": dominant,
        "health_bar": _health_bar(frame_bgr),
        "objects": objects, "yolo": yolo,
        "flags": flags, "templates": template_scores,
        "frame": int(frame),
    }


def to_context(p: dict) -> str:
    """Flatten to the jevlike text context. Order is fixed (motion | health |
    bright | objects | menu | game_over | ping_ready | frame) so the scorer's
    context vocabulary stays stable across runs."""
    parts = [f"motion:{p.get('motion', 'none')}"]
    bar = p.get("health_bar")
    if bar:
        parts.append(f"health:{bar['pct']}%")
    if p.get("bright"):
        parts.append(f"bright:{p['bright']}")
    for obj in (p.get("objects") or [])[:MAX_BOXES_IN_CONTEXT]:
        x, y, w, h = obj["box"]
        parts.append(f"{obj['label']}@({int(x + w / 2)},{int(y + h / 2)})")
    if not p.get("objects") and p.get("yolo", "").startswith("unavailable"):
        parts.append("objects:unknown")
    flags = set(p.get("flags") or [])
    parts.append(f"menu:{'yes' if 'menu_visible' in flags else 'no'}")
    parts.append(f"game_over:{'yes' if 'game_over' in flags else 'no'}")
    parts.append(f"ping_ready:{'yes' if 'ping_ready' in flags else 'no'}")
    parts.append(f"frame:{p.get('frame', 0)}")
    return " | ".join(parts)


# ---------------------------------------------------------------- selftest --
def _synthetic_frame(move_x: int | None, health_pct: int,
                     seed: int = 1) -> np.ndarray:
    """Random colour noise + an optional white 200x100 rectangle at
    (move_x, 380-480) + a red HUD bar covering health_pct of the top strip's
    width. The mover is sized so its appearance is ~5% of the frame — a
    strong motion, one connected blob, dead-centre."""
    rng = np.random.default_rng(seed)
    frame = rng.integers(0, 64, size=(800, 480, 3), dtype=np.uint8)
    if move_x is not None:
        frame[380:480, move_x:move_x + 200] = (255, 255, 255)  # the mover
    bar_w = int(480 * health_pct / 100)
    frame[:int(800 * HEALTH_STRIP), :bar_w] = (40, 40, 220)   # BGR red bar
    return frame


def _selftest() -> int:
    global TEMPLATE_DIR
    t0 = time.perf_counter()
    f1 = _synthetic_frame(move_x=None, health_pct=30, seed=1)
    f2 = _synthetic_frame(move_x=140, health_pct=30, seed=1)   # mover appears
    f3 = _synthetic_frame(move_x=140, health_pct=30, seed=1)   # no change
    p1 = perceive(f1, None, frame=41)
    p2 = perceive(f2, f1, frame=42)
    p3 = perceive(f3, f2, frame=43)

    assert p1["motion"] == "none", p1["motion"]        # no prev -> no motion
    assert p2["motion"] == "strong-center", p2["motion"]
    assert p2["motion_px"] >= 20000, p2["motion_px"]
    assert p3["motion"] == "none", p3["motion"]        # identical frames
    assert p2["health_bar"] == {"color": "red", "pct": 30}, p2["health_bar"]
    assert p2["bright"] in ("top", "mid", "bottom")
    print(f"motion      : {p1['motion']} -> {p2['motion']} "
          f"({p2['motion_px']} px) -> {p3['motion']} (still frames)")
    print(f"health bar  : health_bar:{p2['health_bar']['color']}:"
          f"{p2['health_bar']['pct']}%")
    print(f"colors      : bright:{p2['bright']} dominant:{p2['dominant_color']}")

    state = yolo_state()
    print(f"yolo        : {state} -> {len(p2['objects'])} box(es)"
          + (f" {[o['label'] for o in p2['objects'][:3]]}" if p2["objects"]
             else " (no detections on the noise frame)"
             if state == "ready" else
             " (graceful: OpenCV-only context keeps flowing)"))
    print(f"templates   : dir={'present' if TEMPLATE_DIR.is_dir() else 'absent'} "
          f"-> flags={p2['flags']} scores={p2['templates']}")

    # -- the template lane, proven: a game_over card on screen, its sprite cut
    #    into a temp templates/ dir, the flag must fire
    import tempfile
    card = _synthetic_frame(move_x=None, health_pct=30, seed=2)
    card[300:420, 140:340] = (90, 90, 230)                     # the card
    with tempfile.TemporaryDirectory(prefix="p1v2_tmpl_") as tmp:
        tmpl_dir = Path(tmp)
        sprite_gray = cv2.cvtColor(card[300:420, 140:340], cv2.COLOR_BGR2GRAY)
        cv2.imwrite(str(tmpl_dir / "game_over.png"), sprite_gray)
        real_dir = TEMPLATE_DIR
        try:
            TEMPLATE_DIR = tmpl_dir
            p_card = perceive(card, None, frame=50)
        finally:
            TEMPLATE_DIR = real_dir
        assert "game_over" in p_card["flags"], p_card
        assert p_card["templates"]["game_over"] >= 0.80, p_card["templates"]
        ctx_card = to_context(p_card)
        assert "game_over:yes" in ctx_card, ctx_card
        print(f"templates+  : sprite match score="
              f"{p_card['templates']['game_over']} -> flag 'game_over' -> "
              f"context says game_over:yes")

    ctx = to_context(p2)
    print(f"context(42) : {ctx}")
    assert ctx.startswith("motion:strong-center"), ctx
    assert "health:30%" in ctx, ctx
    assert "menu:no" in ctx and "frame:42" in ctx, ctx
    if not p2["objects"] and p2["yolo"].startswith("unavailable"):
        assert "objects:unknown" in ctx, ctx
    print(f"context(43) : {to_context(p3)}")

    ms = (time.perf_counter() - t0) * 1000
    print(f"selftest: PASS — motion law, health bar, colour zones, template "
          f"lane, context flattening verified offline in {ms:.0f} ms "
          f"(yolo: {state.split(':')[0]})")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="playerone-v2 perceive — "
                                 "OpenCV + YOLO + templates -> context")
    ap.add_argument("--selftest", action="store_true",
                    help="synthetic frames: prove motion, health bar, "
                         "colour zones and the context line, offline")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
