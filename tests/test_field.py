"""Field data collection + tuning loop (Task 9)."""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from behsard.config import Config
from behsard.core.models import CartonEvent, CartonStatus, ReviewDecision
from behsard.core.repository import SqliteRepository
from behsard.eval.harness import load_samples
from behsard.eval.tuning import rebuild, split
from behsard.recognition.glyphs import clear_learned, load_learned
from behsard.review.field_export import export_field_dataset
from behsard.review.service import ReviewService
from behsard.web.app import create_app

from conftest import FIXTURES


def _test_event(repo: SqliteRepository, media: Path, i: int, auto: str | None) -> CartonEvent:
    for c in ("A12", "B07"):
        if repo.get_stamp_by_code(c) is None:
            from behsard.core.models import Stamp

            repo.add_stamp(Stamp(c))
    ev = repo.add_carton_event(CartonEvent(
        1, datetime(2026, 11, 1, 9, 0, i), CartonStatus.RECOGNIZED if auto else CartonStatus.PENDING_REVIEW,
        meta={"stamp_code": auto, "candidates": ["A12", "B07"], "scores": {"A12": 0.7}}, mode="test",
    ))
    d = media / "20261101" / "conveyor1"
    d.mkdir(parents=True, exist_ok=True)
    img = np.full((40, 60, 3), 180, np.uint8)
    for suffix in ("frame", "carton0"):
        cv2.imwrite(str(d / f"{ev.id:07d}_{suffix}.jpg"), img)
    ev.frame_path = f"20261101/conveyor1/{ev.id:07d}_frame.jpg"
    repo.update_carton_event(ev)
    return ev


def test_export_field_dataset(repo: SqliteRepository, tmp_path: Path) -> None:
    cfg = Config()
    cfg.media_dir = tmp_path / "media"
    cfg.cameras = [{"id": "c1", "conveyor_id": 1, "side": "L", "host": "10.0.0.1", "user": "admin", "password": "S3cret"}]
    svc = ReviewService(repo)
    e1 = _test_event(repo, cfg.media_dir, 1, "A12")
    e2 = _test_event(repo, cfg.media_dir, 2, None)
    svc.resolve(e1.id, ReviewDecision.STAMP, "A12")  # test mode: confirm
    svc.resolve(e2.id, ReviewDecision.STAMP, "B07")  # test mode: correct
    z = export_field_dataset(repo, cfg, tmp_path / "out")
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
        root = names[0].split("/")[0]
        assert f"{root}/events.jsonl" in names and f"{root}/summary.json" in names
        cfgtxt = zf.read(f"{root}/config.yaml").decode()
        assert "S3cret" not in cfgtxt
        events = [json.loads(l) for l in zf.read(f"{root}/events.jsonl").decode().splitlines()]
        assert {e["review"]["stamp"] for e in events} == {"A12", "B07"}
        assert events[0]["scores"] == {"A12": 0.7} and events[0]["mode"] == "test"
        zf.extractall(tmp_path / "x")
    samples = load_samples(tmp_path / "x" / root / "labeled")  # usable by eval/
    assert sorted(s.stamp for s in samples) == ["A12", "B07"]
    assert samples[0].active == ["A12", "B07"]  # the test stamp list


def test_rebuild_templates_improves_held_out(tmp_path: Path) -> None:
    src = FIXTURES / "frames"
    data = tmp_path / "data"
    data.mkdir()
    labels = json.loads((src / "labels.json").read_text())
    keep = [l for l in labels if l["meta"]["background"] == "kraft"]  # small, fast subset
    for l in keep:
        shutil.copy(src / l["file"], data / l["file"])
    (data / "labels.json").write_text(json.dumps(keep))
    train, test = split(load_samples(data), 0.5)
    assert train and test and not {s.name for s in train} & {s.name for s in test}
    out = tmp_path / "learned.npz"
    res = rebuild(data, out, test_frac=0.5)
    b, a = res.before.summary(), res.after.summary()
    assert a["accuracy"] >= b["accuracy"] and a["error_rate"] == 0.0
    assert "before" in res.table()
    assert load_learned(out) > 0
    clear_learned()


def test_advanced_settings_and_export(repo: SqliteRepository, tmp_path: Path) -> None:
    cfg = Config()
    cfg.home = tmp_path
    cfg.conveyors = [{"id": 1, "name": "n1"}]
    c = TestClient(create_app(cfg, repo, tmp_path / "media", start_live=False))
    assert "آستانه پذیرش" in c.get("/advanced").text
    c.post("/advanced/save", data={"recognition.accept_threshold": "0.6", "tracking.edge_k": "99"})
    assert cfg.recognition.accept_threshold == 0.6 and cfg.tracking.edge_k == 20.0  # clamped
    c.post("/advanced/defaults")
    assert cfg.recognition.accept_threshold == 0.45 and cfg.tracking.edge_k == 6.0
    r = c.post("/advanced/export")
    assert r.status_code == 200 and zipfile.ZipFile(io.BytesIO(r.content)).namelist()
