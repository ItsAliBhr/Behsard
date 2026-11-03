"""CLI: serve (web UI), export-labels."""

from __future__ import annotations

import argparse

from behsard.cli import command
from behsard.config import load_config


def _serve_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)


@command("serve", _serve_args)
def serve(args: argparse.Namespace) -> int:
    import uvicorn

    from behsard.web.app import create_app

    cfg = load_config(args.config)
    uvicorn.run(create_app(cfg), host=args.host or cfg.review_host, port=args.port or cfg.review_port)
    return 0


def _export_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", required=True)


@command("export-labels", _export_args)
def export(args: argparse.Namespace) -> int:
    from behsard.core.repository import SqliteRepository
    from behsard.review.service import export_labels

    cfg = load_config(args.config)
    n = export_labels(SqliteRepository(cfg.db_path), cfg.media_dir, args.out)
    print(f"exported {n} reviewed cartons to {args.out}")
    return 0
