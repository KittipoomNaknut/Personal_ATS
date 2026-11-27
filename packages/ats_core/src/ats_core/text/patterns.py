"""Contact-information patterns (e-mail, phone) for Thai and international resumes."""

from __future__ import annotations

import re
from typing import Final

__all__ = ["EMAIL_RE", "find_emails", "find_phones"]

EMAIL_RE: Final = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])"
)

_SEP: Final = r"[\s.\-]?"

# Thai numbers. Mobile: 0[689]x-xxx-xxxx. Bangkok: 02-xxx-xxxx.
# Provincial: 0xx-xxx-xxx. With +66 the leading 0 is dropped, sometimes as "(0)".
_THAI_SUBSCRIBER: Final = (
    rf"(?:[689]\d{_SEP}\d{{3}}{_SEP}\d{{4}}"
    rf"|2{_SEP}\d{{3}}{_SEP}\d{{4}}"
    rf"|[3-7]\d{_SEP}\d{{3}}{_SEP}\d{{3}})"
)
_THAI_PHONE: Final = re.compile(
    rf"(?<![\d+])(?:(?:\+66|0066){_SEP}(?:\(0\))?{_SEP}|0){_THAI_SUBSCRIBER}(?!\d)"
)
# Other countries: + country code then 7-14 more digits in groups.
_INTL_PHONE: Final = re.compile(r"(?<![\d+])\+(?!66)\d{1,3}(?:[\s.\-]?\(?\d{1,4}\)?){2,6}(?!\d)")
_MIN_INTL_DIGITS: Final = 8
_MAX_INTL_DIGITS: Final = 15


def find_emails(text: str) -> list[str]:
    """Return e-mail addresses found in ``text`` in order of appearance."""
    return EMAIL_RE.findall(text)


def find_phones(text: str) -> list[str]:
    """Return phone numbers found in ``text`` in order of appearance."""
    found: list[tuple[int, str]] = [(m.start(), m.group()) for m in _THAI_PHONE.finditer(text)]
    for m in _INTL_PHONE.finditer(text):
        digits = sum(ch.isdigit() for ch in m.group())
        if _MIN_INTL_DIGITS <= digits <= _MAX_INTL_DIGITS:
            found.append((m.start(), m.group().strip()))
    return [number for _, number in sorted(found)]
