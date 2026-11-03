"""Command-line entry point: ``behsard <command>``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from behsard.config import load_config

Handler = Callable[[argparse.Namespace], int]
_COMMANDS: dict[str, tuple[Callable[[argparse.ArgumentParser], None], Handler]] = {}


def command(name: str, setup: Callable[[argparse.ArgumentParser], None]) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _COMMANDS[name] = (setup, fn)
        return fn

    return deco


def _no_args(p: argparse.ArgumentParser) -> None:
    pass


@command("init-db", _no_args)
def _init_db(args: argparse.Namespace) -> int:
    from behsard.core.repository import SqliteRepository
    from behsard.core.services import sync_config

    cfg = load_config(args.config)
    repo = SqliteRepository(cfg.db_path)
    sync_config(repo, cfg)
    print(f"database ready: {cfg.db_path}")
    return 0


def _register_plugins() -> None:
    # Import modules that register their own commands.
    for mod in ("behsard.eval.cli", "behsard.tracking.cli", "behsard.web.cli"):
        try:
            __import__(mod)
        except ModuleNotFoundError as e:  # module not implemented yet
            if e.name != mod:
                raise


def main(argv: list[str] | None = None) -> int:
    _register_plugins()
    parser = argparse.ArgumentParser(prog="behsard")
    parser.add_argument("--config", default=None, help="path to YAML config")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, (setup, _) in sorted(_COMMANDS.items()):
        setup(sub.add_parser(name))
    args = parser.parse_args(argv)
    return _COMMANDS[args.cmd][1](args)


if __name__ == "__main__":
    sys.exit(main())
