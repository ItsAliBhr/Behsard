"""Run a Recognizer over a labeled folder and report metrics.

A labeled folder holds images and ``labels.json``: a list of
``{"file": str | [str, ...], "stamp": str | null, "active": [str], "meta": {}}``.
Several files in one sample = several views of the same carton.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from behsard.recognition.base import Recognizer

REJECT = "∅"  # predicted label for rejected samples / true label for negatives


@dataclass
class Sample:
    paths: list[Path]
    stamp: str | None
    active: list[str]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.paths[0].name


def load_samples(folder: str | Path) -> list[Sample]:
    root = Path(folder)
    raw = json.loads((root / "labels.json").read_text(encoding="utf-8"))
    out = []
    for item in raw:
        files = item["file"] if isinstance(item["file"], list) else [item["file"]]
        out.append(
            Sample(
                paths=[root / f for f in files],
                stamp=item.get("stamp"),
                active=list(item["active"]),
                meta=dict(item.get("meta", {})),
            )
        )
    return out


def load_images(sample: Sample) -> list[np.ndarray]:
    imgs = []
    for p in sample.paths:
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            raise FileNotFoundError(p)
        imgs.append(img)
    return imgs


@dataclass
class SampleOutcome:
    name: str
    truth: str | None
    pred: str | None
    best_guess: str | None
    confidence: float
    raw_text: str
    seconds: float
    meta: dict[str, Any]

    @property
    def correct(self) -> bool:
        return self.truth == self.pred


@dataclass
class EvalReport:
    recognizer: str
    outcomes: list[SampleOutcome]

    # ---- counts --------------------------------------------------------
    @property
    def n(self) -> int:
        return len(self.outcomes)

    @property
    def positives(self) -> list[SampleOutcome]:
        return [o for o in self.outcomes if o.truth is not None]

    @property
    def negatives(self) -> list[SampleOutcome]:
        return [o for o in self.outcomes if o.truth is None]

    def summary(self) -> dict[str, Any]:
        pos, neg = self.positives, self.negatives
        acc_pos = [o for o in pos if o.pred is not None]
        correct = sum(o.correct for o in acc_pos)
        wrong = len(acc_pos) - correct
        neg_false = sum(o.pred is not None for o in neg)
        accepted = len(acc_pos) + neg_false
        top1 = sum(o.best_guess == o.truth for o in pos)
        return {
            "recognizer": self.recognizer,
            "n": self.n,
            "n_stamped": len(pos),
            "n_unstamped": len(neg),
            # share of stamped cartons auto-credited to the right worker
            "accuracy": _ratio(correct, len(pos)),
            # share of all cartons sent to manual review
            "rejection_rate": _ratio(self.n - accepted, self.n),
            # among auto-accepted cartons, share credited to the wrong stamp
            "error_rate": _ratio(wrong + neg_false, accepted),
            # top candidate correct, ignoring the reject decision
            "top1_accuracy": _ratio(top1, len(pos)),
            "unstamped_false_accepts": neg_false,
            "mean_seconds": _ratio(sum(o.seconds for o in self.outcomes), self.n),
        }

    def confusion(self) -> Counter[tuple[str, str]]:
        return Counter((o.truth or REJECT, o.pred or REJECT) for o in self.outcomes)

    def breakdown(self, key: str) -> dict[str, dict[str, float]]:
        """Accuracy / rejection per value of ``meta[key]`` (stamped samples)."""
        groups: dict[str, list[SampleOutcome]] = defaultdict(list)
        for o in self.positives:
            groups[str(o.meta.get(key, "-"))].append(o)
        return {
            k: {
                "n": len(v),
                "accuracy": _ratio(sum(o.correct for o in v), len(v)),
                "rejection_rate": _ratio(sum(o.pred is None for o in v), len(v)),
                "errors": sum(o.pred is not None and not o.correct for o in v),
            }
            for k, v in sorted(groups.items())
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "confusion": [
                {"truth": t, "pred": p, "n": n} for (t, p), n in sorted(self.confusion().items())
            ],
            "outcomes": [o.__dict__ for o in self.outcomes],
        }


def _ratio(a: float, b: float) -> float:
    return round(a / b, 4) if b else 0.0


def evaluate(recognizer: Recognizer, samples: list[Sample]) -> EvalReport:
    outcomes = []
    for s in samples:
        imgs = load_images(s)
        t0 = time.perf_counter()
        r = recognizer.recognize(imgs, s.active)
        dt = time.perf_counter() - t0
        outcomes.append(
            SampleOutcome(
                name=s.name,
                truth=s.stamp,
                pred=r.stamp,
                best_guess=r.best_guess if r.best_guess is not None else r.stamp,
                confidence=round(float(r.confidence), 4),
                raw_text=r.raw_text,
                seconds=round(dt, 4),
                meta=s.meta,
            )
        )
    return EvalReport(recognizer.name, outcomes)


def format_report(report: EvalReport, breakdown_keys: tuple[str, ...] = ()) -> str:
    s = report.summary()
    lines = [
        f"recognizer: {s['recognizer']}",
        f"samples: {s['n']} (stamped {s['n_stamped']}, unstamped {s['n_unstamped']})",
        f"accuracy (auto-credited correctly / stamped): {s['accuracy']:.1%}",
        f"rejection rate (to manual review / all):      {s['rejection_rate']:.1%}",
        f"error rate (wrong / auto-accepted):           {s['error_rate']:.1%}",
        f"top-1 accuracy (best guess, no reject):       {s['top1_accuracy']:.1%}",
        f"unstamped false accepts: {s['unstamped_false_accepts']}",
        f"mean time per sample: {s['mean_seconds'] * 1000:.0f} ms",
    ]
    for key in breakdown_keys:
        lines.append(f"\nby {key}:")
        for k, v in report.breakdown(key).items():
            lines.append(
                f"  {k:<14} n={v['n']:<4} acc={v['accuracy']:.1%} "
                f"rej={v['rejection_rate']:.1%} err={v['errors']}"
            )
    # confusion: only the off-diagonal entries (diagonal is the accuracy)
    conf = report.confusion()
    off = sorted(((n, t, p) for (t, p), n in conf.items() if t != p), reverse=True)
    lines.append("\nconfusion (truth -> pred, off-diagonal, top 20):")
    if not off:
        lines.append("  (none)")
    for n, t, p in off[:20]:
        lines.append(f"  {t:>4} -> {p:<4} {n}")
    return "\n".join(lines)
