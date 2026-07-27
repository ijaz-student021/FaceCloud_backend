import csv
import io
import json
from html import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import ProcessingReport


CONTENT_TYPES = {
    ProcessingReport.Format.JSON: "application/json",
    ProcessingReport.Format.CSV: "text/csv; charset=utf-8",
    ProcessingReport.Format.PDF: "application/pdf",
}


def _csv_safe(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " | ".join(str(item) for item in value)
    value = str(value)
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        value = "'" + value
    return value


def _flatten_image(record):
    return {
        "image_id": record["image_id"],
        "batch_id": record["batch_id"],
        "filename": record["filename"],
        "status": record["status"],
        "rejection_reasons": record["rejection_reasons"],
        "failure_reason": record["failure_reason"],
        "enhancement_requested": record["enhancement"]["requested_level"],
        "enhancement_applied": record["enhancement"]["applied_level"],
        "enhancement_algorithm": record["enhancement"]["algorithm"],
        "brightness_score": record["quality"]["brightness_score"],
        "blur_score": record["quality"]["blur_score"],
        "sharpness_score": record["quality"]["sharpness_score"],
        "overall_quality_score": record["quality"]["overall_score"],
        "face_count": record["face_validation"]["face_count"],
        "face_confidence": record["face_validation"]["primary_confidence"],
        "duplicate_decision": record["duplicate_analysis"]["decision"],
        "duplicate_match_count": record["duplicate_analysis"]["match_count"],
        "identity_group": record["identity_group"]["group_id"],
        "processing_duration_ms": record["processing_duration_ms"],
        "original_link": record["protected_links"]["original"],
        "standardized_link": record["protected_links"]["standardized"],
        "enhanced_link": record["protected_links"]["enhanced"],
        "uploaded_at": record["uploaded_at"],
    }


def render_json(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def render_csv(payload):
    rows = [_flatten_image(record) for record in payload["images"]]
    output = io.StringIO(newline="")
    fieldnames = list(rows[0].keys()) if rows else ["image_id", "filename", "status"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_safe(value) for key, value in row.items()})
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def render_pdf(payload):
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=14 * mm,
        leftMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title="FaceCloud Processing Report",
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("FaceCloud Processing Report", styles["Title"]),
        Spacer(1, 4 * mm),
        Paragraph(
            f"Type: {escape(payload['report_type'].title())}<br/>"
            f"Generated: {escape(payload['generated_at'])}<br/>"
            f"Source: {escape(str(payload['source'].get('name', '')))}",
            styles["BodyText"],
        ),
        Spacer(1, 5 * mm),
    ]
    summary_rows = [["Summary field", "Value"]]
    for key, value in payload["summary"].items():
        if isinstance(value, dict):
            value = ", ".join(f"{item}: {count}" for item, count in value.items())
        summary_rows.append([key.replace("_", " ").title(), str(value)])
    summary_table = Table(summary_rows, colWidths=[55 * mm, 115 * mm], repeatRows=1)
    summary_table.setStyle(_table_style())
    story.extend([summary_table, Spacer(1, 6 * mm)])

    rows = [[
        "Filename", "Status", "Quality", "Brightness", "Blur", "Faces", "Duplicate",
    ]]
    for image in payload["images"]:
        rows.append([
            Paragraph(escape(str(image["filename"])), styles["BodyText"]),
            image["status"],
            image["quality"]["overall_score"],
            image["quality"]["brightness_score"],
            image["quality"]["blur_score"],
            image["face_validation"]["face_count"],
            image["duplicate_analysis"]["decision"],
        ])
    image_table = Table(
        rows,
        colWidths=[50 * mm, 24 * mm, 20 * mm, 22 * mm, 17 * mm, 14 * mm, 25 * mm],
        repeatRows=1,
    )
    image_table.setStyle(_table_style(font_size=7))
    story.append(image_table)
    document.build(story)
    return output.getvalue()


def _table_style(font_size=8):
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B5CAD")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8C2CC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6F9")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ])


def render_report(payload, report_format):
    if report_format == ProcessingReport.Format.JSON:
        content = render_json(payload)
    elif report_format == ProcessingReport.Format.CSV:
        content = render_csv(payload)
    elif report_format == ProcessingReport.Format.PDF:
        content = render_pdf(payload)
    else:
        raise ValueError("Unsupported report format.")
    return content, CONTENT_TYPES[report_format], report_format
