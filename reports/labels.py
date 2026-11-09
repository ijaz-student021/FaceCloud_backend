"""Plain-words versions of what a processing report records, for the PDF.

The JSON and CSV reports keep the raw codes and raw numbers (they are for programs). The PDF is for people, so it shows these instead: a readable date, file
sizes in KB or MB, labels for statuses, reasons and duplicate decisions, and file names wrapped at natural places. Nothing here changes the stored payload.
"""
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from reportlab.pdfbase.pdfmetrics import stringWidth

STATUS_LABELS = {
    "accepted": "Accepted",
    "rejected": "Rejected",
    "needs_review": "Needs review",
    "completed": "Completed",
    "partial": "Partly completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "uploaded": "Uploaded",
    "pending": "Waiting",
    "queued": "Waiting",
    "processing": "In progress",
    "deleted": "Deleted",
}

# Reason codes as the checks write them (quality_control/services.py, validation/services.py) in short words. Anything else (the text of an upload
# error such as "Unsupported image", or a failure sentence) is shown as it was recorded.
REASON_LABELS = {
    "face_too_small": "Face too small",
    "group_face_too_small": "Some faces too small",
    "severe_blur": "Blurry photo",
    "borderline_blur": "Slightly blurry",
    "resolution_too_low": "Resolution too low",
    "borderline_resolution": "Low resolution",
    "severe_underexposure": "Almost black",
    "underexposed": "Dark (can be brightened)",
    "severe_overexposure": "Too bright",
    "overexposed": "Rather bright",
    "contrast_too_low": "Contrast too low",
    "borderline_contrast": "Low contrast",
    "excessive_noise": "Very grainy",
    "overall_score_below_acceptance": "Overall quality too low",
    "no_face_detected": "No face found",
    "large_group_detected": "Too many faces",
    "face_off_center": "Face too close to the edge",
    "face_validation_rejected": "Face not clear",
    "face_validation_required": "Face not clear",
    "detector_failed": "Face check could not run",
}

DUPLICATE_LABELS = {
    "no_duplicate": "No duplicate",
    "duplicate": "Duplicate",
    "needs_review": "Possible duplicate",
}

UPLOAD_TYPE_LABELS = {"single": "One photo", "multiple": "Several photos", "zip": "ZIP archive"}

SHARP_FROM = 70
SOFT_FROM = 40


def humanize(code):
    """``some_code`` -> ``Some code``: the fallback for a value this module has no label for."""
    text = str(code).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def status_label(code):
    if code in (None, ""):
        return "Not available"
    return STATUS_LABELS.get(code, humanize(code))


def reason_label(reason):
    return REASON_LABELS.get(reason, str(reason))


def duplicate_label(decision):
    if not decision:
        return "Not checked"
    return DUPLICATE_LABELS.get(decision, humanize(decision))


def upload_type_label(code):
    return UPLOAD_TYPE_LABELS.get(code, humanize(code or "unknown"))


def format_bytes(size):
    """``83.7 KB``, ``1.2 MB``: 1024-based units, one decimal, plain ``bytes`` under 1 KB. ``None`` (not recorded) reads as a dash."""
    if size is None:
        return "—"
    size = int(size)
    if size < 1024:
        return f"{size} bytes" if size != 1 else "1 byte"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"          # unreachable; keeps the function total


def format_timestamp(value):
    """``19 Oct 2026, 09:29 UTC`` from an ISO 8601 string or a datetime. The server runs in UTC; the zone is written out so nobody has to guess.
    Something that cannot be read is returned as it came."""
    if value in (None, ""):
        return "—"
    moment = value
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return f"{moment.day} {moment:%b %Y}, {moment:%H:%M} UTC"


def format_score(value):
    """A 0-100 score with at most one decimal and no trailing ``.0``; ``None`` is a dash."""
    if value is None:
        return "—"
    text = str(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))        # 82.25 -> 82.3, not the 82.2 float formatting gives
    return text[:-2] if text.endswith(".0") else text


def sharpness_words(score):
    """The sharpness score with a word for it. The bands are a reading guide; the accept / reject decision uses the full set of checks."""
    if score is None:
        return "—"
    words = "Sharp" if score >= SHARP_FROM else "Slightly soft" if score >= SOFT_FROM else "Blurry"
    return f"{format_score(score)} ({words})"


_BREAK_AFTER = re.compile(r"(?<=[_\-. /])")


def wrap_filename(name, width, font="Helvetica", size=7):
    """Lines of ``name`` that each fit in ``width`` points, broken after ``_ - . /`` or a space, never inside a run of letters when it can be avoided.

    Only a single piece wider than the whole line is cut, as a last resort. The result is for a table cell: join it with line breaks.
    """
    name = str(name)
    lines, current = [], ""
    for piece in _BREAK_AFTER.split(name):
        if not piece:
            continue
        if stringWidth(current + piece, font, size) <= width:
            current += piece
            continue
        if current:
            lines.append(current)
            current = ""
        while stringWidth(piece, font, size) > width and len(piece) > 1:     # one piece wider than a line: cut it as late as it fits
            cut = len(piece) - 1
            while cut > 1 and stringWidth(piece[:cut], font, size) > width:
                cut -= 1
            lines.append(piece[:cut])
            piece = piece[cut:]
        current = piece
    if current:
        lines.append(current)
    return lines or [""]
