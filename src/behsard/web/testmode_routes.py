"""Test mode (known test stamps, live accuracy) and replay of recorded video."""

from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from behsard.config import save_config
from behsard.core.models import ReviewDecision
from behsard.core.repository import Repository
from behsard.core.services import DomainError
from behsard.review.service import ReviewService
from behsard.web.fa import en_digits

router = APIRouter()

REPLAY_ID = "replay"


def _labels(repo: Repository) -> dict[int, str | None]:
    """event id -> supervisor truth (stamp code or None = no stamp)."""
    out: dict[int, str | None] = {}
    for r in repo.list_manual_reviews():
        if r.decision == ReviewDecision.NOT_CARTON:
            out.pop(r.carton_event_id, None)
            continue
        st = repo.get_stamp(r.stamp_id) if r.stamp_id else None
        out[r.carton_event_id] = st.code if st else None
    return out


def test_accuracy(repo: Repository) -> dict[str, Any]:
    labels = _labels(repo)
    evs = [e for e in repo.list_carton_events(mode="test", limit=1_000_000)]
    confirmed = [(e, labels[e.id]) for e in evs if e.id in labels]  # type: ignore[index]
    per: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "correct": 0, "rejected": 0, "wrong": 0})
    tot = {"n": 0, "correct": 0, "rejected": 0, "wrong": 0, "neg_ok": 0, "neg_fa": 0}
    for e, truth in confirmed:
        pred = e.meta.get("stamp_code")
        if truth is None:
            tot["neg_ok" if pred is None else "neg_fa"] += 1
            continue
        row = per[truth]
        row["n"] += 1
        tot["n"] += 1
        key = "correct" if pred == truth else ("rejected" if pred is None else "wrong")
        row[key] += 1
        tot[key] += 1
    accepted = tot["correct"] + tot["wrong"] + tot["neg_fa"]
    return {
        "events": len(evs),
        "confirmed": len(confirmed),
        "unconfirmed": len(evs) - len(confirmed),
        "total": tot,
        "accuracy": tot["correct"] / tot["n"] if tot["n"] else 0.0,
        "rejection": (tot["rejected"] + tot["neg_ok"]) / len(confirmed) if confirmed else 0.0,
        "error": (tot["wrong"] + tot["neg_fa"]) / accepted if accepted else 0.0,
        "per_stamp": sorted(per.items()),
    }


def _next_unconfirmed(repo: Repository, after: int | None = None):  # type: ignore[no-untyped-def]
    labels = _labels(repo)
    reviewed = {r.carton_event_id for r in repo.list_manual_reviews()}
    evs = [e for e in repo.list_carton_events(mode="test", limit=1_000_000) if e.id not in reviewed and e.id not in labels]
    if after is not None:
        later = [e for e in evs if e.id > after]  # type: ignore[operator]
        if later:
            return later[0]
    return evs[0] if evs else None


def _card(request: Request, ev, last: dict | None = None, error: str = "") -> HTMLResponse:  # type: ignore[no-untyped-def]
    repo = request.app.state.repo
    svc = ReviewService(repo)
    acc = test_accuracy(repo)
    ctx = {
        "request": request, "ev": ev, "conveyor": repo.get_conveyor(ev.conveyor_id) if ev else None,
        "candidates": svc.candidates(ev) if ev else [], "extra_frames": [],
        "pending": acc["unconfirmed"], "pending_label": "تأییدنشده (حالت آزمایش)",
        "message": "", "error": error, "last": last, "base": "/test",
        "empty_text": "همه کارتن‌های آزمایشی تأیید شده‌اند. یک کارتن مهرخورده از جلوی دوربین عبور دهید.",
    }
    return request.app.state.templates.TemplateResponse(request, "_review_card.html", ctx)


@router.get("/test", response_class=HTMLResponse)
async def test_page(request: Request) -> HTMLResponse:
    repo = request.app.state.repo
    cfg = request.app.state.cfg
    ev = _next_unconfirmed(repo)
    svc = ReviewService(repo)
    ctx = {
        "request": request, "nav": "test", "tm": cfg.test_mode, "acc": test_accuracy(repo),
        "ev": ev, "conveyor": repo.get_conveyor(ev.conveyor_id) if ev else None,
        "candidates": svc.candidates(ev) if ev else [], "extra_frames": [],
        "pending": test_accuracy(repo)["unconfirmed"], "pending_label": "تأییدنشده (حالت آزمایش)",
        "message": "", "error": "", "last": None, "base": "/test",
        "empty_text": "همه کارتن‌های آزمایشی تأیید شده‌اند. یک کارتن مهرخورده از جلوی دوربین عبور دهید.",
    }
    return request.app.state.templates.TemplateResponse(request, "test.html", ctx)


@router.post("/test/settings")
async def test_settings(request: Request, enabled: str = Form(""), stamps: str = Form("")) -> RedirectResponse:
    cfg = request.app.state.cfg
    codes = [c for c in en_digits(stamps).replace(",", " ").replace("،", " ").split() if c]
    request.app.state.live.set_test_mode(bool(enabled), codes)
    if cfg.path is not None:
        save_config(cfg)
    return RedirectResponse("/test", status_code=303)


@router.get("/test/card", response_class=HTMLResponse)
async def test_card(request: Request, after: int | None = None) -> HTMLResponse:
    return _card(request, _next_unconfirmed(request.app.state.repo, after))


@router.get("/test/accuracy", response_class=HTMLResponse)
async def test_accuracy_partial(request: Request) -> HTMLResponse:
    ctx = {"request": request, "acc": test_accuracy(request.app.state.repo)}
    return request.app.state.templates.TemplateResponse(request, "_test_accuracy.html", ctx)


@router.post("/test/{event_id}/resolve", response_class=HTMLResponse)
async def test_resolve(request: Request, event_id: int, decision: str = Form(...), stamp: str = Form("")) -> HTMLResponse:
    repo = request.app.state.repo
    code = en_digits(stamp).strip().upper()
    try:
        ReviewService(repo).resolve(event_id, ReviewDecision(decision), code or None, reviewer="test-mode")
    except (DomainError, ValueError):
        return _card(request, repo.get_carton_event(event_id), error=f"مهر {code} در فهرست مهرهای آزمایشی نیست.")
    label = code or {"no_stamp": "بدون مهر", "not_carton": "کارتن نیست"}.get(decision, decision)
    return _card(request, _next_unconfirmed(repo, event_id), last={"id": event_id, "label": label, "base": "/test"})


@router.post("/test/{event_id}/reopen", response_class=HTMLResponse)
async def test_reopen(request: Request, event_id: int) -> HTMLResponse:
    # in test mode "undo" = forget the label; the event shows up again
    repo = request.app.state.repo
    with repo.lock:  # type: ignore[attr-defined]
        repo.conn.execute("DELETE FROM manual_review WHERE carton_event_id = ?", (event_id,))  # type: ignore[attr-defined]
        repo.conn.commit()  # type: ignore[attr-defined]
    return _card(request, repo.get_carton_event(event_id))


# ---- replay -------------------------------------------------------------


@router.get("/replay", response_class=HTMLResponse)
async def replay_page(request: Request) -> HTMLResponse:
    live = request.app.state.live
    ctx = {
        "request": request, "nav": "replay", "conveyors": request.app.state.repo.list_conveyors(),
        "running": REPLAY_ID in live.cameras, "health": live.health().get(REPLAY_ID),
        "files": sorted(p.name for p in _replay_dir(request).glob("*") if p.is_file()),
    }
    return request.app.state.templates.TemplateResponse(request, "replay.html", ctx)


def _replay_dir(request: Request) -> Path:
    d = request.app.state.cfg.resolve("data/replays")
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.post("/replay/start")
async def replay_start(
    request: Request,
    path: str = Form(""),
    upload: UploadFile | None = File(None),
    conveyor_id: int = Form(1),
    side: str = Form("L"),
    speed: float = Form(1.0),
    roi: str = Form(""),
) -> RedirectResponse:
    live = request.app.state.live
    src = path.strip()
    if upload is not None and upload.filename:
        dst = _replay_dir(request) / Path(upload.filename).name
        with dst.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        src = str(dst)
    elif src and not Path(src).is_absolute():
        src = str(_replay_dir(request) / src)
    if not src or not Path(src).exists():
        return RedirectResponse("/replay?error=1", status_code=303)
    cam: dict[str, Any] = {
        "id": REPLAY_ID, "url": src, "conveyor_id": conveyor_id, "side": side.upper()[:1] or "L",
        "loop": False, "speed": max(0.25, min(speed, 8.0)),
    }
    if roi:
        cam["roi"] = [float(v) for v in roi.split(",")]
    if not live.started:
        live.start()
    live.start_camera(cam, replay=True)
    return RedirectResponse("/", status_code=303)


@router.post("/replay/stop")
async def replay_stop(request: Request) -> RedirectResponse:
    request.app.state.live.stop_camera(REPLAY_ID)
    return RedirectResponse("/replay", status_code=303)
