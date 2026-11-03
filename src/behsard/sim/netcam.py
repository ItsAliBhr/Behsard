"""Network camera simulators (tests, demos): a looped video served live.

* :class:`HttpCamera` - ffmpeg serves the video as H.264 in MPEG-TS over
  HTTP (``-listen 1``). Needs only the ffmpeg binary. OpenCV reads it like a
  network camera, so timeouts and reconnects are exercised for real.
* :class:`RtspCamera` - MediaMTX RTSP server + ffmpeg publisher, the closest
  to a Hikvision camera. Needs the ``mediamtx`` binary (CI downloads it).

``drop(seconds)`` simulates an outage: the stream process is killed and not
restarted until the outage is over.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _encode_args(fps: float | None) -> list[str]:
    r = ["-r", str(fps)] if fps else []
    return ["-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "10", *r]


class _Looped:
    """Keeps a stream process alive (restarts it) except during outages."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[bytes] | None = None
        self._down_until = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.starts = 0

    def _cmd(self) -> list[str]:
        raise NotImplementedError

    def _loop(self) -> None:
        while not self._stop.is_set():
            if time.time() < self._down_until:
                time.sleep(0.1)
                continue
            self.proc = subprocess.Popen(self._cmd(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.starts += 1
            while self.proc.poll() is None and not self._stop.is_set():
                if time.time() < self._down_until:
                    self.proc.kill()
                    break
                time.sleep(0.1)
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait()

    def start(self):  # type: ignore[no-untyped-def]
        self._thread.start()
        return self

    def drop(self, seconds: float) -> None:
        self._down_until = time.time() + seconds

    def stop(self) -> None:
        self._stop.set()
        self._down_until = time.time() + 3600
        self._thread.join(timeout=5)


class HttpCamera(_Looped):
    def __init__(self, video: str | Path, port: int | None = None, fps: float | None = None) -> None:
        super().__init__()
        exe = shutil.which("ffmpeg")
        if exe is None:
            raise RuntimeError("ffmpeg not found")
        self.exe = exe
        self.video = str(video)
        self.fps = fps
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}/cam"

    def _cmd(self) -> list[str]:
        return [
            self.exe, "-v", "error", "-re", "-stream_loop", "-1", "-i", self.video,
            *_encode_args(self.fps), "-f", "mpegts", "-listen", "1", self.url,
        ]


class MediaMtx:
    """A MediaMTX RTSP server on a free port."""

    def __init__(self) -> None:
        exe = shutil.which("mediamtx")
        if exe is None:
            raise RuntimeError("mediamtx not found")
        self.port = free_port()
        self.dir = Path(tempfile.mkdtemp())
        cfg = self.dir / "mediamtx.yml"
        cfg.write_text(
            f"rtspAddress: :{self.port}\nrtmp: no\nhls: no\nwebrtc: no\nsrt: no\n"
            "protocols: [tcp]\npaths:\n  all_others:\n"
        )
        self.proc = subprocess.Popen([exe, str(cfg)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)

    def stop(self) -> None:
        self.proc.kill()
        self.proc.wait()


class RtspCamera(_Looped):
    def __init__(self, server: MediaMtx, name: str, video: str | Path, fps: float | None = None) -> None:
        super().__init__()
        exe = shutil.which("ffmpeg")
        if exe is None:
            raise RuntimeError("ffmpeg not found")
        self.exe = exe
        self.video = str(video)
        self.fps = fps
        self.url = f"rtsp://127.0.0.1:{server.port}/{name}"

    def _cmd(self) -> list[str]:
        return [
            self.exe, "-v", "error", "-re", "-stream_loop", "-1", "-i", self.video,
            *_encode_args(self.fps), "-f", "rtsp", "-rtsp_transport", "tcp", self.url,
        ]


class _Pair(_Looped):
    """Both side cameras of one conveyor from ONE ffmpeg process, so the
    left and right streams stay frame-synchronised like real cameras."""

    def __init__(self, left: str | Path, right: str | Path, fps: float | None = None) -> None:
        super().__init__()
        exe = shutil.which("ffmpeg")
        if exe is None:
            raise RuntimeError("ffmpeg not found")
        self.exe = exe
        self.videos = (str(left), str(right))
        self.fps = fps
        self.urls: dict[str, str] = {}

    def _inputs(self) -> list[str]:
        out: list[str] = []
        for v in self.videos:
            out += ["-re", "-stream_loop", "-1", "-i", v]
        return out


class HttpConveyor(_Pair):
    """Two HTTP cameras (L, R). Streaming starts once both are connected."""

    def __init__(self, left: str | Path, right: str | Path, fps: float | None = None) -> None:
        super().__init__(left, right, fps)
        self.urls = {s: f"http://127.0.0.1:{free_port()}/cam{s}" for s in "LR"}

    def _cmd(self) -> list[str]:
        cmd = [self.exe, "-v", "error", *self._inputs()]
        for i, s in enumerate("LR"):
            cmd += ["-map", f"{i}:v", *_encode_args(self.fps), "-f", "mpegts", "-listen", "1", self.urls[s]]
        return cmd


class RtspConveyor(_Pair):
    """Two RTSP cameras (L, R) published to MediaMTX by one ffmpeg."""

    def __init__(self, server: MediaMtx, left: str | Path, right: str | Path, fps: float | None = None) -> None:
        super().__init__(left, right, fps)
        self.urls = {s: f"rtsp://127.0.0.1:{server.port}/cam{s}" for s in "LR"}

    def _cmd(self) -> list[str]:
        cmd = [self.exe, "-v", "error", *self._inputs()]
        for i, s in enumerate("LR"):
            cmd += ["-map", f"{i}:v", *_encode_args(self.fps), "-f", "rtsp", "-rtsp_transport", "tcp", self.urls[s]]
        return cmd
