from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from behsard.cli import main
from behsard.core.models import CartonStatus, Conveyor
from behsard.core.repository import SqliteRepository
from behsard.core.services import ShiftService
from behsard.eval.synth_video import VideoConfig, generate_video_pair
from behsard.recognition.stamp import StampRecognizer
from behsard.tracking.pipeline import CameraInput, Pipeline, iter_frames
from behsard.tracking.tracker import CartonSighting, CartonTracker, pair_sightings

T0 = datetime(2026, 11, 1, 9, 0, 0)


@pytest.fixture(scope="module")
def video(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("video")
    generate_video_pair(d, seed=2, cfg=VideoConfig(n_cartons=5))
    return d


def _track(path: Path) -> list[CartonSighting]:
    tr = CartonTracker(path.stem)
    out: list[CartonSighting] = []
    for frame, ts in iter_frames(path, T0, process_fps=10):
        out.extend(tr.update(frame, ts))
    return out


def test_tracker_counts_touching_cartons_stop_and_go(video: Path) -> None:
    truth = json.loads((video / "truth.json").read_text())
    expected = [c["t_center"] for c in truth["cartons"] if c["complete"]]
    for side in ("left", "right"):
        got = _track(video / f"{side}.mp4")
        assert len(got) == len(expected), side
        for s, t in zip(got, expected):
            assert abs((s.ts - T0).total_seconds() - t) <= 0.5
            assert 1 <= len(s.frames) <= 3
            # frames are cropped to the carton (narrower than the full frame)
            assert s.frames[0].shape[1] < s.full_frames[0].shape[1]


def test_stopped_conveyor_counts_nothing() -> None:
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 255, (180, 320, 3), dtype=np.uint8)
    tr = CartonTracker("c")
    out = []
    for i in range(50):
        out += tr.update(frame, T0 + timedelta(seconds=i / 10))
    assert out == [] and len(tr.buffer) == 1  # stopped: one buffered frame


def _s(cam: str, sec: float) -> CartonSighting:
    return CartonSighting(cam, T0 + timedelta(seconds=sec), [], [], 100.0, 0)


def test_pair_sightings_merges_sides_without_double_counting() -> None:
    left = [_s("L", 1.0), _s("L", 4.0), _s("L", 7.0)]
    right = [_s("R", 1.3), _s("R", 7.4), _s("R", 9.0)]
    pairs = pair_sightings(left, right, window_s=1.0)
    kinds = [(l is not None, r is not None) for l, r in pairs]
    assert kinds == [(True, True), (True, False), (True, True), (False, True)]


def test_pipeline_end_to_end(video: Path, tmp_path: Path) -> None:
    truth = json.loads((video / "truth.json").read_text())
    repo = SqliteRepository(":memory:")
    repo.upsert_conveyor(Conveyor(1, "c1"))
    svc = ShiftService(repo)
    shift = svc.open_shift("m", datetime(2026, 11, 1, 6))
    for i, code in enumerate(sorted(set(truth["codes"]) | {"A12", "B07", "C15", "D03"})):
        w = svc.ensure_worker(f"w{i}")
        svc.assign(shift.id, w.id, code, 1)
    pipe = Pipeline(repo, StampRecognizer(), tmp_path / "media")
    res = pipe.run(
        [CameraInput(video / "left.mp4", "cL", 1, "L"), CameraInput(video / "right.mp4", "cR", 1, "R")],
        T0,
    )
    complete = [c for c in truth["cartons"] if c["complete"]]
    assert len(res.events) == len(complete)  # one event per carton, both sides merged
    assert all(ev.meta["sides"] == ["L", "R"] for ev in res.events)
    summ = res.summary()
    wrong = 0
    for ev, c in zip(res.events, complete):
        if ev.status == CartonStatus.RECOGNIZED:
            wrong += ev.meta["stamp_code"] != c["stamp"]
    assert wrong == 0
    assert summ["recognized"] >= len([c for c in complete if c["stamp"]]) - 2
    stored = repo.list_carton_events()
    assert len(stored) == len(res.events)
    ev = stored[0]
    assert ev.frame_path and (tmp_path / "media" / ev.frame_path).exists()
    assert repo.list_recognition_results(ev.id)[0].recognizer == "stamp"


def test_cli_process_video(video: Path, tmp_path: Path, capsys) -> None:
    db = tmp_path / "x.sqlite"
    rc = main([
        "process-video", "--video", str(video / "left.mp4"), "--camera", "camX",
        "--conveyor", "1", "--side", "L", "--start", "2026-11-01T09:00:00",
        "--db", str(db), "--media", str(tmp_path / "m"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"cartons": 5' in out
    # no shift open -> everything goes to manual review
    assert '"pending_review": 5' in out
