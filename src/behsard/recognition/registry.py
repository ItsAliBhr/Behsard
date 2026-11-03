"""Build recognizers by name (for CLI / config)."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np

from behsard.config import RecognitionConfig
from behsard.recognition.base import Recognizer, Result


class RejectAllRecognizer:
    """Baseline: sends every carton to manual review."""

    name = "reject-all"

    def recognize(self, images: Sequence[np.ndarray], active_stamps: Sequence[str]) -> Result:
        return Result(stamp=None, confidence=0.0)


_FACTORIES: dict[str, Callable[[RecognitionConfig], Recognizer]] = {
    "reject-all": lambda cfg: RejectAllRecognizer(),
}


def register(name: str, factory: Callable[[RecognitionConfig], Recognizer]) -> None:
    _FACTORIES[name] = factory


def get_recognizer(name: str, cfg: RecognitionConfig | None = None) -> Recognizer:
    _load_builtin()
    if name not in _FACTORIES:
        raise KeyError(f"unknown recognizer {name!r}; known: {sorted(_FACTORIES)}")
    return _FACTORIES[name](cfg or RecognitionConfig())


def names() -> list[str]:
    _load_builtin()
    return sorted(_FACTORIES)


def _load_builtin() -> None:
    try:
        import behsard.recognition.stamp  # noqa: F401  (registers "stamp")
    except ModuleNotFoundError as e:
        if e.name != "behsard.recognition.stamp":
            raise
