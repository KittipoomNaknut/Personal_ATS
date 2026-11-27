"""Page layout analysis: detecting side-by-side text columns.

Naive ATS text extraction reads a page line by line across its full width, so a
two-column resume (for example a skills sidebar next to the experience section)
comes out interleaved. We detect this by looking for a vertical *gutter*: an x
position that no word crosses over a long vertical stretch while text exists on
both sides of it.

Right-aligned dates ("Engineer ........ 2020 - 2023") do not trigger the
detector, because the bullet points between job titles cross the gutter and
break the stretch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ats_core.extract.models import BBox

__all__ = ["detect_multi_column"]

_GUTTER_HALF_WIDTH: Final = 4.0  # points; the gap must be at least 8 pt wide
_SCAN_STEP: Final = 2.0
_SCAN_FROM: Final = 0.15  # fraction of page width
_SCAN_TO: Final = 0.85
_LINE_TOLERANCE: Final = 3.0  # words whose tops differ less than this share a line
_MIN_LINES_PER_SIDE: Final = 5
_MIN_HEIGHT_FRACTION: Final = 0.25


@dataclass(slots=True)
class _Band:
    top: float
    bottom: float
    words: list[BBox]


def _group_into_bands(words: Sequence[BBox]) -> list[_Band]:
    bands: list[_Band] = []
    for word in sorted(words, key=lambda w: (w.top, w.x0)):
        if bands and abs(word.top - bands[-1].top) <= _LINE_TOLERANCE:
            band = bands[-1]
            band.words.append(word)
            band.bottom = max(band.bottom, word.bottom)
        else:
            bands.append(_Band(word.top, word.bottom, [word]))
    return bands


def _has_column_split_at(x: float, bands: Sequence[_Band], page_height: float) -> bool:
    lo, hi = x - _GUTTER_HALF_WIDTH, x + _GUTTER_HALF_WIDTH
    run_start: float | None = None
    left = right = 0
    for band in bands:
        crosses = any(w.x0 < hi and w.x1 > lo for w in band.words)
        if crosses:
            run_start, left, right = None, 0, 0
            continue
        if run_start is None:
            run_start = band.top
        left += any(w.x1 <= lo for w in band.words)
        right += any(w.x0 >= hi for w in band.words)
        tall_enough = (band.bottom - run_start) >= _MIN_HEIGHT_FRACTION * page_height
        if min(left, right) >= _MIN_LINES_PER_SIDE and tall_enough:
            return True
    return False


def detect_multi_column(words: Sequence[BBox], page_width: float, page_height: float) -> bool:
    """Return True if the words on a page are laid out in side-by-side columns."""
    if not words or page_width <= 0 or page_height <= 0:
        return False
    bands = _group_into_bands(words)
    x = page_width * _SCAN_FROM
    stop = page_width * _SCAN_TO
    while x <= stop:
        if _has_column_split_at(x, bands, page_height):
            return True
        x += _SCAN_STEP
    return False
