from __future__ import annotations

import pytest

from ats_core.text.patterns import find_emails, find_phones


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("contact: somchai.k@example.co.th", ["somchai.k@example.co.th"]),
        ("a+b@mail.example.com, c_d@ex-ample.org", ["a+b@mail.example.com", "c_d@ex-ample.org"]),
        ("อีเมล: test.user@gmail.com โทร", ["test.user@gmail.com"]),
    ],
)
def test_find_emails(text: str, expected: list[str]) -> None:
    assert find_emails(text) == expected


@pytest.mark.parametrize("text", ["no at sign", "user@localhost", "@example.com", "a@b.c"])
def test_find_emails_rejects_non_addresses(text: str) -> None:
    assert find_emails(text) == []


@pytest.mark.parametrize(
    "number",
    [
        "081-234-5678",
        "0812345678",
        "081 234 5678",
        "091.234.5678",
        "+66 81 234 5678",
        "+66812345678",
        "+66 (0) 81-234-5678",
        "0066 81 234 5678",
        "02-123-4567",
        "021234567",
        "+66 2 123 4567",
        "053-123-456",
        "+1 415 555 2671",
        "+44 20 7946 0958",
    ],
)
def test_find_phones_accepts_formats(number: str) -> None:
    assert find_phones(f"Tel: {number} (mobile)") == [number]


@pytest.mark.parametrize(
    "text",
    [
        "2019 - 2023",
        "Jan 2021 - Present",
        "Salary 25,000-30,000 THB",
        "GPA 3.85 / 4.00",
        "1-2345-67890-12-3",  # national ID format, not a phone
        "Handled 2,500,000 requests per day",
        "version 1.2.3",
        "+1 23",  # too few digits
    ],
)
def test_find_phones_rejects_non_phones(text: str) -> None:
    assert find_phones(text) == []


def test_find_phones_returns_in_document_order() -> None:
    text = "Office +44 20 7946 0958, mobile 081-234-5678"
    assert find_phones(text) == ["+44 20 7946 0958", "081-234-5678"]
