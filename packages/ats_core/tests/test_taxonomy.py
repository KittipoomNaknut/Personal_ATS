from __future__ import annotations

import re

import pytest

from ats_core.taxonomy import Skill, SkillTaxonomy, TaxonomyError, default_taxonomy, load_taxonomy

TAXONOMY = default_taxonomy()


def found(text: str) -> list[tuple[str, str]]:
    return [(m.skill_id, m.text) for m in TAXONOMY.find(text)]


def ids(text: str) -> list[str]:
    return [m.skill_id for m in TAXONOMY.find(text)]


# ── Bundled data integrity ───────────────────────────────────────────────


def test_bundled_taxonomy_loads() -> None:
    assert len(TAXONOMY) >= 100


def test_every_skill_is_well_formed() -> None:
    for skill in TAXONOMY.skills:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", skill.id), skill.id
        assert skill.name.strip(), skill.id
        assert skill.aliases or skill.exact_case or skill.aliases_th, skill.id


def test_default_taxonomy_is_cached() -> None:
    assert default_taxonomy() is TAXONOMY


# ── Latin-script matching ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Python, Go and C++ on Kubernetes (k8s).",
            ["python", "go", "cpp", "kubernetes", "kubernetes"],
        ),
        ("JavaScript and Java", ["javascript", "java"]),
        ("C#, C, R and Rust", ["csharp", "c", "r", "rust"]),
        ("ASP.NET and .NET Core", ["dotnet", "dotnet"]),
        ("Built with Node.js.", ["nodejs"]),
        ("Spring Boot microservices", ["spring", "microservices"]),
        ("full-stack CI/CD with GitHub Actions", ["ci_cd", "github_actions"]),
        ("machine-learning and Machine  Learning", ["machine_learning", "machine_learning"]),
        ("python3", ["python"]),
        ("PostgreSQL / Postgres", ["postgresql", "postgresql"]),
        ("React Native and React", ["react_native", "react"]),
        ("pandas, NumPy, scikit-learn", ["pandas", "numpy", "scikit_learn"]),
    ],
)
def test_latin_skills(text: str, expected: list[str]) -> None:
    assert ids(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "I react quickly and excel at go-to-market",  # ordinary words, lower case
        "Grade C- student",
        "file main.go and component app.vue",
        "Javanese culture",
        "Pythonic idioms",
        "R&D department",
        "sparked interest",
    ],
)
def test_no_false_matches(text: str) -> None:
    assert found(text) == []


def test_exact_case_aliases_only_match_their_case() -> None:
    assert ids("React") == ["react"]
    assert ids("react") == []
    assert ids("reactjs") == ["react"]  # unambiguous spellings ignore case
    assert ids("Golang and GO") == ["go"]


def test_longest_alias_wins() -> None:
    assert found("Spring Boot") == [("spring", "Spring Boot")]
    assert found("C++") == [("cpp", "C++")]
    assert found("React Native") == [("react_native", "React Native")]


def test_mention_offsets() -> None:
    text = "We use Kubernetes daily"
    (mention,) = TAXONOMY.find(text)
    assert text[mention.start : mention.end] == "Kubernetes"


# ── Thai matching ────────────────────────────────────────────────────────


def test_thai_skills_in_running_text() -> None:
    text = "มีทักษะการทำงานเป็นทีมและการสื่อสารที่ดี ใช้ภาษาไพธอนและคูเบอร์เนทีส"
    assert found(text) == [
        ("teamwork", "การทำงานเป็นทีม"),
        ("communication", "การสื่อสาร"),
        ("python", "ภาษาไพธอน"),
        ("kubernetes", "คูเบอร์เนทีส"),
    ]


def test_thai_offsets_point_into_text() -> None:
    text = "ประสบการณ์ด้านการวิเคราะห์ข้อมูลและการเรียนรู้ของเครื่อง"
    for mention in TAXONOMY.find(text):
        assert text[mention.start : mention.end] == mention.text


def test_thai_word_boundaries() -> None:
    # "ทีม" (team) alone is not the skill "การทำงานเป็นทีม" (teamwork).
    assert ids("ทีมของเรามีห้าคน") == []


@pytest.mark.parametrize(
    "text",
    [
        "จัดการบัญชีผู้ใช้ในระบบ",  # "manage user accounts": the alias must not steal "การ"
        "เปิดบัญชีธนาคาร",  # "open a bank account"
        "บัญชีผู้ใช้ถูกล็อก",
    ],
)
def test_thai_alias_must_align_with_default_word_boundaries(text: str) -> None:
    assert "accounting" not in ids(text)


@pytest.mark.parametrize("text", ["มีความรู้ด้านบัญชี", "ประสบการณ์งานบัญชี 3 ปี", "นักบัญชีอาวุโส"])
def test_thai_accounting_phrases(text: str) -> None:
    assert ids(text) == ["accounting"]


def test_mixed_thai_and_english() -> None:
    assert ids("พัฒนาด้วย Python และ Docker ร่วมกับการทำงานเป็นทีม") == ["python", "docker", "teamwork"]


def test_thai_text_without_skills() -> None:
    assert found("สวัสดีครับ ยินดีที่ได้รู้จัก") == []


def test_skill_ids_helper() -> None:
    assert TAXONOMY.skill_ids("Python, Python, Docker") == {"python", "docker"}


# ── Validation ───────────────────────────────────────────────────────────


def test_duplicate_skill_id_is_rejected() -> None:
    with pytest.raises(TaxonomyError, match="duplicate skill id"):
        SkillTaxonomy([Skill("a", "A", "x", ("a",)), Skill("a", "A2", "x", ("b",))])


def test_alias_shared_by_two_skills_is_rejected() -> None:
    with pytest.raises(TaxonomyError, match="used by both"):
        SkillTaxonomy([Skill("a", "A", "x", ("same",)), Skill("b", "B", "x", ("Same",))])


def test_alias_both_exact_and_folded_is_rejected() -> None:
    with pytest.raises(TaxonomyError, match="both case-insensitively and exactly"):
        SkillTaxonomy([Skill("a", "A", "x", ("go",)), Skill("b", "B", "x", exact_case=("Go",))])


def test_thai_alias_must_contain_thai() -> None:
    with pytest.raises(TaxonomyError, match="no Thai text"):
        SkillTaxonomy([Skill("a", "A", "x", aliases_th=("latin",))])


def test_empty_alias_is_rejected() -> None:
    with pytest.raises(TaxonomyError, match="empty alias"):
        SkillTaxonomy([Skill("a", "A", "x", ("  ",))])


def test_missing_field_is_reported() -> None:
    with pytest.raises(TaxonomyError, match="missing field"):
        load_taxonomy('[[skill]]\nid = "a"\nname = "A"\n')


def test_load_custom_taxonomy() -> None:
    taxonomy = load_taxonomy(
        '[[skill]]\nid = "kotlin"\nname = "Kotlin"\ncategory = "lang"\naliases = ["kotlin"]\n'
    )
    assert [m.skill_id for m in taxonomy.find("Kotlin and Java")] == ["kotlin"]


def test_taxonomy_without_latin_or_exact_aliases() -> None:
    taxonomy = SkillTaxonomy([Skill("teamwork", "Teamwork", "soft", aliases_th=("ทำงานเป็นทีม",))])
    assert [m.skill_id for m in taxonomy.find("ชอบทำงานเป็นทีม and Python")] == ["teamwork"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("C/C++ and R", ["c", "cpp", "r"]), ("Languages: C, R.", ["c", "r"]), ("C-level", [])],
)
def test_single_letter_languages(text: str, expected: list[str]) -> None:
    assert ids(text) == expected


def test_multi_word_thai_alias_needs_every_word() -> None:
    taxonomy = SkillTaxonomy([Skill("coding", "Coding", "x", aliases_th=("เขียน โปรแกรม",))])
    assert [m.text for m in taxonomy.find("ชอบเขียน โปรแกรม ทุกวัน")] == ["เขียน โปรแกรม"]
    assert taxonomy.find("ชอบเขียน หนังสือ") == []


def test_membership_and_lookup() -> None:
    assert "python" in TAXONOMY
    assert "cobol_on_cogs" not in TAXONOMY
    assert TAXONOMY.get("python").name == "Python"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("มีทักษะการแก้ปัญหาและการคิดวิเคราะห์", ["problem_solving", "analytical_thinking"]),
        ("ภาษาอังกฤษระดับดี ใช้ภาษาญี่ปุ่นและภาษาจีน", ["english", "japanese", "chinese"]),
        ("การบริหารโครงการและการบริหารเวลา", ["project_management", "time_management"]),
        ("ออกแบบไมโครเซอร์วิสและการออกแบบระบบ", ["microservices", "system_design"]),
        ("ชอบทำงานเป็นทีม", ["teamwork"]),
    ],
)
def test_thai_skills_survive_boundary_check(text: str, expected: list[str]) -> None:
    assert ids(text) == expected
