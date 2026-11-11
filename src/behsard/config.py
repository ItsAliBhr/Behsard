"""The single YAML configuration file: load, defaults, save.

Relative paths in the file are resolved against the *data home*: the
directory that contains the ``config/`` folder (the working directory in a
source checkout, the app folder in the Windows build).
"""

from __future__ import annotations

import copy
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config/behsard.yaml")


@dataclass
class RecognitionConfig:
    accept_threshold: float = 0.45
    min_margin: float = 0.05
    tesseract_cmd: str = "tesseract"
    max_regions: int = 8
    # rank blue-hued candidates higher; helps when only carton faces are in the
    # watch region, hurts when a blue belt is in view -> off by default
    blue_boost: bool = False
    work_max_side: int = 1600
    templates_path: str = "data/templates/learned.npz"  # learned from real crops


@dataclass
class TrackingConfig:
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    process_fps: float = 10.0
    pair_window_s: float = 1.5
    frames_per_sighting: int = 3
    work_width: int = 320
    edge_k: float = 6.0
    edge_rel: float = 0.3
    min_carton_frac: float = 0.25
    max_carton_frac: float = 1.6
    store_max_side: int = 0  # 0 = keep full-resolution frames for recognition
    # how cartons are counted: "stamps" = follow each stamp seen (default,
    # works on real footage), "seams" = find carton seams with a fixed camera
    mode: str = "stamps"
    spot_interval_s: float = 0.25  # stamps mode: time between reads
    spot_gap_s: float = 2.0  # stamps mode: a stamp unseen this long has left


@dataclass
class LiveConfig:
    enabled: bool = True  # start camera readers with the web server
    preview_width: int = 960
    record_dir: str = "data/recordings"
    segment_minutes: int = 5
    min_free_gb: float = 5.0  # stop recording below this free disk space


@dataclass
class TestModeConfig:
    enabled: bool = False
    stamps: list[str] = field(default_factory=list)


@dataclass
class Config:
    db_path: Path = Path("data/behsard.sqlite")
    media_dir: Path = Path("data/media")
    timezone: str = "Asia/Tehran"
    conveyors: list[dict[str, Any]] = field(default_factory=list)
    cameras: list[dict[str, Any]] = field(default_factory=list)
    recognition: RecognitionConfig = field(default_factory=RecognitionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    test_mode: TestModeConfig = field(default_factory=TestModeConfig)
    review_host: str = "127.0.0.1"
    review_port: int = 8000
    path: Path | None = None  # file this config was loaded from (for saving)
    home: Path = Path(".")

    def camera(self, camera_id: str) -> dict[str, Any] | None:
        return next((c for c in self.cameras if str(c.get("id")) == camera_id), None)

    def resolve(self, p: str | Path) -> Path:
        q = Path(p)
        return q if q.is_absolute() else self.home / q


def _only_known(cls: type, d: dict[str, Any]) -> dict[str, Any]:
    names = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    return {k: v for k, v in d.items() if k in names}


def load_config(path: str | Path | None = None) -> Config:
    """Load config from ``path``, ``$BEHSARD_CONFIG`` or the default path.

    Missing file means defaults.
    """
    p = Path(path or os.environ.get("BEHSARD_CONFIG", DEFAULT_CONFIG_PATH))
    raw: dict[str, Any] = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    home = p.resolve().parent.parent if p.parent.name == "config" else Path(".")
    paths = raw.get("paths", {})
    rec = dict(raw.get("recognition", {}) or {})
    trk = dict(raw.get("tracking", {}) or {})
    if "roi" in trk:
        trk["roi"] = tuple(trk["roi"])
    review = raw.get("review", {}) or {}
    tm = raw.get("test_mode", {}) or {}
    cfg = Config(
        db_path=Path(paths.get("db", "data/behsard.sqlite")),
        media_dir=Path(paths.get("media", "data/media")),
        timezone=(raw.get("site", {}) or {}).get("timezone", "Asia/Tehran"),
        conveyors=list(raw.get("conveyors", []) or []),
        cameras=list(raw.get("cameras", []) or []),
        recognition=RecognitionConfig(**_only_known(RecognitionConfig, rec)),
        tracking=TrackingConfig(**_only_known(TrackingConfig, trk)),
        live=LiveConfig(**_only_known(LiveConfig, raw.get("live", {}) or {})),
        test_mode=TestModeConfig(
            enabled=bool(tm.get("enabled", False)), stamps=list(tm.get("stamps", []) or [])
        ),
        review_host=review.get("host", "127.0.0.1"),
        review_port=int(review.get("port", 8000)),
        path=p,
        home=home,
    )
    cfg.db_path = cfg.resolve(cfg.db_path)
    cfg.media_dir = cfg.resolve(cfg.media_dir)
    return cfg


def to_dict(cfg: Config) -> dict[str, Any]:
    def rel(p: Path) -> str:
        try:
            return p.relative_to(cfg.home).as_posix()
        except ValueError:
            return str(p)

    trk = asdict(cfg.tracking)
    trk["roi"] = [float(v) for v in cfg.tracking.roi]
    return {
        "paths": {"db": rel(cfg.db_path), "media": rel(cfg.media_dir)},
        "site": {"timezone": cfg.timezone},
        "conveyors": copy.deepcopy(cfg.conveyors),
        "cameras": copy.deepcopy(cfg.cameras),
        "recognition": asdict(cfg.recognition),
        "tracking": trk,
        "live": asdict(cfg.live),
        "test_mode": asdict(cfg.test_mode),
        "review": {"host": cfg.review_host, "port": cfg.review_port},
    }


def save_config(cfg: Config, path: str | Path | None = None) -> Path:
    """Write the config back (atomic replace). Comments are not preserved."""
    p = Path(path or cfg.path or DEFAULT_CONFIG_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    header = "# Behsard configuration (written by the app; edit with care).\n"
    tmp.write_text(
        header + yaml.safe_dump(to_dict(cfg), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    os.replace(tmp, p)
    return p
