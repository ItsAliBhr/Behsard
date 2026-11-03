"""Synthetic stamp-on-carton image generator.

Renders a rubber-stamp code (letter + two digits, blue/navy ink) onto
carton-like backgrounds with rotation, blur, motion blur, faint/partial
ink and lighting variation. Writes images plus a ``labels.json``:

    [{"file": "000001.jpg", "stamp": "A12", "active": ["A12", ...],
      "meta": {"background": "kraft", "angle": 87.0, ...}}, ...]

``stamp`` is ``null`` for negative samples (no stamp on the carton).
"""

from __future__ import annotations

import json
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

BACKGROUNDS = ("kraft", "kraft_print", "white_red")
LETTERS = string.ascii_uppercase

_FONT_DIR = Path(__file__).resolve().parent.parent / "recognition" / "fonts"
# Bundled fonts only, so templates are identical on every machine.
_FONT_CANDIDATES = tuple(
    str(_FONT_DIR / n)
    for n in (
        "DejaVuSans-Bold.ttf",
        "LiberationSans-Bold.ttf",
        "FreeSansBold.ttf",
        "Carlito-Bold.ttf",
        "Inter-Bold.otf",
    )
)


def available_fonts() -> list[str]:
    return [f for f in _FONT_CANDIDATES if Path(f).exists()]


@dataclass
class SynthConfig:
    width: int = 640
    height: int = 480
    p_negative: float = 0.10
    min_text_h: int = 22  # cap height in px
    max_text_h: int = 80
    n_active_min: int = 12
    n_active_max: int = 16
    p_confusers: float = 0.6  # add near-miss codes to the active set
    p_belt: float = 0.3  # blue conveyor belt strip (distractor)
    p_partial: float = 0.12
    p_faint: float = 0.25
    p_motion_blur: float = 0.35
    jpeg: bool = True


def random_code(rng: np.random.Generator) -> str:
    return f"{LETTERS[rng.integers(26)]}{rng.integers(100):02d}"


def confusers(code: str, rng: np.random.Generator, n: int) -> list[str]:
    """Near-miss codes: one character changed."""
    out: set[str] = set()
    while len(out) < n:
        pos = int(rng.integers(3))
        chars = list(code)
        chars[pos] = LETTERS[rng.integers(26)] if pos == 0 else str(rng.integers(10))
        c = "".join(chars)
        if c != code:
            out.add(c)
    return sorted(out)


def active_set(rng: np.random.Generator, cfg: SynthConfig, true_code: str | None) -> list[str]:
    n = int(rng.integers(cfg.n_active_min, cfg.n_active_max + 1))
    codes: set[str] = set()
    if true_code:
        codes.add(true_code)
        if rng.random() < cfg.p_confusers:
            codes.update(confusers(true_code, rng, int(rng.integers(1, 4))))
    while len(codes) < n:
        codes.add(random_code(rng))
    return sorted(codes)


# --------------------------------------------------------------------------
# backgrounds


def _low_freq_noise(rng: np.random.Generator, h: int, w: int, cells: int) -> np.ndarray:
    small = rng.standard_normal((max(2, h // cells), max(2, w // cells))).astype(np.float32)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)


def _kraft(rng: np.random.Generator, h: int, w: int) -> np.ndarray:
    base = np.array([95, 140, 185], np.float32) + rng.normal(0, [10, 12, 12])
    img = np.ones((h, w, 3), np.float32) * base
    img += _low_freq_noise(rng, h, w, 60)[..., None] * 6
    # fibres / corrugation streaks
    streak = _low_freq_noise(rng, 1, w, 3)
    img += np.repeat(streak, h, axis=0)[..., None] * 3
    img += rng.normal(0, 3, (h, w, 1))
    return img


def _white(rng: np.random.Generator, h: int, w: int) -> np.ndarray:
    base = np.array([225, 230, 235], np.float32) + rng.normal(0, 6, 3)
    img = np.ones((h, w, 3), np.float32) * base
    img += _low_freq_noise(rng, h, w, 80)[..., None] * 4
    img += rng.normal(0, 2, (h, w, 1))
    return img


def _draw_print(
    rng: np.random.Generator, img: np.ndarray, colors: list[tuple[int, int, int]]
) -> None:
    """Random carton graphics: blobs, arcs, bars and Latin text."""
    h, w = img.shape[:2]
    canvas = np.clip(img, 0, 255).astype(np.uint8)
    for _ in range(int(rng.integers(2, 6))):
        color = colors[int(rng.integers(len(colors)))]
        kind = rng.integers(4)
        cx, cy = int(rng.integers(w)), int(rng.integers(h))
        if kind == 0:
            r = int(rng.integers(20, max(21, h // 3)))
            cv2.circle(canvas, (cx, cy), r, color, -1, cv2.LINE_AA)
        elif kind == 1:
            axes = (int(rng.integers(30, w // 3)), int(rng.integers(10, h // 4)))
            cv2.ellipse(
                canvas, (cx, cy), axes, float(rng.uniform(0, 180)), 0,
                float(rng.uniform(90, 360)), color, int(rng.integers(6, 25)), cv2.LINE_AA,
            )
        elif kind == 2:
            x2 = cx + int(rng.integers(40, w // 2))
            y2 = cy + int(rng.integers(6, 30))
            cv2.rectangle(canvas, (cx, cy), (x2, y2), color, -1)
        else:
            word = rng.choice(["Golden Apple", "Red Apple", "BEHSARD", "Fresh", "IRAN"])
            cv2.putText(
                canvas, str(word), (cx - 60, cy), cv2.FONT_HERSHEY_SCRIPT_COMPLEX,
                float(rng.uniform(0.8, 2.2)), color, int(rng.integers(2, 5)), cv2.LINE_AA,
            )
    alpha = float(rng.uniform(0.75, 1.0))
    img[:] = img * (1 - alpha) + canvas.astype(np.float32) * alpha


def make_background(rng: np.random.Generator, kind: str, h: int, w: int) -> np.ndarray:
    if kind == "kraft":
        img = _kraft(rng, h, w)
    elif kind == "kraft_print":
        img = _kraft(rng, h, w)
        dark = [(30, 45, 60), (20, 25, 30), (40, 70, 100)]
        yellow = [(30, 200, 235), (40, 180, 220)]
        _draw_print(rng, img, dark + yellow)
    elif kind == "white_red":
        img = _white(rng, h, w)
        _draw_print(rng, img, [(40, 40, 200), (60, 60, 180), (30, 30, 150)])
    else:
        raise ValueError(kind)
    return img


def add_belt_and_seam(rng: np.random.Generator, img: np.ndarray, cfg: SynthConfig) -> int:
    """Distractors seen on site: blue conveyor belt and carton seams/gaps.

    Returns the belt height in px (0 if none); stamps are placed above it.
    """
    h, w = img.shape[:2]
    bh = 0
    if rng.random() < cfg.p_belt:
        bh = int(rng.uniform(0.08, 0.2) * h)
        belt = np.array([170, 90, 40], np.float32) + rng.normal(0, 10, 3)
        img[h - bh :] = belt + rng.normal(0, 8, (bh, w, 1))
    if rng.random() < 0.4:
        x = int(rng.integers(w // 10, w - w // 10))
        seam = np.zeros((h, w), np.uint8)
        cv2.line(seam, (x, 0), (x + int(rng.integers(-15, 15)), h), 255, int(rng.integers(2, 8)))
        img[seam > 0] = (25, 35, 45)
    return bh


# --------------------------------------------------------------------------
# stamp


def render_stamp_mask(
    rng: np.random.Generator, code: str, text_h: int, font_path: str | None
) -> np.ndarray:
    """Upright ink coverage mask (float32 0..1) for ``code``."""
    pad = text_h
    if font_path:
        # cap height is roughly 0.72 of the font size for these sans fonts
        font = ImageFont.truetype(font_path, int(text_h / 0.72))
        spacing = int(rng.uniform(-0.02, 0.15) * text_h)
        widths = [font.getbbox(ch)[2] for ch in code]
        tw = sum(widths) + spacing * (len(code) - 1)
        im = Image.new("L", (tw + 2 * pad, text_h * 2 + 2 * pad), 0)
        d = ImageDraw.Draw(im)
        x = pad
        for ch, cw in zip(code, widths):
            d.text((x, pad), ch, fill=255, font=font)
            x += cw + spacing
        mask = np.asarray(im, np.float32) / 255.0
    else:
        scale = text_h / 22.0
        thick = max(2, int(scale * 2.2))
        (tw, th), _ = cv2.getTextSize(code, cv2.FONT_HERSHEY_DUPLEX, scale, thick)
        m8 = np.zeros((th + 2 * pad, tw + 2 * pad), np.uint8)  # putText needs 8-bit
        cv2.putText(m8, code, (pad, pad + th), cv2.FONT_HERSHEY_DUPLEX, scale, 255, thick, cv2.LINE_AA)
        mask = m8.astype(np.float32) / 255.0
    if rng.random() < 0.15:  # rectangular stamp frame
        ys, xs = np.nonzero(mask > 0.5)
        m = int(text_h * 0.25)
        cv2.rectangle(
            mask, (int(xs.min()) - m, int(ys.min()) - m), (int(xs.max()) + m, int(ys.max()) + m),
            1.0, max(2, text_h // 12),
        )
    # ink bleed or thin strokes
    k = int(rng.integers(-1, 2))
    if k:
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.dilate(mask, kernel) if k > 0 else cv2.erode(mask, kernel)
    ys, xs = np.nonzero(mask > 0.05)
    m = max(4, text_h // 4)
    mask = mask[max(0, ys.min() - m) : ys.max() + m, max(0, xs.min() - m) : xs.max() + m]
    return mask


def rubber_texture(rng: np.random.Generator, mask: np.ndarray, faint: bool) -> np.ndarray:
    h, w = mask.shape
    # porous rubber: speckle holes
    speckle = _low_freq_noise(rng, h, w, 3)
    holes = (speckle > rng.uniform(0.9, 1.6)).astype(np.float32)
    out = mask * (1 - 0.85 * holes)
    # uneven pressure: linear ramp across the stamp
    ang = rng.uniform(0, 2 * np.pi)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    ramp = (np.cos(ang) * xx / max(w, 1) + np.sin(ang) * yy / max(h, 1))
    ramp = (ramp - ramp.min()) / (np.ptp(ramp) + 1e-6)
    lo = rng.uniform(0.3, 0.8) if rng.random() < 0.5 else 1.0
    out *= lo + (1 - lo) * ramp
    strength = rng.uniform(0.3, 0.55) if faint else rng.uniform(0.7, 1.0)
    return np.clip(out * strength, 0, 1)


def make_partial(rng: np.random.Generator, mask: np.ndarray) -> np.ndarray:
    """Cut away part of the stamp (not inked / off the edge)."""
    h, w = mask.shape
    out = mask.copy()
    side = rng.integers(4)
    frac = rng.uniform(0.1, 0.3)
    if side == 0:
        out[:, : int(w * frac)] = 0
    elif side == 1:
        out[:, w - int(w * frac) :] = 0
    elif side == 2:
        out[: int(h * frac * 1.5)] = 0
    else:
        out[h - int(h * frac * 1.5) :] = 0
    return out


def rotate_mask(mask: np.ndarray, angle: float, shear: float = 0.0) -> np.ndarray:
    h, w = mask.shape
    diag = int(np.ceil(np.hypot(h, w))) + 4
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    M[0, 1] += shear
    M[0, 2] += (diag - w) / 2
    M[1, 2] += (diag - h) / 2
    out = cv2.warpAffine(mask, M, (diag, diag), flags=cv2.INTER_LINEAR)
    ys, xs = np.nonzero(out > 0.02)
    if len(ys) == 0:
        return out
    return out[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]


def sample_angle(rng: np.random.Generator) -> float:
    if rng.random() < 0.6:
        return float(rng.choice([0, 90, 180, 270]) + rng.normal(0, 4))
    return float(rng.uniform(0, 360))


def ink_color(rng: np.random.Generator) -> np.ndarray:
    """Blue/navy ink in BGR."""
    if rng.random() < 0.5:  # navy, almost black-blue
        return np.array([rng.uniform(70, 120), rng.uniform(25, 55), rng.uniform(10, 40)])
    return np.array([rng.uniform(140, 200), rng.uniform(50, 100), rng.uniform(20, 60)])


# --------------------------------------------------------------------------
# camera effects


def motion_blur(img: np.ndarray, length: int, angle: float) -> np.ndarray:
    k = np.zeros((length, length), np.float32)
    k[length // 2, :] = 1.0
    M = cv2.getRotationMatrix2D((length / 2 - 0.5, length / 2 - 0.5), angle, 1.0)
    k = cv2.warpAffine(k, M, (length, length))
    k /= max(k.sum(), 1e-6)
    return cv2.filter2D(img, -1, k)


def camera_effects(rng: np.random.Generator, img: np.ndarray, cfg: SynthConfig) -> np.ndarray:
    h, w = img.shape[:2]
    # lighting: gain, colour temperature, gradient
    img = img * rng.uniform(0.65, 1.25) * rng.normal(1.0, 0.04, 3)
    if rng.random() < 0.5:
        g = np.linspace(rng.uniform(0.7, 1.0), rng.uniform(1.0, 1.2), w, dtype=np.float32)
        img = img * (g[None, :, None] if rng.random() < 0.5 else np.resize(g, h)[:, None, None])
    img = np.clip(img, 0, 255).astype(np.uint8)
    gamma = rng.uniform(0.8, 1.25)
    lut = (np.linspace(0, 1, 256) ** gamma * 255).astype(np.uint8)
    img = cv2.LUT(img, lut)
    sigma = rng.uniform(0, 1.4)
    if sigma > 0.3:
        img = cv2.GaussianBlur(img, (0, 0), sigma)
    if rng.random() < cfg.p_motion_blur:
        img = motion_blur(img, int(rng.integers(3, 10)), float(rng.uniform(-15, 15)))
    noise = rng.normal(0, rng.uniform(1, 5), img.shape)
    img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    return img


# --------------------------------------------------------------------------


def generate_sample(
    rng: np.random.Generator, cfg: SynthConfig, fonts: list[str]
) -> tuple[np.ndarray, dict[str, Any]]:
    """Return (BGR image, label dict without "file")."""
    h, w = cfg.height, cfg.width
    kind = str(rng.choice(BACKGROUNDS))
    img = make_background(rng, kind, h, w)
    belt_h = add_belt_and_seam(rng, img, cfg)
    negative = rng.random() < cfg.p_negative
    code = None if negative else random_code(rng)
    meta: dict[str, Any] = {"background": kind}
    if code is not None:
        text_h = int(rng.integers(cfg.min_text_h, cfg.max_text_h + 1))
        font = str(rng.choice(fonts)) if fonts else None
        mask = render_stamp_mask(rng, code, text_h, font)
        faint = bool(rng.random() < cfg.p_faint)
        mask = rubber_texture(rng, mask, faint)
        partial = bool(rng.random() < cfg.p_partial)
        if partial:
            mask = make_partial(rng, mask)
        angle = sample_angle(rng)
        mask = rotate_mask(mask, angle, float(rng.normal(0, 0.05)))
        mh, mw = mask.shape
        scale = min(1.0, 0.9 * w / mw, 0.9 * (h - belt_h) / mh)
        if scale < 1.0:
            mask = cv2.resize(mask, None, fx=scale, fy=scale)
            mh, mw = mask.shape
        y0 = int(rng.integers(0, h - belt_h - mh + 1))
        x0 = int(rng.integers(0, w - mw + 1))
        ink = ink_color(rng)
        roi = img[y0 : y0 + mh, x0 : x0 + mw]
        a = mask[..., None]
        # multiply blend: ink darkens the substrate
        roi[:] = roi * ((1 - a) + a * ink / 255.0)
        meta.update(
            angle=round(angle % 360, 1), text_h=text_h, faint=faint, partial=partial,
            bbox=[x0, y0, mw, mh], font=Path(font).stem if font else "hershey",
        )
    out = camera_effects(rng, img, cfg)
    return out, {"stamp": code, "active": active_set(rng, cfg, code), "meta": meta}


def generate_dataset(
    out_dir: str | Path, n: int, seed: int = 0, cfg: SynthConfig | None = None
) -> list[dict[str, Any]]:
    cfg = cfg or SynthConfig()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    fonts = available_fonts()
    labels: list[dict[str, Any]] = []
    for i in range(n):
        img, label = generate_sample(rng, cfg, fonts)
        name = f"{i:06d}.jpg" if cfg.jpeg else f"{i:06d}.png"
        params = [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(60, 95))] if cfg.jpeg else []
        cv2.imwrite(str(out / name), img, params)
        labels.append({"file": name, **label})
    (out / "labels.json").write_text(json.dumps(labels, indent=1, ensure_ascii=False))
    return labels
