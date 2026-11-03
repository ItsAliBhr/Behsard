"""Offline pipeline: video file(s) -> tracked cartons -> recognition -> DB.

One call processes the videos of one or more cameras recorded from the same
start time. Sightings from the two side cameras of a conveyor are merged into
one carton event, the recognizer chooses among the stamps active on that
conveyor at that time, and a ``CartonEvent`` (+ ``RecognitionResult`` for
audit) is written. Unrecognised cartons go to the manual review queue.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from behsard.core.models import CartonEvent, CartonStatus, RecognitionResult
from behsard.core.repository import Repository
from behsard.core.services import ShiftService
from behsard.recognition.base import Recognizer, Result
from behsard.tracking.tracker import CartonSighting, CartonTracker, TrackerParams, pair_sightings


@dataclass
class CameraInput:
    video: Path
    camera_id: str
    conveyor_id: int
    side: str  # "L" / "R"


@dataclass
class PipelineResult:
    events: list[CartonEvent] = field(default_factory=list)
    sightings: dict[str, int] = field(default_factory=dict)  # per camera

    def summary(self) -> dict[str, object]:
        counts: Counter[str] = Counter()
        pending = 0
        for ev in self.events:
            code = ev.meta.get("stamp_code")
            if ev.status == CartonStatus.RECOGNIZED and isinstance(code, str):
                counts[code] += 1
            else:
                pending += 1
        return {
            "cartons": len(self.events),
            "recognized": sum(counts.values()),
            "pending_review": pending,
            "per_stamp": dict(sorted(counts.items())),
            "sightings": self.sightings,
        }


def iter_frames(
    video: Path, start: datetime, process_fps: float
) -> Iterator[tuple[np.ndarray, datetime]]:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise FileNotFoundError(f"cannot open video {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 10.0
    step = max(1, int(round(fps / process_fps))) if process_fps > 0 else 1
    i = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if i % step == 0:
                yield frame, start + timedelta(seconds=i / fps)
            i += 1
    finally:
        cap.release()


def track_video(
    inp: CameraInput, start: datetime, params: TrackerParams, process_fps: float
) -> list[CartonSighting]:
    tracker = CartonTracker(inp.camera_id, params)
    out: list[CartonSighting] = []
    for frame, ts in iter_frames(inp.video, start, process_fps):
        out.extend(tracker.update(frame, ts))
    for s in out:
        s.meta["side"] = inp.side
    return out


class Pipeline:
    def __init__(
        self,
        repo: Repository,
        recognizer: Recognizer,
        media_dir: str | Path,
        params: TrackerParams | None = None,
        process_fps: float = 10.0,
        pair_window_s: float = 1.5,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.repo = repo
        self.recognizer = recognizer
        self.media_dir = Path(media_dir)
        self.params = params or TrackerParams()
        self.process_fps = process_fps
        self.pair_window_s = pair_window_s
        self.shifts = ShiftService(repo)
        self.log = log or (lambda _msg: None)

    def run(self, inputs: list[CameraInput], start: datetime) -> PipelineResult:
        result = PipelineResult()
        by_conveyor: dict[int, dict[str, list[CartonSighting]]] = {}
        for inp in inputs:
            sightings = track_video(inp, start, self.params, self.process_fps)
            result.sightings[inp.camera_id] = len(sightings)
            self.log(f"{inp.camera_id}: {len(sightings)} carton sightings")
            by_conveyor.setdefault(inp.conveyor_id, {}).setdefault(inp.side, []).extend(sightings)
        for conveyor_id, sides in sorted(by_conveyor.items()):
            pairs = pair_sightings(sides.get("L", []), sides.get("R", []), self.pair_window_s)
            for left, right in pairs:
                result.events.append(self.process_carton(conveyor_id, left, right))
        return result

    def process_carton(
        self, conveyor_id: int, left: CartonSighting | None, right: CartonSighting | None
    ) -> CartonEvent:
        seen = [s for s in (left, right) if s is not None]
        ts = min(s.ts for s in seen)
        images = [f for s in seen for f in s.frames]
        full = [f for s in seen for f in (s.full_frames or s.frames)]
        sources = [(s.camera_id, s.meta.get("side")) for s in seen for _ in s.frames]
        active = self.shifts.active_stamps(conveyor_id, ts)
        shift = self.shifts.shift_at(ts)
        codes = [a.code for a in active]
        res = (
            self.recognizer.recognize(images, codes)
            if images and codes
            else Result(stamp=None, confidence=0.0)
        )
        stamp_id = next((a.stamp_id for a in active if a.code == res.stamp), None)
        status = CartonStatus.RECOGNIZED if stamp_id is not None else CartonStatus.PENDING_REVIEW
        ev = CartonEvent(
            conveyor_id=conveyor_id,
            ts=ts.replace(microsecond=0),
            status=status,
            shift_id=shift.id if shift else None,
            stamp_id=stamp_id,
            confidence=res.confidence,
            meta={
                "stamp_code": res.stamp,
                "best_guess": res.best_guess,
                "raw_text": res.raw_text,
                "scores": res.scores,
                "cameras": [s.camera_id for s in seen],
                "sides": [s.meta.get("side") for s in seen],
                "n_frames": len(images),
                "no_active_stamps": not codes,
            },
        )
        self.repo.add_carton_event(ev)
        assert ev.id is not None
        ev.frame_path, ev.crop_path = self._save_media(ev, full, res, images)
        self.repo.update_carton_event(ev)
        cam = sources[res.image_index][0] if res.image_index is not None and sources else None
        self.repo.add_recognition_result(
            RecognitionResult(
                carton_event_id=ev.id,
                recognizer=self.recognizer.name,
                stamp_code=res.stamp,
                confidence=res.confidence,
                raw_text=res.raw_text,
                camera_id=cam,
                crop_path=ev.crop_path,
                frame_path=ev.frame_path,
            )
        )
        return ev

    def _save_media(
        self,
        ev: CartonEvent,
        images: list[np.ndarray],
        res: Result,
        carton_images: list[np.ndarray] | None = None,
    ) -> tuple[str | None, str | None]:
        """Save full frames (context), carton crops (recognizer input) and
        the stamp crop. ``images`` and ``carton_images`` are index-aligned."""
        if not images:
            return None, None
        d = self.media_dir / ev.ts.strftime("%Y%m%d") / f"conveyor{ev.conveyor_id}"
        d.mkdir(parents=True, exist_ok=True)
        idx = res.image_index if res.image_index is not None else 0
        frame_path = d / f"{ev.id:07d}_frame.jpg"
        cv2.imwrite(str(frame_path), images[idx], [cv2.IMWRITE_JPEG_QUALITY, 85])
        # keep every candidate frame too: the reviewer may need another view
        for i, img in enumerate(images):
            if i != idx:
                cv2.imwrite(str(d / f"{ev.id:07d}_frame{i}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        # exactly what the recognizer saw: replayable as labeled data
        for i, img in enumerate(carton_images or []):
            cv2.imwrite(str(d / f"{ev.id:07d}_carton{i}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        crop_path = None
        if res.crop is not None and res.crop.size:
            crop_path = d / f"{ev.id:07d}_crop.png"
            cv2.imwrite(str(crop_path), res.crop)
        # stored relative to the media dir so the DB survives a move
        rel = lambda p: p.relative_to(self.media_dir).as_posix()  # noqa: E731
        return rel(frame_path), rel(crop_path) if crop_path else None
