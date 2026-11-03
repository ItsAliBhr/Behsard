"""Dashboard (live view), MJPEG streams, health and JSON status."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from behsard.core.models import CartonStatus

router = APIRouter()

BOUNDARY = "frame"


def shift_counts(request: Request) -> dict[str, Any]:
    """Live counters for the open shift(s): per stamp/worker, plus pending."""
    repo = request.app.state.repo
    from behsard.core.services import ShiftService

    shifts = ShiftService(repo).open_shifts()
    rows: Counter[tuple[str, str, str]] = Counter()
    pending = 0
    total = 0
    workers = {w.id: w.name for w in repo.list_workers()}
    conveyors = {c.id: c.name for c in repo.list_conveyors()}
    for sh in shifts:
        assign = {a.stamp_id: a for a in repo.list_assignments(sh.id)}
        for ev in repo.list_carton_events(shift_id=sh.id, limit=1_000_000, mode="prod"):
            if ev.status.counts and ev.stamp_id is not None:
                a = assign.get(ev.stamp_id)
                st = repo.get_stamp(ev.stamp_id)
                rows[(
                    st.code if st else "?",
                    workers.get(a.worker_id, "?") if a else "?",
                    conveyors.get(ev.conveyor_id, str(ev.conveyor_id)),
                )] += 1
                total += 1
            elif ev.status == CartonStatus.PENDING_REVIEW:
                pending += 1
    return {
        "shifts": shifts,
        "rows": sorted(rows.items(), key=lambda kv: (-kv[1], kv[0])),
        "total": total,
        "pending": pending,
    }


def status_payload(request: Request) -> dict[str, Any]:
    live = request.app.state.live
    return {
        "status": "ok",
        "time": datetime.now().isoformat(timespec="seconds"),
        "live_running": bool(getattr(live, "started", False)),
        "cameras": live.health(),
        "recognition_queue": live.pending_recognition(),
        "session": {
            "cartons": live.session.cartons,
            "by_stamp": dict(live.session.by_stamp),
            "pending": live.session.pending,
        },
        "test_mode": {"enabled": request.app.state.cfg.test_mode.enabled, "stamps": request.app.state.cfg.test_mode.stamps},
    }


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """Liveness check for the launcher and CI: DB reachable and app up."""
    request.app.state.repo.count_by_status()
    return JSONResponse({"status": "ok"})


@router.get("/api/status")
async def api_status(request: Request) -> JSONResponse:
    return JSONResponse(status_payload(request))


@router.get("/api/events")
async def api_events(request: Request, after: int = 0, limit: int = 200) -> JSONResponse:
    evs = request.app.state.repo.list_carton_events(after_id=after, limit=limit)
    return JSONResponse(
        [
            {
                "id": e.id, "ts": e.ts.isoformat(), "conveyor": e.conveyor_id, "status": str(e.status),
                "mode": e.mode, "stamp": e.meta.get("stamp_code"), "best_guess": e.meta.get("best_guess"),
                "confidence": e.confidence, "sides": e.meta.get("sides"),
            }
            for e in evs
        ]
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    ctx = {"request": request, "nav": "dashboard", **status_payload(request), **shift_counts(request)}
    ctx["recent"] = list(request.app.state.live.recent)[:15]
    return request.app.state.templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/dashboard/panel", response_class=HTMLResponse)
async def dashboard_panel(request: Request) -> HTMLResponse:
    """Polled by HTMX every 2 s: counters, health, recent cartons."""
    ctx = {"request": request, **status_payload(request), **shift_counts(request)}
    ctx["recent"] = list(request.app.state.live.recent)[:15]
    return request.app.state.templates.TemplateResponse(request, "_dashboard_panel.html", ctx)


@router.get("/live/{camera_id}.jpg")
async def live_jpeg(request: Request, camera_id: str) -> Response:
    lc = request.app.state.live.cameras.get(camera_id)
    if lc is None:
        return Response(status_code=404)
    width = request.app.state.cfg.live.preview_width
    data = await asyncio.to_thread(lc.worker.render, width)
    if data is None:
        return Response(status_code=204)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/live/{camera_id}.mjpg")
async def live_mjpeg(request: Request, camera_id: str, fps: float = 4.0) -> Response:
    live = request.app.state.live
    if camera_id not in live.cameras:
        return Response(status_code=404)
    width = request.app.state.cfg.live.preview_width
    period = 1.0 / max(0.5, min(fps, 10.0))

    async def stream():  # type: ignore[no-untyped-def]
        while True:
            if await request.is_disconnected():
                break
            lc = live.cameras.get(camera_id)
            if lc is None:
                break
            data = await asyncio.to_thread(lc.worker.render, width)
            if data:
                yield (
                    f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(data)}\r\n\r\n"
                ).encode() + data + b"\r\n"
            await asyncio.sleep(period)

    return StreamingResponse(stream(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}")
