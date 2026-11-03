from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from behsard.config import Config
from behsard.core.models import CartonEvent, CartonStatus, ReviewDecision
from behsard.core.repository import SqliteRepository
from behsard.core.services import DomainError
from behsard.eval.harness import load_samples
from behsard.review.service import ReviewService, export_labels, reopen
from behsard.web.app import create_app
from behsard.web.fa import en_digits, fa_digits, jalali_datetime, parse_jalali_date


def _pending(repo: SqliteRepository, shift_id: int, media: Path, n: int = 2) -> list[CartonEvent]:
    out = []
    d = media / "20261101" / "conveyor1"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        ev = repo.add_carton_event(
            CartonEvent(1, datetime(2026, 11, 1, 9, 0, i), CartonStatus.PENDING_REVIEW, shift_id,
                        meta={"scores": {"B07": 0.4, "A12": 0.3}, "best_guess": "B07"})
        )
        img = np.full((60, 80, 3), 200, np.uint8)
        cv2.imwrite(str(d / f"{ev.id:07d}_frame.jpg"), img)
        cv2.imwrite(str(d / f"{ev.id:07d}_carton0.jpg"), img)
        ev.frame_path = f"20261101/conveyor1/{ev.id:07d}_frame.jpg"
        repo.update_carton_event(ev)
        out.append(ev)
    return out


def test_candidates_ordered_by_recognizer_score(shift_setup, repo, tmp_path) -> None:
    ev = _pending(repo, shift_setup["shift"].id, tmp_path, 1)[0]
    cands = ReviewService(repo).candidates(ev)
    assert [c.code for c in cands] == ["B07", "A12"]  # conveyor 1 only, by score
    assert [c.key for c in cands] == [1, 2]
    assert cands[0].worker_name == "Reza"


def test_resolve_stores_label_and_counts(shift_setup, repo, tmp_path) -> None:
    e1, e2 = _pending(repo, shift_setup["shift"].id, tmp_path)
    svc = ReviewService(repo)
    assert svc.pending_count() == 2
    svc.resolve(e1.id, ReviewDecision.STAMP, "a12", reviewer="sup")
    with pytest.raises(DomainError):
        svc.resolve(e2.id, ReviewDecision.STAMP, "C15")  # active, but on conveyor 2
    svc.resolve(e2.id, ReviewDecision.NO_STAMP)
    assert svc.pending_count() == 0
    got = repo.get_carton_event(e1.id)
    assert got.status == CartonStatus.RESOLVED and got.stamp_id == repo.get_stamp_by_code("A12").id
    assert [r.decision for r in repo.list_manual_reviews()] == [ReviewDecision.STAMP, ReviewDecision.NO_STAMP]
    rows = repo.counted_rows(datetime(2026, 11, 1), datetime(2026, 11, 2))
    assert [r["stamp_code"] for r in rows] == ["A12"]
    # undo puts it back in the queue
    reopen(repo, e1.id)
    assert svc.pending_count() == 1
    # labels export in eval format
    svc.resolve(e1.id, ReviewDecision.STAMP, "B07")
    n = export_labels(repo, tmp_path, tmp_path / "labels")
    samples = load_samples(tmp_path / "labels")
    assert n == 2 and {s.stamp for s in samples} == {"B07", None}
    assert all(s.paths[0].name.endswith("_carton0.jpg") for s in samples)
    assert samples[0].active == ["A12", "B07"]


def test_fa_helpers() -> None:
    assert fa_digits("A12 ۱") == "A۱۲ ۱"
    assert en_digits("A۱۲") == "A12"
    assert jalali_datetime(datetime(2026, 11, 1, 9, 5)) == "1405/08/10 09:05"
    assert parse_jalali_date("۱۴۰۵/۰۸/۱۰").isoformat() == "2026-11-01"


@pytest.fixture
def client(shift_setup, repo, tmp_path) -> TestClient:
    _pending(repo, shift_setup["shift"].id, tmp_path)
    return TestClient(create_app(Config(), repo, tmp_path))


def test_review_page_flow(client: TestClient, repo) -> None:
    r = client.get("/review")
    assert r.status_code == 200
    assert 'dir="rtl"' in r.text and "در انتظار بررسی" in r.text
    assert "B07" in r.text and "1405/08/10" in r.text.replace("۱۴۰۵/۰۸/۱۰", "1405/08/10")
    assert "/media/20261101/conveyor1/0000001_frame.jpg" in r.text
    assert client.get("/media/20261101/conveyor1/0000001_frame.jpg").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200
    # pick a stamp (Persian digits typed are normalised)
    r = client.post("/review/1/resolve", data={"decision": "stamp", "stamp": "a۱۲"})
    assert r.status_code == 200 and "ثبت شد" in r.text
    assert "کارتن شماره ۲" in r.text  # next card shown
    assert repo.get_carton_event(1).status == CartonStatus.RESOLVED
    # invalid stamp -> error, event unchanged
    r = client.post("/review/2/resolve", data={"decision": "stamp", "stamp": "Z99"})
    assert "مهر Z99 در این شیفت روی این نقاله فعال نیست" in r.text
    assert repo.get_carton_event(2).status == CartonStatus.PENDING_REVIEW
    r = client.post("/review/2/resolve", data={"decision": "not_carton"})
    assert "صف بررسی خالی است" in r.text
    r = client.post("/review/2/reopen")
    assert repo.get_carton_event(2).status == CartonStatus.PENDING_REVIEW
    r = client.get("/review/card?after=2")
    assert r.status_code == 200
