#!/usr/bin/env python3
"""playerone-lappy perception — PlayerOne's eyes v1: OpenCV template matching.

The move: the v1 critic reads screenshots as prose (LLM descriptions of a
brightness grid). This gives PlayerOne cheap deterministic eyes instead:
cv2.matchTemplate (TM_CCOEFF_NORMED) of each game's own sprite art against
the live frame, multi-scale, NMS'd into one state dict.

    perceive(frame_bgr, game) -> {"t", "player", "threats", "items", "all"}

SPRITES: sprites/<game>/*.png — the game's generated art scp'd off retromonkey
(games-src/<game>/assets/generated). sprites/<game>/roles.json says which
sprite is the player, which are threats, which are items; anything not named
there (tiles, title logos) is not searched for. Sizes in roles.json are the
on-screen long-edge px at SEARCH_WIDTH, read off each game's feel.gd comments.

TM_CCOEFF_NORMED has no mask support, so RGBA sprites are flattened: cropped
to the alpha bounding box, then alpha-blended over a neutral grey. Search runs
on the grayscale frame resized to SEARCH_WIDTH (games scale, so every template
is tried at 0.8/1.0/1.25x); hits are NMS'd per class, then across classes, and
scaled back to the caller's pixel space.

CLI (offline, no network):
    python perception.py --selftest          # synthetic frames -> recall/px
    python perception.py --bench --game sonar [--frames 30]
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
SPRITES_DIR = Path(os.environ.get("P1_SPRITES", HERE / "sprites"))

SEARCH_WIDTH = 480        # frames are searched at this width, hits scaled back
MATCH_THRESHOLD = 0.75    # TM_CCOEFF_NORMED score a hit must clear
# roles.json sizes: an int is the on-screen long-edge px; a [w, h] pair is the
# exact on-screen box — some games draw target_size non-uniformly (star-visitor
# draws its alien at 20x28 where the art's aspect gives 18x28), and at these
# sizes two px of width is ~0.2 of NCC
MATCH_SCALES = (0.8, 1.0, 1.25)   # games scale; search around each class size
COARSE_DIVISOR = 2        # the cheap pass scans at SEARCH_WIDTH // this, then
                          # promising peaks are re-scored at full search res
COARSE_SLACK = 0.06       # coarse threshold slack — refine re-applies the law
MAX_REFINE_PER_CLASS = 4  # coarse candidates re-scored per class (top conf)
NMS_IOU = 0.35            # same-class boxes above this IoU collapse to best
CROSS_CLASS_IOU = 0.5     # different-class overlap keeps the higher conf
MAX_HITS_PER_MAP = 12     # candidate peaks kept per template-scale heatmap
DEFAULT_CLASS_PX = 48     # canonical long edge when roles.json gives no size
MIN_TEMPLATE_STD = 4.0    # flatter than this carries no NCC information —
                          # sonar's creature_lurker.png is an opaque black square
NEUTRAL = 127             # grey sprites are alpha-flattened onto ...
BG_BIN = 16               # ... or onto the frame's own median luma, quantized
                          # to this bin (sonar's sea sits at ~40: a sprite
                          # flattened onto 127 loses ~0.07 NCC against it)
SELFTEST_TOL_PX = 8       # a recovered centre must be within this of the truth
SELFTEST_MIN_RECALL = 0.9
SELFTEST_MIN_SPRITE_PX = 36   # sub-36px art is below template resolution —
                              # still searched live, but outside the recall bar
SELFTEST_SIZE = (480, 800)        # portrait synthetic frame (the phone frame)
BENCH_SIZE = (1280, 720)  # the 720p perf target frame


# ------------------------------------------------------------- sprites --
def _roles(game: str) -> dict:
    """sprites/<game>/roles.json -> {"player": {name: size}, "threats": ...}.

    size is an int (on-screen long-edge px) or a [w, h] pair. Missing file or
    malformed entries are a perception gap, not a crash: the game just yields
    no templates for the roles it does not declare.
    """
    path = SPRITES_DIR / game / "roles.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    roles: dict[str, dict] = {}
    for role in ("player", "threats", "items"):
        block = raw.get(role)
        if not isinstance(block, dict):
            continue
        sized: dict = {}
        for name, px in block.items():
            try:
                if isinstance(px, (list, tuple)) and len(px) == 2:
                    sized[str(name)] = (int(px[0]), int(px[1]))
                else:
                    sized[str(name)] = int(px) if px else DEFAULT_CLASS_PX
            except (TypeError, ValueError):
                sized[str(name)] = DEFAULT_CLASS_PX
        roles[role] = sized
    return roles


def _flatten(sprite_path: Path, canon_px, bg: int = NEUTRAL
             ) -> np.ndarray | None:
    """One sprite -> grayscale template at the roles.json size.

    canon_px is an int (long edge) or a (w, h) pair for art a game draws
    non-uniformly. RGBA: crop to the alpha bounding box, alpha-blend onto a
    constant grey — usually the frame's own median luma (bg), NEUTRAL when the
    caller has no frame yet (the matchTemplate law here has no mask, so the
    flat surround must be constant). Returns None for unreadable/empty art —
    one bad png never kills the game.
    """
    img = cv2.imread(str(sprite_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 2:
        gray = img
    else:
        if img.shape[2] == 4:
            alpha = img[:, :, 3]
            ys, xs = np.where(alpha > 8)
            if ys.size == 0:
                return None
            img = img[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            alpha = img[:, :, 3].astype(np.float32) / 255.0
            rgb = img[:, :, :3]
            gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY).astype(np.float32)
            gray = gray * alpha + float(bg) * (1.0 - alpha)
        else:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = gray.astype(np.uint8)
    h, w = gray.shape[:2]
    if isinstance(canon_px, tuple):
        size = (max(1, canon_px[0]), max(1, canon_px[1]))
    else:
        long_edge = max(h, w)
        if long_edge <= 0:
            return None
        if w >= h:
            size = (canon_px, max(1, round(h * canon_px / w)))
        else:
            size = (max(1, round(w * canon_px / h)), canon_px)
    if size != (w, h):
        gray = cv2.resize(gray, size, interpolation=cv2.INTER_AREA)
    if float(gray.std()) < MIN_TEMPLATE_STD:
        return None   # degenerate art (a flat square) — NCC would fire at 1.0
    return gray


def _templates(game: str, bg: int = NEUTRAL) -> dict[str, list[np.ndarray]]:
    """role class name (e.g. "threats:bug_gold") -> list of gray templates.

    Cached per (game, bg bin); a sprite missing from disk just drops out of
    the list.
    """
    key = (game, int(bg))
    cached = _TEMPLATE_CACHE.get(key)
    if cached is not None:
        return cached
    out: dict[str, list[np.ndarray]] = {}
    for role, sized in _roles(game).items():
        for name, canon_px in sized.items():
            path = SPRITES_DIR / game / f"{name}.png"
            tmpl = _flatten(path, canon_px, bg) if path.is_file() else None
            if tmpl is None:
                continue
            out.setdefault(f"{role}:{name}", []).append(tmpl)
    _TEMPLATE_CACHE[key] = out
    return out


_TEMPLATE_CACHE: dict[tuple, dict[str, list[np.ndarray]]] = {}


def _frame_bg(gray: np.ndarray) -> int:
    """The search image's median luma, quantized to BG_BIN — the grey sprites
    get flattened onto for this frame (bounded template-cache key space)."""
    return int(round(float(np.median(gray)) / BG_BIN) * BG_BIN)


# ------------------------------------------------------------- matching --
def _peaks(res: np.ndarray, threshold: float) -> list[tuple[float, int, int]]:
    """Top peaks of a matchTemplate heatmap above threshold.

    argpartition keeps the work bounded: a flat plateau never floods NMS.
    """
    h, w = res.shape
    flat = res.reshape(-1)
    k = min(MAX_HITS_PER_MAP, flat.size)
    idx = np.argpartition(flat, flat.size - k)[-k:]
    out = []
    for i in idx:
        conf = float(flat[i])
        if conf >= threshold:
            out.append((conf, int(i // w), int(i % w)))
    return out


def _iou(a, b) -> float:
    """IoU of two det tuples (cls, x, y, w, h, conf)."""
    ax0, ay0, ax1, ay1 = a[1], a[2], a[1] + a[3], a[2] + a[4]
    bx0, by0, bx1, by1 = b[1], b[2], b[1] + b[3], b[2] + b[4]
    iw = min(ax1, bx1) - max(ax0, bx0)
    ih = min(ay1, by1) - max(ay0, by0)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / float(a[3] * a[4] + b[3] * b[4] - inter)


def _nms(dets: list[tuple], iou_limit: float) -> list[tuple]:
    """Greedy NMS: highest conf survives, overlapping lower conf falls."""
    kept: list[tuple] = []
    for det in sorted(dets, key=lambda d: -d[-1]):
        if all(_iou(det, kept_) <= iou_limit for kept_ in kept):
            kept.append(det)
    return kept


def _refine(gray: np.ndarray, tmpl: np.ndarray, x: int, y: int,
            threshold: float) -> tuple | None:
    """Re-score one coarse peak at full search resolution.

    A crop around the peak (scaled up by COARSE_DIVISOR) is cheap to match and
    restores the position/confidence the coarse scan blurred away. None means
    the peak did not survive the real threshold.
    """
    fh, fw = gray.shape[:2]
    th, tw = tmpl.shape[:2]
    pad = max(tw, th) // 2 + 2
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    crop = gray[y0:min(fh, y + th + pad), x0:min(fw, x + tw + pad)]
    if crop.shape[0] < th or crop.shape[1] < tw:
        return None
    res = cv2.matchTemplate(crop, tmpl, cv2.TM_CCOEFF_NORMED)
    _, conf, _, loc = cv2.minMaxLoc(res)
    if conf < threshold:
        return None
    return x0 + loc[0], y0 + loc[1], tw, th, round(float(conf), 4)


def perceive(frame_bgr: np.ndarray, game: str,
             threshold: float = MATCH_THRESHOLD) -> dict:
    """One frame -> the state dict. Coordinates are the caller's pixels.

    Two passes keep the CPU bill low: every template-scale is scanned on the
    half-width coarse image (the DFT cost of matchTemplate is per-call, so the
    small image is what saves the millisecond), then the top candidates per
    class are re-scored on the full-width search image where positions are
    exact.

    {"t": epoch seconds, "frame": [w, h], "ms": matcher wall ms,
     "player": (x, y, conf) of the best player hit or None,
     "threats": [(cls, x, y, w, h, conf)], "items": [...],
     "all": [every detection]}
    """
    t0 = time.perf_counter()
    fh, fw = frame_bgr.shape[:2]
    empty = {"t": time.time(), "frame": [fw, fh], "player": None,
             "threats": [], "items": [], "all": []}
    if fw <= 0 or fh <= 0:
        return empty
    k = SEARCH_WIDTH / fw
    sw, sh = SEARCH_WIDTH, max(1, round(fh * k))
    back = fw / float(sw)
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    if (sw, sh) != (fw, fh):
        gray = cv2.resize(gray, (sw, sh), interpolation=cv2.INTER_AREA)
    cw = max(1, sw // COARSE_DIVISOR)
    ch = max(1, sh // COARSE_DIVISOR)
    coarse = cv2.resize(gray, (cw, ch), interpolation=cv2.INTER_AREA)
    coarse_floor = threshold - COARSE_SLACK

    by_role: dict[str, list[tuple]] = {}
    for cls, tmpls in _templates(game, _frame_bg(gray)).items():
        role = cls.split(":", 1)[0]
        candidates: list[tuple[float, int, int, tuple, int]] = []
        for t_idx, tmpl in enumerate(tmpls):
            th, tw = tmpl.shape[:2]
            fine_sizes = []
            for s in MATCH_SCALES:
                size = (max(1, round(tw * s)), max(1, round(th * s)))
                if size[0] < sw and size[1] < sh:
                    fine_sizes.append(size)
            for size in fine_sizes:
                csize = (max(2, size[0] // COARSE_DIVISOR),
                         max(2, size[1] // COARSE_DIVISOR))
                if csize[0] >= cw or csize[1] >= ch:
                    continue
                ctmpl = cv2.resize(tmpl, csize, interpolation=cv2.INTER_AREA)
                res = cv2.matchTemplate(coarse, ctmpl, cv2.TM_CCOEFF_NORMED)
                for conf, cy, cx in _peaks(res, coarse_floor):
                    candidates.append((conf,
                                       cx * COARSE_DIVISOR,
                                       cy * COARSE_DIVISOR, size, t_idx))
        hits: list[tuple] = []
        for conf, x, y, size, t_idx in sorted(
                candidates, key=lambda c: -c[0])[:MAX_REFINE_PER_CLASS]:
            ftmpl = cv2.resize(tmpls[t_idx], size,
                               interpolation=cv2.INTER_AREA)
            got = _refine(gray, ftmpl, x, y, threshold)
            if got is None:
                continue
            rx, ry, rw, rh, rconf = got
            hits.append((cls, round(rx * back), round(ry * back),
                         round(rw * back), round(rh * back), rconf))
        by_role[role] = by_role.get(role, []) + _nms(hits, NMS_IOU)

    all_dets = _nms([d for dets in by_role.values() for d in dets],
                    CROSS_CLASS_IOU)
    player = None
    players = [d for d in all_dets if d[0].startswith("player:")]
    if players:
        best = max(players, key=lambda d: d[-1])
        player = (round(best[1] + best[3] / 2), round(best[2] + best[4] / 2),
                  best[-1])
    threats = [d for d in all_dets if d[0].startswith("threats:")]
    items = [d for d in all_dets if d[0].startswith("items:")]
    return {"t": time.time(), "frame": [fw, fh],
            "ms": round((time.perf_counter() - t0) * 1000.0, 2),
            "player": player, "threats": threats, "items": items,
            "all": all_dets}


def annotate(frame_bgr: np.ndarray, state: dict) -> np.ndarray:
    """Frame + green boxes/labels on every detection (for the evidence set)."""
    out = frame_bgr.copy()
    for cls, x, y, w, h, conf in state.get("all", []):
        cv2.rectangle(out, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(out, f"{cls} {conf:.2f}", (x, max(12, y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1,
                    cv2.LINE_AA)
    return out


# ------------------------------------------------------------- selftest --
def _synthetic_frame(game: str, size: tuple[int, int], rng: np.random.Generator
                     ) -> tuple[np.ndarray, list[tuple[str, int, int]]]:
    """Background + sprites pasted at known centres (the selftest ground truth
    set). Paste size is canon_px * frame_width/SEARCH_WIDTH * a MATCH_SCALE —
    the size that sprite would have on a screen that wide, i.e. exactly what
    the bank offers once the frame is downscaled for search."""
    w, h = size
    base = np.full((h, w, 3), NEUTRAL, np.uint8)
    # a gentle vertical wash (+/-5%) — art flattened onto NEUTRAL can never
    # match over a strongly shifted backdrop (the surround ring dominates the
    # window), so the selftest keeps the backdrop near the neutral it was
    # built for; live frames with dark sea/space pay that same tax
    grad = np.linspace(0.95, 1.05, h, dtype=np.float32)[:, None, None]
    base = np.clip(base.astype(np.float32) * grad, 0, 255).astype(np.uint8)
    frame = base

    sprites: list[tuple[str, Path, object]] = []
    for role, sized in _roles(game).items():
        for name, size in sized.items():
            path = SPRITES_DIR / game / f"{name}.png"
            long = max(size) if isinstance(size, tuple) else size
            # the 36px floor guards *estimates*; an explicit [w, h] pair is a
            # measurement off live captures — it is in, whatever its size
            covered = isinstance(size, tuple) or long >= SELFTEST_MIN_SPRITE_PX
            if path.is_file() and covered:
                sprites.append((f"{role}:{name}", path, size))
    rng.shuffle(sprites)
    sprites = sprites[:10]

    truth: list[tuple[str, int, int]] = []
    placed: list[tuple[int, int, int, int]] = []   # x0, y0, x1, y1, inflated
    for cls, path, size in sprites:
        sprite = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if sprite is None:
            continue
        scale = float(rng.choice(MATCH_SCALES)) * (w / SEARCH_WIDTH)
        if isinstance(size, tuple):
            tw = max(1, int(round(size[0] * scale)))
            th = max(1, int(round(size[1] * scale)))
        else:
            long_px = size * scale
            sh_, sw_ = sprite.shape[:2]
            if sw_ >= sh_:
                tw = int(round(long_px))
                th = max(1, int(round(sh_ * long_px / sw_)))
            else:
                tw = max(1, int(round(sw_ * long_px / sh_)))
                th = int(round(long_px))
        if th >= h or tw >= w:
            continue
        pad = 12   # keep pastes apart so NMS never has a reason to merge them
        x0 = y0 = -1
        for _ in range(40):
            tx = int(rng.integers(0, w - tw + 1))
            ty = int(rng.integers(0, h - th + 1))
            box = (tx - pad, ty - pad, tx + tw + pad, ty + th + pad)
            if all(box[2] < p[0] or box[0] > p[2] or box[3] < p[1]
                   or box[1] > p[3] for p in placed):
                x0, y0 = tx, ty
                placed.append(box)
                break
        if x0 < 0:
            continue   # frame is crowded — this sprite just sits this one out
        if sprite.shape[2] == 4:
            # crop to the alpha bbox exactly like _flatten does, so a pasted
            # sprite's long edge IS the canon size the template bank expects
            a8 = sprite[:, :, 3]
            ys, xs = np.where(a8 > 8)
            if ys.size:
                sprite = sprite[ys.min():ys.max() + 1,
                                xs.min():xs.max() + 1]
            alpha = sprite[:, :, 3].astype(np.float32) / 255.0
            rgb = sprite[:, :, :3]
        else:
            alpha = np.ones(sprite.shape[:2], np.float32)
            rgb = sprite
        rgb = cv2.resize(rgb, (tw, th), interpolation=cv2.INTER_AREA)
        alpha = cv2.resize(alpha, (tw, th), interpolation=cv2.INTER_AREA)
        region = frame[y0:y0 + th, x0:x0 + tw].astype(np.float32)
        blended = rgb.astype(np.float32) * alpha[..., None] + \
            region * (1.0 - alpha[..., None])
        frame[y0:y0 + th, x0:x0 + tw] = blended.astype(np.uint8)
        truth.append((cls, x0 + tw // 2, y0 + th // 2))

    noise = rng.normal(0, 3.0, frame.shape).astype(np.float32)
    frame = np.clip(frame.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    return frame, truth


def _score(state: dict, truth: list, tol: int) -> dict:
    """Match detections to truth centres (same class, nearest) -> numbers."""
    used: set[int] = set()
    errors: list[int] = []
    misses: list[str] = []
    for cls, tx, ty in truth:
        best_i, best_d = None, tol + 1
        for i, det in enumerate(state.get("all", [])):
            if i in used or det[0] != cls:
                continue
            _, x, y, w, h, _ = det
            d = round(((x + w / 2 - tx) ** 2 + (y + h / 2 - ty) ** 2) ** 0.5)
            if d < best_d:
                best_i, best_d = i, d
        if best_i is not None:
            used.add(best_i)
            errors.append(best_d)
        else:
            misses.append(cls)
    claimed = {det[0] for i, det in enumerate(state.get("all", []))
               if i not in used}
    return {"expected": len(truth), "found": len(errors),
            "recall": len(errors) / len(truth) if truth else 1.0,
            "max_err_px": max(errors) if errors else 0,
            "missed": misses, "false_classes": sorted(claimed),
            "false_positives": len(state.get("all", [])) - len(errors)}


def selftest() -> int:
    """Synthetic frames for every game that has sprites -> recall/px report."""
    games = sorted(p.name for p in SPRITES_DIR.iterdir() if p.is_dir()) \
        if SPRITES_DIR.is_dir() else []
    if not games:
        print(f"FAIL: no sprite dirs under {SPRITES_DIR}")
        return 1
    ok = True
    for game in games:
        rng = np.random.default_rng(1984)
        for size in (SELFTEST_SIZE, (960, 540)):
            frame, truth = _synthetic_frame(game, size, rng)
            if not truth:
                print(f"  {game} {size}: no sprites to paste — skipped")
                continue
            state = perceive(frame, game)
            score = _score(state, truth, SELFTEST_TOL_PX)
            passed = (score["recall"] >= SELFTEST_MIN_RECALL
                      and score["max_err_px"] <= SELFTEST_TOL_PX)
            ok = ok and passed
            detail = ""
            if score["missed"]:
                detail += f" missed={score['missed']}"
            if score["false_classes"]:
                detail += f" fp={score['false_classes']}"
            print(f"  {game} {size[0]}x{size[1]}: {score['found']}/"
                  f"{score['expected']} recall={score['recall']:.2f} "
                  f"max_err={score['max_err_px']}px"
                  f"{detail} ms={state['ms']}"
                  f" -> {'PASS' if passed else 'FAIL'}")
    print("SELFTEST " + ("GREEN" if ok else "RED"))
    return 0 if ok else 1


# ------------------------------------------------------------------ bench --
def bench(game: str, frames: int = 30) -> float:
    """Mean perceive() ms over synthetic BENCH_SIZE frames (the 720p target)."""
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 255, (BENCH_SIZE[1], BENCH_SIZE[0], 3),
                         dtype=np.uint8)
    n_tmpl = sum(len(t) for t in _templates(game).values())
    times = []
    for _ in range(frames):
        state = perceive(frame, game)
        times.append(state["ms"])
    times.sort()
    mean = sum(times) / len(times)
    print(f"bench {game}: {n_tmpl} templates x{len(MATCH_SCALES)} scales "
          f"@ {BENCH_SIZE[0]}x{BENCH_SIZE[1]} mean={mean:.1f}ms "
          f"p50={times[len(times) // 2]:.1f}ms max={times[-1]:.1f}ms "
          f"({frames} frames)")
    return mean


def main(argv: list[str]) -> int:
    args = argv[1:]
    if "--selftest" in args:
        return selftest()
    if "--bench" in args:
        game = args[args.index("--game") + 1] if "--game" in args else "sonar"
        n = int(args[args.index("--frames") + 1]) if "--frames" in args else 30
        bench(game, n)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
