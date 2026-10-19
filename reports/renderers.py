import csv
import io
import json
from html import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .labels import (
    duplicate_label, format_bytes, format_score, format_timestamp, reason_label, sharpness_words, status_label, upload_type_label, wrap_filename,
)
from .models import ProcessingReport


CONTENT_TYPES = {
    ProcessingReport.Format.JSON: "application/json",
    ProcessingReport.Format.CSV: "text/csv; charset=utf-8",
    ProcessingReport.Format.PDF: "application/pdf",
}


def csv_safe(value):
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
        writer.writerow({key: csv_safe(value) for key, value in row.items()})
    return ("\ufeff" + output.getvalue()).encode("utf-8")


PAGE_MARGIN = 14 * mm
CONTENT_WIDTH = A4[0] - 2 * PAGE_MARGIN              # 182 mm
CELL_PADDING = 4                                       # points, each side (see table_style)
PHOTO_COLUMNS = [50 * mm, 24 * mm, 20 * mm, 21 * mm, 29 * mm, 12 * mm, 26 * mm]
PHOTO_HEADERS = [
    "Filename", "Status", "Quality score (0–100)", "Brightness (0–100)", "Sharpness (0–100)", "Faces", "Duplicate check",
]
REPORT_TYPE_LABELS = {"batch": "Batch report", "image": "Photo report"}
LEGEND = (
    "How to read the scores: quality, brightness and sharpness are scores out of 100, and a higher number is better. Sharpness 100 is perfectly sharp; "
    "70 or more reads as sharp, 40 to 69 as slightly soft and under 40 as blurry (a guide to the number; the accept or reject decision uses all the checks). "
    "Faces is the number of faces found in the photo."
)
DELETED_ALL = "The photos of this upload have been deleted; only totals are shown."


class _Styles:
    def __init__(self):
        base = getSampleStyleSheet()
        self.title = base["Title"]
        self.body = base["BodyText"]
        self.heading = ParagraphStyle("SectionHeading", parent=base["Heading3"], fontName="Helvetica-Bold", spaceBefore=6, spaceAfter=3)
        self.cell = ParagraphStyle("Cell", parent=base["BodyText"], fontSize=7, leading=9, spaceAfter=0, splitLongWords=0)
        self.cell_bold = ParagraphStyle("CellBold", parent=self.cell, fontName="Helvetica-Bold")
        self.head = ParagraphStyle("CellHead", parent=self.cell_bold, textColor=colors.white)
        self.summary_cell = ParagraphStyle("SummaryCell", parent=self.cell, fontSize=8, leading=10)
        self.notice = ParagraphStyle(
            "Notice", parent=base["BodyText"], fontSize=9, leading=12, backColor=colors.HexColor("#FFF4D6"),
            borderColor=colors.HexColor("#E0B84A"), borderWidth=0.6, borderPadding=5, spaceBefore=4, spaceAfter=8,
        )
        self.legend = ParagraphStyle("Legend", parent=base["BodyText"], fontSize=7, leading=9, textColor=colors.HexColor("#444444"), spaceBefore=4)


def _wrapped(text, width, style):
    """A paragraph whose text is broken at natural places (after ``_ - . /`` or a space), never in the middle of a run of letters if it can be avoided."""
    lines = wrap_filename(text, width - 2 * CELL_PADDING, style.fontName, style.fontSize)
    return Paragraph("<br/>".join(escape(line) for line in lines), style)


def _results_rows(summary, images):
    """Rows for the results of the checks: the three outcomes people look for, always (even at 0), then any other status that occurred."""
    counts = dict(summary.get("status_counts") or {})
    if summary.get("images_deleted") and not summary.get("images_remaining"):
        return [("Photos deleted", counts.get("deleted", summary.get("total_images", 0)))]      # nothing left to count: the other outcomes are unknown, not 0
    rows = [(status_label(code), counts.pop(code, 0)) for code in ("accepted", "rejected", "needs_review")]
    for code in sorted(counts):
        if counts[code]:
            rows.append((status_label(code), counts[code]))
    return rows


def _enhanced_count(summary, images):
    if "enhanced_images" in summary:
        return summary["enhanced_images"]
    return sum(1 for image in images if image["enhancement"]["status"] == "completed") if images else None


def _summary_table(payload, styles):
    summary = payload["summary"]
    images = payload["images"]
    rows = []                                              # (label, value) or ("#", heading)
    if payload["report_type"] == "batch":
        rows += [
            ("Total images", summary.get("total_images", len(images))),
            ("Accepted uploads", summary.get("accepted_uploads", 0)),
            ("Rejected uploads", summary.get("rejected_uploads", 0)),
            ("Processing status", status_label(summary.get("processing_status"))),
            ("Total input size", format_bytes(summary.get("total_input_size"))),
        ]
        if summary.get("images_deleted"):
            rows.append(("Photos still stored", summary.get("images_remaining", len(images))))
        rows.append(("#", "Results of the quality checks"))
        rows += _results_rows(summary, images)
    else:
        rows += [
            ("Total images", summary.get("total_images", len(images))),
            ("Result", status_label(summary.get("status"))),
        ]
    enhanced = _enhanced_count(summary, images)
    if payload["report_type"] == "batch" and enhanced is not None:
        rows.append(("Enhanced", enhanced))
    elif payload["report_type"] == "image" and images:
        rows.append(("Enhanced", "Yes" if images[0]["enhancement"]["status"] == "completed" else "No"))

    table_rows, spans = [[Paragraph("Summary field", styles.head), Paragraph("Value", styles.head)]], []
    for label, value in rows:
        if label == "#":
            spans.append(len(table_rows))
            table_rows.append([Paragraph(escape(value), styles.cell_bold), ""])
        else:
            table_rows.append([Paragraph(escape(str(label)), styles.summary_cell), Paragraph(escape(str(value)), styles.summary_cell)])
    table = Table(table_rows, colWidths=[55 * mm, 127 * mm], repeatRows=1)
    style = table_style()
    for row in spans:
        style.add("SPAN", (0, row), (1, row))
        style.add("BACKGROUND", (0, row), (1, row), colors.HexColor("#DCE8F5"))
    table.setStyle(style)
    return table


def _rejection_text(image):
    reasons = [reason_label(reason) for reason in image.get("rejection_reasons") or []]
    if not reasons and image.get("failure_reason"):
        reasons = [str(image["failure_reason"])]
    return "; ".join(reasons) or "No reason was recorded."


def _rejected_table(images, styles):
    rejected = [image for image in images if image["status"] == "rejected"]
    if not rejected:
        return None
    rows = [[Paragraph("Rejected photo", styles.head), Paragraph("Why it was rejected", styles.head)]]
    for image in rejected:
        rows.append([_wrapped(image["filename"], 60 * mm, styles.cell), Paragraph(escape(_rejection_text(image)), styles.cell)])
    table = Table(rows, colWidths=[60 * mm, 122 * mm], repeatRows=1)
    table.setStyle(table_style(font_size=7))
    return table


def _photos_table(images, styles):
    rows = [[Paragraph(escape(header), styles.head) for header in PHOTO_HEADERS]]
    for image in images:
        quality = image["quality"]
        faces = image["face_validation"]["face_count"]
        rows.append([
            _wrapped(image["filename"], PHOTO_COLUMNS[0], styles.cell),
            Paragraph(escape(status_label(image["status"])), styles.cell),
            Paragraph(format_score(quality["overall_score"]), styles.cell),
            Paragraph(format_score(quality["brightness_score"]), styles.cell),
            Paragraph(escape(sharpness_words(quality["sharpness_score"])), styles.cell),
            Paragraph("—" if faces is None else str(faces), styles.cell),
            Paragraph(escape(duplicate_label(image["duplicate_analysis"]["decision"])), styles.cell),
        ])
    table = Table(rows, colWidths=PHOTO_COLUMNS, repeatRows=1)
    table.setStyle(table_style(font_size=7))
    return table


def _source_label(payload):
    return str(payload["source"].get("name") or "")


def _footer_canvas(left_text):
    """A canvas that writes the report type and source on the left of every page's footer and ``Page n of N`` on the right (N is known only at the end)."""

    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_pages = []

        def showPage(self):
            self._saved_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_pages)
            for state in self._saved_pages:
                self.__dict__.update(state)
                self._draw_footer(total)
                super().showPage()
            super().save()

        def _draw_footer(self, total):
            right = f"Page {self._pageNumber} of {total}"
            self.setFont("Helvetica", 7)
            self.setFillColor(colors.HexColor("#555555"))
            room = CONTENT_WIDTH - stringWidth(right, "Helvetica", 7) - 8 * mm
            text = left_text
            while text and stringWidth(text, "Helvetica", 7) > room:
                text = text[:-2].rstrip() + "…" if len(text) > 2 else ""
            self.line(PAGE_MARGIN, 11 * mm, A4[0] - PAGE_MARGIN, 11 * mm)
            self.drawString(PAGE_MARGIN, 7.5 * mm, text)
            self.drawRightString(A4[0] - PAGE_MARGIN, 7.5 * mm, right)

    return NumberedCanvas


def render_pdf(payload):
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=PAGE_MARGIN,
        leftMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN,
        bottomMargin=18 * mm,
        title="FaceCloud Processing Report",
    )
    styles = _Styles()
    summary = payload["summary"]
    images = payload["images"]
    report_label = REPORT_TYPE_LABELS.get(payload["report_type"], payload["report_type"].title())
    source = payload["source"]
    details = [
        f"<b>Report type:</b> {escape(report_label)}",
        f"<b>Source:</b> {'<br/>'.join(escape(line) for line in wrap_filename(_source_label(payload), CONTENT_WIDTH - 24 * mm, 'Helvetica', 10))}",
        f"<b>Generated:</b> {escape(format_timestamp(payload['generated_at']))}",
    ]
    if payload["report_type"] == "batch":
        details.append(f"<b>Upload:</b> {escape(upload_type_label(source.get('upload_type')))}, {escape(format_timestamp(source.get('created_at')))}")
    story = [Paragraph("FaceCloud Processing Report", styles.title), Spacer(1, 3 * mm), Paragraph("<br/>".join(details), styles.body)]

    deleted = bool(summary.get("images_deleted"))
    if deleted:
        story.append(Spacer(1, 3 * mm))
    if deleted and not images:
        story.append(Paragraph(DELETED_ALL, styles.notice))
    elif deleted:
        story.append(Paragraph(
            f"Some photos of this upload have been deleted. {len(images)} of {summary.get('total_images', len(images))} remain; "
            "the table lists the photos that remain and the totals count all of them.", styles.notice))
    else:
        story.append(Spacer(1, 4 * mm))
    story += [_summary_table(payload, styles), Spacer(1, 5 * mm)]

    rejected = _rejected_table(images, styles)
    if rejected is not None:
        story += [Paragraph("Why photos were rejected", styles.heading), rejected, Spacer(1, 5 * mm)]

    story.append(Paragraph("Photos", styles.heading))
    if images:
        story += [_photos_table(images, styles), Paragraph(escape(LEGEND), styles.legend)]
    else:
        story.append(Paragraph("There are no photos to list in this report.", styles.body))
    document.build(story, canvasmaker=_footer_canvas(f"{report_label} · {_source_label(payload)}"))
    return output.getvalue()


def table_style(font_size=8):
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
