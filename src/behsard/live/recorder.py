"""Raw video recording per camera: ffmpeg stream copy into segment files.

Stream copy (no re-encode) costs almost no CPU. Files are
``<dir>/<camera>/<YYYYmmdd-HHMMSS>_%03d.mp4`` in ``segment_minutes`` chunks.
Recording stops by itself when free disk space drops below ``min_free_gb``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from behsard.live.reader import redact

log = logging.getLogger(__name__)


def ffmpeg_exe() -> str | None:
    from behsard.runtime import bundled_tool

    return bundled_tool("ffmpeg")


class Recorder:
    def __init__(
        self, camera_id: str, url: str, out_dir: Path, segment_minutes: int = 5, min_free_gb: float = 5.0
    ) -> None:
        self.camera_id = camera_id
        self.url = url
        self.out_dir = Path(out_dir) / camera_id
        self.segment_s = max(10, int(segment_minutes * 60))
        self.min_free_gb = min_free_gb
        self.proc: subprocess.Popen[bytes] | None = None
        self.error = ""
        self._last_check = 0.0

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def free_gb(self) -> float:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.out_dir).free / 1e9

    def start(self) -> bool:
        exe = ffmpeg_exe()
        if exe is None:
            self.error = "ffmpeg پیدا نشد"
            return False
        if self.free_gb() < self.min_free_gb:
            self.error = "فضای دیسک کافی نیست"
            return False
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        pattern = str(self.out_dir / f"{stamp}_%03d.mp4")
        cmd = [
            exe, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", self.url,
            "-an", "-c", "copy", "-f", "segment", "-segment_time", str(self.segment_s),
            "-reset_timestamps", "1", pattern,
        ]
        log.info("recording %s -> %s", redact(self.url), pattern)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.error = ""
        return True

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(b"q")  # graceful: finalises the segment
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self.proc = None

    def check_disk(self) -> None:
        now = time.time()
        if now - self._last_check < 10:
            return
        self._last_check = now
        if self.running and self.free_gb() < self.min_free_gb:
            log.warning("camera %s: low disk space, recording stopped", self.camera_id)
            self.stop()
            self.error = "ضبط متوقف شد: فضای دیسک کم است"
