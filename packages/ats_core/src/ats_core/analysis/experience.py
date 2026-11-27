"""Years of experience, computed in code (never by the model).

The model extracts each job's start and end exactly as written ("Jan 2021",
"มิ.ย. 2563", "06/2020", "2019", "Present"); this module parses them, converts
Buddhist Era years, and merges overlapping jobs so concurrent work is not
counted twice.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

__all__ = ["DateParseError", "Job", "MonthDate", "parse_resume_date", "total_years"]

_BUDDHIST_ERA_OFFSET: Final = 543
_EN_MONTHS: Final = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
_TH_MONTHS: Final = (
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
)  # fmt: skip
_TH_MONTH_ABBREVIATIONS: Final = (
    "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
)  # fmt: skip
_MONTHS: Final[dict[str, int]] = {
    name: number
    for names in (_EN_MONTHS, _TH_MONTHS, _TH_MONTH_ABBREVIATIONS)
    for number, name in enumerate(names, start=1)
} | {"sept": 9}
_PRESENT: Final = re.compile(r"^(?:present|current|now|today|ปัจจุบัน|ถึงปัจจุบัน)$", re.IGNORECASE)
_ISO: Final = re.compile(r"^(?P<year>\d{4})(?:[-/.](?P<month>\d{1,2}))?$")
_NUMERIC: Final = re.compile(r"^(?P<month>\d{1,2})\s*[/.-]\s*(?P<year>\d{4})$")
_NAMED: Final = re.compile(r"^(?P<month>[^\d\s,]+)[\s,]*(?P<year>\d{4})$")
_ERA_PREFIX: Final = re.compile(r"^(?:พ\.\s?ศ\.|ค\.\s?ศ\.|b\.?e\.?|a\.?d\.?)\s*", re.IGNORECASE)


class DateParseError(ValueError):
    """A resume date could not be understood."""


@dataclass(frozen=True, slots=True, order=True)
class MonthDate:
    year: int
    month: int

    @property
    def index(self) -> int:
        return self.year * 12 + (self.month - 1)

    @classmethod
    def of(cls, day: date) -> MonthDate:
        return cls(day.year, day.month)


def _year(value: str) -> int:
    year = int(value)
    if year >= 2400:  # Buddhist Era
        year -= _BUDDHIST_ERA_OFFSET
    if not 1950 <= year <= 2100:
        raise DateParseError(f"implausible year {value}")
    return year


def _month(value: str) -> int:
    key = value.strip().casefold().rstrip(".")
    month = _MONTHS.get(key) or _MONTHS.get(key + ".") or _MONTHS.get(key[:3])
    if month is None and key.isdigit():
        month = int(key)
    if month is None or not 1 <= month <= 12:
        raise DateParseError(f"unknown month {value!r}")
    return month


def parse_resume_date(text: str, *, today: date, default_month: int = 1) -> MonthDate:
    """Parse one start/end date. ``default_month`` applies when only a year is given."""
    value = " ".join(text.split())
    if _PRESENT.match(value):
        return MonthDate.of(today)
    value = _ERA_PREFIX.sub("", value)
    for pattern in (_ISO, _NUMERIC, _NAMED):
        match = pattern.match(value)
        if match:
            month = match.group("month")
            return MonthDate(_year(match.group("year")), _month(month) if month else default_month)
    raise DateParseError(f"cannot parse date {text!r}")


@dataclass(frozen=True, slots=True)
class Job:
    start: str
    end: str


def _span(job: Job, today: date) -> tuple[int, int]:
    start = parse_resume_date(job.start, today=today, default_month=1)
    end = parse_resume_date(job.end, today=today, default_month=12)
    if end < start:
        raise DateParseError(f"job ends before it starts: {job.start!r} - {job.end!r}")
    # Inclusive months: "Jan 2021 - Jan 2021" is one month of work.
    return start.index, end.index + 1


def total_years(jobs: Sequence[Job], *, today: date) -> float:
    """Total years worked, with overlapping jobs merged. Rounded to 0.1 year."""
    spans = sorted(_span(job, today) for job in jobs)
    months = 0
    current_start, current_end = None, None
    for start, end in spans:
        if current_end is None or start > current_end:
            if current_end is not None and current_start is not None:
                months += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None and current_start is not None:
        months += current_end - current_start
    return round(months / 12, 1)
