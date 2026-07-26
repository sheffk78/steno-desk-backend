"""ReportLab-based invoice PDF generator for Steno Desk.

Produces a clean, professional 8.5x11 invoice document mirroring the line item
schema used by the in-app builder. Returns raw PDF bytes (no temp files).
"""
import io
from datetime import datetime
from typing import List, Dict, Any, Optional

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    HRFlowable,
    Image,
)
from reportlab.lib.enums import TA_LEFT, TA_RIGHT


INK = colors.HexColor("#1C1917")
SUBINK = colors.HexColor("#44403C")
MUTED = colors.HexColor("#78716C")
LINE = colors.HexColor("#E7E5E4")
ACCENT = colors.HexColor("#1E293B")


def _money(n: float) -> str:
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.2f}"


def _styles():
    ss = getSampleStyleSheet()
    return {
        "h_invoice": ParagraphStyle(
            "h_invoice", parent=ss["Title"], fontName="Times-Roman",
            fontSize=28, leading=32, textColor=INK, alignment=TA_RIGHT, spaceAfter=4,
        ),
        "h_brand": ParagraphStyle(
            "h_brand", parent=ss["Title"], fontName="Times-Roman",
            fontSize=22, leading=26, textColor=ACCENT, alignment=TA_LEFT, spaceAfter=2,
        ),
        "small": ParagraphStyle(
            "small", parent=ss["Normal"], fontName="Helvetica",
            fontSize=8.5, leading=11, textColor=MUTED,
        ),
        "small_right": ParagraphStyle(
            "small_right", parent=ss["Normal"], fontName="Helvetica",
            fontSize=8.5, leading=11, textColor=MUTED, alignment=TA_RIGHT,
        ),
        "body": ParagraphStyle(
            "body", parent=ss["Normal"], fontName="Helvetica",
            fontSize=9.5, leading=13, textColor=SUBINK,
        ),
        "body_b": ParagraphStyle(
            "body_b", parent=ss["Normal"], fontName="Helvetica-Bold",
            fontSize=9.5, leading=13, textColor=INK,
        ),
        "label": ParagraphStyle(
            "label", parent=ss["Normal"], fontName="Helvetica-Bold",
            fontSize=7.5, leading=10, textColor=MUTED,
        ),
        "case": ParagraphStyle(
            "case", parent=ss["Normal"], fontName="Times-Roman",
            fontSize=11, leading=14, textColor=INK,
        ),
    }


def generate_invoice_pdf(
    invoice: Dict[str, Any],
    reporter: Dict[str, Any],
    client: Dict[str, Any],
    job: Optional[Dict[str, Any]] = None,
) -> bytes:
    """Generate an invoice PDF and return its bytes.

    invoice keys: invoice_number, invoice_date, due_date, line_items[], total,
                  notes (optional), payment_instructions (optional)
    line_items: [{label, detail, amount}]
    reporter keys: name, address, email, cert_number, phone (all optional)
    client keys: name, contact_name, billing_address, contact_email
    job keys (optional): case_caption, case_number, witness, job_date
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
        title=f"Invoice {invoice.get('invoice_number', '')}",
    )
    s = _styles()
    story = []

    # ---- Header: brand left, INVOICE right
    brand_name = reporter.get("business_name") or reporter.get("name") or "Court Reporter"
    brand_sub = None
    if reporter.get("business_name") and reporter.get("name"):
        brand_sub = reporter["name"]
    cert_bits = []
    if reporter.get("cert_type"):
        cert_bits.append(reporter["cert_type"])
    if reporter.get("cert_number"):
        cert_bits.append(f"Cert. {reporter['cert_number']}")
    cert = " · ".join(cert_bits) if cert_bits else None

    # If a letterhead image is provided, prefer it over the text brand.
    letterhead_bytes = reporter.get("letterhead_bytes")
    header_left_block = []
    if letterhead_bytes:
        try:
            img = Image(io.BytesIO(letterhead_bytes))
            iw, ih = img.imageWidth, img.imageHeight
            max_w = 2.5 * inch
            max_h = 0.9 * inch
            scale = min(max_w / iw, max_h / ih, 1.0)
            img.drawWidth = iw * scale
            img.drawHeight = ih * scale
            img.hAlign = "LEFT"
            header_left_block.append(img)
        except Exception:
            # Fall back to text branding if the image is unreadable
            header_left_block.append(Paragraph(f"<b>{brand_name}</b>", s["h_brand"]))
    else:
        brand_html = f"<b>{brand_name}</b>"
        if brand_sub:
            brand_html += f"<br/><font size=10 color='#44403C'>{brand_sub}</font>"
        if cert:
            brand_html += f"<br/><font size=9 color='#78716C'>{cert}</font>"
        header_left_block.append(Paragraph(brand_html, s["h_brand"]))

    # Compose the address block (used regardless of letterhead — keeps the bill-from honest).
    addr_parts = []
    line1 = reporter.get("address_line1")
    line2 = reporter.get("address_line2")
    csz_bits = []
    if reporter.get("city"):
        csz_bits.append(reporter["city"])
    state_zip = " ".join(b for b in [reporter.get("state"), reporter.get("zip")] if b)
    if state_zip:
        csz_bits.append(state_zip)
    csz = ", ".join(csz_bits)
    if line1:
        addr_parts.append(line1)
    if line2:
        addr_parts.append(line2)
    if csz:
        addr_parts.append(csz)
    if not addr_parts and reporter.get("address"):
        addr_parts.append(reporter["address"].replace("\n", "<br/>"))
    if reporter.get("phone"):
        addr_parts.append(reporter["phone"])
    if reporter.get("email"):
        addr_parts.append(reporter["email"])
    if letterhead_bytes and cert:
        # When using a letterhead image, surface cert info in the smaller addr block.
        addr_parts.insert(0, cert)
    addr_html = "<br/>".join(addr_parts) if addr_parts else ""
    if addr_html:
        header_left_block.append(Spacer(1, 4))
        header_left_block.append(Paragraph(addr_html, s["small"]))

    header_left = header_left_block
    header_right = [
        Paragraph("INVOICE", s["h_invoice"]),
        Paragraph(
            f"<font color='#1C1917'><b>#{invoice.get('invoice_number','')}</b></font>",
            s["small_right"],
        ),
        Paragraph(
            f"Issued {invoice.get('invoice_date','')}<br/>Due {invoice.get('due_date','')}",
            s["small_right"],
        ),
    ]
    htbl = Table([[header_left, header_right]], colWidths=[3.6 * inch, 3.4 * inch])
    htbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(htbl)
    story.append(Spacer(1, 14))
    story.append(HRFlowable(width="100%", thickness=0.6, color=LINE))
    story.append(Spacer(1, 14))

    # ---- Bill To + Case info
    bill_html = f"<b>{client.get('name','')}</b>"
    if client.get("contact_name"):
        bill_html += f"<br/>Attn: {client['contact_name']}"
    if client.get("billing_address"):
        bill_html += f"<br/>{client['billing_address'].replace(chr(10), '<br/>')}"
    if client.get("contact_email"):
        bill_html += f"<br/>{client['contact_email']}"

    case_block = []
    if job:
        if job.get("case_caption"):
            case_block.append(Paragraph(job["case_caption"], s["case"]))
        bits = []
        if job.get("case_number"):
            bits.append(f"<b>Case No.</b> {job['case_number']}")
        if job.get("witness"):
            bits.append(f"<b>Witness:</b> {job['witness']}")
        if job.get("job_date"):
            bits.append(f"<b>Job date:</b> {job['job_date']}")
        if bits:
            case_block.append(Paragraph("<br/>".join(bits), s["body"]))

    bill_col = [Paragraph("BILL TO", s["label"]), Spacer(1, 4), Paragraph(bill_html, s["body"])]
    case_col = [Paragraph("MATTER", s["label"]), Spacer(1, 4)] + case_block

    bctbl = Table([[bill_col, case_col]], colWidths=[3.4 * inch, 3.6 * inch])
    bctbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(bctbl)
    story.append(Spacer(1, 18))

    # ---- Line items
    items = invoice.get("line_items") or []
    rows = [[
        Paragraph("DESCRIPTION", s["label"]),
        Paragraph("DETAIL", s["label"]),
        Paragraph("AMOUNT", ParagraphStyle("rt", parent=s["label"], alignment=TA_RIGHT)),
    ]]
    for li in items:
        amt = float(li.get("amount") or 0)
        rows.append([
            Paragraph(str(li.get("label", "")), s["body_b"]),
            Paragraph(str(li.get("detail", "")), s["body"]),
            Paragraph(_money(amt), ParagraphStyle("rt2", parent=s["body"], alignment=TA_RIGHT)),
        ])

    tbl = Table(rows, colWidths=[2.3 * inch, 3.0 * inch, 1.7 * inch], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 8))

    # ---- Total
    total = float(invoice.get("total") or 0)
    tot_tbl = Table(
        [[
            Paragraph("TOTAL DUE", ParagraphStyle("rl", parent=s["label"], alignment=TA_RIGHT, fontSize=9)),
            Paragraph(
                f"<b>{_money(total)}</b>",
                ParagraphStyle("rt3", parent=s["body"], alignment=TA_RIGHT, fontSize=14, leading=18, textColor=INK),
            ),
        ]],
        colWidths=[5.3 * inch, 1.7 * inch],
    )
    tot_tbl.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 1.2, INK),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(tot_tbl)
    story.append(Spacer(1, 18))

    # ---- Footer notes
    if invoice.get("notes"):
        story.append(Paragraph("NOTES", s["label"]))
        story.append(Spacer(1, 4))
        story.append(Paragraph(invoice["notes"].replace("\n", "<br/>"), s["body"]))
        story.append(Spacer(1, 12))

    pay = invoice.get("payment_instructions") or "Please remit payment within 30 days. Make checks payable to the reporter named above."
    story.append(Paragraph("PAYMENT", s["label"]))
    story.append(Spacer(1, 4))
    story.append(Paragraph(pay.replace("\n", "<br/>"), s["body"]))

    # ---- Build
    doc.build(story)
    buf.seek(0)
    return buf.read()
