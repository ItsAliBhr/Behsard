"""Manual review pages (keyboard-driven)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from behsard.core.models import ReviewDecision
from behsard.core.services import DomainError
from behsard.review.service import ReviewService, reopen
from behsard.web.fa import en_digits

router = APIRouter()


def _svc(request: Request) -> ReviewService:
    return ReviewService(request.app.state.repo)


def _card_context(request: Request, event_id: int | None, after: int | None = None,
                  message: str = "", error: str = "", last: dict | None = None) -> dict:
    svc = _svc(request)
    repo = request.app.state.repo
    ev = repo.get_carton_event(event_id) if event_id is not None else svc.next_pending(after)
    conveyor = repo.get_conveyor(ev.conveyor_id) if ev else None
    extra_frames: list[str] = []
    if ev and ev.frame_path:
        folder = request.app.state.media_dir / ev.frame_path
        stem = f"{ev.id:07d}_frame"
        extra_frames = sorted(
            p.relative_to(request.app.state.media_dir).as_posix()
            for p in folder.parent.glob(f"{stem}*.jpg")
            if p.name != folder.name
        )
    return {
        "request": request,
        "nav": "review",
        "ev": ev,
        "conveyor": conveyor,
        "candidates": svc.candidates(ev) if ev else [],
        "extra_frames": extra_frames,
        "pending": svc.pending_count(),
        "message": message,
        "error": error,
        "last": last,
    }


@router.get("/review", response_class=HTMLResponse)
async def review_page(request: Request, event: int | None = None) -> HTMLResponse:
    ctx = _card_context(request, event)
    return request.app.state.templates.TemplateResponse(request, "review.html", ctx)


@router.get("/review/card", response_class=HTMLResponse)
async def review_card(request: Request, after: int | None = None) -> HTMLResponse:
    ctx = _card_context(request, None, after=after)
    return request.app.state.templates.TemplateResponse(request, "_review_card.html", ctx)


@router.post("/review/{event_id}/resolve", response_class=HTMLResponse)
async def resolve(
    request: Request,
    event_id: int,
    decision: str = Form(...),
    stamp: str = Form(""),
) -> HTMLResponse:
    svc = _svc(request)
    tpl = request.app.state.templates
    code = en_digits(stamp).strip().upper()
    try:
        dec = ReviewDecision(decision)
        ev = svc.resolve(event_id, dec, code or None, reviewer="supervisor")
    except (DomainError, ValueError):
        msg = f"مهر {code} در این شیفت روی این نقاله فعال نیست." if code else "درخواست نامعتبر است."
        ctx = _card_context(request, event_id, error=msg)
        return tpl.TemplateResponse(request, "_review_card.html", ctx, status_code=200)
    label = code if dec == ReviewDecision.STAMP else {"no_stamp": "بدون مهر", "not_carton": "کارتن نیست"}[dec.value]
    last = {"id": ev.id, "label": label}
    ctx = _card_context(request, None, after=event_id, last=last)
    return tpl.TemplateResponse(request, "_review_card.html", ctx)


@router.post("/review/{event_id}/reopen", response_class=HTMLResponse)
async def reopen_event(request: Request, event_id: int) -> HTMLResponse:
    reopen(request.app.state.repo, event_id)
    ctx = _card_context(request, event_id, message="تصمیم قبلی لغو شد")
    return request.app.state.templates.TemplateResponse(request, "_review_card.html", ctx)
