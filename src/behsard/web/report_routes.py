"""Reports per worker / shift / day / conveyor, with Excel export."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from behsard.reports.counts import build_report
from behsard.reports.excel import report_xlsx
from behsard.web.fa import jalali_date, parse_jalali_date

router = APIRouter()


def _range(start: str, end: str) -> tuple[date, date, str]:
    error = ""
    today = date.today()
    try:
        d0 = parse_jalali_date(start) if start else today
        d1 = parse_jalali_date(end) if end else d0
    except (ValueError, TypeError):
        d0 = d1 = today
        error = "تاریخ نامعتبر است؛ قالب درست: ۱۴۰۵/۰۸/۱۰"
    if d1 < d0:
        d0, d1 = d1, d0
    return d0, d1, error


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, start: str = "", end: str = "", by: str = "worker") -> HTMLResponse:
    d0, d1, error = _range(start, end)
    rep = build_report(request.app.state.repo, d0, d1)
    if by not in ("worker", "shift", "day", "conveyor", "detail"):
        by = "worker"
    ctx = {
        "request": request,
        "nav": "reports",
        "rep": rep,
        "groups": rep.group(by),
        "by": by,
        "start": jalali_date(d0),
        "end": jalali_date(d1),
        "error": error,
        "presets": {
            "امروز": (jalali_date(date.today()), jalali_date(date.today())),
            "دیروز": (jalali_date(date.today() - timedelta(days=1)),) * 2,
            "۷ روز اخیر": (jalali_date(date.today() - timedelta(days=6)), jalali_date(date.today())),
            "۳۰ روز اخیر": (jalali_date(date.today() - timedelta(days=29)), jalali_date(date.today())),
        },
    }
    return request.app.state.templates.TemplateResponse(request, "reports.html", ctx)


@router.get("/reports/export.xlsx")
async def export_xlsx(request: Request, start: str = "", end: str = "") -> Response:
    d0, d1, _ = _range(start, end)
    data = report_xlsx(build_report(request.app.state.repo, d0, d1))
    name = f"behsard_{jalali_date(d0).replace('/', '')}_{jalali_date(d1).replace('/', '')}.xlsx"
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
