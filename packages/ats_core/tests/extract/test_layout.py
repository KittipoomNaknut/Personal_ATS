from __future__ import annotations

from ats_core.extract.layout import detect_multi_column
from ats_core.extract.models import BBox

W, H = 612.0, 792.0


def box(x0: float, top: float, x1: float, height: float = 10) -> BBox:
    return BBox(x0, top, x1, top + height)


def single_column(lines: int = 30) -> list[BBox]:
    boxes: list[BBox] = []
    for i in range(lines):
        top = 72 + i * 16
        # A justified line made of words with normal 3 pt spaces.
        x = 72.0
        while x < 520:
            boxes.append(box(x, top, x + 40))
            x += 43
    return boxes


def test_single_column_page_is_not_multi_column() -> None:
    assert not detect_multi_column(single_column(), W, H)


def test_sidebar_layout_is_multi_column() -> None:
    sidebar = [box(40, 72 + i * 16, 170) for i in range(25)]
    main = [box(220, 72 + i * 16, 560) for i in range(30)]
    assert detect_multi_column(sidebar + main, W, H)


def test_right_aligned_dates_are_not_columns() -> None:
    words: list[BBox] = []
    top = 72.0
    for _job in range(5):
        words += [box(72, top, 300), box(470, top, 540)]  # title ........ dates
        top += 16
        for _bullet in range(3):  # bullets span the full width
            words.append(box(85, top, 540))
            top += 16
    assert not detect_multi_column(words, W, H)


def test_short_side_by_side_block_is_not_enough() -> None:
    # Two columns for only 3 lines (e.g. a contact row) do not count.
    words = single_column(20) + [box(72, 500 + i * 16, 250) for i in range(3)]
    words += [box(350, 500 + i * 16, 540) for i in range(3)]
    assert not detect_multi_column(words, W, H)


def test_degenerate_inputs() -> None:
    assert not detect_multi_column([], W, H)
    assert not detect_multi_column([box(72, 72, 100)], 0, H)
    assert not detect_multi_column([box(72, 72, 100)], W, 0)
