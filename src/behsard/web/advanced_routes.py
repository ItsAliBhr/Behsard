"""Advanced: recognizer/tracker thresholds, field dataset export, templates."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from behsard.config import RecognitionConfig, TrackingConfig, save_config
from behsard.review.field_export import export_field_dataset

router = APIRouter()

# (section, key, Persian label, min, max)
FIELDS = [
    ("recognition", "accept_threshold", "آستانه پذیرش خودکار (اطمینان)", 0.05, 0.99),
    ("recognition", "min_margin", "حداقل فاصله با مهر دوم", 0.0, 0.5),
    ("recognition", "max_regions", "حداکثر ناحیه‌های نامزد در هر تصویر", 1, 10),
    ("recognition", "work_max_side", "حداکثر اندازه تصویر برای تشخیص (پیکسل)", 640, 3200),
    ("tracking", "process_fps", "فریم پردازشی در ثانیه", 2, 20),
    ("tracking", "pair_window_s", "پنجره زمانی جفت کردن دو دوربین (ثانیه)", 0.3, 5),
    ("tracking", "frames_per_sighting", "تعداد بهترین فریم برای هر کارتن", 1, 6),
    ("tracking", "edge_k", "حساسیت لبه کارتن (نسبت به میانه)", 1.5, 20),
    ("tracking", "edge_rel", "حساسیت لبه (نسبت به قوی‌ترین لبه)", 0.05, 0.9),
    ("tracking", "min_carton_frac", "حداقل عرض کارتن (کسری از کادر)", 0.05, 1.0),
    ("tracking", "max_carton_frac", "حداکثر عرض کارتن (کسری از کادر)", 0.5, 4.0),
]
DEFAULTS = {"recognition": RecognitionConfig(), "tracking": TrackingConfig()}


def _ctx(request: Request, message: str = "") -> dict[str, object]:
    cfg = request.app.state.cfg
    rows = []
    for sec, key, label, lo, hi in FIELDS:
        rows.append({
            "name": f"{sec}.{key}", "label": label, "value": getattr(getattr(cfg, sec), key),
            "default": getattr(DEFAULTS[sec], key), "lo": lo, "hi": hi,
        })
    live = request.app.state.live
    rec = getattr(getattr(live, "pipeline", None), "recognizer", None)
    exports = sorted(cfg.resolve("data/exports").glob("*.zip"), reverse=True)[:10]
    return {
        "request": request, "nav": "advanced", "rows": rows, "message": message,
        "learned": getattr(rec, "learned_templates", 0), "templates_path": cfg.recognition.templates_path,
        "exports": [p.name for p in exports],
    }


@router.get("/advanced", response_class=HTMLResponse)
async def advanced_page(request: Request, saved: str = "") -> HTMLResponse:
    msg = {"1": "تنظیمات ذخیره و اعمال شد.", "defaults": "تنظیمات پیش‌فرض بازگردانده شد."}.get(saved, "")
    return request.app.state.templates.TemplateResponse(request, "advanced.html", _ctx(request, msg))


def _apply(request: Request) -> None:
    """Persist, then restart cameras so tracker parameters take effect.
    Recognition settings act immediately (the recognizer shares the object)."""
    cfg = request.app.state.cfg
    if cfg.path is not None:
        save_config(cfg)
    live = request.app.state.live
    if getattr(live, "started", False):
        for cid in list(live.cameras):
            lc = live.cameras[cid]
            live.start_camera(lc.cfg, replay=lc.replay)


@router.post("/advanced/save")
async def advanced_save(request: Request) -> RedirectResponse:
    form = await request.form()
    cfg = request.app.state.cfg
    for sec, key, _label, lo, hi in FIELDS:
        raw = form.get(f"{sec}.{key}")
        if raw is None or str(raw).strip() == "":
            continue
        target = getattr(cfg, sec)
        typ = type(getattr(DEFAULTS[sec], key))
        try:
            val = typ(float(str(raw)))
        except ValueError:
            continue
        setattr(target, key, min(max(val, typ(lo)), typ(hi)))
    _apply(request)
    return RedirectResponse("/advanced?saved=1", status_code=303)


@router.post("/advanced/defaults")
async def advanced_defaults(request: Request) -> RedirectResponse:
    cfg = request.app.state.cfg
    for sec, key, *_ in FIELDS:
        setattr(getattr(cfg, sec), key, getattr(DEFAULTS[sec], key))
    _apply(request)
    return RedirectResponse("/advanced?saved=defaults", status_code=303)


@router.post("/advanced/export")
async def advanced_export(request: Request) -> FileResponse:
    import asyncio

    cfg = request.app.state.cfg
    z = await asyncio.to_thread(export_field_dataset, request.app.state.repo, cfg, cfg.resolve("data/exports"))
    return FileResponse(z, filename=z.name, media_type="application/zip")
