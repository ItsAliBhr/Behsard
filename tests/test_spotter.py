"""Stamp-based counting (default mode): unit tests with a scripted reader,
synthetic two-camera run, and the real sample videos."""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from behsard.config import Config
from behsard.core.models import CartonStatus, Conveyor
from behsard.core.repository import SqliteRepository
from behsard.core.services import ShiftService
from behsard.eval.synth_video import VideoConfig, generate_video_pair
from behsard.live.service import LiveService
from behsard.recognition.base import Result
from behsard.recognition.stamp import StampRecognizer
from behsard.tracking.spotter import SpotterParams, StampSpotter

from conftest import FIXTURES

T0 = datetime(2026, 1, 1, 9)


@dataclass
class S:
    code: str
    accepted: bool
    confidence: float
    pos: tuple[float, float]
    size: float = 0.05

    @property
    def result(self) -> Result:
        return Result(self.code if self.accepted else None, self.confidence, crop=np.zeros((4, 4, 3), np.uint8), best_guess=self.code)


def _run(script: dict[float, list[S]], seconds: float, **params) -> list:
    """script: time (s) -> spots seen at that time."""
    rng = np.random.default_rng(0)
    spotter = StampSpotter("c", lambda img, active: script.get(round(float(img[0, 0, 0]) / 4, 2), []),
                           lambda ts: ["A12", "B07"], SpotterParams(interval_s=0.25, **params))
    out = []
    for i in range(int(seconds * 4) + 1):
        frame = rng.integers(0, 255, (40, 60, 3), dtype=np.uint8)  # changing view
        frame[0, 0, 0] = i  # time index, read back by the fake spot function
        out += spotter.update(frame, T0 + timedelta(seconds=i / 4))
    return out + spotter.flush()


def test_moving_stamp_counted_once_and_emitted_early() -> None:
    script = {t / 4: [S("A12", True, 0.7, (0.9 - 0.1 * t, 0.5))] for t in range(8)}
    out = _run(script, 6)
    assert len(out) == 1 and out[0].result.stamp == "A12"
    assert (out[0].ts - T0).total_seconds() == 0  # first seen


def test_two_cartons_same_stamp_are_two_counts() -> None:
    script = {}
    for t in range(6):  # first carton leaves on the left, second enters on the right
        script[t / 4] = [S("A12", True, 0.7, (0.5 - 0.08 * t, 0.5))]
    for t in range(4, 10):
        script.setdefault(t / 4, []).append(S("A12", True, 0.7, (0.95 - 0.08 * (t - 4), 0.5)))
    assert [o.result.stamp for o in _run(script, 6)] == ["A12", "A12"]


def test_duplicate_regions_and_misread_do_not_split() -> None:
    script = {
        0.0: [S("A12", True, 0.7, (0.5, 0.5)), S("A12", False, 0.3, (0.56, 0.5))],  # "A1"+"12" regions
        0.25: [S("B07", False, 0.3, (0.48, 0.5))],  # one misread in the same place
        0.5: [S("A12", True, 0.8, (0.45, 0.5))],
    }
    out = _run(script, 5)
    assert len(out) == 1 and out[0].result.stamp == "A12"


def test_weak_reads_go_to_review_and_clutter_is_dropped() -> None:
    weak = {t / 4: [S("A12", False, 0.35, (0.3, 0.3))] for t in range(5)}
    out = _run(weak, 5)
    assert len(out) == 1 and out[0].result.stamp is None and out[0].result.best_guess == "A12"
    flip = {t / 4: [S("A12" if t % 2 else "B07", False, 0.35, (0.3, 0.3))] for t in range(6)}
    assert _run(flip, 5) == []  # print/clutter flips between codes
    assert _run({0.0: [S("A12", False, 0.25, (0.3, 0.3))]}, 5) == []
    one = _run({0.0: [S("A12", True, 0.5, (0.3, 0.3))]}, 5)  # single borderline read
    assert len(one) == 1 and one[0].result.stamp is None


def test_stamp_hidden_briefly_in_place_is_not_recounted() -> None:
    script = {0.0: [S("A12", True, 0.9, (0.4, 0.6))], 0.25: [S("A12", True, 0.9, (0.4, 0.6))],
              5.0: [S("A12", True, 0.9, (0.41, 0.6))]}  # hand in front for ~5 s
    assert len(_run(script, 8)) == 1
    script[20.0] = [S("A12", True, 0.9, (0.4, 0.6))]  # gone for long: a new carton
    assert len(_run(script, 22)) == 2


def test_no_active_stamps_reads_nothing() -> None:
    sp = StampSpotter("c", lambda img, a: pytest.fail("must not read"), lambda ts: [])
    assert sp.update(np.zeros((10, 10, 3), np.uint8), T0) == [] and sp.stats["no_active"] == 1


def _wait(cond, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def _replay(tmp: Path, cams: list[dict], stamps: list[str]) -> list:
    repo = SqliteRepository(":memory:")
    repo.upsert_conveyor(Conveyor(1, "c1"))
    cfg = Config()
    cfg.media_dir = tmp / "media"
    live = LiveService(cfg, repo, StampRecognizer())
    live.start()
    for cam in cams:
        live.start_camera({**cam, "conveyor_id": 1, "loop": False, "stamps": stamps}, replay=True)
    assert _wait(lambda: all(c.reader.health.state == "finished" for c in live.cameras.values()), 400)
    time.sleep(1.5)
    live.stop()
    return repo.list_carton_events()


def test_synthetic_two_cameras_stamps_mode(tmp_path: Path) -> None:
    d = tmp_path / "v"
    truth = generate_video_pair(d, seed=2, cfg=VideoConfig(n_cartons=5))
    evs = _replay(tmp_path, [{"id": "L", "side": "L", "url": str(d / "left.mp4")},
                             {"id": "R", "side": "R", "url": str(d / "right.mp4")}], truth["codes"] + ["A12"])
    expected = Counter(c["stamp"] for c in truth["cartons"] if c["stamp"])
    credited = Counter(e.meta["stamp_code"] for e in evs if e.status == CartonStatus.RECOGNIZED)
    assert all(k in expected and n <= expected[k] for k, n in credited.items()), (credited, expected)  # 0 wrong, 0 double
    assert sum(credited.values()) >= sum(expected.values()) - 1
    assert all((tmp_path / "media" / e.frame_path).exists() for e in evs)


@pytest.mark.slow
def test_real_phone_video_gives_stamp_events(tmp_path: Path) -> None:
    """Handheld clip with 6 stamped cartons (A12): output, right stamp only."""
    active = ["A12", "A17", "B12", "B07", "C15", "D03", "F21", "G14", "H11", "K12", "M21", "R12"]
    evs = _replay(tmp_path, [{"id": "p", "side": "L", "url": str(FIXTURES / "videos" / "IMG_5796_10fps.mp4")}], active)
    credited = [e.meta["stamp_code"] for e in evs if e.status == CartonStatus.RECOGNIZED]
    assert set(credited) == {"A12"}  # never a wrong worker, even with look-alike codes active
    assert 4 <= len(credited) <= 7, credited  # 6 stamped cartons; handheld camera -> +-1..2
