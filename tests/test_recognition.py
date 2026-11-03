from __future__ import annotations

import cv2
import numpy as np
import pytest

from behsard.eval.harness import load_images, load_samples
from behsard.eval.synth import SynthConfig, generate_sample
from behsard.recognition.base import Recognizer
from behsard.recognition.glyphs import GlyphScorer, segment_line
from behsard.recognition.ink import ink_map, propose_regions, remove_long_lines
from behsard.recognition.match import match_scores, similarity, weighted_distance
from behsard.recognition.registry import get_recognizer
from behsard.recognition.stamp import StampRecognizer

from conftest import FIXTURES

ACTIVE = ["A12", "A17", "B12", "B07", "C15", "D03", "F21", "G14", "H11", "K12", "M21", "R12"]


@pytest.fixture(scope="module")
def rec() -> StampRecognizer:
    return StampRecognizer()


def test_fuzzy_match_prefers_true_code() -> None:
    assert similarity("A12", "A12") == 1.0
    assert weighted_distance("A2", "A12") < 0.5  # thin "1" lost
    for text in ("A2", "AI2", "4I2", "A1Z", "XA12"):
        s = match_scores([text], ACTIVE)
        assert max(s, key=s.get) == "A12", text
    s = match_scores(["XYZW"], ACTIVE)
    assert max(s.values()) < 0.6


def test_remove_long_lines_keeps_short_strokes() -> None:
    m = np.zeros((200, 200), np.uint8)
    m[:, 100:104] = 255  # carton seam
    m[50:70, 20:24] = 255  # a "1"
    out = remove_long_lines(m)
    assert out[:, 100:104].sum() == 0 and out[50:70, 20:24].sum() > 0


def _synthetic(seed: int, **kw) -> tuple[np.ndarray, dict]:
    cfg = SynthConfig(p_negative=0.0, p_faint=0.0, p_partial=0.0, p_motion_blur=0.0, **kw)
    from behsard.eval.synth import available_fonts

    return generate_sample(np.random.default_rng(seed), cfg, available_fonts())


def test_regions_cover_synthetic_stamp() -> None:
    hits = 0
    for seed in range(10):
        img, label = _synthetic(seed, min_text_h=40)
        x, y, w, h = label["meta"]["bbox"]
        cx, cy = x + w / 2, y + h / 2
        mask, strength = ink_map(img)
        regs = propose_regions(mask, strength, max_regions=4)
        hits += any(
            r.box[0] <= cx <= r.box[0] + r.box[2] and r.box[1] <= cy <= r.box[1] + r.box[3]
            for r in regs
        )
    assert hits >= 8


def test_segment_line_splits_three_glyphs() -> None:
    line = np.full((60, 160), 255, np.uint8)
    cv2.putText(line, "A12", (10, 45), cv2.FONT_HERSHEY_DUPLEX, 1.4, 0, 4)
    assert len(segment_line(line)) == 3
    text, pos = GlyphScorer().read(line)
    assert len(text) == 3 and text[0].isalpha() and text[1:].isdigit()


def test_recognizer_protocol_and_registry(rec: StampRecognizer) -> None:
    assert isinstance(rec, Recognizer)
    assert get_recognizer("stamp").name == "stamp"


def test_blank_carton_is_rejected(rec: StampRecognizer) -> None:
    img = np.full((360, 640, 3), (95, 140, 185), np.uint8)
    img = cv2.add(img, np.random.default_rng(0).integers(0, 6, img.shape, dtype=np.uint8))
    r = rec.recognize([img], ACTIVE)
    assert r.stamp is None


def test_clean_synthetic_stamps_mostly_read(rec: StampRecognizer) -> None:
    correct = wrong = 0
    for seed in range(100, 110):
        img, label = _synthetic(seed, min_text_h=45)
        r = rec.recognize([img], label["active"])
        correct += r.stamp == label["stamp"]
        wrong += r.stamp is not None and r.stamp != label["stamp"]
    assert correct >= 4 and wrong <= 1  # ~60% measured on the full set


@pytest.mark.slow
def test_real_fixture_images_read_as_a12(rec: StampRecognizer) -> None:
    samples = load_samples(FIXTURES / "images")
    for s in samples:
        r = rec.recognize(load_images(s), s.active)
        assert r.best_guess == "A12", s.name
        assert r.stamp in ("A12", None), s.name  # never a wrong worker
        assert r.crop is not None
