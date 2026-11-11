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
    info = getattr(request.app.state, "replay_info", None)
    health = live.health().get(REPLAY_ID)
    result = None
    if info:
        evs = [
            e for e in request.app.state.repo.list_carton_events(after_id=info["after_id"], limit=100_000)
            if e.mode in ("replay", "test")
        ]
        result = {
            "file": info["file"],
            "events": len(evs),
            "read": sum(1 for e in evs if e.meta.get("stamp_code")),
            "sightings": health["sightings"] if health else info.get("sightings", 0),
            "moving": health["fps_out"] if health else None,
        }
    ctx = {
        "request": request, "nav": "replay", "conveyors": request.app.state.repo.list_conveyors(),
        "running": REPLAY_ID in live.cameras and (health or {}).get("state") != "finished",
        "health": health, "result": result,
        "files": sorted(p.name for p in _replay_dir(request).glob("*") if p.is_file()),
    }
    return request.app.state.templates.TemplateResponse(request, "replay.html", ctx)


def _replay_dir(request: Request) -> Path:
    d = request.app.state.cfg.resolve("data/replays")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_video(request: Request, path: str) -> Path | None:
    src = path.strip().strip('"')
    if not src:
        return None
    p = Path(src)
    if not p.is_absolute():
        p = _replay_dir(request) / src
    return p if p.exists() else None


def _snapshots(request: Request, video: Path, n: int = 4) -> list[str]:
    """A few frames spread over the video (for drawing the watch region)."""
    import cv2

    d = request.app.state.media_dir / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    out = []
    for i in range(n):
        if total > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 1) / (n + 1)))
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        s = 1280 / frame.shape[1]
        if s < 1:
            frame = cv2.resize(frame, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        name = f"replay_{i}.jpg"
        cv2.imwrite(str(d / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        out.append(f"snapshots/{name}")
    cap.release()
    return out


@router.post("/replay/prepare")
async def replay_prepare(
    request: Request, path: str = Form(""), upload: UploadFile | None = File(None)
) -> RedirectResponse:
    """Step 1: store/locate the video, then draw the watch region on it."""
    if upload is not None and upload.filename:
        dst = _replay_dir(request) / Path(upload.filename).name
        with dst.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        path = str(dst)
    video = _resolve_video(request, path)
    if video is None:
        return RedirectResponse("/replay?error=1", status_code=303)
    from urllib.parse import quote

    return RedirectResponse(f"/replay/setup?file={quote(str(video))}", status_code=303)


@router.get("/replay/setup", response_class=HTMLResponse)
async def replay_setup(request: Request, file: str) -> HTMLResponse:
    import asyncio

    video = _resolve_video(request, file)
    if video is None:
        return RedirectResponse("/replay?error=1", status_code=303)  # type: ignore[return-value]
    snaps = await asyncio.to_thread(_snapshots, request, video)
    roi = getattr(request.app.state, "replay_roi", None) or [0.0, 0.0, 1.0, 1.0]
    cfg = request.app.state.cfg
    stamps = getattr(request.app.state, "replay_stamps", None) or list(cfg.test_mode.stamps)
    if not stamps:  # fall back to the stamps of the open shift
        from datetime import datetime

        from behsard.core.services import ShiftService

        svc = ShiftService(request.app.state.repo)
        stamps = sorted({a.code for c in request.app.state.repo.list_conveyors() for a in svc.active_stamps(c.id, datetime.now())})
    ctx = {
        "stamps": " ".join(stamps), "need_stamps": bool(request.query_params.get("need_stamps")),
        "request": request, "nav": "replay", "file": str(video), "name": video.name,
        "snaps": snaps, "roi": roi, "conveyors": request.app.state.repo.list_conveyors(),
        "v": int(video.stat().st_mtime),
    }
    return request.app.state.templates.TemplateResponse(request, "replay_setup.html", ctx)


@router.post("/replay/start")
async def replay_start(
    request: Request,
    path: str = Form(""),
    upload: UploadFile | None = File(None),
    conveyor_id: int = Form(1),
    side: str = Form("L"),
    speed: float = Form(1.0),
    roi: str = Form(""),
    x: str = Form(""),
    y: str = Form(""),
    w: str = Form(""),
    h: str = Form(""),
    stamps: str = Form(""),
) -> RedirectResponse:
    live = request.app.state.live
    if upload is not None and upload.filename:
        dst = _replay_dir(request) / Path(upload.filename).name
        with dst.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        path = str(dst)
    video = _resolve_video(request, path)
    if video is None:
        return RedirectResponse("/replay?error=1", status_code=303)
    from behsard.core.services import normalize_stamp_code

    codes = []
    for c in en_digits(stamps).replace(",", " ").replace("،", " ").split():
        try:
            codes.append(normalize_stamp_code(c))
        except ValueError:
            continue
    codes = sorted(set(codes))
    cam: dict[str, Any] = {
        "id": REPLAY_ID, "url": str(video), "conveyor_id": conveyor_id, "side": side.upper()[:1] or "L",
        "loop": False, "speed": max(0.25, min(speed, 8.0)),
    }
    if codes:
        cam["stamps"] = codes
        request.app.state.replay_stamps = codes
    elif not live.active_codes(cam, __import__("datetime").datetime.now()):
        # nothing to compare the stamps with: ask for the list
        from urllib.parse import quote

        return RedirectResponse(f"/replay/setup?file={quote(str(video))}&need_stamps=1", status_code=303)
    box = [x, y, w, h] if all(v.strip() for v in (x, y, w, h)) else (roi.split(",") if roi else [])
    if len(box) == 4:
        r = [min(max(float(v), 0.0), 1.0) for v in box]
        if r[2] >= 0.05 and r[3] >= 0.05:
            cam["roi"] = [round(v, 4) for v in r]
            request.app.state.replay_roi = cam["roi"]
    last = request.app.state.repo.list_carton_events(limit=1_000_000)
    request.app.state.replay_info = {"file": video.name, "after_id": max((e.id or 0) for e in last) if last else 0}
    if not live.started:
        live.start()
    live.start_camera(cam, replay=True)
    return RedirectResponse("/", status_code=303)


@router.post("/replay/stop")
async def replay_stop(request: Request) -> RedirectResponse:
    live = request.app.state.live
    lc = live.cameras.get(REPLAY_ID)
    info = getattr(request.app.state, "replay_info", None)
    if lc is not None and info is not None:
        info["sightings"] = lc.worker.sightings
    live.stop_camera(REPLAY_ID)
    return RedirectResponse("/replay", status_code=303)
