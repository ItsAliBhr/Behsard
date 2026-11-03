"""Capture screenshots of every page for README.fa.md.

Usage: python scripts/screenshots.py OUT_DIR [VIDEO_DIR]
Starts the app in a temporary data folder with two simulated cameras.
"""

from __future__ import annotations

import glob
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

from behsard.eval.synth_video import VideoConfig, generate_video_pair


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    vid = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp())
    if not (vid / "truth.json").exists():
        generate_video_pair(vid, seed=1, cfg=VideoConfig(n_cartons=8))
    truth = json.loads((vid / "truth.json").read_text())
    home = Path(tempfile.mkdtemp())
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([sys.executable, "-m", "behsard.launcher", "--home", str(home), "--port", str(port), "--no-browser"])
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    try:
        with sync_playwright() as p:
            try:
                b = p.chromium.launch()
            except Exception:
                b = p.chromium.launch(executable_path=sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))[-1])
            pg = b.new_page(viewport={"width": 1366, "height": 860}, device_scale_factor=1)
            pg.goto(url + "/shifts")
            pg.fill("input[name=name]", "صبح")
            pg.click("#open-shift")
            pg.wait_for_url("**/shifts/*")
            names = ["علی رضایی", "حسن کریمی", "مریم احمدی", "رضا محمدی", "سارا حسینی", "مهدی نوری"]
            for name, code in zip(names, truth["codes"]):
                pg.fill("#worker-input", name)
                pg.fill("input[name=stamp]", code)
                pg.press("input[name=stamp]", "Enter")
                pg.wait_for_selector(f"#assignments >> text={code}")
            pg.screenshot(path=str(out / "shift.png"), full_page=True)
            for side in ("L", "R"):
                pg.goto(url + "/cameras/new")
                pg.fill("input[name=id]", f"cam1{side}")
                pg.select_option("select[name=side]", side)
                pg.select_option("#source", "url")
                pg.fill("input[name=url]", str(vid / ("left.mp4" if side == "L" else "right.mp4")))
                if side == "L":
                    pg.screenshot(path=str(out / "camera-add.png"), full_page=True)
                pg.click("#save-camera")
                pg.wait_for_url("**/setup")
            pg.click("#test-conn")
            pg.wait_for_selector("#conn-ok", timeout=20000)
            time.sleep(1)
            pg.screenshot(path=str(out / "camera-setup.png"), full_page=True)
            pg.goto(url + "/cameras")
            pg.screenshot(path=str(out / "cameras.png"), full_page=True)
            pg.goto(url + "/")
            time.sleep(35)
            pg.screenshot(path=str(out / "dashboard.png"), full_page=True)
            pg.goto(url + "/review")
            pg.screenshot(path=str(out / "review.png"), full_page=True)
            pg.goto(url + "/reports?by=detail")
            pg.screenshot(path=str(out / "reports.png"), full_page=True)
            pg.goto(url + "/test")
            pg.fill("input[name=stamps]", " ".join(truth["codes"]))
            pg.check("input[name=enabled]")
            pg.click("#save-test")
            time.sleep(20)
            pg.goto(url + "/test")
            pg.keyboard.press("Enter")
            time.sleep(1)
            pg.screenshot(path=str(out / "test-mode.png"), full_page=True)
            pg.goto(url + "/replay")
            pg.screenshot(path=str(out / "replay.png"), full_page=True)
            b.close()
    finally:
        proc.terminate()
        proc.wait(20)


if __name__ == "__main__":
    main()
