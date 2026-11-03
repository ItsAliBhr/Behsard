"""Manual review of unrecognised cartons.

Every supervisor decision is stored as a ``ManualReview`` row and the event
gets its final status, so the decisions double as labeled data
(:func:`export_labels` writes them in the eval-harness format).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from behsard.core.models import CartonEvent, CartonStatus, ManualReview, ReviewDecision
from behsard.core.repository import Repository
from behsard.core.services import DomainError, ShiftService


@dataclass
class Candidate:
    key: int | None  # number key shown in the UI (1..9) or None
    code: str
    stamp_id: int
    worker_name: str
    score: float | None  # recognizer score, if any


class ReviewService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.shifts = ShiftService(repo)

    # ---- queue ---------------------------------------------------------
    def queue(self, limit: int = 50, conveyor_id: int | None = None) -> list[CartonEvent]:
        events = self.repo.list_carton_events(CartonStatus.PENDING_REVIEW, limit=10_000)
        if conveyor_id is not None:
            events = [e for e in events if e.conveyor_id == conveyor_id]
        return events[:limit]

    def pending_count(self) -> int:
        return self.repo.count_by_status().get(str(CartonStatus.PENDING_REVIEW), 0)

    def next_pending(self, after_id: int | None = None) -> CartonEvent | None:
        q = self.queue(limit=10_000)
        if after_id is not None:
            later = [e for e in q if e.id is not None and e.id > after_id]
            if later:
                return later[0]
        return q[0] if q else None

    # ---- candidates ----------------------------------------------------
    def candidates(self, ev: CartonEvent) -> list[Candidate]:
        """Stamps active on the event's conveyor at its time, likeliest first."""
        active = self.shifts.active_stamps(ev.conveyor_id, ev.ts)
        scores = ev.meta.get("scores") or {}
        if not isinstance(scores, dict):
            scores = {}
        workers = {w.id: w.name for w in self.repo.list_workers()}

        def order(a: object) -> tuple[float, str]:
            code = a.code  # type: ignore[attr-defined]
            return (-float(scores.get(code, -1.0)), code)

        out = []
        for i, a in enumerate(sorted(active, key=order)):
            out.append(
                Candidate(
                    key=i + 1 if i < 9 else None,
                    code=a.code,
                    stamp_id=a.stamp_id,
                    worker_name=workers.get(a.worker_id, "?"),
                    score=float(scores[a.code]) if a.code in scores else None,
                )
            )
        return out

    # ---- decisions -----------------------------------------------------
    def resolve(
        self,
        event_id: int,
        decision: ReviewDecision,
        stamp_code: str | None = None,
        reviewer: str | None = None,
    ) -> CartonEvent:
        ev = self.repo.get_carton_event(event_id)
        if ev is None:
            raise DomainError(f"unknown carton event {event_id}")
        stamp_id = None
        if decision == ReviewDecision.STAMP:
            code = (stamp_code or "").strip().upper()
            match = [c for c in self.candidates(ev) if c.code == code]
            if not match:
                raise DomainError(f"stamp {code!r} is not active on this conveyor/shift")
            stamp_id = match[0].stamp_id
            ev.status = CartonStatus.RESOLVED
        elif decision == ReviewDecision.NO_STAMP:
            ev.status = CartonStatus.NO_STAMP
        else:
            ev.status = CartonStatus.NOT_CARTON
        ev.stamp_id = stamp_id
        ev.meta = {**ev.meta, "reviewed_at": datetime.now().isoformat(timespec="seconds")}
        self.repo.update_carton_event(ev)
        self.repo.add_manual_review(ManualReview(event_id, decision, stamp_id, reviewer))
        return ev


def reopen(repo: Repository, event_id: int) -> CartonEvent:
    """Undo a review decision: put the carton back into the queue."""
    ev = repo.get_carton_event(event_id)
    if ev is None:
        raise DomainError(f"unknown carton event {event_id}")
    ev.status = CartonStatus.PENDING_REVIEW
    ev.stamp_id = None
    repo.update_carton_event(ev)
    return ev


def export_labels(repo: Repository, media_dir: str | Path, out_dir: str | Path) -> int:
    """Write reviewed cartons as an eval dataset (images + labels.json).

    ``stamp`` is the supervisor's choice (``null`` for "no stamp"); "not a
    carton" decisions are skipped. ``active`` is the stamp set the recognizer
    had to choose from, so the harness can replay the exact decision.
    """
    media = Path(media_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    svc = ShiftService(repo)
    latest: dict[int, ManualReview] = {}
    for r in repo.list_manual_reviews():
        latest[r.carton_event_id] = r  # last decision wins
    labels = []
    for event_id, r in sorted(latest.items()):
        if r.decision == ReviewDecision.NOT_CARTON:
            continue
        ev = repo.get_carton_event(event_id)
        if ev is None or not ev.frame_path:
            continue
        folder = (media / ev.frame_path).parent
        frames = sorted(folder.glob(f"{event_id:07d}_carton*.jpg")) or sorted(
            folder.glob(f"{event_id:07d}_frame*.jpg")
        )
        files = []
        for f in frames:
            dst = out / f.name
            shutil.copy(f, dst)
            files.append(dst.name)
        if not files:
            continue
        stamp = repo.get_stamp(r.stamp_id) if r.stamp_id else None
        labels.append(
            {
                "file": files,
                "stamp": stamp.code if stamp else None,
                "active": [a.code for a in svc.active_stamps(ev.conveyor_id, ev.ts)],
                "meta": {
                    "event_id": event_id,
                    "conveyor": ev.conveyor_id,
                    "ts": ev.ts.isoformat(),
                    "auto_guess": ev.meta.get("best_guess"),
                    "source": "manual_review",
                },
            }
        )
    (out / "labels.json").write_text(json.dumps(labels, indent=1, ensure_ascii=False))
    return len(labels)
