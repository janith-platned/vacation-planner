"""The range-selection logic that runs in the browser, tested under Node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "static" / "leave" / "range_select.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")

# A calendar week plus the next Monday: Mon 7 Dec 2026 ... Mon 14 Dec; the weekend is not selectable.
CELLS = [
    {"date": "2026-12-07", "selectable": True},
    {"date": "2026-12-08", "selectable": True},
    {"date": "2026-12-09", "selectable": True},
    {"date": "2026-12-10", "selectable": True},
    {"date": "2026-12-11", "selectable": True},
    {"date": "2026-12-12", "selectable": False},  # Saturday
    {"date": "2026-12-13", "selectable": False},  # Sunday
    {"date": "2026-12-14", "selectable": True},
]


def compute(cells, a, b):
    code = (
        "const r = require(%s);"
        "const [cells, a, b] = JSON.parse(process.argv[1]);"
        "console.log(JSON.stringify(r.computeSelection(cells, a, b)));"
    ) % json.dumps(str(SCRIPT))
    out = subprocess.run(
        ["node", "-e", code, json.dumps([cells, a, b])], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out)


def test_range_across_a_weekend_skips_it_and_trims_to_working_days():
    sel = compute(CELLS, "2026-12-10", "2026-12-14")  # Thu to next Mon
    assert sel["start"] == "2026-12-10" and sel["end"] == "2026-12-14"
    assert sel["days"] == ["2026-12-10", "2026-12-11", "2026-12-14"]
    assert sel["skipped"] == ["2026-12-12", "2026-12-13"]


def test_dragging_backwards_gives_the_same_selection():
    assert compute(CELLS, "2026-12-14", "2026-12-10") == compute(CELLS, "2026-12-10", "2026-12-14")


def test_a_single_day_is_a_range_of_one():
    sel = compute(CELLS, "2026-12-09", "2026-12-09")
    assert sel == {"start": "2026-12-09", "end": "2026-12-09", "days": ["2026-12-09"], "skipped": []}


def test_a_weekend_only_selection_has_nothing_to_book():
    sel = compute(CELLS, "2026-12-12", "2026-12-13")
    assert sel["start"] is None and sel["end"] is None and sel["days"] == []
    assert sel["skipped"] == ["2026-12-12", "2026-12-13"]


def test_a_range_that_ends_on_a_weekend_is_trimmed_to_the_last_working_day():
    sel = compute(CELLS, "2026-12-09", "2026-12-13")
    assert (sel["start"], sel["end"]) == ("2026-12-09", "2026-12-11")
    assert sel["skipped"] == ["2026-12-12", "2026-12-13"]


def test_unselectable_days_such_as_holidays_or_the_past_are_skipped():
    cells = [dict(c, selectable=c["date"] != "2026-12-09") for c in CELLS]  # 9 Dec is a holiday
    sel = compute(cells, "2026-12-07", "2026-12-11")
    assert sel["days"] == ["2026-12-07", "2026-12-08", "2026-12-10", "2026-12-11"]
    assert sel["skipped"] == ["2026-12-09"]
