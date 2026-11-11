from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

from behsard.config import Config, load_config, save_config
from behsard.core.models import Conveyor
from behsard.core.repository import SqliteRepository
from behsard.core.services import ShiftService
from behsard.eval.synth_video import VideoConfig, generate_video_pair
from behsard.live.reader import CameraReader, redact, rtsp_url
from behsard.live.recorder import Recorder
from behsard.live.service import LiveService
from behsard.recognition.stamp import StampRecognizer


@pytest.fixture(scope="module")
def video(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("livevideo")
    generate_video_pair(d, seed=2, cfg=VideoConfig(n_cartons=5))
    return d


def _wait(cond, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_redact_and_url() -> None:
    url = rtsp_url("192.168.1.64", "admin", "S3cret!", "102")
    assert url == "rtsp://admin:S3cret!@192.168.1.64:554/Streaming/Channels/102"
    assert "S3cret" not in redact(url) and "admin:***@" in redact(url)


def test_reader_reconnects_when_source_appears(video: Path, tmp_path: Path) -> None:
    target = tmp_path / "late.mp4"
    frames: list[datetime] = []
    r = CameraReader("c", str(target), lambda f, ts: frames.append(ts), process_fps=10, backoff_max=1.0, speed=4)
    r.start()
    assert _wait(lambda: r.health.reconnects >= 1, 5)
    assert r.health.state in ("connecting", "reconnecting")
    shutil.copy(video / "left.mp4", target)
    assert _wait(lambda: r.health.state == "connected" and len(frames) > 5, 5)  # < 10 s
    h = r.health.as_dict()
    assert h["resolution"] == "640x360" and h["frames"] > 0
    r.stop()
    r.join(5)


def test_reader_throttles_to_process_fps(video: Path) -> None:
    n = []
    r = CameraReader("c", str(video / "left.mp4"), lambda f, ts: n.append(1), process_fps=5, loop=False)
    r.start()
    time.sleep(3)
    r.stop()
    r.join(5)
    assert 10 <= len(n) <= 20  # 5 fps out of a 10 fps source
    assert r.health.frames_in > len(n)


def test_live_service_end_to_end(video: Path, tmp_path: Path) -> None:
    truth = json.loads((video / "truth.json").read_text())
    repo = SqliteRepository(tmp_path / "db.sqlite")
    repo.upsert_conveyor(Conveyor(1, "c1"))
    svc = ShiftService(repo)
    sh = svc.open_shift("m", datetime(2026, 1, 1))
    for i, c in enumerate(sorted(set(truth["codes"]) | {"A12"})):
        svc.assign(sh.id, svc.ensure_worker(f"w{i}").id, c, 1)
    cfg = Config()
    cfg.tracking.mode = "seams"
    cfg.media_dir = tmp_path / "media"
    cfg.cameras = [
        {"id": "L", "conveyor_id": 1, "side": "L", "url": str(video / "left.mp4"), "loop": False, "speed": 3},
        {"id": "R", "conveyor_id": 1, "side": "R", "url": str(video / "right.mp4"), "loop": False, "speed": 3},
    ]
    seen = []
    live = LiveService(cfg, repo, StampRecognizer(), on_event=seen.append)
    live.start()
    assert _wait(lambda: all(c.reader.health.state == "finished" for c in live.cameras.values()), 60)
    live.stop()  # flushes held sightings and the recognition queue
    events = repo.list_carton_events()
    complete = [c for c in truth["cartons"] if c["complete"]]
    assert len(events) == len(complete) == len(seen)
    assert all(e.meta["sides"] == ["L", "R"] for e in events)  # merged, not double counted
    for e, c in zip(events, complete):  # 0 wrong credits
        if e.meta["stamp_code"]:
            assert e.meta["stamp_code"] == c["stamp"]
    # persisted immediately with media on disk
    assert all((cfg.media_dir / e.frame_path).exists() for e in events)
    assert live.recent and all(r.latency_s < 10 for r in live.recent)


def test_test_mode_events_do_not_count(video: Path, tmp_path: Path) -> None:
    repo = SqliteRepository(":memory:")
    repo.upsert_conveyor(Conveyor(1, "c1"))
    cfg = Config()
    cfg.tracking.mode = "seams"
    cfg.media_dir = tmp_path / "m"
    cfg.cameras = [{"id": "L", "conveyor_id": 1, "side": "L", "url": str(video / "left.mp4"), "loop": False, "speed": 3}]
    live = LiveService(cfg, repo, StampRecognizer())
    assert live.set_test_mode(True, ["f60", "bad", "H09"]) == ["F60", "H09"]
    live.start()
    assert _wait(lambda: all(c.reader.health.state == "finished" for c in live.cameras.values()), 60)
    live.stop()
    evs = repo.list_carton_events()
    assert evs and all(e.mode == "test" for e in evs)
    assert repo.count_by_status() == {}  # production counters untouched
    assert repo.counted_rows(datetime(2000, 1, 1), datetime(2100, 1, 1)) == []


def test_recorder_disk_guard(tmp_path: Path) -> None:
    rec = Recorder("c", "rtsp://x/1", tmp_path, min_free_gb=1e9)  # impossible threshold
    assert rec.start() is False and rec.error


def test_config_save_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "config" / "behsard.yaml"
    cfg = load_config(p)  # missing file -> defaults
    cfg.cameras = [{"id": "cam1L", "conveyor_id": 1, "side": "L", "host": "10.0.0.5", "password": "x", "roi": [0.1, 0.2, 0.7, 0.6]}]
    cfg.test_mode.stamps = ["A12"]
    save_config(cfg)
    again = load_config(p)
    assert again.cameras == cfg.cameras and again.test_mode.stamps == ["A12"]
    assert again.db_path == tmp_path / "data" / "behsard.sqlite"  # relative to data home


def test_replay_setup_flow_with_roi(video: Path, tmp_path: Path) -> None:
    """Replay: choose video -> frames to draw the watch region -> start with ROI."""
    from fastapi.testclient import TestClient

    from behsard.web.app import create_app

    repo = SqliteRepository(":memory:")
    cfg = Config()
    cfg.home = tmp_path
    cfg.conveyors = [{"id": 1, "name": "c1"}]
    app = create_app(cfg, repo, tmp_path / "media", start_live=False)
    c = TestClient(app)
    r = c.post("/replay/prepare", data={"path": str(video / "left.mp4")}, follow_redirects=False)
    assert r.status_code == 303 and "/replay/setup" in r.headers["location"]
    page = c.get(r.headers["location"]).text
    assert "roi-canvas" in page and page.count("snapshots/replay_") >= 4
    # no stamp list and no open shift -> back to the setup page asking for it
    r = c.post("/replay/start", data={"path": str(video / "left.mp4")}, follow_redirects=False)
    assert "need_stamps=1" in r.headers["location"]
    truth = json.loads((video / "truth.json").read_text())
    c.post("/replay/start", data={"path": str(video / "left.mp4"), "x": "0.1", "y": "0.2", "w": "0.8", "h": "0.6",
                                  "stamps": " ".join(truth["codes"])})
    live = app.state.live
    try:
        assert live.cameras["replay"].cfg["roi"] == [0.1, 0.2, 0.8, 0.6]
        assert live.cameras["replay"].worker.spotter is not None  # stamps mode by default
        assert _wait(lambda: live.cameras["replay"].reader.health.state == "finished", 120)
    finally:
        live.stop()
    html = c.get("/replay").text
    assert "کارتن‌های مهردار پیداشده" in html
    # the left camera sees the stamps on the left side only; none may be wrong
    left = [c_["stamp"] for c_ in truth["cartons"] if c_["stamp"] and c_["side"] == "L"]
    evs = repo.list_carton_events()
    read = [e.meta["stamp_code"] for e in evs if e.meta["stamp_code"]]
    assert all(e.mode == "test" for e in evs)
    assert read and all(code in left for code in read) and len(read) <= len(left)
