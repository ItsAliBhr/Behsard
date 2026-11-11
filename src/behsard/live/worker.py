"""Per-camera processing thread: frames -> CartonTracker -> sightings."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime

import cv2
import numpy as np

from behsard.tracking.spotter import StampSpotter
from behsard.tracking.tracker import CartonSighting, CartonTracker, TrackerParams, sharpness

log = logging.getLogger(__name__)

SightingCallback = Callable[[CartonSighting], None]


class CameraWorker(threading.Thread):
    """Consumes frames from a small queue (newest wins) and runs the tracker.

    The queue holds at most 2 frames: if tracking falls behind, the oldest
    frame is dropped (counted), so latency stays bounded and memory flat.
    """

    def __init__(
        self,
        camera_id: str,
        side: str,
        conveyor_id: int,
        params: TrackerParams,
        on_sighting: SightingCallback,
        spotter: StampSpotter | None = None,
        lossless: bool = False,
    ) -> None:
        super().__init__(name=f"worker-{camera_id}", daemon=True)
        self.camera_id = camera_id
        self.side = side
        self.conveyor_id = conveyor_id
        self.tracker = CartonTracker(camera_id, params)
        # stamps mode: the spotter counts; the seam tracker is not used
        self.spotter = spotter
        # replay: never drop a frame, the reader waits for us instead
        self.lossless = lossless
        self.source_done: Callable[[], bool] = lambda: False
        self._flushed = False
        self.on_sighting = on_sighting
        self.q: queue.Queue[tuple[np.ndarray, datetime]] = queue.Queue(maxsize=2)
        self.dropped = 0
        self.processed = 0
        self.proc_ms = 0.0  # moving average tracking time per frame
        self.blur_scores: list[float] = []  # recent sharpness while moving
        self.latest_frame: np.ndarray | None = None
        self.latest_overlay: dict[str, object] = {}
        self.last_label: tuple[str, float, float] | None = None  # (text, conf, time)
        self._stop_event = threading.Event()

    @property
    def sightings(self) -> int:
        return self.spotter.count if self.spotter is not None else self.tracker.count

    def submit(self, frame: np.ndarray, ts: datetime) -> None:
        """Called from the reader thread."""
        if self.lossless:
            while not self._stop_event.is_set():
                try:
                    self.q.put((frame, ts), timeout=0.2)
                    return
                except queue.Full:
                    continue
            return
        try:
            self.q.put_nowait((frame, ts))
        except queue.Full:
            try:
                self.q.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self.q.put_nowait((frame, ts))
            except queue.Full:
                self.dropped += 1

    def stop(self) -> None:
        self._stop_event.set()

    def _deliver(self, sightings: list[CartonSighting]) -> None:
        for s in sightings:
            s.meta["side"] = self.side
            s.meta["conveyor_id"] = self.conveyor_id
            try:
                self.on_sighting(s)
            except Exception:
                log.exception("camera %s: sighting handler failed", self.camera_id)

    def _flush(self) -> None:
        if self.spotter is not None and not self._flushed:
            self._flushed = True
            self._deliver(self.spotter.flush())

    def run(self) -> None:
        idle = 0.0
        while not self._stop_event.is_set():
            try:
                frame, ts = self.q.get(timeout=0.2)
            except queue.Empty:
                idle += 0.2
                if idle >= 0.6 and self.source_done():  # end of a replayed video
                    self._flush()
                continue
            idle = 0.0
            self._flushed = False
            t0 = time.perf_counter()
            try:
                if self.spotter is not None:
                    sightings = self.spotter.update(frame, ts)
                else:
                    sightings = self.tracker.update(frame, ts)
            except Exception:
                log.exception("camera %s: tracking failed", self.camera_id)
                continue
            dt = (time.perf_counter() - t0) * 1000
            self.proc_ms = 0.9 * self.proc_ms + 0.1 * dt if self.processed else dt
            self.processed += 1
            self.latest_frame = frame
            if self.spotter is not None:
                self.latest_overlay = self.spotter.overlay()
            else:
                self.latest_overlay = self.tracker.overlay()
                if self.tracker.last_shift:
                    g = self.tracker.prev
                    if g is not None:
                        self.blur_scores.append(sharpness(g))
                        del self.blur_scores[:-50]
            self._deliver(sightings)
        self._flush()

    # ---- live view -----------------------------------------------------
    def render(self, width: int = 960) -> bytes | None:
        """Annotated JPEG of the latest frame (ROI, line, edges, last carton)."""
        frame = self.latest_frame
        if frame is None:
            return None
        h, w = frame.shape[:2]
        s = width / w
        img = cv2.resize(frame, (width, int(h * s)), interpolation=cv2.INTER_AREA)
        H, W = img.shape[:2]
        ov = self.latest_overlay
        rx, ry, rw, rh = ov.get("roi", (0, 0, 1, 1))  # type: ignore[misc]
        x0, y0, x1, y1 = int(rx * W), int(ry * H), int((rx + rw) * W), int((ry + rh) * H)
        cv2.rectangle(img, (x0, y0), (x1, y1), (255, 200, 0), 2)
        if "line_x" in ov:  # seams mode: counting line
            lx = int(x0 + float(ov["line_x"]) * (x1 - x0))  # type: ignore[arg-type]
            cv2.line(img, (lx, y0), (lx, y1), (0, 0, 255), 2)
        for fx, confirmed in ov.get("edges", []):  # type: ignore[union-attr]
            ex = int(x0 + fx * (x1 - x0))
            cv2.line(img, (ex, y0), (ex, y1), (0, 220, 0) if confirmed else (0, 140, 255), 2 if confirmed else 1)
        for px, py, code, conf, ok in ov.get("spots", []):  # type: ignore[union-attr]
            cx, cy = int(x0 + px * (x1 - x0)), int(y0 + py * (y1 - y0))
            col = (0, 220, 0) if ok else (0, 165, 255)
            cv2.circle(img, (cx, cy), 26, col, 3)
            cv2.putText(img, f"{code} {conf:.0%}", (max(0, cx - 60), max(20, cy - 34)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, col, 2, cv2.LINE_AA)
        if self.last_label and time.time() - self.last_label[2] < 6:
            text, conf, _ = self.last_label
            label = f"{text}  {conf:.0%}"
            cv2.rectangle(img, (10, 10), (10 + 26 * len(label), 70), (0, 0, 0), -1)
            cv2.putText(img, label, (18, 58), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 3, cv2.LINE_AA)
        if ov.get("moving"):
            cv2.circle(img, (W - 25, 25), 10, (0, 220, 0), -1)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes() if ok else None
