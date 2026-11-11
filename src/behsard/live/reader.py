"""Camera reader thread: RTSP (or a video file for simulation/replay).

* reconnects automatically with exponential backoff (capped, so a camera that
  comes back is picked up within a few seconds);
* delivers frames at ``process_fps`` (extra frames are read and dropped,
  so the stream never lags behind real time);
* keeps health counters for the dashboard.

Credentials live inside the URL; :func:`redact` must be used for anything
that is logged or shown.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

# RTSP over TCP (no UDP packet loss artefacts) and a 5 s socket timeout so a
# dead camera is detected quickly instead of blocking forever.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000|timeout;5000000")

FrameCallback = Callable[[np.ndarray, datetime], None]


def redact(url: str) -> str:
    """Hide the password in rtsp://user:pass@host/... for logs and UI."""
    return re.sub(r"(rtsp[s]?://[^:/@]+):[^@]*@", r"\1:***@", url)


def rtsp_url(host: str, user: str = "", password: str = "", channel: str = "101", port: int = 554) -> str:
    cred = f"{user}:{password}@" if user else ""
    return f"rtsp://{cred}{host}:{port}/Streaming/Channels/{channel}"


def is_file_source(url: str) -> bool:
    return not re.match(r"^[a-z]+://", url) or url.startswith("file://")


@dataclass
class CameraHealth:
    state: str = "stopped"  # stopped | connecting | connected | reconnecting | finished
    connected_since: float | None = None
    last_frame_at: float | None = None
    fps_in: float = 0.0  # frames read from the camera per second
    fps_out: float = 0.0  # frames delivered for processing per second
    frames_in: int = 0
    frames_out: int = 0
    dropped: int = 0  # frames dropped because processing was busy
    reconnects: int = 0
    last_error: str = ""
    resolution: tuple[int, int] = (0, 0)
    _win_in: list[float] = field(default_factory=list, repr=False)
    _win_out: list[float] = field(default_factory=list, repr=False)

    def as_dict(self) -> dict[str, object]:
        now = time.time()
        return {
            "state": self.state,
            "fps_in": round(self.fps_in, 1),
            "fps_out": round(self.fps_out, 1),
            "frames": self.frames_out,
            "dropped": self.dropped,
            "reconnects": self.reconnects,
            "last_frame_age": round(now - self.last_frame_at, 1) if self.last_frame_at else None,
            "last_error": self.last_error,
            "resolution": f"{self.resolution[0]}x{self.resolution[1]}",
        }


def _rate(win: list[float], now: float) -> float:
    while win and now - win[0] > 2.0:
        win.pop(0)
    return len(win) / 2.0


class CameraReader(threading.Thread):
    def __init__(
        self,
        camera_id: str,
        url: str,
        on_frame: FrameCallback,
        process_fps: float = 10.0,
        loop: bool = True,
        realtime: bool = True,
        backoff_max: float = 3.0,
        speed: float = 1.0,
        video_clock: bool = False,
    ) -> None:
        super().__init__(name=f"reader-{camera_id}", daemon=True)
        self.camera_id = camera_id
        self.url = url
        self.on_frame = on_frame
        self.process_fps = process_fps
        self.loop = loop
        self.realtime = realtime
        self.backoff_max = backoff_max
        self.speed = max(0.1, speed)  # file playback speed (tests / fast replay)
        # replay: timestamps follow the video (frame index / fps), not the
        # wall clock, so results do not depend on how fast this PC is
        self.video_clock = video_clock
        self.health = CameraHealth()
        self.latest: np.ndarray | None = None  # last frame read (for snapshots)
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _open(self) -> cv2.VideoCapture | None:
        src = self.url[len("file://") :] if self.url.startswith("file://") else self.url
        if is_file_source(self.url) and not Path(src).exists():
            self.health.last_error = "فایل ویدیو پیدا نشد"
            return None
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            self.health.last_error = "اتصال به دوربین برقرار نشد"
            cap.release()
            return None
        return cap

    def run(self) -> None:
        backoff = 0.5
        file_src = is_file_source(self.url)
        while not self._stop_event.is_set():
            self.health.state = "connecting" if self.health.reconnects == 0 else "reconnecting"
            cap = self._open()
            if cap is None:
                self._sleep(backoff)
                backoff = min(backoff * 2, self.backoff_max)
                self.health.reconnects += 1
                continue
            backoff = 0.5
            self.health.state = "connected"
            self.health.connected_since = time.time()
            self.health.last_error = ""
            src_fps = cap.get(cv2.CAP_PROP_FPS) or 10.0
            if not 1 <= src_fps <= 120:
                src_fps = 10.0
            ended = self._read_loop(cap, src_fps, file_src)
            cap.release()
            if self._stop_event.is_set():
                break
            if ended and file_src and not self.loop:
                self.health.state = "finished"
                return
            if not (ended and file_src):
                log.warning("camera %s: stream lost, reconnecting", self.camera_id)
                self.health.reconnects += 1
                self.health.state = "reconnecting"
                self._sleep(backoff)
        self.health.state = "stopped"

    def _read_loop(self, cap: cv2.VideoCapture, src_fps: float, file_src: bool) -> bool:
        """Read until the stream fails. Returns True at a clean end of file."""
        period = 1.0 / (self.process_fps * (self.speed if file_src else 1.0)) if self.process_fps > 0 else 0.0
        next_due = 0.0
        t_start = time.monotonic()
        t_wall = datetime.now()
        n = 0
        while not self._stop_event.is_set():
            if file_src and self.realtime:  # pace a file like a live camera
                wait = t_start + n / (src_fps * self.speed) - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            ok = cap.grab()
            if not ok:
                if file_src:
                    return True
                self.health.last_error = "قطع تصویر"
                return False
            n += 1
            now = time.time()
            self.health.frames_in += 1
            self.health._win_in.append(now)
            self.health.fps_in = _rate(self.health._win_in, now)
            mono = time.monotonic()
            # files without pacing (fast replay): process every frame
            due = (not file_src or self.realtime) and period and mono < next_due
            if due:
                continue
            next_due = max(next_due + period, mono) if period else mono
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                continue
            self.latest = frame
            self.health.resolution = (frame.shape[1], frame.shape[0])
            self.health.last_frame_at = now
            self.health.frames_out += 1
            self.health._win_out.append(now)
            self.health.fps_out = _rate(self.health._win_out, now)
            try:
                ts = t_wall + timedelta(seconds=(n - 1) / src_fps) if self.video_clock else datetime.now()
                self.on_frame(frame, ts)
            except Exception:  # never let a processing bug kill the reader
                log.exception("camera %s: frame handler failed", self.camera_id)
        return False

    def _sleep(self, seconds: float) -> None:
        self._stop_event.wait(seconds)


def grab_snapshot(url: str, timeout_s: float = 8.0) -> np.ndarray | None:
    """One frame from a camera or file (for the setup wizard)."""
    result: list[np.ndarray] = []

    def work() -> None:
        src = url[len("file://") :] if url.startswith("file://") else url
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        try:
            for _ in range(5):  # first frames of a stream can be grey
                ok, frame = cap.read()
                if ok and frame is not None:
                    result.append(frame)
                    if not is_file_source(url):
                        continue
                    break
        finally:
            cap.release()

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout_s)
    return result[-1] if result else None
