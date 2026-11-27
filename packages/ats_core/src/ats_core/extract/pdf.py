"""PDF text and layout extraction.

Text comes from PDFium (``pypdfium2``, BSD-3/Apache-2.0), the engine used by
Chrome. pdfminer.six was evaluated first but mis-decodes Thai in ordinary,
well-formed PDFs (e.g. LibreOffice exports with correct ToUnicode maps), which
would make the parseability audit blame good resumes. pdfplumber (built on
pdfminer) is still used for ruled-table detection, which only needs vector
graphics, not text.

Lines are rebuilt from character geometry rather than content-stream order:
characters are grouped by baseline and sorted left to right, with Thai vowels
and tone marks kept attached to their base consonant. A space is inserted where
the gap between glyphs exceeds a quarter of the font size.

Hidden text is judged visually: a character is hidden if it is invisible by
render mode, transparent, smaller than 2 pt, or its colour has almost no contrast
with the rendered background underneath it. A white heading on a dark sidebar is
therefore visible; white text on a white page is not.
"""

from __future__ import annotations

import ctypes
import io
import logging
import statistics
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

import pdfplumber
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from ats_core.extract.errors import (
    DocumentTooLargeError,
    EncryptedDocumentError,
    ExtractionError,
)
from ats_core.extract.layout import detect_multi_column
from ats_core.extract.models import BBox, ExtractedDocument, PageInfo, SourceFormat, TextLine

__all__ = ["MAX_PDF_PAGES", "extract_pdf"]

logger = logging.getLogger(__name__)

MAX_PDF_PAGES: Final = 20
_MIN_VISIBLE_SIZE: Final = 2.0
_MIN_CONTRAST: Final = 0.12  # luminance difference (0..1) between text and background
_SPACE_GAP_EM: Final = 0.25
_BASELINE_TOLERANCE_EM: Final = 0.3
_INVISIBLE_MODES: Final = frozenset(
    {pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE, pdfium_c.FPDF_TEXTRENDERMODE_CLIP}
)


@dataclass(slots=True)
class _Char:
    text: str
    index: int
    x: float  # glyph origin, PDF user space (y up)
    baseline: float
    x0: float
    x1: float
    bottom: float
    top: float
    size: float
    hidden: bool
    unmapped: bool


@dataclass(slots=True)
class _Cluster:
    """A base character plus the combining marks drawn on it."""

    text: str
    x: float
    x0: float
    x1: float
    bottom: float
    top: float
    size: float
    is_space: bool
    chars: list[_Char] = field(default_factory=list)


def _is_attached_mark(ch: str) -> bool:
    # Combining marks (Thai above/below vowels and tone marks) belong to the
    # preceding base character. Sara am (U+0E33) also attaches: some fonts draw
    # its nikhahit part on the previous consonant.
    return unicodedata.category(ch) in ("Mn", "Me") or ch == "ำ"


def _luminance(r: float, g: float, b: float) -> float:
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


class _Background:
    """Samples the rendered page to find the colour behind a glyph."""

    def __init__(self, page: pdfium.PdfPage) -> None:
        self._bitmap = page.render(scale=1, may_draw_forms=True)
        self._posconv = self._bitmap.get_posconv(page)
        self._buffer = self._bitmap.buffer
        self._stride = self._bitmap.stride
        self._channels = self._bitmap.n_channels
        self._w, self._h = self._bitmap.width, self._bitmap.height

    def close(self) -> None:
        self._bitmap.close()

    def _pixel(self, x: float, y: float) -> float:
        px, py = self._posconv.to_bitmap(x, y)
        px = min(max(int(px), 0), self._w - 1)
        py = min(max(int(py), 0), self._h - 1)
        offset = py * self._stride + px * self._channels
        b, g, r = self._buffer[offset], self._buffer[offset + 1], self._buffer[offset + 2]
        return _luminance(r, g, b)

    def luminance(self, x0: float, bottom: float, x1: float, top: float) -> float:
        # Corners and edge midpoints of the loose glyph box: mostly background,
        # occasionally ink, so the median is the background.
        xm = (x0 + x1) / 2
        samples = [
            self._pixel(x, y)
            for x, y in ((x0, bottom), (x1, bottom), (x0, top), (x1, top), (xm, top), (xm, bottom))
        ]
        return statistics.median(samples)


def _read_chars(textpage: pdfium.PdfTextPage, background: _Background) -> list[_Char]:
    handle = textpage.raw
    count = pdfium_c.FPDFText_CountChars(handle)
    left, right, bottom, top = (ctypes.c_double() for _ in range(4))
    origin_x, origin_y = ctypes.c_double(), ctypes.c_double()
    loose = pdfium_c.FS_RECTF()
    r, g, b, a = (ctypes.c_uint() for _ in range(4))
    chars: list[_Char] = []
    for i in range(count):
        if pdfium_c.FPDFText_IsGenerated(handle, i) == 1:
            continue  # spaces / line breaks invented by PDFium; we rebuild our own
        text = chr(pdfium_c.FPDFText_GetUnicode(handle, i))
        if text in "\r\n":
            continue
        pdfium_c.FPDFText_GetCharOrigin(handle, i, origin_x, origin_y)
        pdfium_c.FPDFText_GetCharBox(handle, i, left, right, bottom, top)
        pdfium_c.FPDFText_GetLooseCharBox(handle, i, loose)
        size = float(pdfium_c.FPDFText_GetFontSize(handle, i))
        is_space = text.isspace()
        hidden = False
        if not is_space:
            obj = pdfium_c.FPDFText_GetTextObject(handle, i)
            mode = pdfium_c.FPDFTextObj_GetTextRenderMode(obj) if obj else 0
            has_fill = pdfium_c.FPDFText_GetFillColor(handle, i, r, g, b, a)
            transparent = bool(has_fill) and a.value == 0
            if size < _MIN_VISIBLE_SIZE or mode in _INVISIBLE_MODES or transparent:
                hidden = True
            elif has_fill:
                text_lum = _luminance(r.value, g.value, b.value)
                bg_lum = background.luminance(loose.left, loose.bottom, loose.right, loose.top)
                hidden = abs(text_lum - bg_lum) < _MIN_CONTRAST
        chars.append(
            _Char(
                text=text,
                index=i,
                x=origin_x.value,
                baseline=origin_y.value,
                x0=left.value,
                x1=right.value,
                bottom=bottom.value,
                top=top.value,
                size=size,
                hidden=hidden,
                unmapped=bool(pdfium_c.FPDFText_HasUnicodeMapError(handle, i) == 1),
            )
        )
    return chars


def _group_by_baseline(chars: Sequence[_Char]) -> list[list[_Char]]:
    groups: list[tuple[float, list[_Char]]] = []
    for char in sorted(chars, key=lambda c: -c.baseline):
        tolerance = max(2.0, char.size * _BASELINE_TOLERANCE_EM)
        for baseline, members in groups:
            if abs(baseline - char.baseline) <= tolerance:
                members.append(char)
                break
        else:
            groups.append((char.baseline, [char]))
    groups.sort(key=lambda item: -item[0])
    return [members for _, members in groups]


def _clusters(chars: Sequence[_Char]) -> list[_Cluster]:
    clusters: list[_Cluster] = []
    for char in sorted(chars, key=lambda c: c.index):  # stream order keeps marks after bases
        if clusters and _is_attached_mark(char.text) and not clusters[-1].is_space:
            cluster = clusters[-1]
            cluster.text += char.text
            cluster.x1 = max(cluster.x1, char.x1)
            cluster.top = max(cluster.top, char.top)
            cluster.bottom = min(cluster.bottom, char.bottom)
            cluster.chars.append(char)
        else:
            clusters.append(
                _Cluster(
                    text=char.text,
                    x=char.x,
                    x0=char.x0,
                    x1=char.x1,
                    bottom=char.bottom,
                    top=char.top,
                    size=char.size,
                    is_space=char.text.isspace(),
                    chars=[char],
                )
            )
    clusters.sort(key=lambda c: c.x)
    return clusters


def _assemble(
    clusters: Sequence[_Cluster], page_height: float
) -> tuple[str, list[BBox], BBox | None]:
    """Join clusters into text; return text, word boxes (top-left origin) and line box."""
    parts: list[str] = []
    words: list[BBox] = []
    word: list[_Cluster] = []
    previous: _Cluster | None = None

    def flush() -> None:
        if word:
            words.append(
                BBox(
                    min(c.x0 for c in word),
                    page_height - max(c.top for c in word),
                    max(c.x1 for c in word),
                    page_height - min(c.bottom for c in word),
                )
            )
            word.clear()

    for cluster in clusters:
        if cluster.is_space:
            parts.append(" ")
            flush()
            previous = None
            continue
        if previous is not None:
            gap = cluster.x0 - previous.x1
            if gap > _SPACE_GAP_EM * max(cluster.size, previous.size):
                parts.append(" ")
                flush()
        parts.append(cluster.text)
        word.append(cluster)
        previous = cluster
    flush()
    text = unicodedata.normalize("NFC", " ".join("".join(parts).split()))
    if not words:
        return text, words, None
    line_box = BBox(
        min(w.x0 for w in words),
        min(w.top for w in words),
        max(w.x1 for w in words),
        max(w.bottom for w in words),
    )
    return text, words, line_box


def _page_lines(
    chars: Sequence[_Char], page_number: int, page_height: float
) -> tuple[list[TextLine], list[BBox]]:
    lines: list[TextLine] = []
    all_words: list[BBox] = []
    for group in _group_by_baseline(chars):
        text, words, box = _assemble(_clusters(group), page_height)
        if not text:
            continue
        visible = [c for c in group if not c.text.isspace()]
        sizes = sorted(c.size for c in visible)
        lines.append(
            TextLine(
                text=text,
                page=page_number,
                bbox=box,
                font_size=round(statistics.median(sizes), 2) if sizes else None,
                min_font_size=round(sizes[0], 2) if sizes else None,
                hidden_chars=sum(c.hidden for c in visible),
            )
        )
        all_words.extend(words)
    return lines, all_words


def _count_tables(data: bytes, page_count: int) -> list[int]:
    """Ruled tables per page. Advisory only: failures degrade to zero."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return [len(page.find_tables()) for page in pdf.pages[:page_count]]
    except Exception:  # pdfminer may choke on PDFs PDFium reads fine
        logger.warning("table detection failed; continuing without table counts")
        return [0] * page_count


def _open(data: bytes) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == pdfium_c.FPDF_ERR_PASSWORD:
            raise EncryptedDocumentError("PDF is password protected") from exc
        raise ExtractionError("PDF could not be parsed") from exc


def _read_document(document: pdfium.PdfDocument, data: bytes) -> ExtractedDocument:
    page_count = len(document)
    if page_count > MAX_PDF_PAGES:
        raise DocumentTooLargeError(f"PDF has {page_count} pages; the limit is {MAX_PDF_PAGES}")
    tables = _count_tables(data, page_count)
    lines: list[TextLine] = []
    pages: list[PageInfo] = []
    for index in range(page_count):
        page = document[index]
        textpage = page.get_textpage()
        background = _Background(page)
        try:
            width, height = page.get_size()
            chars = _read_chars(textpage, background)
            page_lines, words = _page_lines(chars, index + 1, height)
            images = len(list(page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE])))
        finally:
            background.close()
            textpage.close()
            page.close()
        lines.extend(page_lines)
        pages.append(
            PageInfo(
                number=index + 1,
                width=width,
                height=height,
                char_count=sum(1 for c in chars if not c.text.isspace()),
                image_count=images,
                table_count=tables[index],
                multi_column=detect_multi_column(words, width, height),
                unmapped_chars=sum(c.unmapped for c in chars),
            )
        )
    return ExtractedDocument(
        source_format=SourceFormat.PDF,
        lines=tuple(lines),
        pages=tuple(pages),
        image_count=sum(p.image_count for p in pages),
        table_count=sum(p.table_count for p in pages),
    )


def extract_pdf(data: bytes) -> ExtractedDocument:
    """Extract text lines and layout facts from PDF bytes.

    The input is untrusted: any parser failure becomes an :class:`ExtractionError`.
    """
    document = _open(data)
    try:
        return _read_document(document, data)
    except ExtractionError:
        raise
    except Exception as exc:  # malformed input can make the parser raise anything
        raise ExtractionError("PDF could not be parsed") from exc
    finally:
        document.close()
