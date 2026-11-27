from __future__ import annotations

import pytest

from ats_core.text.sections import Section, detect_section


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Experience", Section.EXPERIENCE),
        ("WORK EXPERIENCE", Section.EXPERIENCE),
        ("Professional Experience:", Section.EXPERIENCE),
        ("ประสบการณ์ทำงาน", Section.EXPERIENCE),
        ("ประวัติการทำงาน", Section.EXPERIENCE),
        ("Education", Section.EDUCATION),
        ("ประวัติการศึกษา", Section.EDUCATION),
        ("Skills", Section.SKILLS),
        ("— Technical Skills —", Section.SKILLS),
        ("ทักษะ", Section.SKILLS),
        ("ทักษะ (Skills)", Section.SKILLS),
        ("Experience / ประสบการณ์", Section.EXPERIENCE),
        ("Licenses & Certifications", Section.CERTIFICATIONS),
        ("ภาษา", Section.LANGUAGES),
        ("Summary", Section.SUMMARY),
        ("•  Projects  •", Section.PROJECTS),
    ],
)
def test_detects_headings(line: str, expected: Section) -> None:
    assert detect_section(line) is expected


@pytest.mark.parametrize(
    "line",
    [
        "Built Python services with 5 years of experience in backend development",
        "Experienced engineer",
        "Skills matter",
        "Python, FastAPI, PostgreSQL",
        "",
    ],
)
def test_ignores_non_headings(line: str) -> None:
    assert detect_section(line) is None
