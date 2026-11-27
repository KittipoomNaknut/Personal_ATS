from __future__ import annotations

import pytest

from ats_core.text.thai import analyze_thai

VALID_THAI = [
    "ประสบการณ์ทำงาน 5 ปี ด้านการพัฒนาซอฟต์แวร์",
    "นักพัฒนาซอฟต์แวร์อาวุโส บริษัท ตัวอย่าง จำกัด",
    "น้ำ พันธุ์ กี่ปี เก็บ ผู้จัดการ ไม้ไต่คู้ ก็ได้ เป็นต้น",
    "สามารถใช้ภาษาอังกฤษได้ดีมาก ๆ และทำงานเป็นทีม",  # mai yamok written with spaces
    "ฤดูกาล ฤๅษี โครงการ แผนงาน ใบอนุญาต ไฟฟ้า",
    "Senior Engineer ที่ บริษัท ABC (2563 - ปัจจุบัน)",
]


@pytest.mark.parametrize("text", VALID_THAI)
def test_correct_thai_is_not_corrupted(text: str) -> None:
    result = analyze_thai(text)
    assert result.invalid_sequences == 0, text
    assert not result.is_corrupted


def test_english_only_text_has_no_thai() -> None:
    result = analyze_thai("Python developer with Kubernetes experience")
    assert result.thai_chars == 0
    assert result.invalid_ratio == 0.0
    assert not result.is_corrupted


def test_empty_text() -> None:
    result = analyze_thai("")
    assert result.thai_chars == 0
    assert not result.is_corrupted


def test_private_use_area_glyphs_mean_corruption() -> None:
    # Legacy Thai fonts map shifted tone marks to U+F70A..U+F70E.
    result = analyze_thai("ผู\uf70bจัดการ")
    assert result.private_use_chars == 1
    assert result.is_corrupted


def test_cid_tokens_mean_corruption() -> None:
    result = analyze_thai("(cid:123)(cid:45) Engineer")
    assert result.cid_tokens == 2
    assert result.is_corrupted


def test_replacement_characters_mean_corruption() -> None:
    result = analyze_thai("ประสบ\ufffd\ufffdการณ์")
    assert result.replacement_chars == 2
    assert result.is_corrupted


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\u0e48ก", 1),  # tone mark with no base consonant
        ("ก่้", 1),  # two tone marks stacked
        ("กิี", 1),  # two above vowels
        ("เ ก", 1),  # leading vowel followed by a space
        ("งานเ", 1),  # leading vowel at the very end
        (" ำ", 1),  # sara am without a consonant
        ("\u0e4cก", 1),  # thanthakhat without a base
    ],
)
def test_invalid_sequences_are_counted(text: str, expected: int) -> None:
    assert analyze_thai(text).invalid_sequences == expected


def test_detached_marks_above_threshold_mean_corruption() -> None:
    # Typical broken extraction: marks moved after a space.
    broken = "ผ \u0e39 \u0e49จ \u0e31 ดการ ประสบการณ \u0e4c ทำงาน " * 3
    assert analyze_thai(broken).is_corrupted


def test_single_typo_in_long_text_is_not_corruption() -> None:
    long_text = "ประสบการณ์ทำงานด้านการพัฒนาซอฟต์แวร์ " * 20 + "\u0e48"
    result = analyze_thai(long_text)
    assert result.invalid_sequences == 1
    assert not result.is_corrupted


def test_spaced_out_characters_mean_corruption() -> None:
    spaced = " ".join("ประสบการณ์ทำงานด้านการพัฒนาซอฟต์แวร์")
    result = analyze_thai(spaced)
    assert result.spaced_char_ratio > 0.3
    assert result.is_corrupted


def test_spacing_check_needs_enough_samples() -> None:
    # Only a few short Thai words: not enough evidence to judge spacing.
    assert analyze_thai("ก ข ค").spaced_char_ratio == 0.0


def test_mai_yamok_with_spaces_is_not_spacing_corruption() -> None:
    text = "ทำงานได้ดีมาก ๆ และเรียนรู้เร็ว ๆ " * 15
    result = analyze_thai(text)
    assert result.spaced_char_ratio == 0.0
    assert not result.is_corrupted


def test_control_characters_mean_unmapped_glyphs() -> None:
    result = analyze_thai("วิศวกร มิ\x00ย\x00 \x00\x00\x00\x00 - ปัจจุบัน")
    assert result.control_chars == 6
    assert result.is_corrupted


def test_tabs_and_newlines_are_not_control_noise() -> None:
    assert analyze_thai("ทักษะ\tPython\nภาษา\r\n").control_chars == 0


def test_icon_font_private_use_glyphs_are_not_thai_corruption() -> None:
    # U+F095 (phone) and U+F0E0 (envelope) in Font Awesome.
    result = analyze_thai("\uf095 081-234-5678 \uf0e0 somchai@example.com ประสบการณ์ทำงาน")
    assert result.private_use_chars == 0
    assert not result.is_corrupted
