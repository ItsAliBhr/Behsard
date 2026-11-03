"""CLI commands: gen-synth, eval."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from behsard.cli import command
from behsard.config import load_config


def _gen_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", required=True)
    p.add_argument("--n", type=int, default=300)
    p.add_argument("--seed", type=int, default=0)


@command("gen-synth", _gen_args)
def gen_synth(args: argparse.Namespace) -> int:
    from behsard.eval.synth import generate_dataset

    labels = generate_dataset(args.out, args.n, seed=args.seed)
    print(f"wrote {len(labels)} samples to {args.out}")
    return 0


def _eval_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data", required=True, action="append", help="labeled folder (repeatable)")
    p.add_argument("--recognizer", default="stamp")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--json", default=None, help="write full report JSON here")
    p.add_argument("--by", action="append", default=[], help="meta key breakdown")


@command("eval", _eval_args)
def run_eval(args: argparse.Namespace) -> int:
    from behsard.eval.harness import evaluate, format_report, load_samples
    from behsard.recognition.registry import get_recognizer

    cfg = load_config(args.config)
    rec = get_recognizer(args.recognizer, cfg.recognition)
    for folder in args.data:
        samples = load_samples(folder)
        if args.limit:
            samples = samples[: args.limit]
        report = evaluate(rec, samples)
        print(f"=== {folder}")
        print(format_report(report, tuple(args.by)))
        if args.json:
            out = Path(args.json)
            if len(args.data) > 1:
                out = out.with_name(f"{out.stem}_{Path(folder).name}{out.suffix}")
            out.write_text(json.dumps(report.to_dict(), indent=1, ensure_ascii=False))
    return 0
