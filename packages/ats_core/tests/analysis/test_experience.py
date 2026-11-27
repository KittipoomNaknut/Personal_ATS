from __future__ import annotations

from datetime import date

import pytest

from ats_core.analysis.experience import (
    DateParseError,
    Job,
    MonthDate,
    parse_resume_date,
    total_years,
)

TODAY = date(2026, 11, 27)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jan 2021", MonthDate(2021, 1)),
        ("January 2021", MonthDate(2021, 1)),
        ("Sept. 2018", MonthDate(2018, 9)),
        ("sep, 2018", MonthDate(2018, 9)),
        ("2021-03", MonthDate(2021, 3)),
        ("2021/3", MonthDate(2021, 3)),
        ("06/2020", MonthDate(2020, 6)),
        ("6-2020", MonthDate(2020, 6)),
        ("มิ.ย. 2563", MonthDate(2020, 6)),
        ("มิ.ย.2563", MonthDate(2020, 6)),
        ("มิถุนายน 2563", MonthDate(2020, 6)),
        ("พ.ศ. 2560", MonthDate(2017, 1)),
        ("ค.ศ. 2017", MonthDate(2017, 1)),
        ("  2019  ", MonthDate(2019, 1)),
        ("Present", MonthDate(2026, 11)),
        ("ปัจจุบัน", MonthDate(2026, 11)),
        ("current", MonthDate(2026, 11)),
    ],
)
def test_parse_resume_date(text: str, expected: MonthDate) -> None:
    assert parse_resume_date(text, today=TODAY) == expected


def test_year_only_end_defaults_to_december() -> None:
    assert parse_resume_date("2020", today=TODAY, default_month=12) == MonthDate(2020, 12)


@pytest.mark.parametrize("text", ["soon", "Smarch 2020", "13/2020", "1899", "2200", "", "20/20"])
def test_unparseable_dates(text: str) -> None:
    with pytest.raises(DateParseError):
        parse_resume_date(text, today=TODAY)


def test_total_years_with_present() -> None:
    jobs = [Job("Jun 2018", "Dec 2020"), Job("Jan 2021", "Present")]
    assert total_years(jobs, today=TODAY) == 8.5  # 31 + 71 months


def test_overlapping_jobs_are_not_double_counted() -> None:
    jobs = [Job("Jan 2020", "Dec 2021"), Job("Jun 2021", "Dec 2022"), Job("Jan 2025", "Dec 2025")]
    assert total_years(jobs, today=TODAY) == 4.0  # 2020-2022 = 36 months + 12 months


def test_contained_job_adds_nothing() -> None:
    assert total_years([Job("2018", "2023"), Job("Mar 2019", "Apr 2019")], today=TODAY) == 6.0


def test_single_month_job() -> None:
    assert total_years([Job("Jan 2021", "Jan 2021")], today=TODAY) == 0.1


def test_buddhist_and_gregorian_mixed() -> None:
    assert total_years([Job("มิ.ย. 2563", "Dec 2021")], today=TODAY) == 1.6  # 19 months


def test_no_jobs() -> None:
    assert total_years([], today=TODAY) == 0.0


def test_end_before_start_is_an_error() -> None:
    with pytest.raises(DateParseError, match="ends before"):
        total_years([Job("2022", "2020")], today=TODAY)
