"""Locate stamp ink and propose candidate stamp regions.

Ink map: a pixel is ink if it is clearly *darker* and *less yellow* (Lab b*)
than the local carton background, or if it is saturated blue in HSV.
The relative test matters on real footage: phone/CCTV white balance often
turns navy ink into a neutral dark grey on kraft, which a strict HSV blue
mask misses. The strict HSV mask catches bright blue ink (and the blue
belt, which is removed by region-size filtering).
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class InkParams:
    min_dark: float = 14.0  # L units darker than local background
    min_blue: float = 5.0  # b* units less yellow than local background
    strong_dark: float = 45.0  # very dark: accept with small blue shift
    strong_dark_blue: float = 1.0
    hsv_hue: tuple[int, int] = (95, 135)  # OpenCV hue (0..180) for blue
    hsv_min_s: int = 70
    hsv_min_v: int = 40
    bg_kernel_frac: float = 0.06  # background closing kernel, frac of min side


def ink_map(img: np.ndarray, p: InkParams | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Return (binary ink mask uint8 0/255, float ink strength map)."""
    p = p or InkParams()
    h, w = img.shape[:2]
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    L, b = lab[..., 0], lab[..., 2]
    # local background = grey closing (removes dark/thin strokes); done at
    # reduced resolution for speed.
    f = 0.5 if min(h, w) > 400 else 1.0
    ks = max(5, int(min(h, w) * f * p.bg_kernel_frac) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ks, ks))

    def background(ch: np.ndarray) -> np.ndarray:
        small = cv2.resize(ch, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f != 1 else ch
        bg = cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel)
        bg = cv2.GaussianBlur(bg, (0, 0), ks / 4)
        return cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR) if f != 1 else bg

    dark = background(L) - L
    blue = background(b) - b
    rel = ((dark > p.min_dark) & (blue > p.min_blue)) | (
        (dark > p.strong_dark) & (blue > p.strong_dark_blue)
    )
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hue, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    strict = (hue >= p.hsv_hue[0]) & (hue <= p.hsv_hue[1]) & (s >= p.hsv_min_s) & (v >= p.hsv_min_v)
    mask = (rel | strict).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    mask = remove_long_lines(mask)
    strength = np.clip(dark, 0, None) * 0.6 + np.clip(blue, 0, None) * 2.0
    strength[strict] = np.maximum(strength[strict], 60.0)
    return mask, strength


def remove_long_lines(mask: np.ndarray, frac: float = 0.22) -> np.ndarray:
    """Remove long straight structures (carton seams, edges, belt borders).

    Stamp strokes are at most one character long, far shorter than ``frac``
    of the image side, so they survive.
    """
    h, w = mask.shape
    lv = max(15, int(h * frac))
    lh = max(15, int(w * frac))
    vert = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((lv, 1), np.uint8))
    horiz = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1, lh), np.uint8))
    lines = cv2.dilate(vert | horiz, np.ones((3, 3), np.uint8))
    return cv2.bitwise_and(mask, cv2.bitwise_not(lines))


@dataclass
class Region:
    """A candidate stamp: a group of character-sized ink components."""

    box: tuple[int, int, int, int]  # x, y, w, h in the image
    points: np.ndarray  # (N, 2) ink pixel coords (x, y) inside the group
    n_parts: int
    char_h: float  # median component size
    score: float


@dataclass
class _Comp:
    label: int
    x: int
    y: int
    w: int
    h: int
    size: float  # max(w, h)
    cx: float
    cy: float
    strength: float
    blue: float = 0.0  # fraction of pixels with a blue hue (stamp ink cue)


def blue_hue_map(img: np.ndarray, min_s: int = 30, min_v: int = 25) -> np.ndarray:
    """Pixels whose hue is blue (navy included): the stamp-ink colour cue.

    Weaker than the strict HSV test in :func:`ink_map` (low saturation is
    allowed) because it only *ranks* candidates; black/brown print fails it.
    """
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return (h >= 95) & (h <= 135) & (s >= min_s) & (v >= min_v)


def _components(
    mask: np.ndarray, strength: np.ndarray, min_char: int, max_char: float,
    blue: np.ndarray | None = None,
) -> tuple[list[_Comp], np.ndarray]:
    m = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, labels, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    comps = []
    for i in range(1, n):
        x, y, cw, ch, area = (int(v) for v in stats[i])
        size = max(cw, ch)
        if size < min_char or size > max_char or min(cw, ch) < 2:
            continue
        fill = area / float(cw * ch)
        if fill > 0.85 and min(cw, ch) > 6:  # solid blob (belt, print block)
            continue
        if fill < 0.08:  # thin curve / outline spanning a big box
            continue
        roi = labels[y : y + ch, x : x + cw] == i
        st = float(strength[y : y + ch, x : x + cw][roi].mean())
        bl = float(blue[y : y + ch, x : x + cw][roi].mean()) if blue is not None else 0.0
        comps.append(_Comp(i, x, y, cw, ch, float(size), float(cents[i][0]), float(cents[i][1]), st, bl))
    return comps, labels


def _region(comps: list[_Comp], labels: np.ndarray, score: float) -> Region:
    x0 = min(c.x for c in comps)
    y0 = min(c.y for c in comps)
    x1 = max(c.x + c.w for c in comps)
    y1 = max(c.y + c.h for c in comps)
    sub = labels[y0:y1, x0:x1]
    sel = np.isin(sub, [c.label for c in comps])
    ys, xs = np.nonzero(sel)
    pts = np.stack([xs + x0, ys + y0], axis=1)
    return Region(
        box=(x0, y0, x1 - x0, y1 - y0),
        points=pts,
        n_parts=len(comps),
        char_h=float(np.median([c.size for c in comps])),
        score=score,
    )


def propose_regions(
    mask: np.ndarray,
    strength: np.ndarray,
    max_regions: int = 4,
    min_char: int = 8,
    max_char_frac: float = 0.45,
    blue: np.ndarray | None = None,
) -> list[Region]:
    """Propose stamp candidates from character-sized ink components.

    A stamp is three characters: we look for collinear triples of
    similar-sized, evenly spaced components. Pairs (two characters merged or
    one missing) and elongated single components (all merged) are kept as
    lower-ranked fallbacks.
    """
    h, w = mask.shape
    comps, labels = _components(mask, strength, min_char, max_char_frac * min(h, w), blue)
    if not comps:
        return []
    n = len(comps)
    cx = np.array([c.cx for c in comps])
    cy = np.array([c.cy for c in comps])
    sz = np.array([c.size for c in comps])
    dist = np.hypot(cx[:, None] - cx[None, :], cy[:, None] - cy[None, :])
    big = np.maximum(sz[:, None], sz[None, :])
    ratio = big / np.minimum(sz[:, None], sz[None, :])
    near = (dist <= 1.5 * big) & (dist >= 0.3 * big) & (ratio <= 1.9)
    np.fill_diagonal(near, False)

    def ink_factor(cs: list[_Comp]) -> float:
        f = min(1.0, float(np.mean([c.strength for c in cs])) / 35.0)
        if blue is not None:
            # blue/navy ink ranks above black or brown print
            f *= 0.6 + 0.4 * min(1.0, 2.0 * float(np.mean([c.blue for c in cs])))
        return f

    def size_factor(cs: list[_Comp]) -> float:
        return min(1.0, float(np.median([c.size for c in cs])) / 14.0)

    cands: dict[tuple[int, ...], float] = {}
    # triples: middle component j with two neighbours on opposite sides
    for j in range(n):
        nb = np.nonzero(near[j])[0]
        for a_i, a in enumerate(nb):
            for b in nb[a_i + 1 :]:
                va = np.array([cx[a] - cx[j], cy[a] - cy[j]])
                vb = np.array([cx[b] - cx[j], cy[b] - cy[j]])
                da, db = np.linalg.norm(va), np.linalg.norm(vb)
                cos = float(va @ vb / (da * db + 1e-6))
                if cos > -0.9:  # not on a line through j
                    continue
                spacing = min(da, db) / max(da, db)
                if spacing < 0.5:
                    continue
                cs = [comps[a], comps[j], comps[b]]
                sizes = [c.size for c in cs]
                size_cons = min(sizes) / max(sizes)
                straight = (-cos - 0.9) / 0.1
                score = (
                    (0.55 + 0.45 * straight) * (0.6 + 0.4 * spacing) * (0.6 + 0.4 * size_cons)
                    * ink_factor(cs) * size_factor(cs)
                )
                key = tuple(sorted((int(a), j, int(b))))
                cands[key] = max(cands.get(key, 0.0), score)
    in_triple = {i for k in cands for i in k}
    # pairs: one elongated part (two merged characters) or partial stamp
    for i in range(n):
        for j in np.nonzero(near[i])[0]:
            j = int(j)
            if j <= i or (i in in_triple and j in in_triple):
                continue
            cs = [comps[i], comps[j]]
            elong = max(max(c.w, c.h) / max(1, min(c.w, c.h)) for c in cs)
            score = 0.45 * (1.0 if elong >= 1.6 else 0.7) * ink_factor(cs) * size_factor(cs)
            cands[(i, j)] = score
    # gapped pairs: first and last character with the middle one too faint
    # to be found (thin "1"/"I" printed weakly): similar size, about two
    # character pitches apart
    gap = (dist > 1.5 * big) & (dist <= 2.8 * big) & (ratio <= 1.5)
    for i in range(n):
        for j in np.nonzero(gap[i])[0]:
            j = int(j)
            if j <= i or (i, j) in cands:
                continue
            cs = [comps[i], comps[j]]
            cands[(i, j)] = 0.5 * ink_factor(cs) * size_factor(cs)
    # singles: all three characters merged into one elongated blob
    for i, c in enumerate(comps):
        elong = max(c.w, c.h) / max(1, min(c.w, c.h))
        if i in in_triple or not 1.8 <= elong <= 6.0:
            continue
        thickness = min(c.w, c.h)
        if thickness < min_char:
            continue
        cands[(i,)] = 0.3 * ink_factor([c]) * min(1.0, thickness / 14.0)
    ranked = sorted(cands.items(), key=lambda kv: kv[1], reverse=True)
    regions: list[Region] = []
    used_boxes: list[tuple[int, int, int, int]] = []
    for key, score in ranked:
        r = _region([comps[i] for i in key], labels, score)
        if any(_iou(r.box, b) > 0.5 for b in used_boxes):
            continue
        used_boxes.append(r.box)
        regions.append(r)
        if len(regions) >= max_regions:
            break
    return regions


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / float(aw * ah + bw * bh - inter + 1e-6)
