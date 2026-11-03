"""StampRecognizer v1.

Pipeline per image:
    ink map (relative darkness/blueness + HSV blue) -> character-sized
    components -> 3-character candidate regions -> crop along the region's
    main axis -> rotation search (axis, +180, +-90 for squarish regions) ->
    binarise -> two closed-set readers:
        * Tesseract (whitelist A-Z0-9, single line) + fuzzy match
        * glyph templates (per-position letter/digit correlation)
    -> combined per-stamp score over all regions, rotations and images ->
    confidence; below threshold -> manual review.

Tesseract is the expensive step (one process per call), so it only runs on
the few variants the cheap glyph reader ranks highest.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field

import cv2
import numpy as np
import pytesseract

from behsard.config import RecognitionConfig
from behsard.recognition.base import Result
from behsard.recognition.glyphs import GlyphScorer
from behsard.recognition.ink import InkParams, Region, ink_map, propose_regions
from behsard.recognition.match import clean, match_scores
from behsard.recognition.registry import register

# Tesseract's OpenMP threading only adds overhead on tiny line images and
# makes parallel workers fight for cores.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
TESS_CONFIG = (
    f"--psm 7 --oem 1 -c tessedit_char_whitelist={WHITELIST} "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)
TEXT_H = 40  # target character height for OCR, px
# Binarisation threshold relative to Otsu (< 1 keeps thin/faint strokes).
BIN_FACTOR = 0.75


@dataclass
class Reading:
    image_index: int
    region_index: int
    angle: float
    line: np.ndarray  # binarised line, black on white
    crop: np.ndarray  # colour crop, rotated upright
    region_score: float
    glyph_text: str = ""
    glyph_pos: list[dict[str, float]] = field(default_factory=list)
    glyph_best: float = 0.0
    text: str = ""  # Tesseract text ("" if not run / nothing read)
    ocr_conf: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)


def rotated_crop(
    img: np.ndarray, strength: np.ndarray, region: Region, angle: float, margin: float
) -> tuple[np.ndarray, np.ndarray]:
    """Rotate so the region's text line is horizontal; return (colour, strength) crops."""
    (cx, cy), _, _ = cv2.minAreaRect(region.points.astype(np.float32))
    pts = region.points.astype(np.float32)
    M = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    rp = pts @ M[:, :2].T + M[:, 2]
    x0, y0 = rp.min(axis=0)
    x1, y1 = rp.max(axis=0)
    m = margin * region.char_h
    x0, y0, x1, y1 = x0 - m, y0 - m, x1 + m, y1 + m
    w, h = int(np.ceil(x1 - x0)), int(np.ceil(y1 - y0))
    M[0, 2] -= x0
    M[1, 2] -= y0
    colour = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    st = cv2.warpAffine(strength, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    return colour, st


def binarise(strength: np.ndarray, char_h: float) -> np.ndarray:
    """Ink strength crop -> black text on white at ~TEXT_H character height."""
    s = np.clip(strength.astype(np.float32), 0, None)
    scale = TEXT_H / max(char_h, 1.0)
    s = cv2.resize(s, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    hi = float(np.percentile(s, 99)) or 1.0
    u8 = np.clip(s / hi * 255, 0, 255).astype(np.uint8)
    u8 = cv2.GaussianBlur(u8, (3, 3), 0)
    otsu, _ = cv2.threshold(u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, bw = cv2.threshold(u8, BIN_FACTOR * otsu, 255, cv2.THRESH_BINARY)
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    out = 255 - bw
    return cv2.copyMakeBorder(out, 12, 12, 16, 16, cv2.BORDER_CONSTANT, value=255)


def base_angles(region: Region) -> list[float]:
    """Rotations that make the region's long axis horizontal (+ flips)."""
    _, (rw, rh), a = cv2.minAreaRect(region.points.astype(np.float32))
    base = a if rw >= rh else a - 90.0
    long_, short = max(rw, rh), max(1.0, min(rw, rh))
    angles = [base, base + 180.0]
    if long_ / short < 1.7:  # squarish: text direction ambiguous
        angles += [base + 90.0, base - 90.0]
    return angles


def glyph_norm(ncc: float) -> float:
    """Map glyph correlation to 0..1 (0.45 ~ random glyph, 0.9 ~ clean match)."""
    return float(np.clip((ncc - 0.45) / 0.45, 0.0, 1.0))


class StampRecognizer:
    name = "stamp"

    def __init__(
        self,
        cfg: RecognitionConfig | None = None,
        ink: InkParams | None = None,
        tesseract_top_k: int = 4,
        use_tesseract: bool = True,
        use_glyphs: bool = True,
        weights: tuple[float, float, float] = (1.0, 1.0, 1.0),
    ) -> None:
        self.cfg = cfg or RecognitionConfig()
        self.ink = ink or InkParams()
        self.tesseract_top_k = tesseract_top_k
        self.use_tesseract = use_tesseract
        self.use_glyphs = use_glyphs
        wt, wg, ww = weights
        if not use_tesseract:
            wt = 0.0
        if not use_glyphs:
            wg = ww = 0.0
        total = wt + wg + ww
        # weights of (tesseract, per-glyph, whole-code) evidence
        self.weights = (wt / total, wg / total, ww / total)
        self.glyphs = GlyphScorer()
        cmd = shutil.which(self.cfg.tesseract_cmd) or self.cfg.tesseract_cmd
        pytesseract.pytesseract.tesseract_cmd = cmd

    # ---- public --------------------------------------------------------
    def recognize(self, images: Sequence[np.ndarray], active_stamps: Sequence[str]) -> Result:
        active = [s.strip().upper() for s in active_stamps]
        readings: list[Reading] = []
        for i, img in enumerate(images):
            readings.extend(self.read_image(img, i))
        return self.decide(readings, active)

    # ---- steps ---------------------------------------------------------
    def read_image(self, img: np.ndarray, image_index: int = 0) -> list[Reading]:
        img = self._resize(img)
        mask, strength = ink_map(img, self.ink)
        regions = propose_regions(mask, strength, max_regions=self.cfg.max_regions)
        readings: list[Reading] = []
        for ri, region in enumerate(regions):
            for angle in base_angles(region):
                colour, st = rotated_crop(img, strength, region, angle, margin=0.35)
                if st.size == 0 or min(st.shape) < 4:
                    continue
                r = Reading(
                    image_index, ri, angle % 360, binarise(st, region.char_h), colour, region.score
                )
                r.glyph_text, r.glyph_pos = self.glyphs.read(r.line)
                if r.glyph_pos:
                    r.glyph_best = float(np.mean([max(p.values()) for p in r.glyph_pos]))
                readings.append(r)
        if self.use_tesseract and readings:
            order = sorted(
                readings, key=lambda r: (r.glyph_best, r.region_score), reverse=True
            )
            for r in order[: self.tesseract_top_k] if self.use_glyphs else order:
                r.text, r.ocr_conf = self._ocr_line(r.line)
        return readings

    def score_reading(self, r: Reading, active: Sequence[str]) -> dict[str, float]:
        tess = match_scores([r.text], active) if r.text else {c: 0.0 for c in active}
        tw = 0.55 + 0.45 * r.ocr_conf
        wt, wg, ww = self.weights
        glyph = GlyphScorer.code_scores(r.glyph_pos, active) if wg else {}
        whole = GlyphScorer.whole_code_scores(r.line, active) if ww else {}
        return {
            c: wt * tess[c] * tw
            + wg * glyph_norm(glyph.get(c, 0.0))
            + ww * glyph_norm(whole.get(c, 0.0))
            for c in active
        }

    def decide(self, readings: list[Reading], active: Sequence[str]) -> Result:
        if not readings or not active:
            return Result(stamp=None, confidence=0.0)
        per_code: dict[str, float] = {c: 0.0 for c in active}
        support: dict[str, set[int]] = {c: set() for c in active}
        best_for: dict[str, Reading] = {}
        for r in readings:
            r.scores = self.score_reading(r, active)
            for code, s in r.scores.items():
                if s >= 0.6:
                    support[code].add(r.image_index)
                if s > per_code[code]:
                    per_code[code] = s
                    best_for[code] = r
        # agreement across several images of the same carton adds confidence
        for code in active:
            extra = max(0, len(support[code]) - 1)
            # not capped: capping at 1.0 makes several codes tie
            per_code[code] = per_code[code] + 0.05 * min(extra, 3)
        ranked = sorted(per_code.items(), key=lambda kv: kv[1], reverse=True)
        best_code, best = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = best - second
        confidence = float(np.clip(0.75 * best + 0.75 * margin, 0.0, 1.0))
        top = best_for.get(best_code) or max(readings, key=lambda r: r.region_score)
        accepted = confidence >= self.cfg.accept_threshold and margin >= self.cfg.min_margin
        raw = top.text or top.glyph_text
        return Result(
            stamp=best_code if accepted else None,
            confidence=round(confidence, 4),
            crop=top.crop,
            raw_text=raw,
            scores={k: round(v, 4) for k, v in ranked[:5]},
            best_guess=best_code if best > 0 else None,
            image_index=top.image_index,
        )

    # ---- helpers -------------------------------------------------------
    def _resize(self, img: np.ndarray) -> np.ndarray:
        h, w = img.shape[:2]
        s = self.cfg.work_max_side / max(h, w)
        if s < 1.0:
            img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        return img

    def _ocr_line(self, image: np.ndarray) -> tuple[str, float]:
        data = pytesseract.image_to_data(
            image, config=TESS_CONFIG, output_type=pytesseract.Output.DICT
        )
        words, confs = [], []
        for i, word in enumerate(data["text"]):
            word = clean(word or "")
            if word:
                words.append(word)
                confs.append(max(0.0, float(data["conf"][i])) / 100.0)
        return "".join(words), float(np.mean(confs)) if confs else 0.0


register("stamp", lambda cfg: StampRecognizer(cfg))
register("stamp-tesseract", lambda cfg: StampRecognizer(cfg, use_glyphs=False))
register("stamp-glyphs", lambda cfg: StampRecognizer(cfg, use_tesseract=False))
for _name, _w in {"w-pos": (0, 1, 0), "w-whole": (0, 0, 1), "w-tp": (0.5, 0.5, 0), "w-tw": (0.5, 0, 0.5), "w-eq": (1, 1, 1)}.items():
    register(f"stamp-{_name}", lambda cfg, _w=_w: StampRecognizer(cfg, use_tesseract=_w[0] > 0, weights=_w))
