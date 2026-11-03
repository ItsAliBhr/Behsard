"""Soak test: 2 simulated cameras for N minutes; memory, CPU, lost events.

Usage: python scripts/soak.py --minutes 120 [--width 3200 --height 1800]
Cameras are synthetic conveyor videos (upscaled to the camera resolution),
paced at 10 fps and looped. Pass/fail:
  * RSS after the warm-up grows < 10 %
  * mean CPU of this process (incl. Tesseract children) < 70 % of all cores
  * cartons stored >= 90 % of cartons shown (no lost events)
Prints a JSON summary; exit code 1 on failure.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import psutil

from behsard.config import Config
from behsard.core.models import Conveyor
from behsard.core.repository import SqliteRepository
from behsard.core.services import ShiftService
from behsard.eval.synth_video import VideoConfig, generate_video_pair
from behsard.live.service import LiveService
from behsard.recognition.stamp import StampRecognizer


def upscale(src: Path, dst: Path, w: int, h: int) -> None:
    ff = shutil.which("ffmpeg")
    if ff is None or (w, h) == (640, 360):
        shutil.copy(src, dst)
        return
    subprocess.run([ff, "-v", "error", "-y", "-i", str(src), "-vf", f"scale={w}:{h}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-r", "10", str(dst)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--width", type=int, default=3200)
    ap.add_argument("--height", type=int, default=1800)
    ap.add_argument("--warmup", type=float, default=3, help="minutes before the memory baseline")
    a = ap.parse_args()
    work = Path(tempfile.mkdtemp())
    raw = work / "raw"
    truth = generate_video_pair(raw, seed=0, cfg=VideoConfig(n_cartons=8))
    for s in ("left", "right"):
        upscale(raw / f"{s}.mp4", work / f"{s}.mp4", a.width, a.height)
    loop_s = len(list(_frames(work / "left.mp4"))) / 10.0
    repo = SqliteRepository(work / "soak.sqlite")
    repo.upsert_conveyor(Conveyor(1, "c1"))
    svc = ShiftService(repo)
    sh = svc.open_shift("soak", datetime.now())
    for i, c in enumerate(truth["codes"]):
        svc.assign(sh.id, svc.ensure_worker(f"w{i}").id, c, 1)
    cfg = Config()
    cfg.media_dir = work / "media"
    cfg.cameras = [{"id": s, "conveyor_id": 1, "side": s, "url": str(work / f"{n}.mp4"), "loop": True}
                   for s, n in (("L", "left"), ("R", "right"))]
    proc = psutil.Process()
    live = LiveService(cfg, repo, StampRecognizer())
    t0 = time.time()
    live.start()
    samples, cpu_t = [], proc.cpu_times()
    base_rss = None
    while time.time() - t0 < a.minutes * 60:
        time.sleep(30)
        kids = proc.children(recursive=True)
        rss = proc.memory_info().rss / 1e6
        el = time.time() - t0
        if base_rss is None and el >= a.warmup * 60:
            base_rss = rss
        samples.append({"t": round(el), "rss_mb": round(rss), "events": live.session.cartons, "kids": len(kids)})
        print(json.dumps(samples[-1]), flush=True)
    live.stop()
    elapsed = time.time() - t0
    ct = proc.cpu_times()
    cpu_s = (ct.user - cpu_t.user) + (ct.system - cpu_t.system) + (ct.children_user - cpu_t.children_user) + (ct.children_system - cpu_t.children_system)
    cpu_pct = 100 * cpu_s / elapsed / psutil.cpu_count()
    shown = int(elapsed / loop_s) * sum(c["complete"] for c in truth["cartons"])
    stored = len(repo.list_carton_events(limit=10**7))
    last_rss = samples[-1]["rss_mb"] if samples else 0
    growth = (last_rss - base_rss) / base_rss if base_rss else 0.0
    out = {
        "minutes": round(elapsed / 60, 1), "resolution": f"{a.width}x{a.height}", "cpus": psutil.cpu_count(),
        "cpu_pct_of_machine": round(cpu_pct, 1), "rss_baseline_mb": base_rss, "rss_last_mb": last_rss,
        "rss_growth": round(growth, 3), "cartons_shown_complete_loops": shown, "cartons_stored": stored,
    }
    ok = growth < 0.10 and cpu_pct < 70 and stored >= 0.9 * shown
    out["ok"] = ok
    print(json.dumps(out, indent=1))
    return 0 if ok else 1


def _frames(p: Path):  # type: ignore[no-untyped-def]
    import cv2

    cap = cv2.VideoCapture(str(p))
    while cap.grab():
        yield 1
    cap.release()


if __name__ == "__main__":
    sys.exit(main())
