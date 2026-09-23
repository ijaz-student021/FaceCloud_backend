"""JSON/CSV/PDF rendering for ModeratorReport.

Reuses reports.renderers's table_style()/csv_safe() helpers rather than
duplicating them -- those two were promoted from private (_table_style/
_csv_safe) to public names specifically so this module could import them.
The rest is new: reports/renderers.py's own render_csv/render_pdf are
hard-coded to a per-image row shape (images: [...]), which doesn't fit
review-summary/moderator-activity data, so both payload types here are
normalized to a common {summary: {...}, rows: [...]} shape instead and
rendered generically.
"""
import csv
import io
import json
from html import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table

from reports.renderers import csv_safe, table_style

from .models import ModeratorReport

CONTENT_TYPES = {
    ModeratorReport.Format.JSON: "application/json",
    ModeratorReport.Format.CSV: "text/csv; charset=utf-8",
    ModeratorReport.Format.PDF: "application/pdf",
}


def render_json(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def render_csv(payload):
    rows = payload["rows"]
    output = io.StringIO(newline="")
    fieldnames = list(rows[0].keys()) if rows else ["id"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: csv_safe(value) for key, value in row.items()})
    return ("﻿" + output.getvalue()).encode("utf-8")


def render_pdf(payload):
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, rightMargin=14 * mm, leftMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm, title="FaceCloud Moderator Report",
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("FaceCloud Moderator Report", styles["Title"]),
        Spacer(1, 4 * mm),
        Paragraph(
            f"Type: {escape(payload['report_type'].replace('_', ' ').title())}<br/>"
            f"Organization: {escape(str(payload['organization'].get('name', '')))}<br/>"
            f"Generated: {escape(str(payload['generated_at']))}",
            styles["BodyText"],
        ),
        Spacer(1, 5 * mm),
    ]
    summary_rows = [["Summary field", "Value"]]
    for key, value in payload["summary"].items():
        if isinstance(value, dict):
            value = ", ".join(f"{item}: {count}" for item, count in value.items())
        elif isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        summary_rows.append([key.replace("_", " ").title(), str(value)])
    summary_table = Table(summary_rows, colWidths=[55 * mm, 115 * mm], repeatRows=1)
    summary_table.setStyle(table_style())
    story.extend([summary_table, Spacer(1, 6 * mm)])

    rows = payload["rows"]
    if rows:
        headers = list(rows[0].keys())
        table_data = [[header.replace("_", " ").title() for header in headers]]
        for row in rows[:200]:
            table_data.append([
                Paragraph(escape(str(row.get(header, ""))), styles["BodyText"]) for header in headers
            ])
        column_width = 189 * mm / len(headers)
        detail_table = Table(table_data, colWidths=[column_width] * len(headers), repeatRows=1)
        detail_table.setStyle(table_style(font_size=7))
        story.append(detail_table)
    document.build(story)
    return output.getvalue()


def render_moderator_report(payload, report_format):
    if report_format == ModeratorReport.Format.JSON:
        content = render_json(payload)
    elif report_format == ModeratorReport.Format.CSV:
        content = render_csv(payload)
    elif report_format == ModeratorReport.Format.PDF:
        content = render_pdf(payload)
    else:
        raise ValueError("Unsupported report format.")
    return content, CONTENT_TYPES[report_format], report_format
