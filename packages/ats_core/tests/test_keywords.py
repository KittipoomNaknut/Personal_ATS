from __future__ import annotations

from pathlib import Path

import pytest

from ats_core.extract import extract_document
from ats_core.keywords import Importance, extract_jd_skills, match_keywords

JD_EN = """Senior Backend Engineer
About us
We are a fintech company and teach a free Python course.
Responsibilities
- Build microservices in Go and Python
Requirements:
- 5+ years with Python and PostgreSQL
- Experience with K8s and Docker
- Good communication skills in English
Nice to have
- Kafka, Terraform
Other
- AWS experience is a plus
Benefits
- Free Kafka training
"""

JD_TH = """ตำแหน่ง วิศวกรซอฟต์แวร์
หน้าที่ความรับผิดชอบ
- พัฒนาระบบด้วย Python และ Docker
คุณสมบัติผู้สมัคร
- มีประสบการณ์ด้าน SQL อย่างน้อย 3 ปี
- มีทักษะการสื่อสารและการทำงานเป็นทีม
- หากมีประสบการณ์ AWS จะพิจารณาเป็นพิเศษ
สวัสดิการ
- ประกันสุขภาพ และคอร์สเรียน Kubernetes ฟรี
"""


def importance(jd: str) -> dict[str, Importance]:
    return {s.skill.id: s.importance for s in extract_jd_skills(jd)}


# ── Job description analysis ─────────────────────────────────────────────


def test_english_jd_sections_and_cues() -> None:
    assert importance(JD_EN) == {
        "microservices": Importance.REQUIRED,
        "go": Importance.REQUIRED,
        "python": Importance.REQUIRED,
        "postgresql": Importance.REQUIRED,
        "kubernetes": Importance.REQUIRED,
        "docker": Importance.REQUIRED,
        "communication": Importance.REQUIRED,
        "english": Importance.REQUIRED,
        "kafka": Importance.PREFERRED,  # benefits mention ignored
        "terraform": Importance.PREFERRED,
        "aws": Importance.PREFERRED,  # "is a plus" cue
    }


def test_thai_jd_sections_and_cues() -> None:
    assert importance(JD_TH) == {
        "python": Importance.REQUIRED,
        "docker": Importance.REQUIRED,
        "sql": Importance.REQUIRED,
        "communication": Importance.REQUIRED,
        "teamwork": Importance.REQUIRED,
        "aws": Importance.PREFERRED,  # "จะพิจารณาเป็นพิเศษ"
    }  # Kubernetes only appears under สวัสดิการ (benefits)


def test_required_anywhere_beats_preferred() -> None:
    jd = "Nice to have\n- Docker\nRequirements\n- Docker and Linux"
    assert importance(jd) == {"docker": Importance.REQUIRED, "linux": Importance.REQUIRED}


def test_jd_terms_keep_original_spelling() -> None:
    (skill,) = extract_jd_skills("Requirements\n- K8s or Kubernetes")
    assert skill.terms == ("K8s", "Kubernetes")


@pytest.mark.parametrize(
    "heading",
    [
        "Requirements:",
        "## Qualifications",
        "What you'll need",
        "Qualifications / คุณสมบัติ",
        "คุณสมบัติที่ต้องการ",
        "MUST HAVE",
    ],
)
def test_required_headings(heading: str) -> None:
    assert importance(f"Benefits\n- x\n{heading}\n- Docker") == {"docker": Importance.REQUIRED}


@pytest.mark.parametrize(
    "heading",
    ["Nice to have:", "Preferred qualifications", "Bonus points", "คุณสมบัติที่จะพิจารณาเป็นพิเศษ"],
)
def test_preferred_headings(heading: str) -> None:
    assert importance(f"{heading}\n- Docker") == {"docker": Importance.PREFERRED}


@pytest.mark.parametrize(
    "heading", ["Benefits", "What we offer", "About us", "สวัสดิการ", "How to apply"]
)
def test_ignored_headings(heading: str) -> None:
    assert importance(f"{heading}\n- Docker") == {}


def test_sentence_starting_with_heading_word_is_not_a_heading() -> None:
    # "Benefits of ..." is a sentence in the requirements, not a benefits section.
    jd = "Requirements\nBenefits of microservices and Docker must be understood"
    assert importance(jd) == {"microservices": Importance.REQUIRED, "docker": Importance.REQUIRED}


def test_jd_without_headings_is_all_required() -> None:
    assert importance("We need Python and SQL. Docker would be a plus.") == {
        "python": Importance.REQUIRED,
        "sql": Importance.REQUIRED,
        "docker": Importance.PREFERRED,
    }


# ── Resume comparison ────────────────────────────────────────────────────


def test_match_report() -> None:
    resume = (
        "Skills: Python, Kubernetes, Docker, PostgreSQL, Terraform, React\nทักษะการสื่อสารภาษาอังกฤษ"
    )
    report = match_keywords(resume, JD_EN)
    assert report.required_coverage == pytest.approx(6 / 8)
    assert report.preferred_coverage == pytest.approx(1 / 3)
    assert {m.requirement.skill.id for m in report.missing} == {
        "microservices",
        "go",
        "kafka",
        "aws",
    }
    # Found, but not with the JD's own wording -> exact-term advice.
    assert {m.requirement.skill.id for m in report.synonym_only} == {
        "kubernetes",
        "communication",
        "english",
    }
    assert [s.id for s in report.resume_only] == ["react"]


def test_exact_term_is_case_and_spacing_insensitive() -> None:
    report = match_keywords("ci-cd pipelines", "Requirements\n- CI/CD\n- ci cd")
    (match,) = report.matches
    assert match.found
    assert match.exact_term


def test_empty_jd_has_no_coverage() -> None:
    report = match_keywords("Python developer", "We are hiring!")
    assert report.matches == ()
    assert report.required_coverage is None
    assert report.preferred_coverage is None
    assert [s.id for s in report.resume_only] == ["python"]


def test_thai_resume_against_thai_jd() -> None:
    resume = "ใช้ Python และ Docker ทุกวัน มีทักษะการทำงานเป็นทีม เขียน SQL ได้"
    report = match_keywords(resume, JD_TH)
    assert report.required_coverage == pytest.approx(4 / 5)
    assert [m.requirement.skill.id for m in report.missing] == ["communication", "aws"]


def test_long_lines_and_rules_are_not_headings() -> None:
    long_line = "Requirements " + "x" * 60 + " Docker"
    jd = f"Benefits\n----\n{long_line}"
    # The long line is content (inside Benefits), and "----" is just a rule.
    assert importance(jd) == {}
    assert importance("Requirements\n----\n- Docker") == {"docker": Importance.REQUIRED}


@pytest.mark.parametrize(
    ("fixture", "jd", "found", "missing"),
    [
        (
            "en_clean",
            "Requirements\n- Python, FastAPI and PostgreSQL\n- Kubernetes\nNice to have\n- Kafka",
            {"python", "fastapi", "postgresql", "kubernetes"},
            {"kafka"},
        ),
        (
            "th_sarabun",
            "คุณสมบัติ\n- Python และ Kubernetes\n- มีทักษะการสื่อสาร\n- หากมีประสบการณ์ AWS จะพิจารณาเป็นพิเศษ",
            {"python", "kubernetes", "communication"},
            {"aws"},
        ),
    ],
)
def test_real_pdf_resume_against_jd(
    fixture: str, jd: str, found: set[str], missing: set[str]
) -> None:
    pdf = Path(__file__).parent / "fixtures" / "libreoffice" / f"{fixture}.pdf"
    report = match_keywords(extract_document(pdf.read_bytes()).text, jd)
    assert {m.requirement.skill.id for m in report.matches if m.found} == found
    assert {m.requirement.skill.id for m in report.missing} == missing


@pytest.mark.parametrize(
    ("resume", "jd", "exact"),
    [
        ("communication with stakeholders", "Requirements\n- communication skills", True),
        ("มีการสื่อสารที่ดี", "คุณสมบัติ\n- มีทักษะการสื่อสาร", True),
        ("Kubernetes", "Requirements\n- K8s", False),
        ("ภาษาอังกฤษ", "Requirements\n- English", False),
    ],
)
def test_exact_term_containment(resume: str, jd: str, exact: bool) -> None:
    (match,) = match_keywords(resume, jd).matches
    assert match.found
    assert match.exact_term is exact
