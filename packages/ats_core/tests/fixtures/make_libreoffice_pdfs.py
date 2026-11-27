"""Regenerate the LibreOffice-exported PDF fixtures in ``fixtures/libreoffice/``.

These fixtures matter because reportlab (used by most tests) does not shape Thai
text the way real word processors do. Real exports exercise glyph positioning,
font subsetting and ToUnicode maps, which is where PDF extraction breaks.

All content is synthetic; no real resume is used.

Usage (requires LibreOffice ``soffice`` and the fonts named below)::

    uv run python packages/ats_core/tests/fixtures/make_libreoffice_pdfs.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import docx
from docx.document import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Pt, RGBColor

HERE = Path(__file__).parent
OUT = HERE / "libreoffice"
sys.path.insert(0, str(HERE.parent))

from builders import (  # noqa: E402
    CLEAN_RESUME_LINES,
    HIDDEN_KEYWORDS,
    THAI_RESUME_LINES,
    set_columns,
)

THAI_FONTS = {
    "th_sarabun": "Sarabun",
    "th_garuda": "Garuda",
    "th_ibm_plex": "IBM Plex Sans Thai Looped",
}


def _document(font: str) -> Document:
    d = docx.Document()
    style = d.styles["Normal"]
    style.font.name = font
    style.font.size = Pt(11)
    rfonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), font)
    return d


def _resume(font: str, lines: list[str]) -> Document:
    d = _document(font)
    for line in lines:
        d.add_paragraph(line)
    return d


def build_docx(workdir: Path) -> None:
    for name, font in THAI_FONTS.items():
        _resume(font, THAI_RESUME_LINES).save(workdir / f"{name}.docx")

    _resume("Carlito", CLEAN_RESUME_LINES).save(workdir / "en_clean.docx")

    d = _resume("Carlito", CLEAN_RESUME_LINES)
    for i in range(30):
        d.add_paragraph(f"Additional project detail number {i} with measurable impact")
    set_columns(d, 2)
    d.save(workdir / "en_two_column.docx")

    d = _resume("Carlito", CLEAN_RESUME_LINES)
    table = d.add_table(rows=3, cols=2)
    table.style = "Table Grid"
    for row, (skill, years) in enumerate(
        [("Skill", "Years"), ("Python", "5"), ("Kubernetes", "3")]
    ):
        table.cell(row, 0).text, table.cell(row, 1).text = skill, years
    d.save(workdir / "en_table.docx")

    d = _resume("Carlito", CLEAN_RESUME_LINES)
    cell = d.add_table(rows=1, cols=1).cell(0, 0)
    cell._tc.get_or_add_tcPr().append(
        parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="1F3864"/>')
    )
    cell.paragraphs[0].add_run("CONTACT SIDEBAR HEADING").font.color.rgb = RGBColor(255, 255, 255)
    d.save(workdir / "en_dark_cell_white_text.docx")

    d = _resume("Carlito", CLEAN_RESUME_LINES)
    d.add_paragraph().add_run(HIDDEN_KEYWORDS).font.color.rgb = RGBColor(255, 255, 255)
    d.save(workdir / "en_white_on_white.docx")


def main() -> None:
    soffice = shutil.which("soffice")
    if soffice is None:
        sys.exit("LibreOffice (soffice) is required")
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        build_docx(workdir)
        subprocess.run(  # noqa: S603 - fixed arguments, trusted local tool
            [
                soffice,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(OUT),
                *sorted(str(p) for p in workdir.glob("*.docx")),
            ],
            check=True,
            capture_output=True,
        )
    print(f"wrote {len(list(OUT.glob('*.pdf')))} PDFs to {OUT}")


if __name__ == "__main__":
    main()
