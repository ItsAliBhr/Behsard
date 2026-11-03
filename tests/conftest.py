from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from behsard.core.models import Conveyor
from behsard.core.repository import SqliteRepository
from behsard.core.services import ShiftService

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def repo() -> SqliteRepository:
    r = SqliteRepository(":memory:")
    for i in (1, 2):
        r.upsert_conveyor(Conveyor(i, f"conveyor {i}", 1.0))
    return r


@pytest.fixture
def shift_setup(repo: SqliteRepository) -> dict[str, object]:
    """One open shift with two stamps on conveyor 1 and one on conveyor 2."""
    svc = ShiftService(repo)
    shift = svc.open_shift("morning", datetime(2026, 11, 1, 6, 0))
    assert shift.id is not None
    w1 = svc.ensure_worker("Ali")
    w2 = svc.ensure_worker("Reza")
    w3 = svc.ensure_worker("Sara")
    assert w1.id and w2.id and w3.id
    svc.assign(shift.id, w1.id, "A12", 1)
    svc.assign(shift.id, w2.id, "B07", 1)
    svc.assign(shift.id, w3.id, "C15", 2)
    return {"svc": svc, "shift": shift, "workers": [w1, w2, w3]}
