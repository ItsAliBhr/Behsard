"""Domain entities.

Timestamps are naive local-time ``datetime`` objects (site time). The whole
system runs on one site in one time zone, so we avoid tz bookkeeping.

The identification method (stamp today, maybe a printed label later) only
shows up as ``Stamp.code`` (an opaque worker-token string) and
``RecognitionResult.recognizer``; nothing else depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class CartonStatus(StrEnum):
    RECOGNIZED = "recognized"  # auto-accepted by a recognizer
    PENDING_REVIEW = "pending_review"  # waiting in the manual review queue
    RESOLVED = "resolved"  # stamp chosen by the supervisor
    NO_STAMP = "no_stamp"  # supervisor: carton without readable stamp
    NOT_CARTON = "not_carton"  # supervisor: false detection

    @property
    def counts(self) -> bool:
        """Whether a carton in this state is credited to a worker."""
        return self in (CartonStatus.RECOGNIZED, CartonStatus.RESOLVED)


class ReviewDecision(StrEnum):
    STAMP = "stamp"
    NO_STAMP = "no_stamp"
    NOT_CARTON = "not_carton"


@dataclass
class Worker:
    name: str
    personnel_code: str | None = None
    active: bool = True
    id: int | None = None


@dataclass
class Stamp:
    code: str  # e.g. "A12"
    active: bool = True
    id: int | None = None


@dataclass
class Conveyor:
    id: int
    name: str
    coefficient: float = 1.0


@dataclass
class Camera:
    id: str
    conveyor_id: int
    side: str  # "L" or "R"
    host: str | None = None


@dataclass
class Shift:
    name: str
    start_at: datetime
    end_at: datetime | None = None
    id: int | None = None

    @property
    def is_open(self) -> bool:
        return self.end_at is None

    def contains(self, ts: datetime) -> bool:
        return self.start_at <= ts and (self.end_at is None or ts < self.end_at)


@dataclass
class ShiftAssignment:
    shift_id: int
    worker_id: int
    stamp_id: int
    conveyor_id: int
    id: int | None = None


@dataclass
class CartonEvent:
    conveyor_id: int
    ts: datetime
    status: CartonStatus
    shift_id: int | None = None
    stamp_id: int | None = None
    confidence: float = 0.0
    frame_path: str | None = None  # best full frame
    crop_path: str | None = None  # best stamp crop
    meta: dict[str, object] = field(default_factory=dict)  # tracking info
    mode: str = "prod"  # prod | test (test mode) | replay (recorded video)
    id: int | None = None


@dataclass
class RecognitionResult:
    carton_event_id: int
    recognizer: str
    stamp_code: str | None
    confidence: float
    raw_text: str = ""
    camera_id: str | None = None
    crop_path: str | None = None
    frame_path: str | None = None
    created_at: datetime | None = None
    id: int | None = None


@dataclass
class ManualReview:
    carton_event_id: int
    decision: ReviewDecision
    stamp_id: int | None = None
    reviewer: str | None = None
    created_at: datetime | None = None
    id: int | None = None
