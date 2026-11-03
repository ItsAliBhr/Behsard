"""Fixtures for browser smoke tests.

Runs against ``$BEHSARD_URL`` if set (e.g. the packaged Windows app in CI),
otherwise starts ``behsard.launcher`` in a temporary data folder.
"""

from __future__ import annotations

import glob
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from behsard.eval.synth_video import VideoConfig, generate_video_pair


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/health", timeout=2) as r:
            return json.load(r).get("status") == "ok"
    except Exception:
        return False


@pytest.fixture(scope="session")
def synth_video(tmp_path_factory) -> Path:
    d = Path(os.environ.get("BEHSARD_E2E_VIDEO_DIR") or tmp_path_factory.mktemp("e2evideo"))
    if not (d / "truth.json").exists():
        generate_video_pair(d, seed=2, cfg=VideoConfig(n_cartons=6))
    return d


@pytest.fixture(scope="session")
def app_url(tmp_path_factory) -> str:
    url = os.environ.get("BEHSARD_URL")
    if url:
        for _ in range(120):
            if _healthy(url):
                return url
            time.sleep(1)
        pytest.fail(f"{url} is not healthy")
    home = tmp_path_factory.mktemp("apphome")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log = open(home / "server.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "behsard.launcher", "--home", str(home), "--port", str(port), "--no-browser"],
        stdout=log, stderr=subprocess.STDOUT,
    )
    for _ in range(60):
        if _healthy(url):
            break
        time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("server did not start: " + (home / "server.log").read_text()[-2000:])
    yield url
    proc.terminate()
    try:
        proc.wait(20)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            exe = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
            if not exe:
                pytest.skip("no Chromium for Playwright")
            b = p.chromium.launch(executable_path=exe[-1])
        yield b
        b.close()
