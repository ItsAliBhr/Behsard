from __future__ import annotations

from datetime import date, datetime
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from behsard.config import Config
from behsard.core.models import CartonEvent, CartonStatus, Conveyor
from behsard.core.repository import SqliteRepository
from behsard.core.services import ShiftService
from behsard.reports.counts import build_report
from behsard.reports.excel import report_xlsx
from behsard.web.app import create_app


def _carton(repo, shift, conveyor, ts, code, status=CartonStatus.RECOGNIZED):
    st = repo.get_stamp_by_code(code) if code else None
    return repo.add_carton_event(
        CartonEvent(conveyor, ts, status, shift.id if shift else None, st.id if st else None)
    )


def _data(repo: SqliteRepository) -> None:
    repo.upsert_conveyor(Conveyor(2, "conveyor 2", 0.5))
    svc = ShiftService(repo)
    day = svc.open_shift("morning", datetime(2026, 11, 1, 6))
    ali, reza = svc.ensure_worker("Ali"), svc.ensure_worker("Reza")
    svc.assign(day.id, ali.id, "A12", 1)
    svc.assign(day.id, reza.id, "B07", 2)
    svc.close_shift(day.id, datetime(2026, 11, 1, 18))
    night = svc.open_shift("night", datetime(2026, 11, 1, 18))
    svc.assign(night.id, reza.id, "A12", 1)  # stamps are re-issued per shift
    for m in range(3):
        _carton(repo, day, 1, datetime(2026, 11, 1, 9, m), "A12")
    _carton(repo, day, 2, datetime(2026, 11, 1, 10), "B07", CartonStatus.RESOLVED)
    _carton(repo, day, 2, datetime(2026, 11, 1, 10, 5), None, CartonStatus.PENDING_REVIEW)
    _carton(repo, day, 2, datetime(2026, 11, 1, 10, 6), None, CartonStatus.NO_STAMP)
    # night shift, after midnight: still work day 2026-11-01
    _carton(repo, night, 1, datetime(2026, 11, 2, 1, 0), "A12")


def test_report_groups_and_work_day(repo: SqliteRepository) -> None:
    _data(repo)
    rep = build_report(repo, date(2026, 11, 1), date(2026, 11, 1))
    assert rep.total == 5 and rep.pending_review == 1
    by_worker = {(g["worker_name"], g["stamp_code"]): g["count"] for g in rep.group("worker")}
    # same stamp, different workers in different shifts
    assert by_worker == {("Ali", "A12"): 3, ("Reza", "B07"): 1, ("Reza", "A12"): 1}
    by_conv = {g["conveyor_name"]: (g["count"], g["adjusted"]) for g in rep.group("conveyor")}
    assert by_conv == {"conveyor 1": (4, 4.0), "conveyor 2": (1, 0.5)}
    assert [g["count"] for g in rep.group("shift")] == [3 + 1, 1]
    assert rep.group("day") == [{"day": date(2026, 11, 1), "count": 5, "adjusted": 4.5}]
    assert build_report(repo, date(2026, 11, 2), date(2026, 11, 2)).total == 0


def test_excel_export(repo: SqliteRepository) -> None:
    _data(repo)
    wb = load_workbook(BytesIO(report_xlsx(build_report(repo, date(2026, 11, 1), date(2026, 11, 1)))))
    ws = wb["خلاصه کارگران"]
    assert ws.sheet_view.rightToLeft
    assert [c.value for c in ws[1]] == ["کارگر", "مهر", "تعداد کارتن", "تعداد با ضریب نقاله"]
    assert ws[2][0].value == "Ali" and ws[2][2].value == 3
    assert wb["روزها"]["A2"].value == "1405/08/10"
    assert wb["ریز کارتن‌ها"].max_row == 6
    assert "اطلاعات" in wb.sheetnames


def test_admin_and_report_pages(repo: SqliteRepository, tmp_path) -> None:
    c = TestClient(create_app(Config(), repo, tmp_path))
    r = c.post("/shifts/open", data={"name": "صبح", "copy_from": ""}, follow_redirects=False)
    assert r.status_code == 303
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    r = c.post(f"/shifts/{sid}/assign", data={"worker": "علی", "stamp": "a۱۲", "conveyor": "1"})
    assert "ثبت شد" in r.text and "A12" in r.text
    r = c.post(f"/shifts/{sid}/assign", data={"worker": "رضا", "stamp": "A12", "conveyor": "2"})
    assert "به کارگر دیگری داده شده" in r.text and 'value="رضا"' in r.text
    r = c.post(f"/shifts/{sid}/assign", data={"worker": "رضا", "stamp": "12", "conveyor": "2"})
    assert "یک حرف انگلیسی و دو رقم" in r.text
    assert "علی" in c.get(f"/shifts/{sid}").text
    assert c.get("/shifts").status_code == 200
    svc = ShiftService(repo)
    assert [a.code for a in svc.active_stamps(1, datetime.now())] == ["A12"]
    sh = repo.get_shift(sid)
    sh.start_at = sh.start_at.replace(hour=0, minute=0, second=0)
    repo.update_shift(sh)
    c.post(f"/shifts/{sid}/close", follow_redirects=False)
    assert repo.get_shift(sid).end_at is not None
    r = c.get("/reports?start=۱۴۰۵/۰۸/۱۰&end=1405/08/10&by=detail")
    assert r.status_code == 200 and "۱۴۰۵/۰۸/۱۰" in r.text
    assert "تاریخ نامعتبر" in c.get("/reports?start=bad").text
    r = c.get("/reports/export.xlsx?start=1405/08/10&end=1405/08/10")
    assert r.status_code == 200 and r.content[:2] == b"PK"
