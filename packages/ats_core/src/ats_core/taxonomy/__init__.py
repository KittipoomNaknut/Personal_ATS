"""Bilingual skill taxonomy and skill mention detection.

Latin-script aliases are matched with regular expressions that respect token
boundaries suited to tech terms (so "Java" does not match inside "JavaScript"
and "C++" / ".NET" / "Node.js." work). Thai has no spaces between words, so Thai
aliases are matched on PyThaiNLP word boundaries; the aliases are added to the
tokenizer dictionary so a skill such as "การทำงานเป็นทีม" stays one token.
"""

from __future__ import annotations

import re
import tomllib
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any, Final

from pythainlp.corpus.common import thai_words
from pythainlp.tokenize import word_tokenize
from pythainlp.util import Trie, dict_trie

__all__ = ["Skill", "SkillMention", "SkillTaxonomy", "TaxonomyError", "default_taxonomy"]

_LATIN_EDGE: Final = r"A-Za-z0-9_"
_THAI_CHAR: Final = re.compile(r"[฀-๿]")


class TaxonomyError(ValueError):
    """The taxonomy data is invalid (duplicate ids or ambiguous aliases)."""


@dataclass(frozen=True, slots=True)
class Skill:
    id: str
    name: str
    category: str
    aliases: tuple[str, ...] = ()
    exact_case: tuple[str, ...] = ()
    aliases_th: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillMention:
    skill_id: str
    text: str
    """The text exactly as written in the document."""
    start: int
    end: int


def _alias_key(alias: str) -> str:
    return re.sub(r"[\s\-]+", " ", unicodedata.normalize("NFC", alias).casefold()).strip()


def _alias_regex(alias: str) -> str:
    """Regex for one alias: spaces/hyphens are interchangeable, boundaries are tech-aware."""
    parts = [re.escape(part) for part in re.split(r"[\s\-]+", alias.strip())]
    body = r"[\s\-]+".join(parts)
    # A letter/digit may not touch the alias; "+" and "#" may not follow it either,
    # so "C" does not match inside "C++" or "C#". A trailing "." is allowed
    # (end of sentence: "Node.js.").
    # A preceding "." is also disallowed (so "vue" does not match "ex.vue"), except
    # for aliases that themselves start with "." such as ".net".
    preceding = _LATIN_EDGE if alias.startswith(".") else _LATIN_EDGE + "."
    before = rf"(?<![{preceding}])"
    after = rf"(?![{_LATIN_EDGE}+#]|\.[{_LATIN_EDGE}])"
    if alias[-1] in "+#":
        after = rf"(?![{_LATIN_EDGE}+#])"
    if len(alias) == 1:
        # One-letter languages (C, R) need extra care: "Grade C-", "R&D", "C's".
        before = rf"(?<![{_LATIN_EDGE}.&\-])"
        after = rf"(?![{_LATIN_EDGE}+#&'\-]|\.[{_LATIN_EDGE}])"
    return f"{before}{body}{after}"


class SkillTaxonomy:
    """A validated set of skills with fast mention detection."""

    def __init__(self, skills: Iterable[Skill]) -> None:
        self._skills: dict[str, Skill] = {}
        folded: dict[str, str] = {}
        exact: dict[str, str] = {}
        thai: dict[str, str] = {}
        for skill in skills:
            if skill.id in self._skills:
                raise TaxonomyError(f"duplicate skill id: {skill.id}")
            self._skills[skill.id] = skill
            for alias in skill.aliases:
                self._register(folded, _alias_key(alias), skill.id, alias)
            for alias in skill.exact_case:
                self._register(exact, alias, skill.id, alias)
            for alias in skill.aliases_th:
                if not _THAI_CHAR.search(alias):
                    raise TaxonomyError(f"{skill.id}: Thai alias has no Thai text: {alias!r}")
                self._register(thai, unicodedata.normalize("NFC", alias), skill.id, alias)
        overlap = set(folded) & {_alias_key(a) for a in exact}
        if overlap:
            raise TaxonomyError(f"aliases listed both case-insensitively and exactly: {overlap}")

        self._folded = folded
        self._exact = exact
        self._folded_re = self._compile(folded, flags=re.IGNORECASE)
        self._exact_re = self._compile(exact, flags=0)
        # Each Thai alias is added to the tokenizer dictionary, so it always comes
        # out as a single token and matching is a token lookup.
        self._thai_trie: Trie = dict_trie(set(thai_words()) | set(thai))
        self._thai = thai

    @staticmethod
    def _register(index: dict[str, str], key: str, skill_id: str, alias: str) -> None:
        if not key:
            raise TaxonomyError(f"{skill_id}: empty alias")
        owner = index.get(key)
        if owner is not None and owner != skill_id:
            raise TaxonomyError(f"alias {alias!r} is used by both {owner} and {skill_id}")
        index[key] = skill_id

    @staticmethod
    def _compile(index: Mapping[str, str], flags: int) -> re.Pattern[str] | None:
        if not index:
            return None
        # Longest first, so "spring boot" wins over "spring" and "c++" over "c".
        ordered = sorted(index, key=len, reverse=True)
        return re.compile("|".join(_alias_regex(a) for a in ordered), flags)

    def _tokens(self, text: str) -> list[str]:
        return list(
            word_tokenize(text, engine="newmm", custom_dict=self._thai_trie, keep_whitespace=True)
        )

    # ── Lookup ──────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, skill_id: object) -> bool:
        return skill_id in self._skills

    def get(self, skill_id: str) -> Skill:
        return self._skills[skill_id]

    @property
    def skills(self) -> tuple[Skill, ...]:
        return tuple(self._skills.values())

    # ── Detection ───────────────────────────────────────────────────────

    def _latin_mentions(self, text: str) -> list[SkillMention]:
        found: list[SkillMention] = []
        if self._folded_re is not None:
            for m in self._folded_re.finditer(text):
                found.append(
                    SkillMention(self._folded[_alias_key(m.group())], m.group(), *m.span())
                )
        if self._exact_re is not None:
            for m in self._exact_re.finditer(text):
                found.append(SkillMention(self._exact[m.group()], m.group(), *m.span()))
        return found

    def _thai_mentions(self, text: str) -> list[SkillMention]:
        if not _THAI_CHAR.search(text):
            return []
        # Maximal-matching (newmm) can split ambiguous text so that an alias steals
        # characters from its neighbour ("จัดการบัญชี" -> "จัด|การบัญชี"). A mention
        # only counts if it also starts and ends on a word boundary of an
        # independent segmentation (longest-matching). Measured on the test
        # sentences, this removed the false positives without losing a true one.
        boundaries = {0}
        position = 0
        for token in word_tokenize(text, engine="longest", keep_whitespace=True):
            position += len(token)
            boundaries.add(position)
        found: list[SkillMention] = []
        position = 0
        for token in self._tokens(text):
            start, end = position, position + len(token)
            skill_id = self._thai.get(token)
            if skill_id is not None and start in boundaries and end in boundaries:
                found.append(SkillMention(skill_id, token, start, end))
            position = end
        return found

    def find(self, text: str) -> list[SkillMention]:
        """Return skill mentions in ``text`` in order; overlapping matches keep the longest."""
        text = unicodedata.normalize("NFC", text)
        candidates = self._latin_mentions(text) + self._thai_mentions(text)
        chosen: list[SkillMention] = []
        for mention in sorted(candidates, key=lambda m: (-(m.end - m.start), m.start)):
            if all(mention.end <= c.start or mention.start >= c.end for c in chosen):
                chosen.append(mention)
        return sorted(chosen, key=lambda m: m.start)

    def skill_ids(self, text: str) -> set[str]:
        return {m.skill_id for m in self.find(text)}


def _skill_from(entry: Mapping[str, Any]) -> Skill:
    try:
        return Skill(
            id=str(entry["id"]),
            name=str(entry["name"]),
            category=str(entry["category"]),
            aliases=tuple(entry.get("aliases", ())),
            exact_case=tuple(entry.get("exact_case", ())),
            aliases_th=tuple(entry.get("aliases_th", ())),
        )
    except KeyError as exc:
        raise TaxonomyError(f"skill entry missing field {exc}: {dict(entry)}") from exc


def load_taxonomy(data: str) -> SkillTaxonomy:
    """Build a taxonomy from TOML text (see ``skills.toml`` for the format)."""
    parsed = tomllib.loads(data)
    return SkillTaxonomy(_skill_from(entry) for entry in parsed.get("skill", []))


@cache
def default_taxonomy() -> SkillTaxonomy:
    """The bundled taxonomy (loaded once per process)."""
    data = resources.files(__package__).joinpath("skills.toml").read_text(encoding="utf-8")
    return load_taxonomy(data)
