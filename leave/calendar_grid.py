"""Builds the month grid shown on the calendar page from anonymous per-day counts."""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Optional

from .services import DayCount


@dataclass(frozen=True)
class Cell:
    date: dt.date
    in_month: bool
    counts: Optional[DayCount]  # None on non-working days and outside the month
    mine: bool  # the signed-in user is on leave that day
    level: str  # off, nofloor, ok, low, full (also shown as words, never colour alone)
    label: str


def level_and_label(counts: Optional[DayCount]) -> tuple[str, str]:
    if counts is None:
        return "off", "Non-working"
    if counts.floor is None:
        return "nofloor", "No floor set"
    if counts.slots_left == 0:
        return "full", "Full"
    if counts.slots_left == 1:
        return "low", "1 slot left"
    return "ok", f"{counts.slots_left} slots left"


def build_month_grid(year: int, month: int, counts: list[DayCount], mine: set[dt.date]) -> list[list[Cell]]:
    """Weeks (Monday first) of cells for the month."""
    by_date = {c.date: c for c in counts}
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        row = []
        for day in week:
            in_month = day.month == month
            day_counts = by_date.get(day) if in_month else None
            level, label = level_and_label(day_counts) if in_month else ("out", "")
            row.append(Cell(day, in_month, day_counts, in_month and day in mine, level, label))
        weeks.append(row)
    return weeks
