"""Shift admin: open/close shifts and assign worker <-> stamp <-> conveyor."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from behsard.core.services import DomainError, ShiftService
from behsard.web.fa import en_digits

router = APIRouter()

ERRORS_FA = {
    "already assigned in this shift": "این مهر در همین شیفت به کارگر دیگری داده شده است.",
    "worker already assigned": "این کارگر در همین شیفت قبلاً مهر گرفته است.",
    "invalid stamp code": "کد مهر باید یک حرف انگلیسی و دو رقم باشد (مثل A12).",
    "closed shift": "شیفت بسته شده است.",
    "unknown conveyor": "نقاله نامعتبر است.",
    "already closed": "شیفت قبلاً بسته شده است.",
    "end must be after": "زمان پایان شیفت باید بعد از شروع آن باشد.",
}


def _fa_error(e: Exception) -> str:
    msg = str(e)
    for k, v in ERRORS_FA.items():
        if k in msg:
            return v
    return msg


def _ctx(request: Request, **kw: object) -> dict[str, object]:
    return {"request": request, "nav": "shifts", **kw}


@router.get("/shifts", response_class=HTMLResponse)
async def shifts_page(request: Request, error: str = "") -> HTMLResponse:
    repo = request.app.state.repo
    shifts = repo.list_shifts(limit=60)
    counts = {s.id: len(repo.list_assignments(s.id)) for s in shifts}
    return request.app.state.templates.TemplateResponse(
        request, "shifts.html", _ctx(request, shifts=shifts, counts=counts, error=error, now=datetime.now())
    )


@router.post("/shifts/open")
async def open_shift(request: Request, name: str = Form(...), copy_from: str = Form("")) -> RedirectResponse:
    svc = ShiftService(request.app.state.repo)
    shift = svc.open_shift(name.strip() or "شیفت")
    if copy_from.strip():
        svc.copy_assignments(int(copy_from), shift.id)  # type: ignore[arg-type]
    return RedirectResponse(f"/shifts/{shift.id}", status_code=303)


@router.post("/shifts/{shift_id}/close")
async def close_shift(request: Request, shift_id: int) -> RedirectResponse:
    try:
        ShiftService(request.app.state.repo).close_shift(shift_id)
    except DomainError as e:
        return RedirectResponse(f"/shifts?error={_fa_error(e)}", status_code=303)
    return RedirectResponse("/shifts", status_code=303)


def _shift_ctx(request: Request, shift_id: int, error: str = "", message: str = "") -> dict[str, object]:
    repo = request.app.state.repo
    shift = repo.get_shift(shift_id)
    assigns = repo.list_assignments(shift_id) if shift else []
    workers = {w.id: w for w in repo.list_workers()}
    stamps = {s.id: s for s in repo.list_stamps()}
    conveyors = repo.list_conveyors()
    conv_by_id = {c.id: c for c in conveyors}
    rows = [
        {
            "id": a.id,
            "worker": workers[a.worker_id].name,
            "stamp": stamps[a.stamp_id].code,
            "conveyor": conv_by_id[a.conveyor_id].name if a.conveyor_id in conv_by_id else a.conveyor_id,
            "conveyor_id": a.conveyor_id,
        }
        for a in assigns
    ]
    return _ctx(
        request,
        shift=shift,
        rows=rows,
        conveyors=conveyors,
        workers=sorted(workers.values(), key=lambda w: w.name),
        error=error,
        message=message,
        prev_shifts=[s for s in repo.list_shifts(limit=20) if s.id != shift_id],
    )


@router.get("/shifts/{shift_id}", response_class=HTMLResponse)
async def shift_page(request: Request, shift_id: int) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "shift.html", _shift_ctx(request, shift_id)
    )


@router.post("/shifts/{shift_id}/assign", response_class=HTMLResponse)
async def assign(
    request: Request,
    shift_id: int,
    worker: str = Form(...),
    stamp: str = Form(...),
    conveyor: int = Form(...),
) -> HTMLResponse:
    svc = ShiftService(request.app.state.repo)
    error = message = ""
    try:
        name = worker.strip()
        if not name:
            raise DomainError("نام کارگر خالی است.")
        w = svc.ensure_worker(name)
        svc.assign(shift_id, w.id, en_digits(stamp), conveyor)  # type: ignore[arg-type]
        message = f"{name} ← {en_digits(stamp).upper()}"
    except DomainError as e:
        error = _fa_error(e)
    ctx = _shift_ctx(request, shift_id, error=error, message=message)
    ctx["last_conveyor"] = conveyor
    if error:  # keep what was typed so it can be corrected
        ctx["form_worker"], ctx["form_stamp"] = worker, stamp
    return request.app.state.templates.TemplateResponse(request, "_assignments.html", ctx)


@router.post("/shifts/{shift_id}/unassign/{assignment_id}", response_class=HTMLResponse)
async def unassign(request: Request, shift_id: int, assignment_id: int) -> HTMLResponse:
    ShiftService(request.app.state.repo).unassign(assignment_id)
    return request.app.state.templates.TemplateResponse(
        request, "_assignments.html", _shift_ctx(request, shift_id)
    )


@router.post("/shifts/{shift_id}/copy", response_class=HTMLResponse)
async def copy(request: Request, shift_id: int, from_shift: int = Form(...)) -> HTMLResponse:
    n = ShiftService(request.app.state.repo).copy_assignments(from_shift, shift_id)
    ctx = _shift_ctx(request, shift_id, message=f"{n} تخصیص کپی شد")
    return request.app.state.templates.TemplateResponse(request, "_assignments.html", ctx)
