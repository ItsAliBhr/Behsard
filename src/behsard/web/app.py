"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from behsard.config import Config, load_config
from behsard.core.models import CartonStatus
from behsard.core.repository import Repository, SqliteRepository
from behsard.core.services import sync_config
from behsard.web.fa import STATUS_FA, fa_digits, jalali_date, jalali_datetime

HERE = Path(__file__).parent
log = logging.getLogger(__name__)


def make_templates(repo: Repository | None = None) -> Jinja2Templates:
    t = Jinja2Templates(directory=str(HERE / "templates"))
    t.env.filters["fa"] = fa_digits
    t.env.filters["jdate"] = jalali_date
    t.env.filters["jdt"] = jalali_datetime
    t.env.filters["status_fa"] = lambda s: STATUS_FA.get(str(s), str(s))
    if repo is not None:
        t.env.globals["pending_badge"] = lambda: repo.count_by_status().get(
            str(CartonStatus.PENDING_REVIEW), 0
        )
    else:
        t.env.globals["pending_badge"] = lambda: 0
    return t


def create_app(
    cfg: Config | None = None,
    repo: Repository | None = None,
    media_dir: str | Path | None = None,
    live: object | None = None,
    start_live: bool | None = None,
) -> FastAPI:
    """Build the web app.

    Route handlers are ``async`` (event loop thread); the live capture
    threads share the repository, which serialises access with a lock.
    The live service starts with the server (lifespan) when
    ``cfg.live.enabled`` (or ``start_live``) is true.
    """
    cfg = cfg or load_config()
    if repo is None:
        repo = SqliteRepository(cfg.db_path)
    sync_config(repo, cfg)
    media = Path(media_dir or cfg.media_dir)
    media.mkdir(parents=True, exist_ok=True)
    cfg.media_dir = media

    if live is None:
        from behsard.live.service import LiveService
        from behsard.recognition.registry import get_recognizer

        live = LiveService(cfg, repo, get_recognizer("stamp", cfg.recognition))
    run_live = cfg.live.enabled if start_live is None else start_live

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if run_live:
            live.start()  # type: ignore[attr-defined]
        try:
            yield
        finally:
            if getattr(live, "started", False):
                live.stop()  # type: ignore[attr-defined]

    app = FastAPI(title="Behsard", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.repo = repo
    app.state.cfg = cfg
    app.state.media_dir = media
    app.state.live = live
    app.state.templates = make_templates(repo)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.mount("/media", StaticFiles(directory=str(media)), name="media")

    from behsard.web import (
        admin_routes,
        advanced_routes,
        camera_routes,
        live_routes,
        report_routes,
        review_routes,
        testmode_routes,
    )

    for m in (live_routes, review_routes, admin_routes, report_routes, camera_routes, testmode_routes, advanced_routes):
        app.include_router(m.router)
    return app
