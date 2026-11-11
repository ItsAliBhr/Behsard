"""Stamp-based carton counting ("stamps" mode).

Instead of finding the cartons first (seams between touching cartons, see
``tracker.py``), look for the stamps themselves: every ``interval_s`` the
recognizer reports all stamp candidates in the watch region
(``StampRecognizer.spot``); candidates are followed from frame to frame
(same place / same code, short time gap) so one physical stamp becomes one
*track*, and each track is counted once.

Why this is the default: on real footage (handheld clips and the first
fixed-camera test) carton seams were not reliably visible, while the stamps
were read. It needs no carton geometry, no pairing of the two side cameras
(a carton carries its stamp on one side only) and tolerates camera shake.
Limitation: a carton without any readable stamp produces no event.

A track is
* **recognized** as soon as it is solid: two confident reads of the same
  code, or one very confident read (emitted immediately, so a carton that
  stops in view is not delayed);
* sent to **review** when it ends with only weak evidence (one borderline
  read, or several weak reads agreeing on a code);
* dropped otherwise (print, clutter).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

import cv2
import numpy as np

from behsard.recognition.base import Result
from behsard.tracking.tracker import CartonSighting


@dataclass
class SpotterParams:
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    interval_s: float = 0.25  # time between reads
    gap_s: float = 2.0  # a moving stamp not seen for this long has left
    still_gap_s: float = 8.0  # ... but one that reappears in the same place (hidden by a hand) is the same
    max_jump: float = 0.4  # max movement between reads (fraction of ROI) for the same code
    near: float = 0.12  # within this distance a candidate joins a track whatever its code
    dup: float = 0.25  # same code this close to a stamp already seen in this read = same stamp
    weak_conf: float = 0.2  # weaker candidates are ignored
    solid_conf: float = 0.6  # one confident read this strong is enough
    pending_reads: int = 4  # weak reads needed to ask the supervisor
    pending_conf: float = 0.3  # ... with at least this mean confidence
    pending_share: float = 0.8  # ... and this share of the votes for one code (print flips between codes)
    static_diff: float = 1.5  # mean grey difference below which the view is "unchanged"
    jpeg_quality: int = 90


class _SpotLike(Protocol):
    code: str
    accepted: bool
    confidence: float
    pos: tuple[float, float]
    result: Result


class SpotFn(Protocol):
    def __call__(self, img: np.ndarray, active: Sequence[str]) -> Sequence[_SpotLike]: ...


@dataclass
class _Track:
    first_ts: datetime
    last_ts: datetime
    pos: tuple[float, float]
    votes: dict[str, float] = field(default_factory=dict)  # code -> summed confidence
    accepted: dict[str, int] = field(default_factory=dict)  # code -> confident reads
    best: dict[str, Result] = field(default_factory=dict)  # code -> best result
    best_conf: dict[str, float] = field(default_factory=dict)
    frames: dict[str, bytes] = field(default_factory=dict)  # code -> JPEG of the best frame
    reads: int = 0
    conf_sum: float = 0.0
    emitted: bool = False
    last_read: int = -1

    def winner(self) -> str:
        return max(self.votes, key=lambda c: (self.accepted.get(c, 0), self.votes[c]))

    def solid(self, solid_conf: float) -> bool:
        w = self.winner()
        n = self.accepted.get(w, 0)
        return n >= 2 or (n >= 1 and self.best_conf.get(w, 0.0) >= solid_conf)


class StampSpotter:
    """Feed frames in time order with :meth:`update`; get sightings back."""

    def __init__(
        self,
        camera_id: str,
        spot: SpotFn,
        active_codes: Callable[[datetime], Sequence[str]],
        params: SpotterParams | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.spot = spot
        self.active_codes = active_codes
        self.p = params or SpotterParams()
        self.tracks: list[_Track] = []
        self.count = 0
        self.read_index = 0
        self.last_read_ts: datetime | None = None
        self._thumb: np.ndarray | None = None
        self._last_spots: list[_SpotLike] = []
        self.last_spots_view: list[tuple[float, float, str, float, bool]] = []
        self.stats = {"reads": 0, "reused": 0, "recognized": 0, "review": 0, "no_active": 0}

    # ---- helpers -------------------------------------------------------
    def _roi(self, frame: np.ndarray) -> np.ndarray:
        H, W = frame.shape[:2]
        x, y, w, h = self.p.roi
        return frame[int(y * H) : int((y + h) * H), int(x * W) : int((x + w) * W)]

    def _encode(self, frame: np.ndarray) -> bytes:
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.p.jpeg_quality])
        assert ok
        return buf.tobytes()

    # ---- main ----------------------------------------------------------
    def update(self, frame: np.ndarray, ts: datetime) -> list[CartonSighting]:
        if self.last_read_ts is not None and (ts - self.last_read_ts).total_seconds() < self.p.interval_s - 1e-3:
            return []
        self.last_read_ts = ts
        self.read_index += 1
        active = list(self.active_codes(ts))
        if not active:
            self.stats["no_active"] += 1
            self.last_spots_view = []
            return self._close(ts)
        roi = self._roi(frame)
        thumb = cv2.resize(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), (96, 54), interpolation=cv2.INTER_AREA)
        unchanged = (
            self._thumb is not None
            and float(np.abs(thumb.astype(np.int16) - self._thumb.astype(np.int16)).mean()) < self.p.static_diff
        )
        if unchanged:
            spots = self._last_spots  # conveyor stopped: nothing new to read
            self.stats["reused"] += 1
        else:
            spots = list(self.spot(roi, active))
            self._thumb = thumb
            self._last_spots = spots
            self.stats["reads"] += 1
        spots = [s for s in spots if s.accepted or s.confidence >= self.p.weak_conf]
        self.last_spots_view = [(s.pos[0], s.pos[1], s.code, s.confidence, s.accepted) for s in spots]
        out: list[CartonSighting] = []
        jpg: bytes | None = None
        for s in sorted(spots, key=lambda s: s.confidence, reverse=True):
            tr = self._match(s, ts)
            if tr is None:
                if self._duplicate(s):
                    continue  # a second region on the same stamp (e.g. "A1" + "12")
                tr = _Track(ts, ts, s.pos)
                self.tracks.append(tr)
            tr.last_ts, tr.pos, tr.last_read = ts, s.pos, self.read_index
            tr.reads += 1
            tr.conf_sum += s.confidence
            tr.votes[s.code] = tr.votes.get(s.code, 0.0) + s.confidence
            if s.accepted:
                tr.accepted[s.code] = tr.accepted.get(s.code, 0) + 1
            if s.confidence > tr.best_conf.get(s.code, -1.0):
                tr.best_conf[s.code] = s.confidence
                tr.best[s.code] = s.result
                if jpg is None:
                    jpg = self._encode(frame)
                tr.frames[s.code] = jpg
            if not tr.emitted and tr.solid(self.p.solid_conf):
                out.append(self._sighting(tr, recognized=True))
        out.extend(self._close(ts))
        return out

    def _duplicate(self, s: _SpotLike) -> bool:
        for tr in self.tracks:
            if tr.last_read == self.read_index and s.code in tr.votes:
                if float(np.hypot(s.pos[0] - tr.pos[0], s.pos[1] - tr.pos[1])) <= self.p.dup:
                    return True
        return False

    def _match(self, s: _SpotLike, ts: datetime) -> _Track | None:
        best, bd = None, 1e9
        for tr in self.tracks:
            if tr.last_read == self.read_index:  # one candidate per track per read
                continue
            age = (ts - tr.last_ts).total_seconds()
            d = float(np.hypot(s.pos[0] - tr.pos[0], s.pos[1] - tr.pos[1]))
            same = s.code in tr.votes
            if age <= self.p.gap_s:
                ok = d <= self.p.near or (same and d <= self.p.max_jump)
            else:  # reappeared where it was (stopped carton, hand in front)
                ok = same and d <= self.p.near and age <= self.p.still_gap_s
            if ok and d < bd:
                best, bd = tr, d
        return best

    def _close(self, ts: datetime | None) -> list[CartonSighting]:
        """Decide tracks whose stamp has left (all tracks when ``ts`` is None).

        Undecided tracks are judged after ``gap_s``; every track is then kept
        (already judged) until ``still_gap_s`` so a stamp that reappears in
        the same place is not counted twice.
        """
        out, keep = [], []
        for tr in self.tracks:
            age = (ts - tr.last_ts).total_seconds() if ts is not None else 1e9
            if age <= self.p.gap_s:
                keep.append(tr)
                continue
            if not tr.emitted:
                w = tr.winner()
                weak_ok = (
                    tr.reads >= self.p.pending_reads
                    and tr.conf_sum / tr.reads >= self.p.pending_conf
                    and tr.votes[w] / sum(tr.votes.values()) >= self.p.pending_share
                )
                if tr.accepted.get(w, 0) >= 1 or weak_ok:
                    out.append(self._sighting(tr, recognized=False))
                else:
                    continue  # clutter: forget it
            if age <= self.p.still_gap_s:
                keep.append(tr)
        self.tracks = keep
        return out

    def flush(self) -> list[CartonSighting]:
        """End of stream: decide every open track."""
        return self._close(None)

    def _sighting(self, tr: _Track, recognized: bool) -> CartonSighting:
        tr.emitted = True
        w = tr.winner()
        res = tr.best[w]
        if recognized:
            res = replace(res, stamp=w, best_guess=w, image_index=0)
            self.stats["recognized"] += 1
        else:  # ask the supervisor, with our best guess
            res = replace(res, stamp=None, best_guess=w, image_index=0)
            self.stats["review"] += 1
        full = cv2.imdecode(np.frombuffer(tr.frames[w], np.uint8), cv2.IMREAD_COLOR)
        assert full is not None
        self.count += 1
        return CartonSighting(
            camera_id=self.camera_id,
            ts=tr.first_ts,
            frames=[self._roi(full).copy()],
            frame_times=[tr.last_ts],
            width_px=0.0,
            index=self.count,
            meta={"mode": "stamps", "reads": tr.reads, "votes": {k: round(v, 2) for k, v in tr.votes.items()}},
            full_frames=[full],
            result=res,
        )

    def overlay(self) -> dict[str, object]:
        return {"roi": self.p.roi, "spots": list(self.last_spots_view), "mode": "stamps"}
