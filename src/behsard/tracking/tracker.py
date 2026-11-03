"""Carton passage detection on one camera stream.

The camera looks straight at the carton faces on a straight roller section.
Cartons touch each other and the line is stop-and-go, so we do not count
frames or blobs. Instead:

1. **Conveyor displacement.** The horizontal shift between consecutive
   frames is measured with phase correlation on the ROI and integrated into
   an ``offset`` (px). A point on a carton keeps a constant *conveyor
   coordinate* ``u = x - offset``.
2. **Carton edges.** Vertical edges (seams between touching cartons, leading
   and trailing carton edges) are column peaks of |dI/dx|. Each detection is
   tracked in conveyor coordinates; an edge is *confirmed* once it has been
   seen several times **and** has moved in the image, i.e. it travels with
   the belt (static background edges never qualify).
3. **Counting line.** When a confirmed edge crosses the vertical line at
   ``line_x``, a boundary is emitted. The stretch between two consecutive
   boundaries, if it has a plausible carton width, is one carton.
4. **Best frames.** Frames are buffered keyed by offset; while the conveyor
   is stopped only the sharpest frame for that position is kept, so a carton
   standing still for a minute does not flood the buffer. When a carton is
   complete, the buffered frames in which it is most centred and sharpest
   become its candidate frames.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import cv2
import numpy as np


@dataclass
class TrackerParams:
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)  # x, y, w, h fractions
    work_width: int = 320  # analysis resolution (px)
    line_x: float = 0.5  # counting line, fraction of ROI width
    min_carton_frac: float = 0.25  # carton width limits, fraction of ROI width
    max_carton_frac: float = 1.6
    edge_k: float = 6.0
    edge_rel: float = 0.3  # ... and at least this fraction of the strongest edge  # edge peak threshold = k * median column gradient
    edge_tol: float = 4.0  # px tolerance when matching edges in conveyor coords
    confirm_hits: int = 3
    confirm_steps: int = 2  # frame-to-frame steps that matched the belt
    confirm_motion: float = 6.0  # px the edge must have moved in the image
    min_shift: float = 0.4  # px; below this the conveyor counts as stopped
    frames_per_sighting: int = 3
    buffer_step: float = 2.0  # keep at most one frame per this many px of travel


@dataclass
class CartonSighting:
    """One physical carton seen by one camera."""

    camera_id: str
    ts: datetime  # when the carton centre crossed the counting line
    frames: list[np.ndarray]  # best frames cropped to this carton (BGR, full res)
    frame_times: list[datetime]
    width_px: float  # carton width in work pixels
    index: int  # running number on this camera
    meta: dict[str, object] = field(default_factory=dict)
    full_frames: list[np.ndarray] = field(default_factory=list)  # uncropped, for review


@dataclass
class _EdgeTrack:
    u: float
    hits: int
    first_x: float
    last_x: float
    last_t: int
    first_offset: float
    last_offset: float
    good_steps: int = 0  # consecutive-frame steps that moved with the belt
    confirmed: bool = False
    crossed: bool = False


@dataclass
class _Buffered:
    offset: float
    t: datetime
    frame: np.ndarray  # full-resolution ROI-independent frame
    sharp: float


def column_edges(gray: np.ndarray, k: float, rel: float = 0.3) -> np.ndarray:
    """x positions of strong vertical edges in a (work-size) grey image."""
    h = gray.shape[0]
    band = gray[int(h * 0.15) : int(h * 0.85)].astype(np.float32)
    gx = np.abs(cv2.Sobel(band, cv2.CV_32F, 1, 0, ksize=3))
    # an edge must span most of the band height to be a carton edge: use a
    # robust per-column statistic (40th percentile) rather than the mean
    prof = np.percentile(gx, 40, axis=0)
    prof = np.convolve(prof, np.ones(3) / 3, mode="same")
    med = float(np.median(prof)) + 1e-3
    thr = max(k * med, rel * float(prof.max()), 8.0)
    peaks = []
    r = 4  # non-max window: both flanks of a seam count once
    for x in range(r, len(prof) - r):
        if prof[x] >= thr and prof[x] == prof[x - r : x + r + 1].max():
            peaks.append(x)
    return np.array(peaks, np.float32)


def sharpness(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_32F).var())


class CartonTracker:
    """Feed frames in time order with :meth:`update`; get sightings back."""

    def __init__(self, camera_id: str, params: TrackerParams | None = None) -> None:
        self.camera_id = camera_id
        self.p = params or TrackerParams()
        self.offset = 0.0
        self.t_index = 0
        self.prev: np.ndarray | None = None
        self.tracks: list[_EdgeTrack] = []
        self.boundaries: list[tuple[float, datetime, float]] = []  # (u, t, offset)
        self.buffer: deque[_Buffered] = deque()
        self.history: deque[tuple[float, datetime]] = deque(maxlen=20000)  # (offset, ts)
        self.count = 0
        self.width = 0
        self.last_shift = 0.0
        self.stats = {"frames": 0, "moving_frames": 0, "boundaries": 0, "rejected_widths": 0}

    # ---- geometry helpers ---------------------------------------------
    def _roi(self, frame: np.ndarray) -> np.ndarray:
        H, W = frame.shape[:2]
        x, y, w, h = self.p.roi
        return frame[int(y * H) : int((y + h) * H), int(x * W) : int((x + w) * W)]

    def _work(self, frame: np.ndarray) -> np.ndarray:
        roi = self._roi(frame)
        g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        s = self.p.work_width / g.shape[1]
        return cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)

    # ---- main loop -----------------------------------------------------
    def update(self, frame: np.ndarray, ts: datetime) -> list[CartonSighting]:
        g = self._work(frame)
        self.width = g.shape[1]
        self.stats["frames"] += 1
        dx = 0.0
        if self.prev is not None and self.prev.shape == g.shape:
            dx = self._shift(self.prev, g)
        self.prev = g
        self.offset += dx
        self.last_shift = dx
        self.history.append((self.offset, ts))
        if dx:
            self.stats["moving_frames"] += 1
        self._buffer_frame(frame, g, ts)
        self._update_edges(g, ts)
        self.t_index += 1
        return self._emit(ts)

    def _shift(self, prev: np.ndarray, cur: np.ndarray) -> float:
        """Horizontal conveyor shift between two work frames (px).

        Phase correlation. While a carton only partly covers the view (it is
        entering or leaving), the static background would dominate and pull
        the estimate to zero, so the correlation is restricted to the columns
        that changed between the frames.
        """
        diff = np.abs(cur.astype(np.int16) - prev.astype(np.int16)).mean(axis=0)
        changed = np.nonzero(diff > 6.0)[0]
        W = cur.shape[1]
        a, b = 0, W
        if len(changed) and len(changed) < 0.85 * W:
            a = max(0, int(changed.min()) - 16)
            b = min(W, int(changed.max()) + 17)
            if b - a < 32:
                return 0.0
        elif len(changed) == 0:
            return 0.0
        p_, c_ = prev[:, a:b].astype(np.float32), cur[:, a:b].astype(np.float32)
        win = cv2.createHanningWindow(c_.shape[::-1], cv2.CV_32F)
        (sx, sy), resp = cv2.phaseCorrelate(p_, c_, win)
        if resp > 0.05 and abs(sx) >= self.p.min_shift and abs(sy) < max(4.0, abs(sx)):
            return float(sx)
        return 0.0

    def _buffer_frame(self, frame: np.ndarray, g: np.ndarray, ts: datetime) -> None:
        sh = sharpness(g) / (1.0 + abs(self.last_shift))  # penalise motion blur
        if self.buffer and abs(self.buffer[-1].offset - self.offset) < self.p.buffer_step:
            if sh > self.buffer[-1].sharp:
                self.buffer[-1] = _Buffered(self.offset, ts, frame.copy(), sh)
            return
        self.buffer.append(_Buffered(self.offset, ts, frame.copy(), sh))
        # keep about two frame widths of travel
        limit = 2.5 * self.p.max_carton_frac * self.width
        while self.buffer and abs(self.offset - self.buffer[0].offset) > limit:
            self.buffer.popleft()

    def _update_edges(self, g: np.ndarray, ts: datetime) -> None:
        xs = column_edges(g, self.p.edge_k, self.p.edge_rel)
        line = self.p.line_x * self.width
        seen: set[int] = set()
        for x in xs:
            u = float(x) - self.offset
            best, bd = None, self.p.edge_tol
            for i, tr in enumerate(self.tracks):
                d = abs(tr.u - u)
                if d <= bd and i not in seen:
                    best, bd = i, d
            if best is None:
                self.tracks.append(
                    _EdgeTrack(u, 1, float(x), float(x), self.t_index, self.offset, self.offset)
                )
                seen.add(len(self.tracks) - 1)
                continue
            tr = self.tracks[best]
            seen.add(best)
            prev_x = tr.last_x
            step_belt = self.offset - tr.last_offset
            step_img = float(x) - prev_x
            # A step counts only between consecutive frames: periodic static
            # structures (rollers, posts) can re-match the same conveyor
            # coordinate every few frames (aliasing), never frame to frame.
            if (
                self.t_index - tr.last_t == 1
                and abs(step_belt) >= 1.5
                and abs(step_img - step_belt) <= max(2.0, 0.3 * abs(step_belt))
            ):
                tr.good_steps += 1
            tr.u = 0.7 * tr.u + 0.3 * u
            tr.hits += 1
            tr.last_x = float(x)
            tr.last_t = self.t_index
            tr.last_offset = self.offset
            moved = tr.last_x - tr.first_x
            if (
                not tr.confirmed
                and tr.hits >= self.p.confirm_hits
                and tr.good_steps >= self.p.confirm_steps
                and abs(moved) >= self.p.confirm_motion
            ):
                tr.confirmed = True
            if tr.confirmed and not tr.crossed and (prev_x - line) * (tr.last_x - line) <= 0 and prev_x != tr.last_x:
                tr.crossed = True
                self.boundaries.append((tr.u, ts, self.offset))
                self.stats["boundaries"] += 1
        # forget edges not seen for a while (only while moving; a stopped
        # conveyor keeps everything in place)
        self.tracks = [
            t
            for t in self.tracks
            if self.t_index - t.last_t <= 6 or (self.last_shift == 0 and not t.crossed)
        ]

    def _emit(self, ts: datetime) -> list[CartonSighting]:
        out = []
        while len(self.boundaries) >= 2:
            (u1, t1, _), (u2, t2, _) = self.boundaries[0], self.boundaries[1]
            w = abs(u2 - u1)
            lo, hi = self.p.min_carton_frac * self.width, self.p.max_carton_frac * self.width
            if w < lo:
                # double detection of one seam (e.g. both sides of a gap line)
                self.boundaries.pop(1)
                continue
            if w > hi:
                # empty stretch between two cartons: drop the older boundary
                self.stats["rejected_widths"] += 1
                self.boundaries.pop(0)
                continue
            self.boundaries.pop(0)
            out.append(self._make_sighting(u1, u2, t1, t2))
        return out

    def _centre_time(self, uc: float, t1: datetime, t2: datetime) -> datetime:
        """When the carton centre (conveyor coord ``uc``) crossed the line.

        Uses travelled distance, not the time midpoint, so a stop between the
        two edge crossings does not skew it.
        """
        target = self.p.line_x * self.width - uc
        t = _first_crossing(self.history, target, t1, t2)
        return t if t is not None else t1 + (t2 - t1) / 2

    def _carton_crop(self, frame: np.ndarray, xa: float, xb: float) -> np.ndarray:
        """Crop a full frame to the carton spanning work columns [xa, xb]."""
        H, W = frame.shape[:2]
        rx, ry, rw, rh = self.p.roi
        x0, x1 = int(rx * W), int((rx + rw) * W)
        y0, y1 = int(ry * H), int((ry + rh) * H)
        scale = (x1 - x0) / float(self.width)
        m = 0.03 * (xb - xa)
        a = int(np.clip(x0 + (xa - m) * scale, x0, x1 - 1))
        b = int(np.clip(x0 + (xb + m) * scale, a + 1, x1))
        return frame[y0:y1, a:b].copy()

    def _make_sighting(self, u1: float, u2: float, t1: datetime, t2: datetime) -> CartonSighting:
        uc, w = (u1 + u2) / 2, abs(u2 - u1)
        ua = min(u1, u2)
        centre = self.p.line_x * self.width
        cands = []
        for b in self.buffer:
            xc = uc + b.offset  # carton centre in the image at that moment
            dist = abs(xc - centre) / max(w, 1.0)
            if dist <= 0.35:
                cands.append((dist, -b.sharp, b))
        cands.sort(key=lambda c: (round(c[0] * 4), c[1]))  # centred first, then sharp
        chosen = [c[2] for c in cands[: self.p.frames_per_sighting]]
        self.count += 1
        return CartonSighting(
            camera_id=self.camera_id,
            ts=self._centre_time(uc, t1, t2),
            frames=[self._carton_crop(b.frame, ua + b.offset, ua + w + b.offset) for b in chosen],
            frame_times=[b.t for b in chosen],
            full_frames=[b.frame for b in chosen],
            width_px=w,
            index=self.count,
            meta={"t_first_edge": t1.isoformat(), "t_second_edge": t2.isoformat()},
        )


def _first_crossing(
    history: "deque[tuple[float, datetime]]", target: float, t1: datetime, t2: datetime
) -> datetime | None:
    prev: tuple[float, datetime] | None = None
    for off, t in history:
        if t < t1:
            prev = (off, t)
            continue
        if t > t2:
            break
        if prev is not None and (prev[0] - target) * (off - target) <= 0 and prev[0] != off:
            return t
        prev = (off, t)
    return None


def pair_sightings(
    left: list[CartonSighting], right: list[CartonSighting], window_s: float
) -> list[tuple[CartonSighting | None, CartonSighting | None]]:
    """Merge the two side views of one conveyor into carton events.

    Greedy in time order: a left and a right sighting closer than
    ``window_s`` are the same carton. Unmatched sightings stay single (the
    other camera missed the carton) so nothing is lost or double counted.
    """
    pairs: list[tuple[CartonSighting | None, CartonSighting | None]] = []
    i = j = 0
    L = sorted(left, key=lambda s: s.ts)
    R = sorted(right, key=lambda s: s.ts)
    win = timedelta(seconds=window_s)
    while i < len(L) or j < len(R):
        if i < len(L) and j < len(R):
            dt = L[i].ts - R[j].ts
            if abs(dt) <= win:
                # prefer the closer partner if the next one is even closer
                if j + 1 < len(R) and abs(L[i].ts - R[j + 1].ts) < abs(dt):
                    pairs.append((None, R[j]))
                    j += 1
                    continue
                pairs.append((L[i], R[j]))
                i += 1
                j += 1
            elif dt < timedelta(0):
                pairs.append((L[i], None))
                i += 1
            else:
                pairs.append((None, R[j]))
                j += 1
        elif i < len(L):
            pairs.append((L[i], None))
            i += 1
        else:
            pairs.append((None, R[j]))
            j += 1
    return pairs
