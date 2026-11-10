import datetime as dt

from leave.calendar_grid import build_month_grid, level_and_label
from leave.services import DayCount


def counts(floor, slots_left, on_leave=0, headcount=3):
    return DayCount(dt.date(2026, 12, 8), headcount, on_leave, floor, slots_left)


def test_levels_use_words_as_well_as_colour():
    assert level_and_label(None) == ("off", "Non-working")
    assert level_and_label(counts(None, None)) == ("nofloor", "No floor set")
    assert level_and_label(counts(2, 0)) == ("full", "Full")
    assert level_and_label(counts(2, 1)) == ("low", "1 slot left")
    assert level_and_label(counts(1, 2)) == ("ok", "2 slots left")


def test_december_2026_grid_starts_on_monday_and_marks_days():
    working = [dt.date(2026, 12, d) for d in (1, 2, 3, 4, 7, 8)]
    day_counts = [DayCount(d, 3, 0, 2, 1) for d in working]
    weeks = build_month_grid(2026, 12, day_counts, mine={dt.date(2026, 12, 8), dt.date(2026, 11, 30)})
    assert all(len(week) == 7 for week in weeks)
    assert weeks[0][0].date == dt.date(2026, 11, 30) and not weeks[0][0].in_month
    first_tuesday = weeks[0][1]
    assert first_tuesday.date == dt.date(2026, 12, 1) and first_tuesday.level == "low"
    cells = {cell.date: cell for week in weeks for cell in week}
    assert cells[dt.date(2026, 12, 8)].mine
    assert not cells[dt.date(2026, 11, 30)].mine  # outside the month, never marked
    assert cells[dt.date(2026, 12, 5)].level == "off"  # Saturday: not a working day
    assert cells[dt.date(2026, 11, 30)].level == "out"
