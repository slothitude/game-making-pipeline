#!/usr/bin/env python3
"""gen_data.py — PlayerOne eyes v2: synthetic YOLO dataset from the games' own
sprites.

Per game (sonar, star-visitor, gyro-squadron-45, slime-line): paste RGBA
sprites at known positions onto backgrounds sampled from the game's own
evidence frames (sonar/star-visitor) or tiled from the game's own tile art
(gyro/slime), with the augmentations the live capture domain shows:
rotation +-10deg, scale 0.8-1.3, brightness/contrast jitter, per-row band
offsets that simulate a walk cycle, sensor noise and JPEG-ish softening.
Auto-annotations come free from the paste positions.

Background hygiene: real frames already CONTAIN real sprites. Those are
unlabelled ground truth, so every v1 (template) detection >= 0.60 is inpainted
out before pasting — otherwise the model is punished for finding objects we
did not label.

The one deliberate deviation from the brief: sprites are alpha-COMPOSITED onto
the background, not flattened onto median luma. The v1 flatten law exists
because cv2.matchTemplate has no mask support — it is a template-lane artefact,
not what a live frame looks like (the engine blends sprites over the world).
A 10% flatten-to-local-median slice is kept as robustness to opaque rendering.

Classes are game-prefixed roles from roles.json, so one model serves every
game and the fusion filters by prefix. star-visitor gains a bullet class the
template lane dropped (~11x5 px — the documented gap YOLO must answer).

    python gen_data.py [--out DIR] [--per-game N] [--seed 1984] [--montage-only]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

P1 = Path("/home/aaron/playerone")
SPRITES_DIR = P1 / "sprites"
EVIDENCE = P1 / "evidence" / "perception"
sys.path.insert(0, str(P1))
import perception  # noqa: E402  (the v1 matcher — used to clean backgrounds)

FRAME_W, FRAME_H = 480, 800          # the live capture resolution (phone)
DEFAULT_PER_GAME = 480               # train per game; val is 25% of this
BG_CLEAN_CONF = 0.60                 # template hits at/above this are inpainted
FLATTEN_FRAC = 0.10                  # pastes flattened onto local median luma
NEGATIVE_FRAC = 0.05                 # pure-background images (no sprites)
MONTAGES = 6                         # sample grids saved for visual verification

# games in the dataset; octogram-arcade has no actor sprites (roles.json note)
GAMES = ["sonar", "star-visitor", "gyro-squadron-45", "slime-line"]
# the template lane dropped star-visitor's bullet (~11x5 px, sub-NCC): YOLO
# answers it. roles.json has no entry, so it is injected here at the size
# measured off the live captures (the v1 note).
EXTRA_CLASSES = {
    "star-visitor": {"threats": {"bullet": (8, 4)}},
}
# per-class scale range override — the live captures show star-visitor's
# bullet anywhere from ~4x3 to ~11x5 px, so a fixed size +-30% misses half
# the real range (measure_size.py sweep, frames 000/001)
CLASS_SCALE_RANGE = {
    "star-visitor:threats:bullet": (0.5, 2.5),
}
# formation pastes: the live game walks its agents in an OVERLAPPING row
# (frame 000 shows five at ~16px pitch) — isolated pastes never taught that
# pattern, and it is exactly where the v1 template lane breaks down
FORMATIONS = {
    "star-visitor:threats:enemy_agent": {"count": (2, 5), "pitch": (12, 18)},
}
# sprite classes whose art ANIMATES on screen (walk frames, banking poses) —
# recorded here for the fusion config; the dataset itself needs no special
# casing beyond the band-shift augmentation every class gets.
ANIMATED = {
    "star-visitor": ["threats:enemy_agent"],
    "gyro-squadron-45": ["player:player_bank_left", "threats:bullet_enemy"],
    "slime-line": ["threats:bug_green", "threats:bug_gold"],
}


def game_classes(game: str) -> dict[str, tuple[int, int] | int]:
    """role:sprite -> roles.json size, plus the injected extras."""
    roles = perception._roles(game)
    out: dict[str, object] = {}
    for role, sized in roles.items():
        for name, px in sized.items():
            out[f"{role}:{name}"] = px
    for role, sized in EXTRA_CLASSES.get(game, {}).items():
        for name, px in sized.items():
            out.setdefault(f"{role}:{name}", px)
    return out


# ------------------------------------------------------------- sprites --
class Sprite:
    """One RGBA sprite cropped to its alpha bbox at NATIVE art resolution."""

    def __init__(self, path: Path):
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise FileNotFoundError(path)
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
        if img.shape[2] == 3:
            a = np.full(img.shape[:2], 255, np.uint8)
            img = np.dstack([img, a])
        alpha = img[:, :, 3]
        ys, xs = np.where(alpha > 8)
        if ys.size == 0:
            raise ValueError(f"{path}: empty alpha")
        self.rgba = img[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        self.h, self.w = self.rgba.shape[:2]

    def at(self, tw: int, th: int) -> np.ndarray:
        return cv2.resize(self.rgba, (max(1, tw), max(1, th)),
                          interpolation=cv2.INTER_AREA)


def paste_size(canon, scale: float, native_w: int, native_h: int
               ) -> tuple[int, int]:
    """roles.json size (int long-edge or [w,h] pair) * scale -> paste box."""
    if isinstance(canon, (list, tuple)):
        return max(1, round(canon[0] * scale)), max(1, round(canon[1] * scale))
    if native_w >= native_h:
        return max(1, round(canon * scale)), \
            max(1, round(native_h * canon * scale / native_w))
    return max(1, round(native_w * canon * scale / native_h)), \
        max(1, round(canon * scale))


def walk_shift(sprite: np.ndarray, rng: np.random.Generator
               ) -> tuple[np.ndarray, int, int]:
    """Per-row band offsets: the walk cycle breaks exact templates (the v1
    enemy-agent gap), so a slice of the data must show it. Sprite splits at a
    waist line; the legs band shifts sideways by 1-3px (scaled to sprite
    size), the torso counter-shifts <=1px. Returns (rgba, dx0, dy) where
    dx0/dy is the top-left padding added."""
    h, w = sprite.shape[:2]
    step = 1 if max(h, w) < 40 else 2       # a 20px alien cannot swing 3px
    max_dx = step + (1 if max(h, w) >= 40 else 0)
    dx_legs = int(rng.integers(-max_dx, max_dx + 1))
    dx_torso = int(rng.integers(-1, 2)) if dx_legs else 0
    dy = int(rng.integers(-1, 2))           # the walk bob
    pad = max(abs(dx_legs), abs(dx_torso), abs(dy)) + 1
    out = np.zeros((h + 2 * pad, w + 2 * pad, 4), np.uint8)
    waist = int(h * float(rng.uniform(0.45, 0.6)))
    out[pad + dy:pad + waist + dy, pad + dx_torso:pad + dx_torso + w] = \
        sprite[:waist]
    out[pad + waist + dy:pad + h + dy,
        pad + dx_legs:pad + dx_legs + w] = sprite[waist:]
    return out, pad, pad


def jitter_color(rgba: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Brightness/contrast jitter on the RGB planes (alpha untouched)."""
    rgb = rgba[:, :, :3].astype(np.float32)
    rgb = rgb * float(rng.uniform(0.75, 1.25)) + float(rng.uniform(-25, 25))
    rgba[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    return rgba


def rotate(rgba: np.ndarray, angle: float) -> np.ndarray:
    h, w = rgba.shape[:2]
    diag = int(np.ceil(np.hypot(h, w))) + 2
    canvas = np.zeros((diag, diag, 4), np.uint8)
    y0, x0 = (diag - h) // 2, (diag - w) // 2
    canvas[y0:y0 + h, x0:x0 + w] = rgba
    M = cv2.getRotationMatrix2D((diag / 2.0, diag / 2.0), angle, 1.0)
    out = cv2.warpAffine(canvas, M, (diag, diag), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    a = out[:, :, 3]
    ys, xs = np.where(a > 8)
    if ys.size == 0:
        return rgba
    return out[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def render_one(spr: Sprite, canon, rng: np.random.Generator, bg_patch: int,
               scale_range: tuple[float, float] = (0.8, 1.3)
               ) -> tuple[np.ndarray, int, int]:
    """Sprite + augmentations -> (rgba ready to composite, w, h)."""
    scale = float(rng.uniform(*scale_range))
    tw, th = paste_size(canon, scale, spr.w, spr.h)
    tw, th = min(tw, FRAME_W - 4), min(th, FRAME_H - 4)
    rgba = spr.at(tw, th)
    if rng.random() < 0.8:                       # most sprites walk/bob
        rgba, _, _ = walk_shift(rgba, rng)
    if rng.random() < 0.5:
        rgba = rotate(rgba, float(rng.uniform(-10.0, 10.0)))
    rgba = jitter_color(rgba, rng)
    if rng.random() < FLATTEN_FRAC:
        # robustness to opaque/flat rendering: blend onto the local median
        # luma instead of the background pixels (the v1 calibration look)
        flat = np.full(rgba.shape[:2], int(np.clip(bg_patch, 0, 255)), np.uint8)
        flat = np.dstack([flat, flat, flat, np.full(rgba.shape[:2], 255, np.uint8)])
        a = rgba[:, :, 3:4].astype(np.float32) / 255.0
        rgb = rgba[:, :, :3].astype(np.float32) * a + \
            flat[:, :, :3].astype(np.float32) * (1 - a)
        rgba = np.dstack([rgb.astype(np.uint8),
                          np.full(rgba.shape[:2], 255, np.uint8)])
    if rng.random() < 0.12:                      # capture softness
        k = 3
        rgba[:, :, :3] = cv2.GaussianBlur(rgba[:, :, :3], (k, k), 0.8)
    return rgba, rgba.shape[1], rgba.shape[0]


def composite(frame: np.ndarray, rgba: np.ndarray, x0: int, y0: int) -> None:
    h, w = rgba.shape[:2]
    region = frame[y0:y0 + h, x0:x0 + w].astype(np.float32)
    a = rgba[:, :, 3].astype(np.float32) / 255.0
    rgb = rgba[:, :, :3].astype(np.float32)
    frame[y0:y0 + h, x0:x0 + w] = (rgb * a[..., None]
                                   + region * (1.0 - a[..., None])).astype(np.uint8)


# ----------------------------------------------------------- backgrounds --
def load_tile(game: str) -> np.ndarray | None:
    for cand in SPRITES_DIR.joinpath(game).glob("tile_*.png"):
        img = cv2.imread(str(cand), cv2.IMREAD_UNCHANGED)
        if img is not None:
            if img.ndim == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            return img[:, :, :3]
    return None


def tile_background(game: str, rng: np.random.Generator) -> np.ndarray:
    """Tiled game tile art + luminance wash + palette blobs (for games with
    no captured frames — gyro, slime)."""
    tile = load_tile(game)
    if tile is None:
        return np.full((FRAME_H, FRAME_W, 3), 40, np.uint8)
    s = float(rng.uniform(0.5, 1.5))
    t = cv2.resize(tile, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    th, tw = t.shape[:2]
    reps_y = FRAME_H // th + 2
    reps_x = FRAME_W // tw + 2
    rows = [t if i % 2 == 0 else t[:, ::-1] for i in range(reps_y)]
    big = np.vstack([np.hstack([r if j % 2 == 0 else r[::-1]
                                for j in range(reps_x)]) for r in rows])
    oy = int(rng.integers(0, th))
    ox = int(rng.integers(0, tw))
    bg = big[oy:oy + FRAME_H, ox:ox + FRAME_W].copy()
    # luminance wash + vignette: the live look is never a flat texture
    grad = np.linspace(*rng.uniform(0.8, 1.15, 2), FRAME_H,
                       dtype=np.float32)[:, None, None]
    bg = np.clip(bg.astype(np.float32) * grad, 0, 255).astype(np.uint8)
    yy, xx = np.mgrid[0:FRAME_H, 0:FRAME_W]
    d = np.hypot((xx - FRAME_W / 2) / FRAME_W, (yy - FRAME_H / 2) / FRAME_H)
    vig = np.clip(1.0 - 0.35 * d ** 2, 0.6, 1.0)[..., None]
    bg = np.clip(bg.astype(np.float32) * vig, 0, 255).astype(np.uint8)
    return bg


class Backgrounds:
    """Cleaned real frames (games with captures) + tiled art, with a real:frame
    mix so no game is stranded without domain backgrounds."""

    def __init__(self, game: str):
        self.game = game
        self.frames: list[np.ndarray] = []
        fdir = EVIDENCE / game / "frames"
        if fdir.is_dir():
            for p in sorted(fdir.glob("*.jpg")):
                img = cv2.imread(str(p))
                if img is not None:
                    self.frames.append(self._clean(img))
        self.tile = load_tile(game)
        self.palette = self._palette()

    def _clean(self, frame: np.ndarray) -> np.ndarray:
        """Inpaint every v1 template hit — unlabelled real instances would
        poison the paste annotations."""
        out = frame.copy()
        try:
            state = perception.perceive(frame, self.game, threshold=BG_CLEAN_CONF)
        except Exception:
            return out
        mask = np.zeros(frame.shape[:2], np.uint8)
        for cls, x, y, w, h, conf in state.get("all", []):
            mask[max(0, y - 2):y + h + 2, max(0, x - 2):x + w + 2] = 255
        if mask.any():
            out = cv2.inpaint(out, mask, 4, cv2.INPAINT_TELEA)
        return out

    def _palette(self) -> list[tuple[int, int, int]]:
        """Dominant colours of the game's own art — for soft background blobs."""
        cols: list[tuple[int, int, int]] = []
        for p in sorted(SPRITES_DIR.joinpath(self.game).glob("*.png"))[:6]:
            img = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
            if img is None:
                continue
            small = cv2.resize(img[:, :, :3], (8, 8)).reshape(-1, 3)
            cols.extend((int(c[0]), int(c[1]), int(c[2])) for c in small)
        return cols or [(40, 40, 40)]

    def sample(self, rng: np.random.Generator) -> np.ndarray:
        if self.frames and rng.random() < 0.85:
            base = self.frames[int(rng.integers(0, len(self.frames)))]
            # a shifted view of the capture: breaks background memorisation
            # without changing the domain (edge-replicated jitter crop)
            jy = int(rng.integers(-40, 41))
            jx = int(rng.integers(-30, 31))
            pad = np.repeat(base[0:1], 60, axis=0)
            base = np.vstack([pad, base, base[-60:]])
            base = np.hstack([np.repeat(base[:, 0:1], 60, axis=1), base,
                              np.repeat(base[:, -1:], 60, axis=1)])
            y0 = 60 + jy
            x0 = 60 + jx
            bg = base[y0:y0 + FRAME_H, x0:x0 + FRAME_W].copy()
        else:
            bg = tile_background(self.game, rng) if self.tile is not None \
                else np.full((FRAME_H, FRAME_W, 3),
                             int(rng.integers(20, 70)), np.uint8)
        if self.palette and rng.random() < 0.5:
            # soft palette blob: cheap lighting variety from the game's colours
            layer = np.zeros_like(bg)
            c = self.palette[int(rng.integers(0, len(self.palette)))]
            cx, cy = int(rng.integers(0, FRAME_W)), int(rng.integers(0, FRAME_H))
            r = int(rng.integers(80, 240))
            cv2.circle(layer, (cx, cy), r, c, -1)
            layer = cv2.GaussianBlur(layer, (0, 0), 60)
            bg = cv2.addWeighted(bg, 0.85, layer, 0.15, 0)
        # global exposure jitter + sensor noise
        bg = np.clip(bg.astype(np.float32) * float(rng.uniform(0.85, 1.15))
                     + float(rng.uniform(-12, 12)), 0, 255).astype(np.uint8)
        bg = np.clip(bg.astype(np.float32)
                     + rng.normal(0, float(rng.uniform(1.5, 4.0)),
                                  bg.shape).astype(np.float32),
                     0, 255).astype(np.uint8)
        return bg


# ------------------------------------------------------------------ main --
def build_game(game: str, n_train: int, out: Path, seed: int,
               montage_names: list[str]) -> dict:
    rng = np.random.default_rng(seed)
    classes = game_classes(game)
    cls_names = sorted(classes)
    cls_idx = {c: i for i, c in enumerate(cls_names)}
    sprites = {}
    for c in cls_names:
        name = c.split(":", 1)[1]
        p = SPRITES_DIR / game / f"{name}.png"
        if p.is_file():
            try:
                sprites[c] = Sprite(p)
            except (FileNotFoundError, ValueError):
                pass
    classes = {c: s for c, s in classes.items() if c in sprites}
    cls_names = sorted(classes)
    cls_idx = {c: i for i, c in enumerate(cls_names)}
    bgs = Backgrounds(game)

    stats = {"game": game, "images": 0, "instances": 0,
             "per_class": {c: 0 for c in cls_names},
             "backgrounds_real": len(bgs.frames)}
    cycle = cls_names * 3
    for split, count in (("train", n_train), ("val", max(1, n_train // 4))):
        img_dir = out / "images" / split
        lbl_dir = out / "labels" / split
        img_dir.mkdir(parents=True, exist_ok=True)
        lbl_dir.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            bg = bgs.sample(rng)
            frame = bg
            local_median = int(np.median(cv2.cvtColor(frame,
                                                       cv2.COLOR_BGR2GRAY)))
            boxes: list[tuple[int, float, float, float, float]] = []

            def paste(cls: str, rgba: np.ndarray, w: int, h: int) -> bool:
                if w >= FRAME_W or h >= FRAME_H:
                    return False
                x0 = int(rng.integers(0, FRAME_W - w))
                y0 = int(rng.integers(0, FRAME_H - h))
                composite(frame, rgba, x0, y0)
                boxes.append((cls_idx[cls],
                              (x0 + w / 2) / FRAME_W,
                              (y0 + h / 2) / FRAME_H,
                              w / FRAME_W, h / FRAME_H))
                stats["per_class"][cls] += 1
                return True

            if rng.random() >= NEGATIVE_FRAC:      # 5% pure negatives
                n = int(rng.integers(6, 12))
                for j in range(n):
                    if j < len(cycle):
                        cls = cycle[(i * 7 + j) % len(cycle)]
                    else:
                        cls = cls_names[int(rng.integers(0, len(cls_names)))]
                    form = FORMATIONS.get(f"{game}:{cls}")
                    if form is not None and rng.random() < 0.5:
                        # the walking row: one render, shared scale, same y,
                        # overlapping pitch (a real squad is uniform)
                        rgba0, w0, h0 = render_one(
                            sprites[cls], classes[cls], rng, local_median,
                            CLASS_SCALE_RANGE.get(f"{game}:{cls}",
                                                  (0.8, 1.3)))
                        lo, hi = form["count"]
                        pitch_lo, pitch_hi = form["pitch"]
                        pitch = int(rng.integers(pitch_lo, pitch_hi + 1))
                        count = int(rng.integers(lo, hi + 1))
                        span = w0 + pitch * (count - 1)
                        if span < FRAME_W - 8 and h0 < FRAME_H:
                            x0 = int(rng.integers(0, FRAME_W - span))
                            y0 = int(rng.integers(0, FRAME_H - h0))
                            for k in range(count):
                                composite(frame, rgba0, x0 + k * pitch, y0)
                                boxes.append(
                                    (cls_idx[cls],
                                     (x0 + k * pitch + w0 / 2) / FRAME_W,
                                     (y0 + h0 / 2) / FRAME_H,
                                     w0 / FRAME_W, h0 / FRAME_H))
                                stats["per_class"][cls] += 1
                        continue
                    rgba, w, h = render_one(
                        sprites[cls], classes[cls], rng, local_median,
                        CLASS_SCALE_RANGE.get(f"{game}:{cls}", (0.8, 1.3)))
                    paste(cls, rgba, w, h)
            flip = rng.random() < 0.3
            if flip:
                # fliplr; centres mirror. ascontiguousarray: cv2 refuses to
                # draw on a negative-stride view
                frame = np.ascontiguousarray(frame[:, ::-1])
                boxes = [(ci, 1.0 - cx, cy, w, h) for ci, cx, cy, w, h in boxes]
            labels = [f"{ci} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"
                      for ci, cx, cy, w, h in boxes]
            stem = f"{game}_{split}_{i:05d}"
            cv2.imwrite(str(img_dir / f"{stem}.jpg"), frame,
                        [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(78, 95))])
            (lbl_dir / f"{stem}.txt").write_text("\n".join(labels) + "\n",
                                                 encoding="utf-8")
            stats["images"] += 1
            stats["instances"] += len(labels)
            if split == "train" and len(montage_names) < MONTAGES and i < 3:
                vis = frame
                for ci, cx, cy, nw, nh in boxes:
                    x0 = int((cx - nw / 2) * FRAME_W)
                    y0 = int((cy - nh / 2) * FRAME_H)
                    cv2.rectangle(vis, (x0, y0),
                                  (x0 + int(nw * FRAME_W), y0 + int(nh * FRAME_H)),
                                  (0, 255, 0), 1)
                cv2.putText(vis, f"{game} {split} #{i}", (8, 20),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
                montage_names.append((vis, f"montage_{game}_{split}_{i}.jpg"))
    stats["classes"] = cls_names
    return stats


def main(argv: list[str]) -> int:
    args = argv[1:]
    out = Path(args[args.index("--out") + 1]) if "--out" in args \
        else P1 / "yolo" / "datasets" / "p1"
    per_game = int(args[args.index("--per-game") + 1]) \
        if "--per-game" in args else DEFAULT_PER_GAME
    seed = int(args[args.index("--seed") + 1]) if "--seed" in args else 1984

    out.mkdir(parents=True, exist_ok=True)
    names: dict[int, str] = {}
    offset = 0
    all_stats = []
    montages: list[tuple[np.ndarray, str]] = []
    for gi, game in enumerate(GAMES):
        gstats = build_game(game, per_game, out, seed + gi * 101, montages)
        # global class ids: game-prefixed, games never share a class id
        for c in gstats["classes"]:
            names[offset] = f"{game}.{c}"
            offset += 1
        # remap this game's label files to the global ids
        for split in ("train", "val"):
            for lf in (out / "labels" / split).glob(f"{game}_*.txt"):
                lines = []
                for ln in lf.read_text(encoding="utf-8").splitlines():
                    if not ln.strip():
                        continue
                    ci, rest = ln.split(" ", 1)
                    lines.append(f"{offset - len(gstats['classes']) + int(ci)} {rest}")
                lf.write_text("\n".join(lines) + "\n", encoding="utf-8")
        gstats["class_id_base"] = offset - len(gstats["classes"])
        all_stats.append(gstats)
        print(f"{game}: {gstats['images']} imgs, {gstats['instances']} inst, "
              f"{len(gstats['classes'])} classes, "
              f"{gstats['backgrounds_real']} real backgrounds")

    # class-map + yaml
    (out / "classes.json").write_text(json.dumps(
        {str(k): v for k, v in names.items()}, indent=1), encoding="utf-8")
    yaml = f"path: {out}\ntrain: images/train\nval: images/val\nnames:\n"
    yaml += "".join(f"  {k}: {v}\n" for k, v in names.items())
    (out / "p1.yaml").write_text(yaml, encoding="utf-8")

    # the 6 verification montages (2x3 grid) — visual check before training
    if montages:
        tiles = []
        for img, _ in montages[:MONTAGES]:
            tiles.append(cv2.resize(img, (FRAME_W // 2, FRAME_H // 2)))
        while len(tiles) % 3:
            tiles.append(np.zeros_like(tiles[0]))
        rows = [np.hstack(tiles[i:i + 3]) for i in range(0, len(tiles), 3)]
        grid = np.vstack(rows)
        sdir = P1 / "yolo" / "samples"
        sdir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(sdir / "montages.jpg"), grid)
        for img, name in montages[:MONTAGES]:
            cv2.imwrite(str(sdir / name), img)

    (P1 / "yolo" / "dataset_stats.json").write_text(
        json.dumps({"per_game": all_stats, "total_images":
                    sum(s["images"] for s in all_stats),
                    "total_instances": sum(s["instances"] for s in all_stats),
                    "classes": names}, indent=1), encoding="utf-8")
    print(f"dataset: {out}  total={sum(s['images'] for s in all_stats)} imgs, "
          f"{sum(s['instances'] for s in all_stats)} instances, "
          f"{offset} classes, {len(montages)} montages")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
