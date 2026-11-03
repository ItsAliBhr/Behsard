"""Tuning loop: learn stamp templates from confirmed real crops.

Field data (supervisor-confirmed or test-mode-confirmed cartons, exported in
the eval format) is split into a train and a test half. For every train
sample the reading that best matches the confirmed stamp is used to add
whole-code and per-glyph templates. The recognizer is evaluated on the test
half before and after, so the reported gain is not measured on the data the
templates came from.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from behsard.eval.harness import EvalReport, Sample, evaluate, load_images, load_samples
from behsard.recognition.glyphs import (
    GlyphScorer,
    clear_learned,
    code_vector,
    normalise_glyph,
    save_learned,
    segment_line,
    set_learned,
)
from behsard.recognition.stamp import StampRecognizer, glyph_norm


def split(samples: Sequence[Sample], test_frac: float = 0.5) -> tuple[list[Sample], list[Sample]]:
    """Deterministic split by file-name hash (stable across runs)."""
    train, test = [], []
    for s in samples:
        h = int(hashlib.sha1(s.name.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        (test if h < test_frac else train).append(s)
    return train, test


def learn(samples: Sequence[Sample], min_match: float = 0.25) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], int]:
    """Glyph and whole-code template vectors from labeled stamped samples."""
    rec = StampRecognizer(load_templates=False)
    glyph: dict[str, list[np.ndarray]] = {}
    code: dict[str, list[np.ndarray]] = {}
    used = 0
    for s in samples:
        if not s.stamp or len(s.stamp) != 3:
            continue
        best, best_score = None, -1.0
        for i, img in enumerate(load_images(s)):
            for r in rec.read_image(img, i):
                sc = glyph_norm(GlyphScorer.whole_code_scores(r.line, [s.stamp])[s.stamp])
                if sc > best_score:
                    best, best_score = r, sc
        if best is None or best_score < min_match:
            continue
        v = code_vector(best.line)
        if v is not None:
            code.setdefault(s.stamp, []).append(v)
        glyphs = segment_line(best.line, 3)
        if len(glyphs) == 3:
            for ch, g in zip(s.stamp, glyphs):
                gv = normalise_glyph(g)
                if gv is not None:
                    glyph.setdefault(ch, []).append(gv)
        used += 1
    return (
        {k: np.stack(v) for k, v in glyph.items()},
        {k: np.stack(v) for k, v in code.items()},
        used,
    )


@dataclass
class TuningResult:
    n_train: int
    n_test: int
    used: int
    before: EvalReport
    after: EvalReport

    def table(self) -> str:
        b, a = self.before.summary(), self.after.summary()
        rows = [("accuracy", "accuracy"), ("rejection rate", "rejection_rate"),
                ("error rate", "error_rate"), ("top-1", "top1_accuracy")]
        lines = [
            f"train samples: {self.n_train} (templates from {self.used}), test samples: {self.n_test}",
            f"{'metric':<16}{'before':>10}{'after':>10}{'change':>10}",
        ]
        for label, k in rows:
            lines.append(f"{label:<16}{b[k]:>10.1%}{a[k]:>10.1%}{a[k] - b[k]:>+10.1%}")
        return "\n".join(lines)


def rebuild(data_dir: str | Path, out: str | Path | None = None, test_frac: float = 0.5,
            train_all: bool = True) -> TuningResult:
    """Learn on the train half, report before/after on the test half, then
    (``train_all``) save templates learned from *all* samples to ``out``."""
    samples = load_samples(data_dir)
    train, test = split(samples, test_frac)
    clear_learned()
    before = evaluate(StampRecognizer(load_templates=False), test)
    g, c, used = learn(train)
    set_learned(g, c)
    after = evaluate(StampRecognizer(load_templates=False), test)
    if out is not None:
        if train_all:
            g, c, _ = learn(samples)
        save_learned(out, g, c)
    clear_learned()
    return TuningResult(len(train), len(test), used, before, after)
