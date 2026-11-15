"""One-page savings report for a factory owner (PDF).

Every amount is computed in server/app.py (Live.savings) from logged visits and
the assumptions in data/sim/costs.json; this module only lays it out. The page
says plainly that the data is simulated and the costs are assumptions.
"""

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK, MUTED, ACCENT, RULE = (
    colors.HexColor("#1c1d20"),
    colors.HexColor("#6b6e75"),
    colors.HexColor("#D9480F"),
    colors.HexColor("#d8d6d0"),
)
BASE = ParagraphStyle("base", fontName="Helvetica", fontSize=9, leading=12, textColor=INK)
SMALL = ParagraphStyle("small", parent=BASE, fontSize=7.5, leading=10, textColor=MUTED)
H1 = ParagraphStyle("h1", parent=BASE, fontName="Helvetica-Bold", fontSize=16, leading=20)
H2 = ParagraphStyle("h2", parent=BASE, fontName="Helvetica-Bold", fontSize=10, leading=14, spaceBefore=6)
BIG = ParagraphStyle("big", parent=BASE, fontName="Helvetica-Bold", fontSize=14, leading=17)


def inr(v: float) -> str:
    """Indian digit grouping: 1234567 -> 'Rs 12,34,567'."""
    s = str(round(v))
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "Rs " + ",".join([*groups, tail]) if groups else "Rs " + tail


def _table(rows: list[list[Any]], widths: list[float], header: bool = True) -> Table:
    t = Table(rows, colWidths=widths, hAlign="LEFT")
    style = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if header:
        style += [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("TEXTCOLOR", (0, 0), (-1, 0), MUTED)]
    t.setStyle(TableStyle(style))
    return t


def render(d: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=12 * mm,
        title=f"Dhadkan savings report - {d['factory']['name']}",
        author="Dhadkan",
    )
    f, tot = d["factory"], d["totals"]
    story: list[Any] = [
        Paragraph(f"Savings report - {f['name']}", H1),
        Paragraph(f"{f['area']} · owner {f['owner']} · {d['period_label']}", BASE),
        Spacer(1, 4),
        Paragraph(
            "<b>Simulated demo data.</b> Machine sounds are real recordings (MIMII datasets) replayed in a "
            "simulated cluster; faults that grow over time are synthetic mixes. Costs are assumptions listed "
            "below, not measurements, quotes or an audit. Replace them with this factory's numbers before "
            "showing this page to anyone.",
            ParagraphStyle(
                "warn", parent=BASE, textColor=ACCENT, borderColor=ACCENT, borderWidth=0.6, borderPadding=5
            ),
        ),
        Spacer(1, 10),
    ]
    summary = [
        [
            Paragraph(inr(tot["output_inr"]), BIG),
            Paragraph(inr(tot["part_markup_inr"]), BIG),
            Paragraph(inr(tot["wages_inr"]), BIG),
            Paragraph(inr(tot["saved_inr"]), BIG),
        ],
        [
            Paragraph(f"downtime avoided ({tot['downtime_avoided_h']:g} h of lost output)", SMALL),
            Paragraph("emergency part markup avoided", SMALL),
            Paragraph("wages of idle workers saved", SMALL),
            Paragraph("<b>total, estimated</b>", SMALL),
        ],
    ]
    st = Table(summary, colWidths=[44 * mm] * 4, hAlign="LEFT")
    st.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEABOVE", (0, 0), (-1, 0), 1, INK)]))
    story += [st, Spacer(1, 8), Paragraph("Visits booked before a breakdown", H2)]
    if d["visits"]:
        rows = [["machine", "first warning", "how it was caught", "visit booked for", "saved (est.)"]]
        for v in d["visits"]:
            rows.append(
                [
                    Paragraph(f"{v['name']} ({v['machine']})", BASE),
                    v["first_warning"] or "-",
                    Paragraph(v["how"], BASE),
                    v["visit"],
                    inr(v["saved_inr"]),
                ]
            )
        story.append(_table(rows, [34 * mm, 28 * mm, 58 * mm, 30 * mm, 26 * mm]))
    else:
        story.append(
            Paragraph("No visit has been approved by the owner in this period, so nothing is counted.", BASE)
        )
    story += [Spacer(1, 4), Paragraph("Watching now", H2)]
    rows = [["machine", "status", "days to fault level (estimate)", "rupees at risk, next 2 days"]]
    rows += [
        [
            f"{w['name']} ({w['machine']})",
            w["status"].replace("_", " "),
            Paragraph(w["days"], BASE),
            inr(w["rupees_at_risk"]),
        ]
        for w in d["watch"]
    ]
    story.append(_table(rows, [40 * mm, 24 * mm, 72 * mm, 40 * mm]))
    story += [Spacer(1, 4), Paragraph("Assumptions (data/sim/costs.json)", H2)]
    story.append(_table([[k, v] for k, v in d["assumptions"]], [70 * mm, 106 * mm], header=False))
    story += [
        Spacer(1, 6),
        Paragraph(
            "How it is counted: a visit counts when the owner approved it while the machine was at alarm or "
            "matched a fault confirmed elsewhere. Each counted visit assumes the machine would otherwise have "
            "run until it broke down: unplanned downtime minus the planned service time, times lost output "
            "and idle wages, plus the markup on parts bought in a hurry. Days to fault level is a straight-line "
            "trend of the machine's own anomaly score to the level its recorded faulty clips reach; it is an "
            "estimate, not a guarantee.",
            SMALL,
        ),
        Spacer(1, 4),
        Paragraph(f"Generated {d['generated']} (computer clock) by Dhadkan from {d['source']}.", SMALL),
    ]
    doc.build(story)
    return buf.getvalue()
