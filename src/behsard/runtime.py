"""Locate bundled tools (Windows build) and configure Tesseract/ffmpeg paths.

In the portable Windows build the layout is::

    Behsard/
      Behsard.exe
      tools/tesseract/tesseract.exe (+ tessdata/eng.traineddata)
      tools/ffmpeg/ffmpeg.exe
      config/behsard.yaml   (created on first run)
      data/                 (DB, media, recordings)
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def app_dir() -> Path:
    """Folder of the executable (frozen) or the repository root (source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundled_tool(name: str) -> str | None:
    exe = f"{name}.exe" if os.name == "nt" else name
    for p in (app_dir() / "tools" / name / exe, app_dir() / "tools" / exe):
        if p.exists():
            return str(p)
    return shutil.which(name)


def configure_tesseract() -> str | None:
    """Point pytesseract at the bundled Tesseract if there is one."""
    exe = bundled_tool("tesseract")
    if exe and Path(exe).parent.joinpath("tessdata").exists():
        os.environ.setdefault("TESSDATA_PREFIX", str(Path(exe).parent / "tessdata"))
    return exe
