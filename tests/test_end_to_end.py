"""End-to-end counting on synthetic 2-camera conveyor streams (Task 11).

Offline pipeline: per-stamp counts must match the ground truth exactly.
Live (paced, wall clock): exact carton count, 0 double counts, 0 wrong
credits, and latency carton-on-line -> event stored under 5 s.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from behsard.config import Config
from behsard.core.models import CartonStatus, Conveyor
from behsard.core.repository import SqliteRepository
from behsard.core.services import ShiftService
from behsard.eval.synth_video import VideoConfig, generate_video_pair
from behsard.live.service import LiveService
from behsard.recognition.stamp import StampRecognizer
from behsard.tracking.pipeline import CameraInput, Pipeline

T0 = datetime(2026, 1, 1, 9)


@pytest.fixture(scope="module")
def video(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("e2evideo")
    generate_video_pair(d, seed=0, cfg=VideoConfig(n_cartons=8))
    return d


def _repo(path: Path | str, codes: list[str]) -> SqliteRepository:
    repo = SqliteRepository(path)
    repo.upsert_conveyor(Conveyor(1, "c1"))
    svc = ShiftService(repo)
    sh = svc.open_shift("m", datetime(2026, 1, 1))
    for i, c in enumerate(sorted(set(codes) | {"A12", "B07"})):
        svc.assign(sh.id, svc.ensure_worker(f"w{i}").id, c, 1)
    return repo


def test_offline_exact_per_stamp_counts(video: Path, tmp_path: Path) -> None:
    """Auto credits are never wrong, and after the supervisor resolves the
    review queue the per-stamp report equals the ground truth exactly."""
    from behsard.core.models import ReviewDecision
    from behsard.review.service import ReviewService

    truth = json.loads((video / "truth.json").read_text())
    repo = _repo(":memory:", truth["codes"])
    res = Pipeline(repo, StampRecognizer(), tmp_path).run(
        [CameraInput(video / "left.mp4", "L", 1, "L"), CameraInput(video / "right.mp4", "R", 1, "R")], T0
    )
    complete = [c for c in truth["cartons"] if c["complete"]]
    expected = Counter(c["stamp"] for c in complete if c["stamp"])
    assert len(res.events) == len(complete)  # one event per carton
    auto = Counter()
    review = ReviewService(repo)
    for ev, c in zip(res.events, complete):
        if ev.status == CartonStatus.RECOGNIZED:
            assert ev.meta["stamp_code"] == c["stamp"]  # 0 wrong credits
            auto[c["stamp"]] += 1
        elif c["stamp"]:
            review.resolve(ev.id, ReviewDecision.STAMP, c["stamp"])
        else:
            review.resolve(ev.id, ReviewDecision.NO_STAMP)
    assert sum(auto.values()) >= sum(expected.values()) - 1  # at most one needs review
    rows = repo.counted_rows(datetime(2000, 1, 1), datetime(2100, 1, 1))
    assert Counter(r["stamp_code"] for r in rows) == expected


def test_live_counts_and_latency(video: Path, tmp_path: Path) -> None:
    truth = json.loads((video / "truth.json").read_text())
    repo = _repo(tmp_path / "db.sqlite", truth["codes"])
    cfg = Config()
    cfg.tracking.mode = "seams"
    cfg.media_dir = tmp_path / "media"
    cfg.cameras = [
        {"id": "L", "conveyor_id": 1, "side": "L", "url": str(video / "left.mp4"), "loop": False},
        {"id": "R", "conveyor_id": 1, "side": "R", "url": str(video / "right.mp4"), "loop": False},
    ]
    live = LiveService(cfg, repo, StampRecognizer())
    live.start()
    end = time.time() + 120
    while time.time() < end and any(c.reader.health.state != "finished" for c in live.cameras.values()):
        time.sleep(0.5)
    live.stop()
    complete = [c for c in truth["cartons"] if c["complete"]]
    evs = repo.list_carton_events()
    assert len(evs) == len(complete)  # exact carton count
    assert all(e.meta["sides"] == ["L", "R"] for e in evs)  # 0 double counts
    credited = Counter()
    for e, c in zip(evs, complete):
        if e.status == CartonStatus.RECOGNIZED:
            assert e.meta["stamp_code"] == c["stamp"]  # 0 wrong credits
            credited[c["stamp"]] += 1
    expected = Counter(c["stamp"] for c in complete if c["stamp"])
    assert all(credited[k] <= v for k, v in expected.items())
    assert sum(credited.values()) >= sum(expected.values()) - 1  # at most one to review
    lat = [r.latency_s for r in live.recent]
    assert max(lat) < 5.0, lat  # carton centre on the line -> stored event
