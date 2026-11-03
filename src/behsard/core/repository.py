"""Repository layer.

All SQL lives here. Callers depend on the ``Repository`` protocol so the
storage can later move to Postgres/Oracle by adding another implementation.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Protocol

from behsard.core.models import (
    Camera,
    CartonEvent,
    CartonStatus,
    Conveyor,
    ManualReview,
    RecognitionResult,
    ReviewDecision,
    Shift,
    ShiftAssignment,
    Stamp,
    Worker,
)


def _ts(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt is not None else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class Repository(Protocol):
    """Storage interface used by services, pipeline, review and reports."""

    # workers / stamps / conveyors / cameras
    def add_worker(self, worker: Worker) -> Worker: ...
    def get_worker(self, worker_id: int) -> Worker | None: ...
    def list_workers(self, active_only: bool = False) -> list[Worker]: ...
    def add_stamp(self, stamp: Stamp) -> Stamp: ...
    def get_stamp(self, stamp_id: int) -> Stamp | None: ...
    def get_stamp_by_code(self, code: str) -> Stamp | None: ...
    def list_stamps(self, active_only: bool = False) -> list[Stamp]: ...
    def upsert_conveyor(self, conveyor: Conveyor) -> Conveyor: ...
    def get_conveyor(self, conveyor_id: int) -> Conveyor | None: ...
    def list_conveyors(self) -> list[Conveyor]: ...
    def upsert_camera(self, camera: Camera) -> Camera: ...
    def get_camera(self, camera_id: str) -> Camera | None: ...
    def list_cameras(self, conveyor_id: int | None = None) -> list[Camera]: ...
    # shifts
    def add_shift(self, shift: Shift) -> Shift: ...
    def update_shift(self, shift: Shift) -> None: ...
    def get_shift(self, shift_id: int) -> Shift | None: ...
    def list_shifts(self, limit: int = 100) -> list[Shift]: ...
    def shifts_at(self, ts: datetime) -> list[Shift]: ...
    def add_assignment(self, a: ShiftAssignment) -> ShiftAssignment: ...
    def delete_assignment(self, assignment_id: int) -> None: ...
    def list_assignments(self, shift_id: int) -> list[ShiftAssignment]: ...
    # cartons
    def add_carton_event(self, ev: CartonEvent) -> CartonEvent: ...
    def update_carton_event(self, ev: CartonEvent) -> None: ...
    def get_carton_event(self, event_id: int) -> CartonEvent | None: ...
    def list_carton_events(
        self,
        status: CartonStatus | None = None,
        shift_id: int | None = None,
        limit: int = 1000,
        mode: str | None = None,
        after_id: int | None = None,
    ) -> list[CartonEvent]: ...
    def add_recognition_result(self, r: RecognitionResult) -> RecognitionResult: ...
    def list_recognition_results(self, event_id: int) -> list[RecognitionResult]: ...
    def add_manual_review(self, r: ManualReview) -> ManualReview: ...
    def list_manual_reviews(self) -> list[ManualReview]: ...
    def count_by_status(self, mode: str = "prod") -> dict[str, int]: ...
    def counted_rows(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...
    def close(self) -> None: ...


class SqliteRepository:
    """SQLite implementation of :class:`Repository`."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastAPI may call from a worker thread;
        # we serialise access per request ourselves (single supervisor UI).
        # One connection shared by the web UI and the live capture threads;
        # every statement runs under this lock.
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        schema = resources.files("behsard.core").joinpath("schema.sql").read_text()
        self.conn.executescript(schema)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Small additive migrations for databases created by older versions."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(carton_event)")}
        if "mode" not in cols:
            self.conn.execute("ALTER TABLE carton_event ADD COLUMN mode TEXT NOT NULL DEFAULT 'prod'")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_carton_event_mode ON carton_event(mode)")

    def close(self) -> None:
        self.conn.close()

    # ---- helpers -------------------------------------------------------
    def _insert(self, sql: str, params: Iterable[Any]) -> int:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            assert cur.lastrowid is not None
            return cur.lastrowid

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.lock:
            self.conn.execute(sql, tuple(params))
            self.conn.commit()

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.conn.execute(sql, tuple(params)).fetchall())

    # ---- workers -------------------------------------------------------
    @staticmethod
    def _worker(r: sqlite3.Row) -> Worker:
        return Worker(r["name"], r["personnel_code"], bool(r["active"]), r["id"])

    def add_worker(self, worker: Worker) -> Worker:
        worker.id = self._insert(
            "INSERT INTO worker (name, personnel_code, active) VALUES (?, ?, ?)",
            (worker.name, worker.personnel_code, int(worker.active)),
        )
        return worker

    def get_worker(self, worker_id: int) -> Worker | None:
        rows = self._rows("SELECT * FROM worker WHERE id = ?", (worker_id,))
        return self._worker(rows[0]) if rows else None

    def list_workers(self, active_only: bool = False) -> list[Worker]:
        sql = "SELECT * FROM worker" + (" WHERE active = 1" if active_only else "")
        return [self._worker(r) for r in self._rows(sql + " ORDER BY name")]

    # ---- stamps --------------------------------------------------------
    @staticmethod
    def _stamp(r: sqlite3.Row) -> Stamp:
        return Stamp(r["code"], bool(r["active"]), r["id"])

    def add_stamp(self, stamp: Stamp) -> Stamp:
        stamp.code = stamp.code.strip().upper()
        stamp.id = self._insert(
            "INSERT INTO stamp (code, active) VALUES (?, ?)",
            (stamp.code, int(stamp.active)),
        )
        return stamp

    def get_stamp(self, stamp_id: int) -> Stamp | None:
        rows = self._rows("SELECT * FROM stamp WHERE id = ?", (stamp_id,))
        return self._stamp(rows[0]) if rows else None

    def get_stamp_by_code(self, code: str) -> Stamp | None:
        rows = self._rows("SELECT * FROM stamp WHERE code = ?", (code.strip().upper(),))
        return self._stamp(rows[0]) if rows else None

    def list_stamps(self, active_only: bool = False) -> list[Stamp]:
        sql = "SELECT * FROM stamp" + (" WHERE active = 1" if active_only else "")
        return [self._stamp(r) for r in self._rows(sql + " ORDER BY code")]

    # ---- conveyors / cameras ------------------------------------------
    def upsert_conveyor(self, conveyor: Conveyor) -> Conveyor:
        self._exec(
            "INSERT INTO conveyor (id, name, coefficient) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
            "coefficient = excluded.coefficient",
            (conveyor.id, conveyor.name, conveyor.coefficient),
        )
        return conveyor

    def get_conveyor(self, conveyor_id: int) -> Conveyor | None:
        rows = self._rows("SELECT * FROM conveyor WHERE id = ?", (conveyor_id,))
        if not rows:
            return None
        r = rows[0]
        return Conveyor(r["id"], r["name"], r["coefficient"])

    def list_conveyors(self) -> list[Conveyor]:
        return [
            Conveyor(r["id"], r["name"], r["coefficient"])
            for r in self._rows("SELECT * FROM conveyor ORDER BY id")
        ]

    def upsert_camera(self, camera: Camera) -> Camera:
        self._exec(
            "INSERT INTO camera (id, conveyor_id, side, host) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET conveyor_id = excluded.conveyor_id, "
            "side = excluded.side, host = excluded.host",
            (camera.id, camera.conveyor_id, camera.side, camera.host),
        )
        return camera

    def get_camera(self, camera_id: str) -> Camera | None:
        rows = self._rows("SELECT * FROM camera WHERE id = ?", (camera_id,))
        if not rows:
            return None
        r = rows[0]
        return Camera(r["id"], r["conveyor_id"], r["side"], r["host"])

    def list_cameras(self, conveyor_id: int | None = None) -> list[Camera]:
        if conveyor_id is None:
            rows = self._rows("SELECT * FROM camera ORDER BY id")
        else:
            rows = self._rows(
                "SELECT * FROM camera WHERE conveyor_id = ? ORDER BY id", (conveyor_id,)
            )
        return [Camera(r["id"], r["conveyor_id"], r["side"], r["host"]) for r in rows]

    # ---- shifts --------------------------------------------------------
    @staticmethod
    def _shift(r: sqlite3.Row) -> Shift:
        start = _dt(r["start_at"])
        assert start is not None
        return Shift(r["name"], start, _dt(r["end_at"]), r["id"])

    def add_shift(self, shift: Shift) -> Shift:
        shift.id = self._insert(
            "INSERT INTO shift (name, start_at, end_at) VALUES (?, ?, ?)",
            (shift.name, _ts(shift.start_at), _ts(shift.end_at)),
        )
        return shift

    def update_shift(self, shift: Shift) -> None:
        self._exec(
            "UPDATE shift SET name = ?, start_at = ?, end_at = ? WHERE id = ?",
            (shift.name, _ts(shift.start_at), _ts(shift.end_at), shift.id),
        )

    def get_shift(self, shift_id: int) -> Shift | None:
        rows = self._rows("SELECT * FROM shift WHERE id = ?", (shift_id,))
        return self._shift(rows[0]) if rows else None

    def list_shifts(self, limit: int = 100) -> list[Shift]:
        rows = self._rows("SELECT * FROM shift ORDER BY start_at DESC LIMIT ?", (limit,))
        return [self._shift(r) for r in rows]

    def shifts_at(self, ts: datetime) -> list[Shift]:
        t = _ts(ts)
        rows = self._rows(
            "SELECT * FROM shift WHERE start_at <= ? AND (end_at IS NULL OR end_at > ?) "
            "ORDER BY start_at DESC",
            (t, t),
        )
        return [self._shift(r) for r in rows]

    @staticmethod
    def _assignment(r: sqlite3.Row) -> ShiftAssignment:
        return ShiftAssignment(
            r["shift_id"], r["worker_id"], r["stamp_id"], r["conveyor_id"], r["id"]
        )

    def add_assignment(self, a: ShiftAssignment) -> ShiftAssignment:
        a.id = self._insert(
            "INSERT INTO shift_assignment (shift_id, worker_id, stamp_id, conveyor_id) "
            "VALUES (?, ?, ?, ?)",
            (a.shift_id, a.worker_id, a.stamp_id, a.conveyor_id),
        )
        return a

    def delete_assignment(self, assignment_id: int) -> None:
        self._exec("DELETE FROM shift_assignment WHERE id = ?", (assignment_id,))

    def list_assignments(self, shift_id: int) -> list[ShiftAssignment]:
        rows = self._rows(
            "SELECT * FROM shift_assignment WHERE shift_id = ? ORDER BY conveyor_id, id",
            (shift_id,),
        )
        return [self._assignment(r) for r in rows]

    # ---- carton events -------------------------------------------------
    @staticmethod
    def _event(r: sqlite3.Row) -> CartonEvent:
        ts = _dt(r["ts"])
        assert ts is not None
        return CartonEvent(
            conveyor_id=r["conveyor_id"],
            ts=ts,
            status=CartonStatus(r["status"]),
            shift_id=r["shift_id"],
            stamp_id=r["stamp_id"],
            confidence=r["confidence"],
            frame_path=r["frame_path"],
            crop_path=r["crop_path"],
            meta=json.loads(r["meta"] or "{}"),
            mode=r["mode"],
            id=r["id"],
        )

    def add_carton_event(self, ev: CartonEvent) -> CartonEvent:
        ev.id = self._insert(
            "INSERT INTO carton_event (conveyor_id, shift_id, ts, status, stamp_id, "
            "confidence, frame_path, crop_path, meta, mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ev.conveyor_id,
                ev.shift_id,
                _ts(ev.ts),
                str(ev.status),
                ev.stamp_id,
                ev.confidence,
                ev.frame_path,
                ev.crop_path,
                json.dumps(ev.meta, ensure_ascii=False),
                ev.mode,
            ),
        )
        return ev

    def update_carton_event(self, ev: CartonEvent) -> None:
        self._exec(
            "UPDATE carton_event SET conveyor_id = ?, shift_id = ?, ts = ?, status = ?, "
            "stamp_id = ?, confidence = ?, frame_path = ?, crop_path = ?, meta = ?, mode = ? "
            "WHERE id = ?",
            (
                ev.conveyor_id,
                ev.shift_id,
                _ts(ev.ts),
                str(ev.status),
                ev.stamp_id,
                ev.confidence,
                ev.frame_path,
                ev.crop_path,
                json.dumps(ev.meta, ensure_ascii=False),
                ev.mode,
                ev.id,
            ),
        )

    def get_carton_event(self, event_id: int) -> CartonEvent | None:
        rows = self._rows("SELECT * FROM carton_event WHERE id = ?", (event_id,))
        return self._event(rows[0]) if rows else None

    def list_carton_events(
        self,
        status: CartonStatus | None = None,
        shift_id: int | None = None,
        limit: int = 1000,
        mode: str | None = None,
        after_id: int | None = None,
    ) -> list[CartonEvent]:
        sql = "SELECT * FROM carton_event WHERE 1 = 1"
        params: list[Any] = []
        if mode is not None:
            sql += " AND mode = ?"
            params.append(mode)
        if after_id is not None:
            sql += " AND id > ?"
            params.append(after_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(str(status))
        if shift_id is not None:
            sql += " AND shift_id = ?"
            params.append(shift_id)
        sql += " ORDER BY ts, id LIMIT ?"
        params.append(limit)
        return [self._event(r) for r in self._rows(sql, params)]

    def count_by_status(self, mode: str = "prod") -> dict[str, int]:
        rows = self._rows(
            "SELECT status, COUNT(*) AS n FROM carton_event WHERE mode = ? GROUP BY status", (mode,)
        )
        return {r["status"]: r["n"] for r in rows}

    def add_recognition_result(self, r: RecognitionResult) -> RecognitionResult:
        r.created_at = r.created_at or datetime.now()
        r.id = self._insert(
            "INSERT INTO recognition_result (carton_event_id, recognizer, stamp_code, "
            "confidence, raw_text, camera_id, crop_path, frame_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.carton_event_id,
                r.recognizer,
                r.stamp_code,
                r.confidence,
                r.raw_text,
                r.camera_id,
                r.crop_path,
                r.frame_path,
                _ts(r.created_at),
            ),
        )
        return r

    def list_recognition_results(self, event_id: int) -> list[RecognitionResult]:
        rows = self._rows(
            "SELECT * FROM recognition_result WHERE carton_event_id = ? ORDER BY id",
            (event_id,),
        )
        return [
            RecognitionResult(
                carton_event_id=r["carton_event_id"],
                recognizer=r["recognizer"],
                stamp_code=r["stamp_code"],
                confidence=r["confidence"],
                raw_text=r["raw_text"],
                camera_id=r["camera_id"],
                crop_path=r["crop_path"],
                frame_path=r["frame_path"],
                created_at=_dt(r["created_at"]),
                id=r["id"],
            )
            for r in rows
        ]

    def add_manual_review(self, r: ManualReview) -> ManualReview:
        r.created_at = r.created_at or datetime.now()
        r.id = self._insert(
            "INSERT INTO manual_review (carton_event_id, decision, stamp_id, reviewer, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (r.carton_event_id, str(r.decision), r.stamp_id, r.reviewer, _ts(r.created_at)),
        )
        return r

    def list_manual_reviews(self) -> list[ManualReview]:
        return [
            ManualReview(
                carton_event_id=r["carton_event_id"],
                decision=ReviewDecision(r["decision"]),
                stamp_id=r["stamp_id"],
                reviewer=r["reviewer"],
                created_at=_dt(r["created_at"]),
                id=r["id"],
            )
            for r in self._rows("SELECT * FROM manual_review ORDER BY id")
        ]

    # ---- reporting -----------------------------------------------------
    def counted_rows(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """One row per credited carton in ``[start, end)``.

        Joined with the shift assignment so each carton carries its worker.
        """
        rows = self._rows(
            """
            SELECT e.id AS event_id, e.ts, e.conveyor_id, c.name AS conveyor_name,
                   c.coefficient, e.shift_id, s.name AS shift_name,
                   s.start_at AS shift_start, st.code AS stamp_code,
                   w.id AS worker_id, w.name AS worker_name
            FROM carton_event e
            JOIN conveyor c ON c.id = e.conveyor_id
            JOIN stamp st ON st.id = e.stamp_id
            LEFT JOIN shift s ON s.id = e.shift_id
            LEFT JOIN shift_assignment a
                   ON a.shift_id = e.shift_id AND a.stamp_id = e.stamp_id
            LEFT JOIN worker w ON w.id = a.worker_id
            WHERE e.status IN (?, ?) AND e.ts >= ? AND e.ts < ? AND e.mode = 'prod'
            ORDER BY e.ts
            """,
            (
                str(CartonStatus.RECOGNIZED),
                str(CartonStatus.RESOLVED),
                _ts(start),
                _ts(end),
            ),
        )
        return [dict(r) for r in rows]


def open_repository(path: str | Path = ":memory:") -> SqliteRepository:
    return SqliteRepository(path)


def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day)
    return start, start + timedelta(days=1)
