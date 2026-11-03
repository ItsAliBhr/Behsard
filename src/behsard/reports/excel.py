"""Excel export (RTL sheets, Jalali dates)."""

from __future__ import annotations

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from behsard.reports.counts import Report
from behsard.web.fa import jalali_date

HEADERS = {
    "day": "روز کاری",
    "shift_name": "شیفت",
    "conveyor_name": "نقاله",
    "worker_name": "کارگر",
    "stamp_code": "مهر",
    "count": "تعداد کارتن",
    "adjusted": "تعداد با ضریب نقاله",
    "ts": "زمان",
    "event_id": "شماره کارتن",
}

SHEETS = (
    ("خلاصه کارگران", "worker", ("worker_name", "stamp_code", "count", "adjusted")),
    ("روزانه-کارگر", "detail", ("day", "shift_name", "conveyor_name", "worker_name", "stamp_code", "count", "adjusted")),
    ("شیفت‌ها", "shift", ("day", "shift_name", "count", "adjusted")),
    ("روزها", "day", ("day", "count", "adjusted")),
    ("نقاله‌ها", "conveyor", ("conveyor_name", "count", "adjusted")),
)


def _cell(v: Any) -> Any:
    if hasattr(v, "year") and hasattr(v, "month") and not isinstance(v, str):
        return jalali_date(v)
    return v


def _sheet(wb: Workbook, title: str, cols: tuple[str, ...], rows: list[dict[str, Any]], first: bool) -> None:
    ws = wb.active if first else wb.create_sheet()
    assert ws is not None
    ws.title = title
    ws.sheet_view.rightToLeft = True
    ws.append([HEADERS.get(c, c) for c in cols])
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDE6F0")
        c.alignment = Alignment(horizontal="center")
    for r in rows:
        ws.append([_cell(r.get(c)) for c in cols])
    for i, c in enumerate(cols, 1):
        width = max([len(str(HEADERS.get(c, c)))] + [len(str(_cell(r.get(c)))) for r in rows]) + 4
        ws.column_dimensions[get_column_letter(i)].width = min(width, 40)
    ws.freeze_panes = "A2"


def report_xlsx(report: Report) -> bytes:
    wb = Workbook()
    for i, (title, by, cols) in enumerate(SHEETS):
        _sheet(wb, title, cols, report.group(by), first=i == 0)
    detail = [
        {**r, "ts": r["ts"].replace("T", " ")} for r in sorted(report.rows, key=lambda r: r["ts"])
    ]
    _sheet(
        wb,
        "ریز کارتن‌ها",
        ("event_id", "ts", "day", "shift_name", "conveyor_name", "worker_name", "stamp_code"),
        detail,
        first=False,
    )
    info = wb.create_sheet("اطلاعات")
    info.sheet_view.rightToLeft = True
    info.append(["از تاریخ", jalali_date(report.start)])
    info.append(["تا تاریخ", jalali_date(report.end)])
    info.append(["کل کارتن‌های شمارش‌شده", report.total])
    info.append(["کل با ضریب نقاله", report.total_adjusted])
    info.append(["در انتظار بررسی دستی (شمارش نشده)", report.pending_review])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
