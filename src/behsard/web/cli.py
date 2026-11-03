"""CLI: serve (web UI), export-labels."""

from __future__ import annotations

import argparse

from behsard.cli import command
from behsard.config import load_config


def _serve_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--open", action="store_true", help="open the browser when ready")
    p.add_argument("--no-live", action="store_true", help="do not start cameras")


@command("serve", _serve_args)
def serve(args: argparse.Namespace) -> int:
    import threading

    import uvicorn

    from behsard.launcher import _open_when_ready
    from behsard.web.app import create_app

    cfg = load_config(args.config)
    host, port = args.host or cfg.review_host, args.port or cfg.review_port
    if args.open:
        threading.Thread(target=_open_when_ready, args=(f"http://127.0.0.1:{port}",), daemon=True).start()
    uvicorn.run(create_app(cfg, start_live=not args.no_live and cfg.live.enabled), host=host, port=port)
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
