"""Thai text integrity checks.

PDF text extraction often breaks Thai: legacy fonts map vowels and tone marks to
Private Use Area code points, fonts without a ToUnicode map produce ``(cid:NN)``
tokens, and broken positioning yields marks detached from their consonant. An
ATS sees exactly the same broken text, so detecting it is a parseability check.

The orthographic rules here are a deliberately conservative subset of the WTT 2.0
input-sequence rules: they only flag sequences that cannot occur in correctly
encoded Thai.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = ["ThaiIntegrity", "analyze_thai"]

_CONSONANTS: Final = frozenset(chr(c) for c in range(0x0E01, 0x0E2F))  # ก-ฮ (incl. ฤ ฦ)
_ABOVE_BELOW_VOWELS: Final = frozenset("\u0e31\u0e34\u0e35\u0e36\u0e37\u0e38\u0e39\u0e3a")
_TONE_MARKS: Final = frozenset("\u0e48\u0e49\u0e4a\u0e4b")
_MAITAIKHU: Final = "\u0e47"
_OTHER_MARKS: Final = frozenset("\u0e4c\u0e4d\u0e4e")  # thanthakhat, nikhahit, yamakkan
_LEADING_VOWELS: Final = frozenset("\u0e40\u0e41\u0e42\u0e43\u0e44")  # เ แ โ ใ ไ
_SARA_AM: Final = "\u0e33"

_THAI_RANGE: Final = re.compile(r"[\u0e00-\u0e7f]")
# Legacy Thai fonts (e.g. old Microsoft/WTT encodings) place shifted vowels and
# tone marks at U+F700-U+F71F. Other Private Use code points are typically icon
# fonts (Font Awesome phone/e-mail icons) and are not a sign of broken Thai.
_THAI_PUA: Final = re.compile(r"[\uf700-\uf71f]")
_CID_TOKEN: Final = re.compile(r"\(cid:\d+\)")
# C0/C1 control characters (except tab, newline, CR) appear where a glyph has no
# Unicode mapping, e.g. NUL bytes emitted for glyphs missing from the font.
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# A lone Thai character between non-Thai characters ("ป ร ะ ส บ"). Mai yamok (ๆ)
# is excluded because it is conventionally written with spaces around it.
_SPACED_THAI_CHAR: Final = re.compile(
    r"(?<![\u0e00-\u0e7f])[\u0e01-\u0e45\u0e47-\u0e4e](?![\u0e00-\u0e7f])"
)
_THAI_RUN: Final = re.compile(r"[\u0e00-\u0e7f]+")

# Thresholds above which text is considered corrupted (not just a typo).
_MAX_INVALID_RATIO: Final = 0.01
_MAX_SPACED_RATIO: Final = 0.30
_MIN_RUNS_FOR_SPACING_CHECK: Final = 20


@dataclass(frozen=True, slots=True)
class ThaiIntegrity:
    """Result of :func:`analyze_thai`."""

    thai_chars: int
    invalid_sequences: int
    private_use_chars: int
    cid_tokens: int
    replacement_chars: int
    control_chars: int
    spaced_char_ratio: float

    @property
    def invalid_ratio(self) -> float:
        return self.invalid_sequences / self.thai_chars if self.thai_chars else 0.0

    @property
    def is_corrupted(self) -> bool:
        """True when the text is very likely mangled by extraction."""
        return (
            self.private_use_chars > 0
            or self.cid_tokens > 0
            or self.replacement_chars > 0
            or self.control_chars > 0
            or self.invalid_ratio > _MAX_INVALID_RATIO
            or self.spaced_char_ratio > _MAX_SPACED_RATIO
        )


def _count_invalid_sequences(text: str) -> int:
    invalid = 0
    prev = ""
    for i, ch in enumerate(text):
        if ch in _ABOVE_BELOW_VOWELS or ch == _MAITAIKHU:
            if prev not in _CONSONANTS:
                invalid += 1
        elif ch in _TONE_MARKS or ch in _OTHER_MARKS:
            if prev not in _CONSONANTS and prev not in _ABOVE_BELOW_VOWELS:
                invalid += 1
        elif ch == _SARA_AM:
            if prev not in _CONSONANTS and prev not in _TONE_MARKS:
                invalid += 1
        elif ch in _LEADING_VOWELS:
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if nxt not in _CONSONANTS:
                invalid += 1
        prev = ch
    return invalid


def _spaced_char_ratio(text: str) -> float:
    runs = _THAI_RUN.findall(text)
    if len(runs) < _MIN_RUNS_FOR_SPACING_CHECK:
        return 0.0
    singles = len(_SPACED_THAI_CHAR.findall(text))
    return singles / len(runs)


def analyze_thai(text: str) -> ThaiIntegrity:
    """Measure how intact the Thai text in ``text`` is."""
    return ThaiIntegrity(
        thai_chars=len(_THAI_RANGE.findall(text)),
        invalid_sequences=_count_invalid_sequences(text),
        private_use_chars=len(_THAI_PUA.findall(text)),
        cid_tokens=len(_CID_TOKEN.findall(text)),
        replacement_chars=text.count("\ufffd"),
        control_chars=len(_CONTROL.findall(text)),
        spaced_char_ratio=_spaced_char_ratio(text),
    )
