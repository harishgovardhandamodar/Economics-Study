"""PDF export via reportlab: dossiers, assessments and the board as A4 reports.

Pure-Python rendering (platypus) — no chromium, no pandoc, no LaTeX in the
image. The trade-off is deliberate: the PDFs carry the numbers, tables and
claims, not pixel-perfect figures. A mermaid diagram arrives as its source in
a code block rather than a picture, because a picture that cannot be produced
inside the container is a feature that fails in production.

Markdown subset handled: ``#``/``##``/``###``, ``- `` bullets, ``|`` tables,
``>`` quotes, ``**bold**``/``*italic*/``_inline_``. Everything else is a
paragraph. A renderer that chokes on an unexpected line would make export
fail on exactly the longest, most valuable dossiers.
"""
from __future__ import annotations

import html as _html
import io
import re

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

TITLE = "Economics Cross-Domain Mapper"


def _inline_md(text: str) -> str:
    """Markdown inline -> reportlab <para> XML. Escaped first, so a literal
    ``<`` in a title cannot break the document.

    Order matters: triple-star first (the dossier emits ``**review**`` inside
    a ``*...*`` emphasis line, so ``***accepted**`` must become one nested
    element, not two overlapping ones), then bold, code, links, and italic
    last with a tag-safe body so emphasis can never cross an element
    boundary and produce ``<b><i>x</b>``. An emphasis marker with no partner
    is left literal — a stray ``*`` on the page beats a failed export.
    """
    t = _html.escape(text or "")
    t = re.sub(r"\*\*\*(.+?)\*\*", r"<b><i>\1</i></b>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', t)
    t = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", t)
    t = re.sub(r"(?<!\w)\*([^<>\n]+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", t)
    return t


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    s = {
        "title": ParagraphStyle("Title2", parent=base["Title"], fontSize=20,
                                leading=24, spaceAfter=4),
        "h1": ParagraphStyle("H1", parent=base["Heading1"], fontSize=15,
                             leading=18, spaceBefore=14, spaceAfter=6,
                             textColor=colors.HexColor("#1f2328")),
        "h2": ParagraphStyle("H2", parent=base["Heading2"], fontSize=12.5,
                             leading=15, spaceBefore=10, spaceAfter=5,
                             textColor=colors.HexColor("#1f2328")),
        "h3": ParagraphStyle("H3", parent=base["Heading3"], fontSize=11,
                             leading=13, spaceBefore=8, spaceAfter=4),
        "body": ParagraphStyle("Body2", parent=base["Normal"], fontSize=9.5,
                               leading=13.5, spaceAfter=5),
        "bullet": ParagraphStyle("Bullet2", parent=base["Normal"], fontSize=9.5,
                                 leading=13.5, leftIndent=14, spaceAfter=3,
                                 bulletIndent=6),
        "quote": ParagraphStyle("Quote2", parent=base["Normal"], fontSize=9,
                                leading=12.5, leftIndent=14, spaceAfter=5,
                                textColor=colors.HexColor("#57606a")),
        "code": ParagraphStyle("Code2", parent=base["Code"], fontSize=8,
                               leading=10.5, spaceAfter=5,
                               fontName="Courier"),
        "cell": ParagraphStyle("Cell", parent=base["Normal"], fontSize=8.5,
                               leading=11),
        "cellhead": ParagraphStyle("CellH", parent=base["Normal"], fontSize=8.5,
                                   leading=11, textColor=colors.white),
        "foot": ParagraphStyle("Foot", parent=base["Normal"], fontSize=8,
                               leading=10, textColor=colors.HexColor("#8c959f")),
    }
    return s


_TABLE_STYLE = TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24292f")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTSIZE", (0, 0), (-1, -1), 8.5),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d0d7de")),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8fa")]),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
])


def markdown_flowables(md: str, styles: dict) -> list:
    """One pass over markdown lines -> platypus flowables. Never raises on
    content: an unrecognised line is a paragraph, always."""
    flows: list = []
    table_rows: list[list] = []

    def flush_table() -> None:
        if not table_rows:
            return
        cells = [[Paragraph(_inline_md(c), styles["cellhead" if i == 0 else "cell"])
                  for c in row] for i, row in enumerate(table_rows)]
        widths = [None] * max(len(r) for r in table_rows)
        flows.append(Table(cells, colWidths=widths, repeatRows=1,
                           style=_TABLE_STYLE))
        flows.append(Spacer(1, 6))
        table_rows.clear()

    for raw in (md or "").split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue  # separator row
            table_rows.append(cells)
            continue
        flush_table()
        s = line.strip()
        if not s:
            continue
        if s.startswith("### "):
            flows.append(Paragraph(_inline_md(s[4:]), styles["h3"]))
        elif s.startswith("## "):
            flows.append(Paragraph(_inline_md(s[3:]), styles["h2"]))
        elif s.startswith("# "):
            flows.append(Paragraph(_inline_md(s[2:]), styles["h1"]))
        elif s.startswith(("- ", "* ")):
            flows.append(Paragraph(_inline_md(s[2:]), styles["bullet"],
                                   bulletText="•"))
        elif re.match(r"^\d+\.\s", s):
            flows.append(Paragraph(_inline_md(re.sub(r"^\d+\.\s", "", s)),
                                   styles["bullet"], bulletText="•"))
        elif s.startswith("> "):
            flows.append(Paragraph(_inline_md(s[2:]), styles["quote"]))
        elif s.startswith("```"):
            continue  # fences delimit; content lines render as code below
        elif s.startswith("    ") or (raw.startswith("    ") and len(s) < 120):
            flows.append(Paragraph(_inline_md(s), styles["code"]))
        else:
            flows.append(Paragraph(_inline_md(s), styles["body"]))
    flush_table()
    return flows


def _doc(title: str) -> tuple[io.BytesIO, SimpleDocTemplate, dict]:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=1.8 * cm, bottomMargin=1.8 * cm,
        title=title, author="Economics Cross-Domain Mapper")
    return buf, doc, _styles()


def _footer(canvas, _doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#8c959f"))
    canvas.drawCentredString(A4[0] / 2, 1.2 * cm, f"Page {_doc.page}")
    canvas.restoreState()


def render_pdf(title: str, subtitle: str, markdown: str) -> bytes:
    buf, doc, styles = _doc(title)
    flows: list = [
        Paragraph(_html.escape(title), styles["title"]),
    ]
    if subtitle:
        flows.append(Paragraph(_inline_md(subtitle), styles["foot"]))
    flows += [HRFlowable(width="100%", thickness=0.5,
                         color=colors.HexColor("#d0d7de")),
              Spacer(1, 8)]
    flows += markdown_flowables(markdown, styles)
    doc.build(flows, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def build_investigation_pdf(db, investigation_id: int) -> tuple[str, bytes]:
    from .main import dossier as _dossier_fn
    from .models import Investigation
    inv = db.query(Investigation).filter(
        Investigation.id == investigation_id).first()
    if inv is None:
        raise ValueError("no such investigation")
    text = _dossier_fn(investigation_id, fmt="text", db=db)["dossier"]
    pdf = render_pdf(inv.title, f"Investigation dossier · {inv.keywords or ''}",
                     text)
    return (f"investigation-{investigation_id}-dossier.pdf", pdf)


def build_assessment_pdf(db, assessment_id: int) -> tuple[str, bytes]:
    from .models import EconomicAssessment
    r = db.query(EconomicAssessment).filter(
        EconomicAssessment.id == assessment_id).first()
    if r is None:
        raise ValueError("no such assessment")
    pdf = render_pdf(f"Transmission assessment #{r.id}",
                     f"Driver {r.driver or '—'} · overall {r.overall_pct}%",
                     r.markdown or "")
    return (f"assessment-{assessment_id}.pdf", pdf)


def build_board_pdf(db, investigation_id: int | None = None) -> tuple[str, bytes]:
    from . import leadership as _lead
    md = _lead.export_markdown(db, investigation_id)
    pdf = render_pdf("Economics leadership board",
                     "Portfolio aggregations — no new judgments", md)
    return ("leadership-board.pdf", pdf)
