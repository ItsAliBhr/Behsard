"""Regression thresholds on the real fixtures (Task 11).

The numbers measured when the thresholds were set (docs/progress.md) are the
minimums; recognition changes must not make them worse.
"""

from __future__ import annotations

import pytest

from behsard.eval.harness import evaluate, load_samples
from behsard.recognition.stamp import StampRecognizer

from conftest import FIXTURES

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def rec() -> StampRecognizer:
    return StampRecognizer()


def test_real_screenshots_regression(rec: StampRecognizer) -> None:
    s = evaluate(rec, load_samples(FIXTURES / "images")).summary()
    assert s["accuracy"] >= 1.0 and s["top1_accuracy"] >= 1.0 and s["error_rate"] == 0.0


def test_real_video_frames_regression(rec: StampRecognizer) -> None:
    s = evaluate(rec, load_samples(FIXTURES / "frames")).summary()
    assert s["accuracy"] >= 0.48, s  # measured 0.50
    assert s["top1_accuracy"] >= 0.80, s  # measured 0.8214
    assert s["error_rate"] == 0.0, s
    assert s["unstamped_false_accepts"] == 0, s
