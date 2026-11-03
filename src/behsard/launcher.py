"""One-click start: data folder, config, server, browser.

Used by ``Behsard.exe`` (Windows build) and ``behsard serve --open``.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import shutil
import sys
import threading
import time
import urllib.request
import webbrowser
from importlib import resources
from pathlib import Path

from behsard.runtime import app_dir

log = logging.getLogger("behsard")


def data_home(explicit: str | None = None) -> Path:
    """Where config/ and data/ live: --home, $BEHSARD_HOME, the app folder
    (frozen build) or the current directory (source checkout)."""
    if explicit:
        return Path(explicit).resolve()
    if os.environ.get("BEHSARD_HOME"):
        return Path(os.environ["BEHSARD_HOME"]).resolve()
    if getattr(sys, "frozen", False):
        return app_dir()
    return Path.cwd()


def ensure_home(home: Path) -> Path:
    """Create config (from the packaged default) and data folders."""
    cfg = home / "config" / "behsard.yaml"
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        src = resources.files("behsard").joinpath("default_config.yaml")
        with resources.as_file(src) as p:
            shutil.copy(p, cfg)
    for d in ("data", "data/media", "data/logs", "data/recordings", "data/replays", "data/exports"):
        (home / d).mkdir(parents=True, exist_ok=True)
    return cfg


def setup_logging(home: Path) -> None:
    fh = logging.handlers.RotatingFileHandler(
        home / "data" / "logs" / "behsard.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    if sys.stderr is not None:
        root.addHandler(logging.StreamHandler())


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _open_when_ready(url: str) -> None:
    for _ in range(120):
        if _healthy(url):
            webbrowser.open(url)
            return
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="Behsard")
    ap.add_argument("--home", default=None, help="data folder (config/, data/)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-live", action="store_true", help="do not start cameras")
    args = ap.parse_args(argv)

    home = data_home(args.home)
    cfg_path = ensure_home(home)
    os.chdir(home)
    setup_logging(home)

    from behsard.config import load_config

    cfg = load_config(cfg_path)
    host = args.host or cfg.review_host
    port = args.port or cfg.review_port
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    if _healthy(url):  # already running: just show it
        log.info("Behsard is already running at %s", url)
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    import uvicorn

    from behsard.web.app import create_app

    app = create_app(cfg, start_live=not args.no_live and cfg.live.enabled)
    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    log.info("Behsard starting at %s (data: %s)", url, home)
    uvicorn.run(app, host=host, port=port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
