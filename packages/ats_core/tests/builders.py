"""In-memory PDF / DOCX builders for tests.

Every fixture document is generated from code so no real resume (personal data)
is ever committed to the repository.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import docx
from docx.document import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Pt, RGBColor
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FIXTURES = Path(__file__).parent / "fixtures"
THAI_FONT = "NotoSansThai"
pdfmetrics.registerFont(TTFont(THAI_FONT, str(FIXTURES / "fonts" / "NotoSansThai-Regular.ttf")))

PAGE_W, PAGE_H = 612.0, 792.0  # US Letter in points


# ── PDF ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Text:
    x: float
    top: float
    text: str
    size: float = 11
    font: str = "Helvetica"
    color: tuple[float, float, float] = (0, 0, 0)


@dataclass(frozen=True)
class Grid:
    """A ruled table: vertical lines at xs, horizontal lines at tops."""

    xs: tuple[float, ...]
    tops: tuple[float, ...]


@dataclass(frozen=True)
class Picture:
    x: float
    top: float
    w: float = 100
    h: float = 100


Item = Text | Grid | Picture


def _png(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 160)).save(buf, format="PNG")
    return buf.getvalue()


def build_pdf(*pages: list[Item]) -> bytes:
    """Draw each page's items. ``top`` coordinates are measured from the top edge."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    for items in pages:
        for item in items:
            if isinstance(item, Text):
                c.setFillColorRGB(*item.color)
                c.setFont(item.font, item.size)
                c.drawString(item.x, PAGE_H - item.top, item.text)
            elif isinstance(item, Grid):
                c.setFillColorRGB(0, 0, 0)
                c.grid(list(item.xs), [PAGE_H - t for t in item.tops])
            else:
                c.drawImage(
                    ImageReader(io.BytesIO(_png())),
                    item.x,
                    PAGE_H - item.top - item.h,
                    item.w,
                    item.h,
                )
        c.showPage()
    c.save()
    return buf.getvalue()


def text_lines(
    lines: list[str],
    *,
    x: float = 72,
    top: float = 72,
    step: float = 16,
    size: float = 11,
    font: str = "Helvetica",
) -> list[Item]:
    return [Text(x, top + i * step, line, size=size, font=font) for i, line in enumerate(lines)]


def mixed_text(x: float, top: float, text: str, size: float = 11) -> list[Item]:
    """Draw mixed Thai/Latin text, switching font per script run like a real document.

    The bundled Thai font has no Latin glyphs (not even digits), so Latin runs use
    Helvetica.
    """
    items: list[Item] = []
    run = ""
    run_is_thai = False
    for ch in text + "\0":
        is_thai = "\u0e00" <= ch <= "\u0e7f"
        if run and (ch == "\0" or is_thai != run_is_thai):
            font = THAI_FONT if run_is_thai else "Helvetica"
            items.append(Text(x, top, run, size=size, font=font))
            x += pdfmetrics.stringWidth(run, font, size)
            run = ""
        run += ch
        run_is_thai = is_thai
    return items


CLEAN_RESUME_LINES = [
    "Somchai Example",
    "somchai@example.com | 081-234-5678 | Bangkok",
    "Summary",
    "Backend engineer with 5 years of experience building Python services.",
    "Experience",
    "Senior Software Engineer, Example Co. Jan 2021 - Present",
    "- Built FastAPI microservices on Kubernetes serving 2,500 requests per second.",
    "- Led migration from MySQL to PostgreSQL with zero downtime.",
    "Software Engineer, Sample Ltd. Jun 2018 - Dec 2020",
    "- Developed data pipelines with Python and Apache Airflow.",
    "Education",
    "B.Eng. Computer Engineering, Example University 2014 - 2018",
    "Skills",
    "Python, FastAPI, PostgreSQL, Docker, Kubernetes, AWS, Git",
]


THAI_RESUME_LINES = [
    "สมชาย ตัวอย่าง",
    "somchai@example.com | 081-234-5678",
    "ประสบการณ์ทำงาน",
    "วิศวกรซอฟต์แวร์อาวุโส บริษัท ตัวอย่าง จำกัด มิ.ย. 2563 - ปัจจุบัน",
    "• พัฒนาระบบหลังบ้านด้วย Python และ FastAPI ให้บริการผู้ใช้กว่า 2,500 คนต่อวัน",
    "• ผู้จัดการโครงการย้ายฐานข้อมูล ปรับปรุงประสิทธิภาพ น้ำหนักเบา พันธุ์ กี่ปี ก็ได้",
    "ประวัติการศึกษา",
    "วิศวกรรมศาสตรบัณฑิต สาขาวิศวกรรมคอมพิวเตอร์ 2559 - 2563",
    "ทักษะ",
    "Python, Kubernetes, PostgreSQL, การทำงานเป็นทีม, การสื่อสาร",
]

HIDDEN_KEYWORDS = "terraform ansible jenkins golang rust"


# ── DOCX ─────────────────────────────────────────────────────────────────


def new_docx() -> Document:
    return docx.Document()


def docx_bytes(document: Document) -> bytes:
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def add_textbox(document: Document, text: str) -> None:
    """Append a paragraph containing a DrawingML text box with a VML fallback."""
    run_xml = f"""
    <w:r {nsdecls("w", "wp", "a")}
         xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
         xmlns:v="urn:schemas-microsoft-com:vml"
         xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">
      <mc:AlternateContent>
        <mc:Choice Requires="wps">
          <w:drawing>
            <wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="1"
                       behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">
              <wp:simplePos x="0" y="0"/>
              <wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset></wp:positionH>
              <wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>
              <wp:extent cx="1828800" cy="457200"/>
              <wp:wrapNone/>
              <wp:docPr id="99" name="Text Box 1"/>
              <a:graphic>
                <a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">
                  <wps:wsp>
                    <wps:cNvSpPr txBox="1"/>
                    <wps:spPr/>
                    <wps:txbx><w:txbxContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent></wps:txbx>
                    <wps:bodyPr/>
                  </wps:wsp>
                </a:graphicData>
              </a:graphic>
            </wp:anchor>
          </w:drawing>
        </mc:Choice>
        <mc:Fallback>
          <w:pict><v:shape><v:textbox><w:txbxContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent></v:textbox></v:shape></w:pict>
        </mc:Fallback>
      </mc:AlternateContent>
    </w:r>"""
    paragraph = document.add_paragraph()
    paragraph._p.append(parse_xml(run_xml))


def set_columns(document: Document, num: int) -> None:
    sect_pr = document.sections[-1]._sectPr
    cols = sect_pr.find(qn("w:cols"))
    if cols is None:
        cols = parse_xml(f"<w:cols {nsdecls('w')}/>")
        sect_pr.append(cols)
    cols.set(qn("w:num"), str(num))


def add_run(
    document: Document,
    text: str,
    *,
    hidden: bool = False,
    white: bool = False,
    size: float | None = None,
) -> None:
    run = document.add_paragraph().add_run(text)
    if hidden:
        run.font.hidden = True
    if white:
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    if size is not None:
        run.font.size = Pt(size)


def add_picture(document: Document) -> None:
    document.add_picture(io.BytesIO(_png()))
