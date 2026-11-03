"""Closed-set glyph template scoring.

Complements Tesseract: the binarised stamp line is split into its three
characters, each is normalised and correlated against glyph templates
rendered from bold sans fonts (letters for position 0, digits for 1-2).
A code's score is the mean correlation of its characters at their positions.
"""

from __future__ import annotations

import string
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

LETTERS = string.ascii_uppercase
DIGITS = string.digits
NORM = 32  # glyphs are compared as NORM x NORM images

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/crosextra/Carlito-Bold.ttf",
    "/usr/share/fonts/opentype/inter/Inter-Bold.otf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/verdanab.ttf",
    "C:/Windows/Fonts/tahomabd.ttf",
)


def normalise_glyph(mask: np.ndarray) -> np.ndarray | None:
    """Tight-crop a binary glyph (ink > 0), pad to square, resize, unit vector."""
    ys, xs = np.nonzero(mask > 0)
    if len(ys) < 4:
        return None
    g = mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1].astype(np.float32)
    h, w = g.shape
    side = max(h, w)
    canvas = np.zeros((side, side), np.float32)
    canvas[(side - h) // 2 : (side - h) // 2 + h, (side - w) // 2 : (side - w) // 2 + w] = g
    canvas = cv2.resize(canvas, (NORM, NORM), interpolation=cv2.INTER_AREA)
    canvas = cv2.GaussianBlur(canvas, (0, 0), 1.2)
    v = canvas.ravel()
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else None


def _render_char(ch: str, font: ImageFont.FreeTypeFont | None) -> np.ndarray:
    if font is None:
        m = np.zeros((90, 90), np.uint8)
        cv2.putText(m, ch, (10, 75), cv2.FONT_HERSHEY_DUPLEX, 2.6, 255, 7, cv2.LINE_AA)
        return m
    im = Image.new("L", (140, 140), 0)
    ImageDraw.Draw(im).text((20, 10), ch, fill=255, font=font)
    return np.asarray(im)


@lru_cache(maxsize=1)
def templates() -> tuple[dict[str, np.ndarray], list[str]]:
    """Return ({char: (n_templates, NORM*NORM) matrix}, font names)."""
    fonts: list[ImageFont.FreeTypeFont | None] = []
    names = []
    for f in _FONT_CANDIDATES:
        if Path(f).exists():
            fonts.append(ImageFont.truetype(f, 96))
            names.append(Path(f).stem)
    if not fonts:
        fonts, names = [None], ["hershey"]
    out: dict[str, np.ndarray] = {}
    k3 = np.ones((3, 3), np.uint8)
    for ch in LETTERS + DIGITS:
        vecs = []
        for font in fonts:
            base = _render_char(ch, font)
            for variant in (base, cv2.dilate(base, k3, iterations=2), cv2.erode(base, k3, iterations=2)):
                for ang in (-6.0, 0.0, 6.0):
                    m = variant
                    if ang:
                        M = cv2.getRotationMatrix2D((70, 70), ang, 1.0)
                        m = cv2.warpAffine(variant, M, (variant.shape[1], variant.shape[0]))
                    v = normalise_glyph((m > 127).astype(np.uint8))
                    if v is not None:
                        vecs.append(v)
        out[ch] = np.stack(vecs)
    return out, names


def clean_line(line_bw: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Ink components of a line, minus frame strokes and specks.

    Returns (labels, stats, kept label ids).
    """
    ink = (line_bw < 128).astype(np.uint8)
    H, W = ink.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    ids = list(range(1, n))
    if not ids:
        return labels, stats, np.array([], int)

    def is_frame(i: int) -> bool:
        x, y, w, h, area = stats[i]
        touches = (x <= 1) + (y <= 1) + (x + w >= W - 1) + (y + h >= H - 1)
        hollow = area / float(w * h) < 0.25
        return (w > 0.75 * W and hollow) or (touches >= 2 and hollow) or (h > 0.9 * H and w > 0.9 * W)

    ids = [i for i in ids if not is_frame(i)]
    if not ids:
        return labels, stats, np.array([], int)
    max_h = max(int(stats[i][3]) for i in ids)
    ids = [i for i in ids if stats[i][3] >= 0.3 * max_h and stats[i][4] >= 6]
    return labels, stats, np.array(ids, int)


def segment_line(line_bw: np.ndarray, n_chars: int = 3) -> list[np.ndarray]:
    """Split a binarised line (black text on white) into ``n_chars`` glyph masks."""
    labels, stats, ids = clean_line(line_bw)
    if len(ids) == 0:
        return []
    parts = [[int(v) for v in stats[i][:4]] + [int(i)] for i in ids]
    parts.sort(key=lambda p: p[0])
    # merge pieces of one broken character (heavy x overlap)
    merged: list[list[int]] = []
    groups: list[list[int]] = []
    for p in parts:
        if merged:
            q = merged[-1]
            ov = min(q[0] + q[2], p[0] + p[2]) - max(q[0], p[0])
            if ov > 0.5 * min(q[2], p[2]):
                x0, y0 = min(q[0], p[0]), min(q[1], p[1])
                x1, y1 = max(q[0] + q[2], p[0] + p[2]), max(q[1] + q[3], p[1] + p[3])
                merged[-1] = [x0, y0, x1 - x0, y1 - y0, q[4]]
                groups[-1].append(p[4])
                continue
        merged.append(p)
        groups.append([p[4]])
    boxes = [(m[0], m[0] + m[2]) for m in merged]
    masks = [np.isin(labels, g).astype(np.uint8) for g in groups]
    # too many parts: keep the n tallest adjacent ones (drop specks / frame)
    while len(masks) > n_chars:
        heights = [int(np.ptp(np.nonzero(m.any(axis=1))[0])) for m in masks]
        drop = int(np.argmin(heights))
        masks.pop(drop)
        boxes.pop(drop)
    # too few: split the widest part at its lowest column-ink point
    while 0 < len(masks) < n_chars:
        widths = [b - a for a, b in boxes]
        i = int(np.argmax(widths))
        a, b = boxes[i]
        need = n_chars - len(masks) + 1
        k = min(need, 2) if need > 1 else 2
        if widths[i] < 6:
            return []
        col = masks[i][:, a:b].sum(axis=0).astype(np.float32)
        cut = a + _best_cut(col, k)
        left = masks[i].copy()
        left[:, cut:] = 0
        right = masks[i].copy()
        right[:, :cut] = 0
        masks[i : i + 1] = [left, right]
        boxes[i : i + 1] = [(a, cut), (cut, b)]
    return masks


def _best_cut(col: np.ndarray, k: int) -> int:
    """Column index (within ``col``) to cut a blob holding ``k`` characters."""
    w = len(col)
    target = w / k  # first character's expected right edge
    lo, hi = int(target * 0.6), max(int(target * 1.4), int(target * 0.6) + 1)
    hi = min(hi, w - 1)
    if lo >= hi:
        return w // 2
    sm = np.convolve(col, np.ones(3) / 3, mode="same")
    return lo + int(np.argmin(sm[lo:hi]))


CODE_H, CODE_W = 28, 80  # whole-code comparison size


def _code_vec(mask: np.ndarray) -> np.ndarray | None:
    ys, xs = np.nonzero(mask > 0)
    if len(ys) < 10:
        return None
    g = mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1].astype(np.float32)
    g = cv2.resize(g, (CODE_W, CODE_H), interpolation=cv2.INTER_AREA)
    g = cv2.GaussianBlur(g, (0, 0), 1.2)
    v = g.ravel() - g.mean()
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else None


@lru_cache(maxsize=4096)
def code_templates(code: str) -> np.ndarray:
    """Rendered whole-code templates (n, CODE_H*CODE_W) for ``code``."""
    vecs = []
    k3 = np.ones((3, 3), np.uint8)
    for f in _FONT_CANDIDATES:
        if not Path(f).exists():
            continue
        font = ImageFont.truetype(f, 72)
        for spacing in (0, 10):
            im = Image.new("L", (400, 130), 0)
            d = ImageDraw.Draw(im)
            x = 10
            for ch in code:
                d.text((x, 10), ch, fill=255, font=font)
                x += font.getbbox(ch)[2] + spacing
            base = np.asarray(im)
            for m in (base, cv2.dilate(base, k3, iterations=2), cv2.erode(base, k3, iterations=2)):
                v = _code_vec((m > 127).astype(np.uint8))
                if v is not None:
                    vecs.append(v)
    if not vecs:
        m = np.zeros((90, 300), np.uint8)
        cv2.putText(m, code, (5, 75), cv2.FONT_HERSHEY_DUPLEX, 2.6, 255, 7, cv2.LINE_AA)
        vecs.append(_code_vec(m))
    return np.stack([v for v in vecs if v is not None])


class GlyphScorer:
    def __init__(self) -> None:
        self.tmpl, self.font_names = templates()

    def char_scores(self, glyph: np.ndarray, alphabet: str) -> dict[str, float]:
        v = normalise_glyph(glyph)
        if v is None:
            return {c: 0.0 for c in alphabet}
        return {c: float((self.tmpl[c] @ v).max()) for c in alphabet}

    def read(self, line_bw: np.ndarray) -> tuple[str, list[dict[str, float]]]:
        """Per-position char scores and the best unconstrained reading."""
        glyphs = segment_line(line_bw, 3)
        if len(glyphs) != 3:
            return "", []
        pos = [
            self.char_scores(glyphs[0], LETTERS),
            self.char_scores(glyphs[1], DIGITS),
            self.char_scores(glyphs[2], DIGITS),
        ]
        text = "".join(max(p, key=p.__getitem__) for p in pos)
        return text, pos

    @staticmethod
    def whole_code_scores(line_bw: np.ndarray, active: Sequence[str]) -> dict[str, float]:
        """Correlate the cleaned line against rendered templates of each code."""
        labels, _, ids = clean_line(line_bw)
        if len(ids) == 0:
            return {c: 0.0 for c in active}
        v = _code_vec(np.isin(labels, ids).astype(np.uint8))
        if v is None:
            return {c: 0.0 for c in active}
        return {c: float((code_templates(c) @ v).max()) for c in active}

    @staticmethod
    def code_scores(pos: list[dict[str, float]], active: Sequence[str]) -> dict[str, float]:
        out = {}
        for code in active:
            if len(pos) != 3 or len(code) != 3:
                out[code] = 0.0
                continue
            out[code] = float(np.mean([pos[i].get(code[i], 0.0) for i in range(3)]))
        return out
