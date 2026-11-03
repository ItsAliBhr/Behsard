"""Shift management and active-stamp lookup."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from behsard.config import Config
from behsard.core.models import Camera, Conveyor, Shift, ShiftAssignment, Stamp, Worker
from behsard.core.repository import Repository

STAMP_CODE_RE = re.compile(r"^[A-Z][0-9]{2}$")


class DomainError(ValueError):
    """Business-rule violation (shown to the user)."""


def normalize_stamp_code(code: str) -> str:
    c = code.strip().upper()
    if not STAMP_CODE_RE.match(c):
        raise DomainError(f"invalid stamp code {code!r}: expected letter + 2 digits")
    return c


@dataclass(frozen=True)
class ActiveStamp:
    """A stamp in use on a conveyor during a shift."""

    code: str
    stamp_id: int
    worker_id: int
    shift_id: int
    conveyor_id: int


class ShiftService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    # ---- shifts --------------------------------------------------------
    def open_shift(self, name: str, start_at: datetime | None = None) -> Shift:
        start = start_at or datetime.now().replace(microsecond=0)
        return self.repo.add_shift(Shift(name=name, start_at=start))

    def close_shift(self, shift_id: int, end_at: datetime | None = None) -> Shift:
        shift = self._shift(shift_id)
        if not shift.is_open:
            raise DomainError("shift already closed")
        end = end_at or datetime.now().replace(microsecond=0)
        if end <= shift.start_at:
            raise DomainError("shift end must be after its start")
        shift.end_at = end
        self.repo.update_shift(shift)
        return shift

    def open_shifts(self) -> list[Shift]:
        return [s for s in self.repo.list_shifts(limit=50) if s.is_open]

    # ---- assignments ---------------------------------------------------
    def ensure_worker(self, name: str, personnel_code: str | None = None) -> Worker:
        for w in self.repo.list_workers():
            if (personnel_code and w.personnel_code == personnel_code) or (
                not personnel_code and w.name == name
            ):
                return w
        return self.repo.add_worker(Worker(name=name, personnel_code=personnel_code))

    def ensure_stamp(self, code: str) -> Stamp:
        c = normalize_stamp_code(code)
        return self.repo.get_stamp_by_code(c) or self.repo.add_stamp(Stamp(code=c))

    def assign(
        self, shift_id: int, worker_id: int, stamp_code: str, conveyor_id: int
    ) -> ShiftAssignment:
        """Assign worker <-> stamp <-> conveyor in a shift.

        A stamp and a worker can each appear at most once per shift.
        """
        shift = self._shift(shift_id)
        if not shift.is_open:
            raise DomainError("cannot assign in a closed shift")
        if self.repo.get_worker(worker_id) is None:
            raise DomainError(f"unknown worker {worker_id}")
        if self.repo.get_conveyor(conveyor_id) is None:
            raise DomainError(f"unknown conveyor {conveyor_id}")
        stamp = self.ensure_stamp(stamp_code)
        assert stamp.id is not None
        for a in self.repo.list_assignments(shift_id):
            if a.stamp_id == stamp.id:
                raise DomainError(f"stamp {stamp.code} already assigned in this shift")
            if a.worker_id == worker_id:
                raise DomainError("worker already assigned in this shift")
        return self.repo.add_assignment(
            ShiftAssignment(shift_id, worker_id, stamp.id, conveyor_id)
        )

    def unassign(self, assignment_id: int) -> None:
        self.repo.delete_assignment(assignment_id)

    def copy_assignments(self, from_shift_id: int, to_shift_id: int) -> int:
        """Copy assignments from a previous shift (quick re-setup)."""
        existing = self.repo.list_assignments(to_shift_id)
        used_stamps = {a.stamp_id for a in existing}
        used_workers = {a.worker_id for a in existing}
        n = 0
        for a in self.repo.list_assignments(from_shift_id):
            if a.stamp_id in used_stamps or a.worker_id in used_workers:
                continue
            self.repo.add_assignment(
                ShiftAssignment(to_shift_id, a.worker_id, a.stamp_id, a.conveyor_id)
            )
            n += 1
        return n

    # ---- lookup --------------------------------------------------------
    def active_stamps(self, conveyor_id: int, ts: datetime) -> list[ActiveStamp]:
        """Stamps active on ``conveyor_id`` at ``ts`` (closed-set for recognition)."""
        out: list[ActiveStamp] = []
        for shift in self.repo.shifts_at(ts):
            assert shift.id is not None
            for a in self.repo.list_assignments(shift.id):
                if a.conveyor_id != conveyor_id:
                    continue
                stamp = self.repo.get_stamp(a.stamp_id)
                assert stamp is not None
                out.append(
                    ActiveStamp(stamp.code, a.stamp_id, a.worker_id, shift.id, conveyor_id)
                )
        return sorted(out, key=lambda s: s.code)

    def shift_at(self, ts: datetime) -> Shift | None:
        shifts = self.repo.shifts_at(ts)
        return shifts[0] if shifts else None

    def _shift(self, shift_id: int) -> Shift:
        shift = self.repo.get_shift(shift_id)
        if shift is None:
            raise DomainError(f"unknown shift {shift_id}")
        return shift


def sync_config(repo: Repository, cfg: Config) -> None:
    """Make conveyors and cameras in the DB match the YAML config."""
    for c in cfg.conveyors:
        repo.upsert_conveyor(
            Conveyor(int(c["id"]), str(c["name"]), float(c.get("coefficient", 1.0)))
        )
    for c in cfg.cameras:
        repo.upsert_camera(
            Camera(str(c["id"]), int(c["conveyor_id"]), str(c["side"]), c.get("host"))
        )
