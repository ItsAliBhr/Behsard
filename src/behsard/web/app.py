"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from behsard.config import Config, load_config
from behsard.core.repository import Repository, SqliteRepository
from behsard.core.services import sync_config
from behsard.web.fa import STATUS_FA, fa_digits, jalali_date, jalali_datetime

HERE = Path(__file__).parent


def make_templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=str(HERE / "templates"))
    t.env.filters["fa"] = fa_digits
    t.env.filters["jdate"] = jalali_date
    t.env.filters["jdt"] = jalali_datetime
    t.env.filters["status_fa"] = lambda s: STATUS_FA.get(str(s), str(s))
    return t


def create_app(
    cfg: Config | None = None,
    repo: Repository | None = None,
    media_dir: str | Path | None = None,
) -> FastAPI:
    """Build the web app.

    All route handlers are ``async`` so they run on the event loop thread and
    share the single SQLite connection safely (one supervisor, fast queries).
    """
    cfg = cfg or load_config()
    if repo is None:
        repo = SqliteRepository(cfg.db_path)
        sync_config(repo, cfg)
    media = Path(media_dir or cfg.media_dir)
    media.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="Behsard", docs_url=None, redoc_url=None)
    app.state.repo = repo
    app.state.cfg = cfg
    app.state.media_dir = media
    app.state.templates = make_templates()
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.mount("/media", StaticFiles(directory=str(media)), name="media")

    from behsard.web import review_routes

    app.include_router(review_routes.router)
    for mod in ("behsard.web.admin_routes", "behsard.web.report_routes"):
        try:
            m = __import__(mod, fromlist=["router"])
        except ModuleNotFoundError as e:
            if e.name != mod:
                raise
            continue
        app.include_router(m.router)

    @app.get("/")
    async def home(request: Request) -> RedirectResponse:
        return RedirectResponse("/review", status_code=303)

    return app
