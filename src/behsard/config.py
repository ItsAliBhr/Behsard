"""Load the single YAML configuration file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config/behsard.yaml")


@dataclass
class RecognitionConfig:
    accept_threshold: float = 0.45
    min_margin: float = 0.05
    tesseract_cmd: str = "tesseract"
    max_regions: int = 4
    work_max_side: int = 1600


@dataclass
class TrackingConfig:
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    process_fps: float = 10.0
    pair_window_s: float = 1.5
    frames_per_sighting: int = 3


@dataclass
class Config:
    db_path: Path = Path("data/behsard.sqlite")
    media_dir: Path = Path("data/media")
    timezone: str = "Asia/Tehran"
    conveyors: list[dict[str, Any]] = field(default_factory=list)
    cameras: list[dict[str, Any]] = field(default_factory=list)
    recognition: RecognitionConfig = field(default_factory=RecognitionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    review_host: str = "127.0.0.1"
    review_port: int = 8000


def load_config(path: str | Path | None = None) -> Config:
    """Load config from ``path``, ``$BEHSARD_CONFIG`` or the default path.

    Missing file means defaults.
    """
    p = Path(path or os.environ.get("BEHSARD_CONFIG", DEFAULT_CONFIG_PATH))
    raw: dict[str, Any] = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    paths = raw.get("paths", {})
    rec = raw.get("recognition", {})
    trk = raw.get("tracking", {})
    if "roi" in trk:
        trk["roi"] = tuple(trk["roi"])
    review = raw.get("review", {})
    return Config(
        db_path=Path(paths.get("db", "data/behsard.sqlite")),
        media_dir=Path(paths.get("media", "data/media")),
        timezone=raw.get("site", {}).get("timezone", "Asia/Tehran"),
        conveyors=list(raw.get("conveyors", [])),
        cameras=list(raw.get("cameras", [])),
        recognition=RecognitionConfig(**rec),
        tracking=TrackingConfig(**trk),
        review_host=review.get("host", "127.0.0.1"),
        review_port=int(review.get("port", 8000)),
    )
