from __future__ import annotations

import pytest

from ats_core.text.dates import Era, find_date_years


def years(text: str) -> list[tuple[int, Era]]:
    return [(m.year, m.era) for m in find_date_years(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2019 - 2023", [(2019, Era.GREGORIAN), (2023, Era.GREGORIAN)]),
        (
            "2019\u20132023",  # en dash
            [(2019, Era.GREGORIAN), (2023, Era.GREGORIAN)],
        ),
        ("2020 to Present", [(2020, Era.GREGORIAN)]),
        ("Jan 2021 - Dec 2023", [(2021, Era.GREGORIAN), (2023, Era.GREGORIAN)]),
        ("September 2018", [(2018, Era.GREGORIAN)]),
        ("06/2020 - 08/2022", [(2020, Era.GREGORIAN), (2022, Era.GREGORIAN)]),
        ("2560 - 2563", [(2560, Era.BUDDHIST), (2563, Era.BUDDHIST)]),
        ("2563 - ปัจจุบัน", [(2563, Era.BUDDHIST)]),
        ("มิ.ย. 2563 - ธ.ค. 2565", [(2563, Era.BUDDHIST), (2565, Era.BUDDHIST)]),
        ("มกราคม 2564", [(2564, Era.BUDDHIST)]),
        ("พ.ศ. 2566", [(2566, Era.BUDDHIST)]),
        ("ค.ศ. 2020", [(2020, Era.GREGORIAN)]),
        ("2563 ถึง 2565", [(2563, Era.BUDDHIST), (2565, Era.BUDDHIST)]),
    ],
)
def test_years_in_date_context(text: str, expected: list[tuple[int, Era]]) -> None:
    assert years(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Served 2500 users daily",
        "Revenue grew to 2,500 - 3,000 units",
        "Omar 2020 award",
        "HEAD 2020 project",
        "Summary 2020",
        "Python 3.12 and 2019",
        "Room 2501",
        "",
    ],
)
def test_numbers_outside_date_context_are_ignored(text: str) -> None:
    assert years(text) == []
