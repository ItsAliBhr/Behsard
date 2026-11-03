"""Network camera tests with simulated live streams (Task 11).

* HTTP (ffmpeg only): outage of 20 s, recovery within 10 s; 2-camera live
  stream end-to-end.
* RTSP via MediaMTX (skipped unless ``mediamtx`` is installed; CI installs it).
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

from behsard.config import Config
from behsard.core.models import Conveyor
from behsard.core.repository import SqliteRepository
from behsard.core.services import ShiftService
from behsard.eval.synth_video import VideoConfig, generate_video_pair
from behsard.live.reader import CameraReader
from behsard.live.service import LiveService
from behsard.recognition.stamp import StampRecognizer

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
needs_mediamtx = pytest.mark.skipif(
    shutil.which("mediamtx") is None or shutil.which("ffmpeg") is None, reason="mediamtx/ffmpeg not installed"
)


@pytest.fixture(scope="module")
def video(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("netvideo")
    generate_video_pair(d, seed=2, cfg=VideoConfig(n_cartons=5))
    return d


def _wait(cond, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def _outage_recovery(url: str, cam) -> float:  # type: ignore[no-untyped-def]
    frames: list[float] = []
    r = CameraReader("net", url, lambda f, ts: frames.append(time.time()), process_fps=10)
    r.start()
    try:
        assert _wait(lambda: len(frames) > 10, 20), "no frames from simulated camera"
        cam.drop(20)
        t_drop = time.time()
        assert _wait(lambda: r.health.state == "reconnecting", 10)
        _wait(lambda: time.time() - t_drop >= 20, 25)
        n = len(frames)
        assert _wait(lambda: len(frames) > n + 3, 15), "did not recover"
        return frames[n] - (t_drop + 20)
    finally:
        r.stop()
        r.join(10)


@needs_ffmpeg
def test_http_camera_drop_20s_recovers_within_10s(video: Path) -> None:
    from behsard.sim.netcam import HttpCamera

    cam = HttpCamera(video / "left.mp4").start()
    try:
        assert _outage_recovery(cam.url, cam) < 10.0
    finally:
        cam.stop()


def _shift_repo(tmp: Path, codes: list[str]) -> SqliteRepository:
    repo = SqliteRepository(tmp / "db.sqlite")
    repo.upsert_conveyor(Conveyor(1, "c1"))
    svc = ShiftService(repo)
    sh = svc.open_shift("m", datetime(2026, 1, 1))
    for i, c in enumerate(codes):
        svc.assign(sh.id, svc.ensure_worker(f"w{i}").id, c, 1)
    return repo


def _run_live(urls: dict[str, str], repo: SqliteRepository, media: Path, seconds: float) -> LiveService:
    cfg = Config()
    cfg.media_dir = media
    cfg.cameras = [{"id": k, "conveyor_id": 1, "side": k, "url": u} for k, u in urls.items()]
    live = LiveService(cfg, repo, StampRecognizer())
    live.start()
    time.sleep(seconds)
    live.stop()
    return live


def _check_events(repo: SqliteRepository, truth: dict) -> None:  # type: ignore[type-arg]
    stamped = {c["stamp"] for c in truth["cartons"] if c["stamp"]}
    evs = repo.list_carton_events()
    assert evs
    # 0 double counts: every carton merged from both cameras
    assert all(e.meta["sides"] == ["L", "R"] for e in evs), [e.meta["sides"] for e in evs]
    # 0 wrong credits
    assert all(e.meta["stamp_code"] in stamped for e in evs if e.meta["stamp_code"])


@needs_ffmpeg
def test_http_camera_live_pipeline(video: Path, tmp_path: Path) -> None:
    """Single network camera (single-camera conveyor): cartons are stored
    through the network reader path, none credited to a wrong stamp."""
    from behsard.sim.netcam import HttpCamera

    truth = json.loads((video / "truth.json").read_text())
    cam = HttpCamera(video / "left.mp4").start()
    try:
        repo = _shift_repo(tmp_path, truth["codes"])
        _run_live({"L": cam.url}, repo, tmp_path / "m", 35)
    finally:
        cam.stop()
    evs = repo.list_carton_events()
    stamped = {c["stamp"] for c in truth["cartons"] if c["stamp"]}
    assert len(evs) >= 3
    assert all(e.meta["sides"] == ["L"] for e in evs)
    assert all(e.meta["stamp_code"] in stamped for e in evs if e.meta["stamp_code"])


@needs_mediamtx
@pytest.mark.rtsp
def test_rtsp_cameras_end_to_end_and_drop(video: Path, tmp_path: Path) -> None:
    from behsard.sim.netcam import MediaMtx, RtspConveyor

    truth = json.loads((video / "truth.json").read_text())
    server = MediaMtx()
    conv = RtspConveyor(server, video / "left.mp4", video / "right.mp4").start()
    try:
        time.sleep(3)
        repo = _shift_repo(tmp_path, truth["codes"])
        _run_live(conv.urls, repo, tmp_path / "m", 40)
        _check_events(repo, truth)
        assert _outage_recovery(conv.urls["L"], conv) < 10.0  # publisher killed for 20 s
    finally:
        conv.stop()
        server.stop()
