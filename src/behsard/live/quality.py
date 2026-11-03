"""Image quality checks for the camera setup wizard.

Thresholds are for the planned setup (3200x1800 camera, carton face ~45 cm
away, stamp letters ~1.5-2 cm): stamp letters should be at least ~30 px tall
in the full-resolution frame for reliable reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from behsard.recognition.ink import ink_map, propose_regions

MIN_SHARPNESS = 60.0  # variance of Laplacian on a 1280 px wide copy
MIN_BRIGHTNESS, MAX_BRIGHTNESS = 70.0, 210.0
MIN_LETTER_PX = 30.0
MIN_MOTION_SHARPNESS = 35.0


@dataclass
class Check:
    name: str
    value: float | None
    ok: bool | None  # None = could not measure
    hint: str


@dataclass
class QualityReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok is not False for c in self.checks)


def roi_crop(frame: np.ndarray, roi: tuple[float, float, float, float]) -> np.ndarray:
    H, W = frame.shape[:2]
    x, y, w, h = roi
    return frame[int(y * H) : int((y + h) * H), int(x * W) : int((x + w) * W)]


def sharpness_score(img: np.ndarray) -> float:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    s = 1280 / g.shape[1]
    if s < 1:
        g = cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(g, cv2.CV_32F).var())


def letter_height(img: np.ndarray) -> float | None:
    """Estimated stamp character height (px, full resolution) or None."""
    s = min(1.0, 1600 / max(img.shape[:2]))
    work = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img
    mask, strength = ink_map(work)
    regs = propose_regions(mask, strength, max_regions=1)
    if not regs or regs[0].n_parts < 2:
        return None
    return regs[0].char_h / s


def check_image(frame: np.ndarray, roi: tuple[float, float, float, float] = (0, 0, 1, 1)) -> QualityReport:
    img = roi_crop(frame, roi)
    rep = QualityReport()
    sh = sharpness_score(img)
    rep.checks.append(
        Check("وضوح تصویر", round(sh, 1), sh >= MIN_SHARPNESS,
              "خوب" if sh >= MIN_SHARPNESS else "تصویر تار است: فوکوس یا فاصله دوربین را بررسی کنید")
    )
    br = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean())
    if br < MIN_BRIGHTNESS:
        hint, ok = "تصویر تاریک است: نور اضافه کنید یا زمان نوردهی را بیشتر کنید", False
    elif br > MAX_BRIGHTNESS:
        hint, ok = "تصویر بیش از حد روشن است: نوردهی یا شدت نور را کم کنید", False
    else:
        hint, ok = "خوب", True
    rep.checks.append(Check("روشنایی", round(br, 1), ok, hint))
    lh = letter_height(img)
    if lh is None:
        rep.checks.append(
            Check("ارتفاع حروف مهر", None, None, "مهری دیده نشد: یک کارتن مهرخورده جلوی دوربین بگیرید")
        )
    else:
        ok = lh >= MIN_LETTER_PX
        rep.checks.append(
            Check("ارتفاع حروف مهر (پیکسل)", round(lh, 1), ok,
                  "خوب" if ok else "حروف مهر کوچک است: دوربین را نزدیک‌تر کنید یا وضوح را بالا ببرید")
        )
    return rep


def motion_check(scores: list[float]) -> Check:
    """Sharpness of frames taken while the conveyor was moving."""
    if len(scores) < 3:
        return Check("تاری حرکت", None, None, "یک کارتن را از جلوی دوربین عبور دهید")
    v = float(np.median(scores))
    ok = v >= MIN_MOTION_SHARPNESS
    return Check("تاری حرکت", round(v, 1), ok,
                 "خوب" if ok else "تصویر در حرکت تار است: زمان نوردهی را کوتاه‌تر کنید (۱/۱۰۰۰) و نور را بیشتر کنید")
