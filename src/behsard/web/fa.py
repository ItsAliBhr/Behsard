"""Persian UI helpers: digits and Jalali dates."""

from __future__ import annotations

from datetime import date, datetime

import jdatetime

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_EN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def fa_digits(value: object) -> str:
    return str(value).translate(_FA_DIGITS)


def en_digits(value: str) -> str:
    """Normalise Persian/Arabic digits typed by the user to ASCII."""
    return value.translate(_EN_DIGITS)


def jalali_date(d: date | datetime | None) -> str:
    if d is None:
        return ""
    if isinstance(d, datetime):
        d = d.date()
    return jdatetime.date.fromgregorian(date=d).strftime("%Y/%m/%d")


def jalali_datetime(dt: datetime | None, seconds: bool = False) -> str:
    if dt is None:
        return ""
    j = jdatetime.datetime.fromgregorian(datetime=dt)
    return j.strftime("%Y/%m/%d %H:%M:%S" if seconds else "%Y/%m/%d %H:%M")


def parse_jalali_date(text: str) -> date:
    """'1405/08/10' (Persian or ASCII digits, / or -) -> Gregorian date."""
    t = en_digits(text.strip()).replace("-", "/")
    y, m, d = (int(x) for x in t.split("/"))
    return jdatetime.date(y, m, d).togregorian()


STATUS_FA = {
    "recognized": "تشخیص خودکار",
    "pending_review": "در انتظار بررسی",
    "resolved": "تأیید دستی",
    "no_stamp": "بدون مهر",
    "not_carton": "کارتن نیست",
}
