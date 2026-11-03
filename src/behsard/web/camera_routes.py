"""Camera setup wizard: add/edit cameras, test connection, ROI, quality checks."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import cv2
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from behsard.config import save_config
from behsard.core.models import Camera
from behsard.live.quality import check_image, motion_check
from behsard.live.reader import grab_snapshot, redact
from behsard.live.service import camera_url

router = APIRouter()

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _cfg(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.cfg


def _snap_path(request: Request, cid: str):  # type: ignore[no-untyped-def]
    d = request.app.state.media_dir / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{cid}.jpg"


def _public(cam: dict[str, Any]) -> dict[str, Any]:
    """Camera entry safe to render: password never sent back to the browser."""
    out = {k: v for k, v in cam.items() if k != "password"}
    out["has_password"] = bool(cam.get("password"))
    out["display_url"] = redact(camera_url(cam))
    return out


def _persist(request: Request) -> None:
    cfg = _cfg(request)
    if cfg.path is not None:
        save_config(cfg)
    repo = request.app.state.repo
    for c in cfg.cameras:
        repo.upsert_camera(Camera(str(c["id"]), int(c["conveyor_id"]), str(c["side"]), c.get("host")))


def _restart(request: Request, cam: dict[str, Any]) -> None:
    live = request.app.state.live
    if getattr(live, "started", False):
        if cam.get("enabled", True):
            live.start_camera(cam)
        else:
            live.stop_camera(str(cam["id"]))


@router.get("/cameras", response_class=HTMLResponse)
async def cameras_page(request: Request, error: str = "") -> HTMLResponse:
    cfg = _cfg(request)
    health = request.app.state.live.health()
    ctx = {
        "request": request, "nav": "cameras", "error": error,
        "cameras": [_public(c) for c in cfg.cameras], "health": health,
        "conveyors": request.app.state.repo.list_conveyors(),
    }
    return request.app.state.templates.TemplateResponse(request, "cameras.html", ctx)


@router.get("/cameras/new", response_class=HTMLResponse)
@router.get("/cameras/{camera_id}/edit", response_class=HTMLResponse)
async def camera_form(request: Request, camera_id: str | None = None) -> HTMLResponse:
    cam = _cfg(request).camera(camera_id) if camera_id else None
    ctx = {
        "request": request, "nav": "cameras", "cam": _public(cam) if cam else None,
        "conveyors": request.app.state.repo.list_conveyors(), "error": "",
    }
    return request.app.state.templates.TemplateResponse(request, "camera_form.html", ctx)


@router.post("/cameras/save")
async def camera_save(
    request: Request,
    id: str = Form(...),
    conveyor_id: int = Form(...),
    side: str = Form(...),
    source: str = Form("hikvision"),
    host: str = Form(""),
    user: str = Form(""),
    password: str = Form(""),
    channel: str = Form("101"),
    url: str = Form(""),
    record: str = Form(""),
    enabled: str = Form("on"),
) -> RedirectResponse:
    cfg = _cfg(request)
    cid = id.strip()
    if not ID_RE.match(cid):
        return RedirectResponse("/cameras?error=شناسه دوربین فقط حروف انگلیسی، عدد، - و _", status_code=303)
    cam = cfg.camera(cid)
    if cam is None:
        cam = {"id": cid}
        cfg.cameras.append(cam)
    cam.update({"conveyor_id": conveyor_id, "side": side.upper()[:1] or "L",
                "record": bool(record), "enabled": bool(enabled)})
    if source == "url":
        cam["url"] = url.strip()
        for k in ("host", "user", "password", "channel"):
            cam.pop(k, None)
    else:
        cam.pop("url", None)
        cam.update({"host": host.strip(), "user": user.strip(), "channel": channel.strip() or "101"})
        if password:  # empty = keep the stored password
            cam["password"] = password
    _persist(request)
    _restart(request, cam)
    return RedirectResponse(f"/cameras/{cid}/setup", status_code=303)


@router.post("/cameras/{camera_id}/delete")
async def camera_delete(request: Request, camera_id: str) -> RedirectResponse:
    cfg = _cfg(request)
    cfg.cameras = [c for c in cfg.cameras if str(c.get("id")) != camera_id]
    request.app.state.live.stop_camera(camera_id)
    _persist(request)
    return RedirectResponse("/cameras", status_code=303)


@router.get("/cameras/{camera_id}/setup", response_class=HTMLResponse)
async def camera_setup(request: Request, camera_id: str) -> HTMLResponse:
    cam = _cfg(request).camera(camera_id)
    if cam is None:
        return RedirectResponse("/cameras", status_code=303)  # type: ignore[return-value]
    snap = _snap_path(request, camera_id)
    ctx = {
        "request": request, "nav": "cameras", "cam": _public(cam),
        "roi": cam.get("roi") or list(_cfg(request).tracking.roi),
        "has_snapshot": snap.exists(), "snap_v": int(snap.stat().st_mtime) if snap.exists() else 0,
    }
    return request.app.state.templates.TemplateResponse(request, "camera_setup.html", ctx)


@router.post("/cameras/{camera_id}/test", response_class=HTMLResponse)
async def camera_test(request: Request, camera_id: str) -> HTMLResponse:
    """Test connection: grab a snapshot (live frame if running) + quality checks."""
    cfg = _cfg(request)
    cam = cfg.camera(camera_id)
    tpl = request.app.state.templates
    if cam is None:
        return HTMLResponse("دوربین پیدا نشد", status_code=404)
    lc = request.app.state.live.cameras.get(camera_id)
    frame = lc.reader.latest if lc is not None else None
    if frame is None:
        frame = await asyncio.to_thread(grab_snapshot, camera_url(cam))
    if frame is None:
        ctx = {"request": request, "ok": False, "cam": _public(cam), "report": None}
        return tpl.TemplateResponse(request, "_camera_test.html", ctx)
    snap = _snap_path(request, camera_id)
    cv2.imwrite(str(snap), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    roi = tuple(cam.get("roi") or cfg.tracking.roi)
    report = await asyncio.to_thread(check_image, frame, roi)
    ctx = {
        "request": request, "ok": True, "cam": _public(cam), "report": report,
        "resolution": f"{frame.shape[1]}×{frame.shape[0]}", "snap_v": int(snap.stat().st_mtime_ns),
    }
    return tpl.TemplateResponse(request, "_camera_test.html", ctx)


@router.post("/cameras/{camera_id}/roi")
async def camera_roi(
    request: Request,
    camera_id: str,
    x: float = Form(...),
    y: float = Form(...),
    w: float = Form(...),
    h: float = Form(...),
    direction: str = Form("auto"),
) -> RedirectResponse:
    cam = _cfg(request).camera(camera_id)
    if cam is None:
        return RedirectResponse("/cameras", status_code=303)
    x, y = min(max(x, 0.0), 0.95), min(max(y, 0.0), 0.95)
    w, h = min(max(w, 0.05), 1.0 - x), min(max(h, 0.05), 1.0 - y)
    cam["roi"] = [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]
    cam["direction"] = direction if direction in ("ltr", "rtl", "auto") else "auto"
    _persist(request)
    _restart(request, cam)
    return RedirectResponse(f"/cameras/{camera_id}/setup?saved=1", status_code=303)


@router.get("/cameras/{camera_id}/motion", response_class=HTMLResponse)
async def camera_motion(request: Request, camera_id: str) -> HTMLResponse:
    lc = request.app.state.live.cameras.get(camera_id)
    check = motion_check(list(lc.worker.blur_scores) if lc else [])
    ctx = {"request": request, "check": check}
    return request.app.state.templates.TemplateResponse(request, "_motion_check.html", ctx)


@router.post("/cameras/{camera_id}/record")
async def camera_record(request: Request, camera_id: str, on: str = Form("")) -> RedirectResponse:
    cam = _cfg(request).camera(camera_id)
    if cam is not None:
        cam["record"] = bool(on)
        request.app.state.live.set_recording(camera_id, bool(on))
        _persist(request)
    return RedirectResponse("/cameras", status_code=303)
