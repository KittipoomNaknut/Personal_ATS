"""Tests for ats_core.pii.

Two properties matter equally:
* recall - every identifier in the fixtures is removed (privacy);
* precision - ordinary resume wording is kept (matching accuracy).

All people, numbers and addresses below are fictitious.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ats_core.extract import extract_document
from ats_core.pii import PiiKind, find_residual_pii, redact_pii

TODAY = date(2026, 11, 27)


def thai_id(first_twelve: str) -> str:
    """Append the correct Thai national-ID check digit."""
    total = sum(int(d) * (13 - i) for i, d in enumerate(first_twelve))
    return first_twelve + str((11 - total % 11) % 10)


VALID_ID = thai_id("110370012345")
FORMATTED_ID = f"{VALID_ID[0]}-{VALID_ID[1:5]}-{VALID_ID[5:10]}-{VALID_ID[10:12]}-{VALID_ID[12]}"


def redact(text: str, **kw: object) -> str:
    return redact_pii(text, today=TODAY, **kw).text  # type: ignore[arg-type]


def kinds(text: str) -> list[PiiKind]:
    return [e.kind for e in redact_pii(text, today=TODAY).entities]


# ── Realistic resumes ────────────────────────────────────────────────────

ENGLISH_RESUME = """Somchai Example
somchai.example@gmail.com | +66 81 234 5678 | linkedin.com/in/somchai-example
123/45 Sukhumvit Rd., Khlong Toei, Bangkok 10110
Date of Birth: 15 June 1995
Summary
Backend engineer with 5 years of experience. Somchai led the payments team.
Experience
Senior Software Engineer, Example Co. Jan 2021 - Present
- Built FastAPI services on Kubernetes serving 2,500 requests per second.
Skills
Python, MS Excel, Product line management, Kubernetes
References
Mr. John Smith, Engineering Manager, john.smith@example.org
"""

THAI_RESUME = f"""นายสมชาย ใจดี
อีเมล: somchai.jaidee@hotmail.com โทร 081-234-5678
ที่อยู่: 99/1 หมู่ 3 ถ.พหลโยธิน ต.คลองหนึ่ง อ.คลองหลวง จ.ปทุมธานี 12120
วันเกิด 15 มิถุนายน 2538 | อายุ 31 ปี | ศาสนา พุทธ | สถานภาพ โสด
เพศ ชาย | สัญชาติ ไทย | ผ่านการเกณฑ์ทหารแล้ว
เลขบัตรประชาชน {FORMATTED_ID}
Line ID: somchai.jd
ประสบการณ์ทำงาน
วิศวกรซอฟต์แวร์ บริษัท ตัวอย่าง จำกัด มิ.ย. 2563 - ปัจจุบัน
- นายจ้างมอบหมายให้ดูแลระบบชำระเงิน
ทักษะ
Python, Kubernetes, การทำงานเป็นทีม
"""


def test_english_resume_identifiers_are_removed() -> None:
    result = redact_pii(ENGLISH_RESUME, today=TODAY)
    out = result.text
    for secret in (
        "Somchai",
        "Example\n",
        "somchai.example@gmail.com",
        "+66 81 234 5678",
        "linkedin.com/in/somchai-example",
        "123/45 Sukhumvit",
        "10110",
        "15 June 1995",
        "John Smith",
        "john.smith@example.org",
    ):
        assert secret not in out, secret
    assert "[DATE_OF_BIRTH_1 age 31]" in out
    assert find_residual_pii(out) == []


def test_english_resume_keeps_job_content() -> None:
    out = redact(ENGLISH_RESUME)
    for kept in (
        "Backend engineer with 5 years of experience.",
        "Senior Software Engineer",
        "Jan 2021 - Present",
        "2,500 requests per second",
        "MS Excel",
        "Product line management",
        "Engineering Manager",
        "Kubernetes",
    ):
        assert kept in out, kept


def test_thai_resume_identifiers_are_removed() -> None:
    result = redact_pii(THAI_RESUME, today=TODAY)
    out = result.text
    for secret in (
        "สมชาย",
        "ใจดี",
        "somchai.jaidee@hotmail.com",
        "081-234-5678",
        "99/1",
        "พหลโยธิน",
        "12120",
        "15 มิถุนายน 2538",
        "พุทธ",
        "โสด",
        FORMATTED_ID,
        "somchai.jd",
    ):
        assert secret not in out, secret
    assert "[DATE_OF_BIRTH_1 age 31]" in out
    assert find_residual_pii(out) == []


def test_thai_resume_keeps_job_relevant_attributes() -> None:
    out = redact(THAI_RESUME)
    for kept in (
        "อายุ 31 ปี",
        "เพศ ชาย",
        "สัญชาติ ไทย",
        "ผ่านการเกณฑ์ทหารแล้ว",
        "วิศวกรซอฟต์แวร์",
        "มิ.ย. 2563 - ปัจจุบัน",
        "นายจ้างมอบหมายให้ดูแลระบบชำระเงิน",  # "นาย" here is part of "employer"
        "การทำงานเป็นทีม",
    ):
        assert kept in out, kept


def test_every_kind_is_detected_in_fixtures() -> None:
    found = set(kinds(ENGLISH_RESUME)) | set(kinds(THAI_RESUME))
    assert found == set(PiiKind)


# ── Placeholders and restore ─────────────────────────────────────────────


def test_same_value_gets_same_placeholder() -> None:
    text = "a@example.com wrote to b@example.com; reply to a@example.com"
    result = redact_pii(text, today=TODAY)
    assert result.text == "[EMAIL_1] wrote to [EMAIL_2]; reply to [EMAIL_1]"
    assert result.count(PiiKind.EMAIL) == 3
    assert result.mapping == {"[EMAIL_1]": "a@example.com", "[EMAIL_2]": "b@example.com"}


def test_restore_round_trips() -> None:
    result = redact_pii(ENGLISH_RESUME, today=TODAY)
    assert result.restore(result.text) == ENGLISH_RESUME


def test_restore_model_output() -> None:
    result = redact_pii("Contact somchai@example.com", today=TODAY)
    model_output = "The candidate lists [EMAIL_1] as contact."
    assert result.restore(model_output) == "The candidate lists somchai@example.com as contact."


def test_restore_prefers_longer_placeholders() -> None:
    text = " ".join(f"user{i}@example.com" for i in range(1, 12))
    result = redact_pii(text, today=TODAY)
    assert "[EMAIL_11]" in result.text
    assert result.restore(result.text) == text  # [EMAIL_1] must not clobber [EMAIL_11]


def test_entity_offsets_point_into_original() -> None:
    result = redact_pii(ENGLISH_RESUME, today=TODAY)
    for entity in result.entities:
        assert ENGLISH_RESUME[entity.start : entity.end] == entity.original


def test_text_without_pii_is_unchanged() -> None:
    text = "Experience\nBuilt data pipelines with Apache Airflow.\nSkills\nPython"
    result = redact_pii(text, today=TODAY)
    assert result.text == text
    assert result.entities == ()


# ── National ID ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [VALID_ID, FORMATTED_ID, " ".join(FORMATTED_ID.split("-"))])
def test_valid_national_id_is_redacted_without_label(value: str) -> None:
    assert kinds(f"Ref {value}") == [PiiKind.NATIONAL_ID]


def test_invalid_checksum_without_label_is_kept() -> None:
    wrong = VALID_ID[:12] + str((int(VALID_ID[12]) + 1) % 10)
    assert kinds(f"Order {wrong}") == []


def test_labelled_id_is_redacted_even_with_bad_checksum() -> None:
    wrong = VALID_ID[:12] + str((int(VALID_ID[12]) + 1) % 10)
    assert kinds(f"เลขบัตรประชาชน: {wrong}") == [PiiKind.NATIONAL_ID]
    assert kinds(f"ID card no. {wrong}") == [PiiKind.NATIONAL_ID]


# ── Dates of birth ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "placeholder"),
    [
        ("Date of Birth: 1995-06-15", "[DATE_OF_BIRTH_1 age 31]"),
        ("DOB 15/06/1995", "[DATE_OF_BIRTH_1 age 31]"),
        ("Birthday: June 15, 1995", "[DATE_OF_BIRTH_1 age 31]"),
        ("วันเกิด: 15 มิ.ย. 2538", "[DATE_OF_BIRTH_1 age 31]"),
        ("วัน/เดือน/ปีเกิด 15/06/2538", "[DATE_OF_BIRTH_1 age 31]"),
        ("Date of Birth: 15 June", "[DATE_OF_BIRTH_1]"),  # no year: no age
    ],
)
def test_date_of_birth(line: str, placeholder: str) -> None:
    assert redact(line).endswith(placeholder)


def test_born_without_a_date_is_kept() -> None:
    line = "Born and raised in Chiang Mai"
    assert redact(line) == line


# ── Addresses ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "Address: Condo One, Floor 12",
        "ที่อยู่ปัจจุบัน คอนโดวัน ชั้น 12",
        "88/8 Soi Ari 4, Phaya Thai, Bangkok 10400",
        "บ้านเลขที่ 7 ซอยลาดพร้าว 101 แขวงคลองจั่น เขตบางกะปิ กรุงเทพฯ",
        "หมู่บ้านสุขใจ 12/3 ต.ในเมือง อ.เมือง จ.ขอนแก่น 40000",
    ],
)
def test_addresses_are_redacted(line: str) -> None:
    assert kinds(line) == [PiiKind.ADDRESS]


@pytest.mark.parametrize(
    "line",
    [
        "Bangkok, Thailand",
        "กรุงเทพมหานคร",
        "Open to relocation to Chiang Mai province",
        "Addressed customer complaints within 24 hours",
        "06/2020 - 08/2022 เขตบางรัก",  # dates are not house numbers
        "ทำงาน จ.-ศ. 8.00-17.00 น.",
    ],
)
def test_non_addresses_are_kept(line: str) -> None:
    assert redact(line) == line


# ── Names ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("นางสาวสมหญิง รักงาน\nประสบการณ์", "สมหญิง"),
        ("1. นายวิชัย มั่นคง ผู้จัดการฝ่ายบุคคล", "วิชัย"),
        ("ชื่อ-นามสกุล: นายสมศักดิ์ ทดสอบ", "สมศักดิ์"),
        ("Name: Anan Sample", "Anan"),
        ("Dr. Napat Sample, Supervisor", "Napat"),
        ("Ms Ploy Sample and team", "Ploy"),
    ],
)
def test_names_are_redacted(text: str, secret: str) -> None:
    assert secret not in redact(text)


def test_name_words_are_redacted_everywhere() -> None:
    out = redact("Somchai Example\nSummary\nSomchai built the platform. Example award winner.")
    assert "Somchai" not in out
    assert "Example" not in out


def test_extra_names_are_redacted() -> None:
    out = redact("Worked with Kanokwan on the launch", extra_names=["Kanokwan"])
    assert "Kanokwan" not in out


@pytest.mark.parametrize(
    "text",
    [
        "Software Engineer\nsomchai@example.com",  # title, not a name
        "Curriculum Vitae\nExperience",
        "Company Name: Example Logistics",
        "Project name: Payment Gateway",
        "Skills: MS Excel, Ms Word",
        "นายจ้างพึงพอใจในผลงาน",
        "ชื่อเสียงของบริษัทดีขึ้น",
    ],
)
def test_non_names_are_kept(text: str) -> None:
    out = redact(text)
    assert PiiKind.NAME not in kinds(text), out


def test_thai_name_word_inside_other_words_is_kept() -> None:
    # The surname "ใจดี" must not be cut out of "ใจดีมาก" (very kind).
    out = redact("นายสมชาย ใจดี\nเพื่อนร่วมงานบอกว่าใจดีมาก")
    assert "ใจดีมาก" in out


# ── Messaging, sensitive attributes, profile URLs ────────────────────────


@pytest.mark.parametrize(
    "line",
    ["Line ID: somchai.jd", "LINE: @somchai", "ไลน์: somchai99", "Skype: live:somchai"],
)
def test_messaging_ids(line: str) -> None:
    assert kinds(line) == [PiiKind.MESSAGING_ID]


def test_product_line_is_not_messaging_id() -> None:
    assert redact("Owned product line: Mobile banking") == "Owned product line: Mobile banking"


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("Religion: Buddhism", PiiKind.RELIGION),
        ("ศาสนาพุทธ", PiiKind.RELIGION),
        ("Marital Status: Single", PiiKind.MARITAL_STATUS),
        ("สถานภาพสมรส: สมรส", PiiKind.MARITAL_STATUS),
    ],
)
def test_sensitive_attributes(line: str, kind: PiiKind) -> None:
    assert kinds(line) == [kind]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/somchai-e",
        "github.com/somchai-dev",
        "https://facebook.com/somchai.e",
        "line.me/ti/p/~somchai",
    ],
)
def test_profile_urls(url: str) -> None:
    assert redact(f"Links: {url}") == "Links: [PROFILE_URL_1]"


def test_company_websites_are_kept() -> None:
    assert (
        redact("Website: https://example.co.th/careers") == "Website: https://example.co.th/careers"
    )


# ── Residual guard ───────────────────────────────────────────────────────


def test_residual_check_ignores_placeholders() -> None:
    assert find_residual_pii("[EMAIL_1] [PHONE_2] [DATE_OF_BIRTH_1 age 31]") == []


def test_residual_check_catches_leaks() -> None:
    leaked = f"mail x@example.com tel 081-234-5678 id {VALID_ID} github.com/someone"
    assert find_residual_pii(leaked) == [
        PiiKind.EMAIL,
        PiiKind.PHONE,
        PiiKind.NATIONAL_ID,
        PiiKind.PROFILE_URL,
    ]


@pytest.mark.parametrize(
    "text",
    [
        "นางพยาบาลวิชาชีพ โรงพยาบาลตัวอย่าง",  # job title "professional nurse"
        "นายช่างไฟฟ้า ประจำโรงงาน",
        "นายหน้าอสังหาริมทรัพย์",
        "นางแบบโฆษณา",
        "นายสมชาย",  # honorific + first name only: too ambiguous to be sure
    ],
)
def test_words_starting_with_honorifics_are_kept(text: str) -> None:
    assert PiiKind.NAME not in kinds(text)


def test_honorific_name_followed_by_text_in_same_segment() -> None:
    out = redact("1. นายวิชัย มั่นคง ผู้จัดการฝ่ายบุคคล โทร 02-123-4567")
    assert "วิชัย" not in out
    assert "มั่นคง" not in out
    assert "ผู้จัดการฝ่ายบุคคล" in out


@pytest.mark.parametrize("fixture", ["en_clean", "th_sarabun", "th_garuda"])
def test_real_pdf_fixtures_have_no_residual_pii(fixture: str) -> None:
    path = Path(__file__).parent / "fixtures" / "libreoffice" / f"{fixture}.pdf"
    text = extract_document(path.read_bytes()).text
    result = redact_pii(text, today=TODAY)
    assert find_residual_pii(result.text) == []
    assert result.count(PiiKind.EMAIL) == 1
    assert result.count(PiiKind.PHONE) == 1
    assert result.count(PiiKind.NAME) >= 1
    assert "somchai" not in result.text.casefold()
    assert "สมชาย" not in result.text


# ── Format variants found by stress testing ──────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Tel (081) 234-5678",
        "Office (02) 123-4567 ext. 12",
        "08-1234-5678",
        "0 8 1 2 3 4 5 6 7 8",
        "+66812345678",
        "+66(0)81-234-5678",
    ],
)
def test_phone_variants_are_redacted(text: str) -> None:
    assert PiiKind.PHONE in kinds(text)
    assert find_residual_pii(redact(text)) == []


@pytest.mark.parametrize(
    "text",
    [
        "somchai [at] example [dot] com",
        "somchai(at)example.com",
        "somchai at example dot co dot th",
    ],
)
def test_obfuscated_emails_are_redacted(text: str) -> None:
    assert kinds(text) == [PiiKind.EMAIL]


@pytest.mark.parametrize(
    "text",
    [
        "Worked at Agoda.com as a data engineer",
        "Interned at example.co.th for 3 months",
        "Look at the dashboard. Com metrics improved.",
    ],
)
def test_prose_with_at_is_not_an_email(text: str) -> None:
    assert redact(text) == text


def test_line_id_without_colon() -> None:
    assert kinds("Line ID somchai.jd") == [PiiKind.MESSAGING_ID]


def test_mobile_facebook_url_is_fully_redacted() -> None:
    assert redact("m.facebook.com/somchai.e") == "[PROFILE_URL_1]"


def test_address_label_stays_visible() -> None:
    out = redact("ที่อยู่: 99/1 หมู่ 3 ถ.พหลโยธิน จ.ปทุมธานี 12120")
    assert out == "ที่อยู่: [ADDRESS_1]"


def test_stoplisted_name_words_are_not_redacted_on_their_own() -> None:
    out = redact("Built data pipelines for Ann.", extra_names=["Ann Data"])
    assert out == "Built data pipelines for [NAME_1]."
