from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from builders import CLEAN_RESUME_LINES, docx_bytes, new_docx

from ats_core import __version__
from ats_core.cli import EXIT_OK, EXIT_UNPARSEABLE, EXIT_UNREADABLE, EXIT_USAGE, main

FIXTURES = Path(__file__).parent / "fixtures" / "libreoffice"
EN_RESUME = FIXTURES / "en_clean.pdf"
TH_RESUME = FIXTURES / "th_sarabun.pdf"
EN_JD = "Requirements\n- Python, FastAPI and PostgreSQL\n- K8s\nNice to have\n- Kafka\n"


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out=out)
    return code, out.getvalue()


@pytest.fixture
def jd_file(tmp_path: Path) -> Path:
    path = tmp_path / "jd.txt"
    path.write_text(EN_JD, encoding="utf-8")
    return path


# ── Happy paths ──────────────────────────────────────────────────────────


def test_english_report(jd_file: Path) -> None:
    code, out = run("check", str(EN_RESUME), "--jd", str(jd_file), "--lang", "en")
    assert code == EXIT_OK
    assert "ATS resume check: en_clean.pdf" in out
    assert "ATS readability: 1.00 / 1.00" in out
    assert "No readability problems found." in out
    assert "Required: 4/4 (100%)" in out
    assert '[x] Kubernetes  (resume says "Kubernetes", job description says "K8s"' in out
    assert "Preferred: 0/1 (0%)" in out
    assert "[ ] Apache Kafka" in out
    assert "names" in out


def test_thai_report_is_default() -> None:
    code, out = run("check", str(TH_RESUME))
    assert code == EXIT_OK
    assert "ผลตรวจ resume สำหรับ ATS: th_sarabun.pdf" in out
    assert "[คำเตือน] วันที่ใช้ปี พ.ศ." in out
    assert "ไม่ได้ระบุ Job Description" in out


def test_report_never_contains_personal_values(jd_file: Path) -> None:
    for argv in (
        ("check", str(EN_RESUME), "--jd", str(jd_file)),
        ("check", str(EN_RESUME), "--json"),
        ("check", str(TH_RESUME), "--lang", "en"),
    ):
        _, out = run(*argv)
        lowered = out.casefold()
        assert "somchai" not in lowered
        assert "081-234-5678" not in out
        assert "สมชาย" not in out


def test_json_report(jd_file: Path) -> None:
    code, out = run("check", str(EN_RESUME), "--jd", str(jd_file), "--json", "--lang", "en")
    assert code == EXIT_OK
    data = json.loads(out)
    assert data["report_version"] == 1
    assert data["format"] == "pdf"
    assert data["pages"] == 1
    assert data["parseability"]["score"] == 1.0
    assert data["parseability"]["issues"] == []
    assert data["keywords"]["required_coverage"] == 1.0
    kubernetes = next(m for m in data["keywords"]["matches"] if m["skill_id"] == "kubernetes")
    assert kubernetes == {
        "skill_id": "kubernetes",
        "name": "Kubernetes",
        "importance": "required",
        "jd_terms": ["K8s"],
        "found": True,
        "exact_term": False,
        "resume_terms": ["Kubernetes"],
    }
    # NAME counts occurrences: the full name plus the surname "Example", which the
    # synthetic resume also uses in "Example Co." and "Example University".
    assert data["privacy"] == {"NAME": 3, "EMAIL": 1, "PHONE": 1}


def test_json_issue_messages_follow_language() -> None:
    data = json.loads(run("check", str(TH_RESUME), "--json")[1])
    (issue,) = data["parseability"]["issues"]
    assert issue["code"] == "buddhist_era_years"
    assert issue["problem"].startswith("วันที่ใช้ปี พ.ศ.")
    assert data["keywords"] is None


def test_show_redacted_prints_text_without_personal_data() -> None:
    code, out = run("check", str(EN_RESUME), "--show-redacted", "--lang", "en")
    assert code == EXIT_OK
    redacted = out.split("-" * 60 + "\n", 1)[1]
    assert "[EMAIL_1]" in redacted
    assert "Backend engineer with 5 years of experience" in redacted
    assert "somchai" not in redacted.casefold()


def test_extra_name_is_redacted(tmp_path: Path) -> None:
    document = new_docx()
    for line in ["Experience", "Mentored Kanokwan on the data team", "Skills", "Python"]:
        document.add_paragraph(line)
    resume = tmp_path / "resume.docx"
    resume.write_bytes(docx_bytes(document))
    _, without = run("check", str(resume), "--show-redacted")
    _, with_name = run("check", str(resume), "--show-redacted", "--name", "Kanokwan")
    assert "Kanokwan" in without
    assert "Kanokwan" not in with_name


@pytest.mark.parametrize("kind", ["docx", "pdf"])
def test_job_description_may_be_a_document(tmp_path: Path, kind: str) -> None:
    jd = tmp_path / f"jd.{kind}"
    if kind == "docx":
        document = new_docx()
        for line in EN_JD.splitlines():
            document.add_paragraph(line)
        jd.write_bytes(docx_bytes(document))
    else:
        jd.write_bytes(EN_RESUME.read_bytes())  # any PDF: its skills become the "JD"
    code, out = run("check", str(EN_RESUME), "--jd", str(jd), "--lang", "en")
    assert code == EXIT_OK
    assert "Required:" in out


def test_resume_as_docx(tmp_path: Path) -> None:
    document = new_docx()
    for line in CLEAN_RESUME_LINES:
        document.add_paragraph(line)
    resume = tmp_path / "resume.docx"
    resume.write_bytes(docx_bytes(document))
    code, out = run("check", str(resume), "--lang", "en")
    assert code == EXIT_OK
    assert "Format: DOCX" in out


def test_jd_text_with_invalid_utf8_is_tolerated(tmp_path: Path) -> None:
    jd = tmp_path / "jd.txt"
    jd.write_bytes(b"Requirements\n- Python \xff\xfe\n")
    code, out = run("check", str(EN_RESUME), "--jd", str(jd), "--lang", "en")
    assert code == EXIT_OK
    assert "[x] Python" in out


def test_jd_without_known_skills(tmp_path: Path) -> None:
    jd = tmp_path / "jd.txt"
    jd.write_text("We are hiring friendly people.", encoding="utf-8")
    _, out = run("check", str(EN_RESUME), "--jd", str(jd), "--lang", "en")
    assert "No recognised skills in the job description." in out


def test_issues_are_listed_with_fixes(tmp_path: Path) -> None:
    document = new_docx()
    document.add_paragraph("Python developer")
    resume = tmp_path / "thin.docx"
    resume.write_bytes(docx_bytes(document))
    code, out = run("check", str(resume), "--lang", "en")
    assert code == EXIT_OK
    assert "[CRITICAL] Almost no text could be extracted." in out
    assert "Fix: Export the resume as a text-based PDF or DOCX." in out
    assert "[WARNING]" in out
    assert "No personal data detected." in out


# ── Errors ───────────────────────────────────────────────────────────────


def test_missing_resume(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run("check", str(tmp_path / "nope.pdf"))
    assert code == EXIT_UNREADABLE
    assert out == ""
    assert "cannot read" in capsys.readouterr().err


def test_unsupported_resume(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    resume = tmp_path / "resume.txt"
    resume.write_text("plain text resume", encoding="utf-8")
    assert run("check", str(resume))[0] == EXIT_UNPARSEABLE
    assert "Only PDF and DOCX" in capsys.readouterr().err


def test_corrupt_job_description(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    jd = tmp_path / "jd.pdf"
    jd.write_bytes(b"%PDF-1.4 broken")
    assert run("check", str(EN_RESUME), "--jd", str(jd))[0] == EXIT_UNPARSEABLE
    assert "cannot parse job description" in capsys.readouterr().err


def test_oversized_text_job_description(tmp_path: Path) -> None:
    jd = tmp_path / "jd.txt"
    jd.write_bytes(b"a" * (1024 * 1024 + 1))
    assert run("check", str(EN_RESUME), "--jd", str(jd))[0] == EXIT_UNPARSEABLE


@pytest.mark.parametrize(
    "argv",
    [[], ["check"], ["frobnicate"], ["check", "x.pdf", "--lang", "fr"]],
    ids=["no-command", "no-file", "bad-command", "bad-lang"],
)
def test_usage_errors(argv: list[str]) -> None:
    assert run(*argv)[0] == EXIT_USAGE


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--version")[0] == EXIT_OK
    assert __version__ in capsys.readouterr().out


def test_module_entry_point() -> None:
    result = subprocess.run(  # noqa: S603 - fixed arguments
        [sys.executable, "-m", "ats_core", "check", str(EN_RESUME), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == EXIT_OK
    assert json.loads(result.stdout)["format"] == "pdf"


def test_hidden_keywords_earn_no_credit(tmp_path: Path) -> None:
    jd = tmp_path / "jd.txt"
    jd.write_text("Requirements\n- Python\n- Terraform and Ansible\n", encoding="utf-8")
    data = json.loads(
        run("check", str(FIXTURES / "en_white_on_white.pdf"), "--jd", str(jd), "--json")[1]
    )
    found = {m["skill_id"]: m["found"] for m in data["keywords"]["matches"]}
    # Terraform and Ansible exist only as white-on-white text.
    assert found == {"python": True, "terraform": False, "ansible": False}
    assert "hidden_text" in {i["code"] for i in data["parseability"]["issues"]}


def test_page_count_wording() -> None:
    assert "Format: PDF, 1 page\n" in run("check", str(EN_RESUME), "--lang", "en")[1]
