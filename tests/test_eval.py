from __future__ import annotations

import json
from collections.abc import Sequence

import numpy as np

from behsard.cli import main
from behsard.eval.harness import REJECT, evaluate, format_report, load_samples
from behsard.eval.synth import SynthConfig, confusers, generate_dataset
from behsard.recognition.base import Recognizer, Result
from behsard.recognition.registry import get_recognizer

from conftest import FIXTURES


class LabelOracle:
    """Test double: answers from a dict, rejects otherwise."""

    name = "oracle"

    def __init__(self, answers: dict[int, str | None]) -> None:
        self.answers = answers

    def recognize(self, images: Sequence[np.ndarray], active_stamps: Sequence[str]) -> Result:
        key = int(images[0].sum()) % 1_000_003
        code = self.answers.get(key)
        return Result(code, 1.0 if code else 0.0, best_guess=code)


def test_generate_dataset_writes_images_and_labels(tmp_path) -> None:
    labels = generate_dataset(tmp_path, 12, seed=3, cfg=SynthConfig(p_negative=0.25))
    on_disk = json.loads((tmp_path / "labels.json").read_text())
    assert on_disk == labels and len(labels) == 12
    for item in labels:
        assert (tmp_path / item["file"]).exists()
        if item["stamp"] is not None:
            assert item["stamp"] in item["active"]
            assert 12 <= len(item["active"]) <= 16
            assert item["meta"]["background"] in ("kraft", "kraft_print", "white_red")
    # deterministic for a seed
    again = generate_dataset(tmp_path / "again", 12, seed=3, cfg=SynthConfig(p_negative=0.25))
    assert [l["stamp"] for l in again] == [l["stamp"] for l in labels]


def test_confusers_differ_by_one_char() -> None:
    rng = np.random.default_rng(0)
    for c in confusers("A12", rng, 3):
        assert c != "A12" and sum(a != b for a, b in zip(c, "A12")) == 1


def test_harness_metrics(tmp_path) -> None:
    generate_dataset(tmp_path, 10, seed=5, cfg=SynthConfig(p_negative=0.3))
    samples = load_samples(tmp_path)
    import cv2

    answers: dict[int, str | None] = {}
    pos = [s for s in samples if s.stamp]
    neg = [s for s in samples if not s.stamp]
    assert pos and neg
    # first stamped sample: wrong answer; second: rejected; rest: correct
    for i, s in enumerate(samples):
        key = int(cv2.imread(str(s.paths[0])).sum()) % 1_000_003
        if s is pos[0]:
            answers[key] = next(a for a in s.active if a != s.stamp)
        elif s is pos[1]:
            answers[key] = None
        else:
            answers[key] = s.stamp
    rec: Recognizer = LabelOracle(answers)
    report = evaluate(rec, samples)
    summ = report.summary()
    assert summ["n"] == 10
    assert summ["accuracy"] == round((len(pos) - 2) / len(pos), 4)
    assert summ["rejection_rate"] == round((1 + len(neg)) / 10, 4)
    assert summ["error_rate"] == round(1 / (len(pos) - 1), 4)
    conf = report.confusion()
    assert conf[(REJECT, REJECT)] == len(neg)
    assert conf[(pos[1].stamp, REJECT)] == 1
    text = format_report(report, ("background",))
    assert "accuracy" in text and "by background" in text


def test_real_fixture_labels_load() -> None:
    samples = load_samples(FIXTURES / "images")
    assert len(samples) == 4 and all(s.stamp == "A12" for s in samples)
    assert all(p.exists() for s in samples for p in s.paths)


def test_cli_gen_and_eval_reject_all(tmp_path, capsys) -> None:
    out = tmp_path / "synth"
    assert main(["gen-synth", "--out", str(out), "--n", "5", "--seed", "1"]) == 0
    rj = tmp_path / "r.json"
    assert main(["eval", "--data", str(out), "--recognizer", "reject-all", "--json", str(rj)]) == 0
    assert "rejection rate" in capsys.readouterr().out
    assert json.loads(rj.read_text())["summary"]["accuracy"] == 0.0
    assert get_recognizer("reject-all").name == "reject-all"
