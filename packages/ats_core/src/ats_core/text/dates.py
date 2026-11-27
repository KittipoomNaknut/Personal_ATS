"""Detection of calendar years used in resume dates (Buddhist vs Gregorian era).

Thai resumes often use the Buddhist Era (พ.ศ. = ค.ศ. + 543). Many ATSs assume
Gregorian years, so "2563 - 2566" may be read as the year 2563, which breaks the
experience calculation. Only years in a date context are counted, so numbers like
"2,500 users" or "2500 units" are not mistaken for years.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = ["Era", "YearMention", "find_date_years"]


class Era(StrEnum):
    BUDDHIST = "buddhist"
    GREGORIAN = "gregorian"


@dataclass(frozen=True, slots=True)
class YearMention:
    year: int
    era: Era


_YEAR: Final = r"(?:25[0-9]{2}|19[5-9][0-9]|20[0-5][0-9])"

_EN_MONTHS: Final = (
    r"(?<![a-z])(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_TH_MONTHS: Final = (
    r"มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|สิงหาคม|กันยายน"
    r"|ตุลาคม|พฤศจิกายน|ธันวาคม"
    r"|ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\."
)
_ERA_PREFIX: Final = r"พ\.\s?ศ\.|ค\.\s?ศ\.|(?<![a-z])(?:b\.?e\.?|a\.?d\.?)"
_PRESENT: Final = r"ปัจจุบัน|present|current|now"
_DASH: Final = r"\s*[-\u2013\u2014]\s*|\s+(?:to|ถึง)\s+"  # hyphen, en dash, em dash

# A year counts when it follows a month or era marker, or is part of a range.
_YEAR_IN_CONTEXT: Final = re.compile(
    rf"""
    (?:(?:{_EN_MONTHS}|{_TH_MONTHS}|{_ERA_PREFIX})[\s.,/]*(?<!\d)(?P<after_marker>{_YEAR})(?!\d))
    | (?:(?<![\d,.])(?P<range_start>{_YEAR})(?:{_DASH})(?:(?:{_EN_MONTHS}|{_TH_MONTHS})[\s.,/]*)?
         (?P<range_end>{_YEAR}(?![\d,])|{_PRESENT}))
    | (?:(?<!\d)(?:0?[1-9]|1[0-2])\s*/\s*(?P<month_numeric>{_YEAR})(?!\d))
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _era(year: int) -> Era:
    return Era.BUDDHIST if year >= 2500 else Era.GREGORIAN


def find_date_years(text: str) -> list[YearMention]:
    """Return the years that appear in date contexts within ``text``."""
    mentions: list[YearMention] = []
    for m in _YEAR_IN_CONTEXT.finditer(text):
        for group in ("after_marker", "range_start", "range_end", "month_numeric"):
            value = m.group(group)
            if value and value.isdigit():
                year = int(value)
                mentions.append(YearMention(year, _era(year)))
    return mentions
