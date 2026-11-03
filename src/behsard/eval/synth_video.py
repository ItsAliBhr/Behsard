"""Synthetic conveyor videos with ground truth, for testing tracking.

Touching cartons pass a fixed camera on a stop-and-go conveyor. Each carton
gets a stamp on one side (or none). Two videos are rendered for the two side
cameras: the right camera sees the motion mirrored and the other carton face.

Ground truth (``truth.json``)::

    {"fps": 10, "start": "...", "cartons": [
        {"index": 1, "stamp": "A12", "side": "L", "t_center": 3.4,
         "complete": true}, ...]}

``t_center`` = seconds from video start when the carton centre crosses the
frame centre; ``complete`` = it crosses within the video (it must be counted).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from behsard.eval.synth import (
    _kraft,
    available_fonts,
    ink_color,
    render_stamp_mask,
    rotate_mask,
    rubber_texture,
)


@dataclass
class VideoConfig:
    width: int = 640
    height: int = 360
    fps: int = 10
    n_cartons: int = 8
    carton_w: tuple[float, float] = (0.38, 0.55)  # fraction of frame width
    speed: tuple[float, float] = (10.0, 18.0)  # px per frame while moving
    move_s: tuple[float, float] = (0.8, 2.5)
    stop_s: tuple[float, float] = (0.5, 3.0)
    p_no_stamp: float = 0.15
    text_h: tuple[int, int] = (40, 60)
    band: tuple[float, float] = (0.12, 0.92)  # carton face rows (fractions)


def _background(rng: np.random.Generator, w: int, h: int) -> np.ndarray:
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = (170, 170, 165)
    for y in range(0, h, 9):  # wall panels
        cv2.line(img, (0, y), (w, y), (150, 150, 145), 1)
    for x in range(0, w, 40):  # rollers / posts
        cv2.rectangle(img, (x, int(h * 0.9)), (x + 25, h), (120, 120, 120), -1)
        cv2.line(img, (x + 30, 0), (x + 30, int(h * 0.9)), (130, 130, 130), 3)
    img = cv2.add(img, rng.integers(0, 8, img.shape, dtype=np.uint8))
    return img


def _carton_face(
    rng: np.random.Generator, w: int, h: int, code: str | None, cfg: VideoConfig, fonts: list[str]
) -> np.ndarray:
    face = _kraft(rng, h, w)
    face *= rng.uniform(0.9, 1.1)
    # carton edges: darker vertical bands at both ends (flaps / seam shadow)
    ew = max(3, w // 60)
    face[:, :ew] *= 0.55
    face[:, -ew:] *= 0.65
    face[:4] *= 0.8
    if code is not None:
        th = int(rng.integers(cfg.text_h[0], cfg.text_h[1] + 1))
        font = str(rng.choice(fonts)) if fonts else None
        m = rubber_texture(rng, render_stamp_mask(rng, code, th, font), faint=False)
        ang = float(rng.choice([0, 90, 180, 270]) + rng.normal(0, 6))
        m = rotate_mask(m, ang)
        mh, mw = m.shape
        if mh < h - 2 * ew and mw < w - 4 * ew:
            y0 = int(rng.integers(ew, h - mh - ew))
            x0 = int(rng.integers(2 * ew, w - mw - 2 * ew))
            ink = ink_color(rng)
            roi = face[y0 : y0 + mh, x0 : x0 + mw]
            a = m[..., None]
            roi[:] = roi * ((1 - a) + a * ink / 255.0)
    return np.clip(face, 0, 255).astype(np.uint8)


def _speed_profile(rng: np.random.Generator, cfg: VideoConfig, total_px: float) -> list[float]:
    """Per-frame displacement (px) until ``total_px`` has been travelled."""
    out: list[float] = []
    travelled = 0.0
    out += [0.0] * int(cfg.fps * 0.5)
    while travelled < total_px:
        v = float(rng.uniform(*cfg.speed))
        n = int(rng.uniform(*cfg.move_s) * cfg.fps)
        ramp = 3
        for k in range(n):
            f = min(1.0, (k + 1) / ramp, (n - k) / ramp)
            out.append(v * f)
            travelled += v * f
            if travelled >= total_px:
                break
        out += [0.0] * int(rng.uniform(*cfg.stop_s) * cfg.fps)
    return out


def generate_video_pair(
    out_dir: str | Path, seed: int = 0, cfg: VideoConfig | None = None
) -> dict[str, Any]:
    """Render ``left.mp4``, ``right.mp4`` and ``truth.json`` into ``out_dir``."""
    cfg = cfg or VideoConfig()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    fonts = available_fonts()
    W, H = cfg.width, cfg.height
    y0, y1 = int(cfg.band[0] * H), int(cfg.band[1] * H)
    ch = y1 - y0
    letters = "ABCDEFGHKMNPRSTX"
    codes = sorted({f"{rng.choice(list(letters))}{rng.integers(100):02d}" for _ in range(6)})
    cartons = []
    u = 0.0  # left edge of carton i in conveyor coords; cartons trail to the left
    for i in range(cfg.n_cartons):
        w = int(rng.uniform(*cfg.carton_w) * W)
        u -= w
        stamp = None if rng.random() < cfg.p_no_stamp else str(rng.choice(codes))
        side = str(rng.choice(["L", "R"]))
        faces = {
            s: _carton_face(rng, w, ch, stamp if side == s else None, cfg, fonts) for s in "LR"
        }
        cartons.append({"index": i + 1, "u": u, "w": w, "stamp": stamp, "side": side, "faces": faces})
    # travel until the last carton has fully left the frame
    total = -cartons[-1]["u"] + W + 20
    profile = _speed_profile(rng, cfg, total)
    offset = 0.0
    bg = _background(rng, W, H)
    writers = {
        s: cv2.VideoWriter(str(out / f"{'left' if s == 'L' else 'right'}.mp4"),
                           cv2.VideoWriter_fourcc(*"mp4v"), cfg.fps, (W, H))
        for s in "LR"
    }
    centre_t: dict[int, float] = {}
    for f, dx in enumerate(profile):
        prev = offset
        offset += dx
        for s in "LR":
            frame = bg.copy()
            covered = np.zeros(W, bool)
            for c in cartons:
                x = int(round(c["u"] + offset))
                face = c["faces"][s]
                if s == "R":  # mirrored view from the other side
                    x = W - x - c["w"]
                a, b = max(0, x), min(W, x + c["w"])
                if a < b:
                    frame[y0:y1, a:b] = face[:, a - x : b - x]
                    covered[a:b] = True
            if dx > 6 and covered.any():  # motion blur on the moving cartons only
                k = np.zeros((1, int(dx // 3) | 1), np.float32)
                k[:] = 1.0 / k.size
                blurred = cv2.filter2D(frame, -1, k)
                frame[y0:y1, covered] = blurred[y0:y1, covered]
            noise = rng.normal(0, 2.0, frame.shape)
            frame = np.clip(frame.astype(np.float32) + noise, 0, 255).astype(np.uint8)
            writers[s].write(frame)
        for c in cartons:
            cx_prev = c["u"] + prev + c["w"] / 2
            cx = c["u"] + offset + c["w"] / 2
            if c["index"] not in centre_t and cx_prev < W / 2 <= cx:
                centre_t[c["index"]] = f / cfg.fps
    for w_ in writers.values():
        w_.release()
    truth = {
        "fps": cfg.fps,
        "codes": codes,
        "cartons": [
            {
                "index": c["index"],
                "stamp": c["stamp"],
                "side": c["side"],
                "t_center": centre_t.get(c["index"]),
                "complete": c["index"] in centre_t,
            }
            for c in cartons
        ],
    }
    (out / "truth.json").write_text(json.dumps(truth, indent=1))
    return truth
