"""Contact-information patterns (e-mail, phone) for Thai and international resumes."""

from __future__ import annotations

import re
from typing import Final

__all__ = ["EMAIL_RE", "Span", "email_spans", "find_emails", "find_phones", "phone_spans"]

EMAIL_RE: Final = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])"
)

_SEP: Final = r"[\s.\-]?"

# Thai numbers, matched by digit count so any grouping works ("081-234-5678",
# "08-1234-5678", "(02) 123-4567"). Mobile: 0[689] + 8 digits. Bangkok and
# provincial landlines: 0[2-7] + 7 digits. With +66 the leading 0 is dropped,
# sometimes written as "(0)".
_DIGIT_SEP: Final = r"(?:[\s.\-]|\)\s?)?"
_THAI_SUBSCRIBER: Final = (
    rf"(?:[689](?:{_DIGIT_SEP}\d){{8}}"
    rf"|[2-7](?:{_DIGIT_SEP}\d){{7}})"
)
_THAI_PHONE: Final = re.compile(
    rf"(?<![\d+])(?:(?:\+66|0066){_SEP}(?:\(0\))?{_SEP}\(?|\(?0{_DIGIT_SEP}){_THAI_SUBSCRIBER}(?!\d)"
)
# Other countries: + country code then 7-14 more digits in groups.
_INTL_PHONE: Final = re.compile(r"(?<![\d+])\+(?!66)\d{1,3}(?:[\s.\-]?\(?\d{1,4}\)?){2,6}(?!\d)")
_MIN_INTL_DIGITS: Final = 8
_MAX_INTL_DIGITS: Final = 15


Span = tuple[int, int]


def email_spans(text: str) -> list[Span]:
    """Return ``(start, end)`` of each e-mail address in ``text``."""
    return [m.span() for m in EMAIL_RE.finditer(text)]


def phone_spans(text: str) -> list[Span]:
    """Return ``(start, end)`` of each phone number in ``text``, in order."""
    spans = [m.span() for m in _THAI_PHONE.finditer(text)]
    for m in _INTL_PHONE.finditer(text):
        digits = sum(ch.isdigit() for ch in m.group())
        if _MIN_INTL_DIGITS <= digits <= _MAX_INTL_DIGITS:
            spans.append(m.span())
    return sorted(spans)


def find_emails(text: str) -> list[str]:
    """Return e-mail addresses found in ``text`` in order of appearance."""
    return [text[a:b] for a, b in email_spans(text)]


def find_phones(text: str) -> list[str]:
    """Return phone numbers found in ``text`` in order of appearance."""
    return [text[a:b] for a, b in phone_spans(text)]
