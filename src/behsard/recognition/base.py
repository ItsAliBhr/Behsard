"""Recognizer interface.

A recognizer looks at one or more images of the same physical carton and
picks one of the ``active_stamps`` (closed set) or returns ``None``.
Everything downstream (counting, review, reports) only sees ``Result``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass
class Result:
    stamp: str | None  # chosen active stamp code, None = rejected / not found
    confidence: float  # 0..1
    crop: np.ndarray | None = None  # best stamp crop (BGR) for review UI
    raw_text: str = ""  # raw OCR text of the best reading
    scores: dict[str, float] = field(default_factory=dict)  # per-stamp score
    best_guess: str | None = None  # top candidate even when rejected
    image_index: int | None = None  # which input image the crop came from


@runtime_checkable
class Recognizer(Protocol):
    name: str

    def recognize(self, images: Sequence[np.ndarray], active_stamps: Sequence[str]) -> Result:
        """Identify the stamp in ``images`` (BGR uint8) among ``active_stamps``."""
        ...
