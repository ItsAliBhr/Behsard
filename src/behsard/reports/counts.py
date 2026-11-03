"""Aggregate credited cartons.

A carton is credited when it was auto-recognised or resolved by the
supervisor. The *work day* of a carton is the start date of its shift (a
night shift crossing midnight stays on one day); cartons without a shift use
their own date. ``adjusted`` applies the per-conveyor coefficient.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from behsard.core.models import CartonStatus
from behsard.core.repository import Repository

# grouping name -> row keys
GROUPINGS: dict[str, tuple[str, ...]] = {
    "worker": ("worker_name", "stamp_code"),
    "shift": ("day", "shift_name", "shift_id"),
    "day": ("day",),
    "conveyor": ("conveyor_name", "conveyor_id"),
    "detail": ("day", "shift_name", "conveyor_name", "worker_name", "stamp_code"),
}


@dataclass
class Report:
    start: date
    end: date  # inclusive
    rows: list[dict[str, Any]]  # one per credited carton
    pending_review: int
    status_counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def total_adjusted(self) -> float:
        return round(sum(r["coefficient"] for r in self.rows), 2)

    def group(self, by: str) -> list[dict[str, Any]]:
        keys = GROUPINGS[by]
        agg: dict[tuple[Any, ...], dict[str, Any]] = {}
        for r in self.rows:
            k = tuple(r[key] for key in keys)
            g = agg.setdefault(k, {**{key: r[key] for key in keys}, "count": 0, "adjusted": 0.0})
            g["count"] += 1
            g["adjusted"] += r["coefficient"]
        out = list(agg.values())
        for g in out:
            g["adjusted"] = round(g["adjusted"], 2)
        sort_keys = [k for k in keys if k != "shift_id" and k != "conveyor_id"]
        if by in ("worker", "conveyor"):
            out.sort(key=lambda g: (-g["count"], *[str(g[k]) for k in sort_keys]))
        else:
            out.sort(key=lambda g: tuple(str(g[k]) for k in sort_keys))
        return out


def _work_day(row: dict[str, Any]) -> date:
    start = row.get("shift_start")
    if start:
        return datetime.fromisoformat(start).date()
    return datetime.fromisoformat(row["ts"]).date()


def build_report(repo: Repository, start: date, end: date) -> Report:
    """Report for work days ``start``..``end`` inclusive."""
    # a night shift starting on ``end`` may run past midnight: widen the
    # query by a day, then filter on the work day.
    q0 = datetime(start.year, start.month, start.day)
    q1 = datetime(end.year, end.month, end.day) + timedelta(days=2)
    rows = []
    for r in repo.counted_rows(q0 - timedelta(days=1), q1):
        day = _work_day(r)
        if not (start <= day <= end):
            continue
        r = dict(r)
        r["day"] = day
        r["worker_name"] = r.get("worker_name") or "(نامشخص)"
        r["shift_name"] = r.get("shift_name") or "(بدون شیفت)"
        r["coefficient"] = float(r.get("coefficient") or 1.0)
        rows.append(r)
    pending = 0
    statuses: dict[str, int] = defaultdict(int)
    for ev in repo.list_carton_events(limit=1_000_000, mode="prod"):
        if q0 - timedelta(days=1) <= ev.ts < q1:
            shift = repo.get_shift(ev.shift_id) if ev.shift_id else None
            day = shift.start_at.date() if shift else ev.ts.date()
            if start <= day <= end:
                statuses[str(ev.status)] += 1
                pending += ev.status == CartonStatus.PENDING_REVIEW
    return Report(start, end, rows, pending, dict(statuses))
