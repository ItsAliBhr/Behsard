from __future__ import annotations

from datetime import datetime

import pytest

from behsard.config import load_config
from behsard.core.models import CartonEvent, CartonStatus, ManualReview, ReviewDecision
from behsard.core.repository import SqliteRepository
from behsard.core.services import DomainError, ShiftService, normalize_stamp_code, sync_config


def test_normalize_stamp_code() -> None:
    assert normalize_stamp_code(" a12 ") == "A12"
    with pytest.raises(DomainError):
        normalize_stamp_code("A1")
    with pytest.raises(DomainError):
        normalize_stamp_code("12A")


def test_active_stamps_by_conveyor_and_time(shift_setup, repo) -> None:
    svc: ShiftService = shift_setup["svc"]
    t = datetime(2026, 11, 1, 9, 0)
    assert [s.code for s in svc.active_stamps(1, t)] == ["A12", "B07"]
    assert [s.code for s in svc.active_stamps(2, t)] == ["C15"]
    # before the shift started
    assert svc.active_stamps(1, datetime(2026, 11, 1, 5, 59)) == []


def test_close_shift_limits_active_window(shift_setup) -> None:
    svc: ShiftService = shift_setup["svc"]
    shift = shift_setup["shift"]
    svc.close_shift(shift.id, datetime(2026, 11, 1, 14, 0))
    assert svc.active_stamps(1, datetime(2026, 11, 1, 13, 59))
    assert svc.active_stamps(1, datetime(2026, 11, 1, 14, 0)) == []
    with pytest.raises(DomainError):
        svc.close_shift(shift.id)
    with pytest.raises(DomainError):
        svc.assign(shift.id, shift_setup["workers"][0].id, "D03", 1)


def test_duplicate_assignments_rejected(shift_setup) -> None:
    svc: ShiftService = shift_setup["svc"]
    shift = shift_setup["shift"]
    w = svc.ensure_worker("New")
    with pytest.raises(DomainError):
        svc.assign(shift.id, w.id, "A12", 2)  # stamp taken
    with pytest.raises(DomainError):
        svc.assign(shift.id, shift_setup["workers"][0].id, "Z99", 2)  # worker taken
    with pytest.raises(DomainError):
        svc.assign(shift.id, w.id, "Z99", 99)  # unknown conveyor


def test_copy_assignments(shift_setup) -> None:
    svc: ShiftService = shift_setup["svc"]
    old = shift_setup["shift"]
    svc.close_shift(old.id, datetime(2026, 11, 1, 14, 0))
    new = svc.open_shift("evening", datetime(2026, 11, 1, 14, 0))
    assert svc.copy_assignments(old.id, new.id) == 3
    assert svc.copy_assignments(old.id, new.id) == 0
    assert [s.shift_id for s in svc.active_stamps(1, datetime(2026, 11, 1, 15))] == [
        new.id,
        new.id,
    ]


def test_carton_event_roundtrip_and_counted_rows(shift_setup, repo: SqliteRepository) -> None:
    svc: ShiftService = shift_setup["svc"]
    shift = shift_setup["shift"]
    a12 = repo.get_stamp_by_code("A12")
    ev = repo.add_carton_event(
        CartonEvent(
            conveyor_id=1,
            ts=datetime(2026, 11, 1, 9, 0, 1),
            status=CartonStatus.RECOGNIZED,
            shift_id=shift.id,
            stamp_id=a12.id,
            confidence=0.9,
            meta={"track": 3},
        )
    )
    pending = repo.add_carton_event(
        CartonEvent(1, datetime(2026, 11, 1, 9, 0, 3), CartonStatus.PENDING_REVIEW, shift.id)
    )
    got = repo.get_carton_event(ev.id)
    assert got is not None and got.meta == {"track": 3} and got.status.counts
    assert [e.id for e in repo.list_carton_events(CartonStatus.PENDING_REVIEW)] == [pending.id]

    pending.status = CartonStatus.RESOLVED
    pending.stamp_id = repo.get_stamp_by_code("B07").id
    repo.update_carton_event(pending)
    repo.add_manual_review(
        ManualReview(pending.id, ReviewDecision.STAMP, pending.stamp_id, "sup")
    )
    rows = repo.counted_rows(datetime(2026, 11, 1), datetime(2026, 11, 2))
    assert [(r["stamp_code"], r["worker_name"]) for r in rows] == [
        ("A12", "Ali"),
        ("B07", "Reza"),
    ]
    assert repo.count_by_status() == {"recognized": 1, "resolved": 1}
    assert svc.shift_at(datetime(2026, 11, 1, 9)).id == shift.id


def test_sqlite_file_persists(tmp_path) -> None:
    path = tmp_path / "db" / "t.sqlite"
    r = SqliteRepository(path)
    r.add_stamp(__import__("behsard.core.models", fromlist=["Stamp"]).Stamp("a12"))
    r.close()
    r2 = SqliteRepository(path)
    assert [s.code for s in r2.list_stamps()] == ["A12"]


def test_config_loads_and_syncs(repo: SqliteRepository) -> None:
    cfg = load_config("config/behsard.yaml")
    assert cfg.recognition.accept_threshold > 0
    sync_config(repo, cfg)
    assert len(repo.list_conveyors()) == 4
    assert {c.side for c in repo.list_cameras(1)} == {"L", "R"}
