"""The downloadable processing reports: totals that agree after photos are deleted, and a PDF written for people (plain words, KB / MB, wrapped names, a footer)."""
import copy
import csv
import io
import json
import re
import shutil
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, override_settings
from reportlab import rl_config
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from rest_framework.test import APITestCase

from accounts.models import User
from uploads.models import ImageUpload, UploadBatch

from .labels import (
    duplicate_label, format_bytes, format_score, format_timestamp, humanize, reason_label, sharpness_words, status_label, wrap_filename,
)
from .models import ProcessingReport
from .renderers import CONTENT_WIDTH, DELETED_ALL, PHOTO_COLUMNS, render_csv, render_json, render_pdf
from .services import build_report_payload
from .tasks import process_processing_report

LONG_NAME = "IMG_20261019_093012_portrait_of_the_whole_family_at_the_lake_original.jpg"
GENERATED = "2026-10-19T09:29:44.123456+00:00"


_SIMPLE_ESCAPES = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f"}


def _unescape(raw):
    """A PDF literal string (backslash escapes, octal codes for accented and typographic characters) as text; reportlab writes WinAnsi (cp1252)."""
    out, i = bytearray(), 0
    while i < len(raw):
        if raw[i] == 0x5C and i + 1 < len(raw):
            nxt = raw[i + 1]
            if 0x30 <= nxt <= 0x37:
                j, digits = i + 1, b""
                while j < len(raw) and len(digits) < 3 and 0x30 <= raw[j] <= 0x37:
                    digits += bytes([raw[j]])
                    j += 1
                out.append(int(digits, 8))
                i = j
                continue
            out += _SIMPLE_ESCAPES.get(nxt, bytes([nxt]))
            i += 2
            continue
        out.append(raw[i])
        i += 1
    return out.decode("cp1252")


def pdf_strings(pdf):
    """The text strings of an uncompressed PDF in drawing order (the renderer's output is compressed, so the tests render with compression off)."""
    return [_unescape(item) for item in re.findall(rb"\(((?:\\.|[^\\)])*)\)\s*Tj", pdf)]


def render_text(payload):
    with patch.object(rl_config, "pageCompression", 0):
        return pdf_strings(render_pdf(payload))


def record(image_id, name, status="accepted", *, quality=82.25, sharpness=71.5, duplicate="no_duplicate", reasons=(), enhanced=True, faces=3, failure=None):
    return {
        "image_id": image_id, "batch_id": 7, "filename": name, "status": status, "rejection_reasons": list(reasons), "failure_reason": failure,
        "uploaded_at": "2026-10-19T09:12:00+00:00",
        "original": {"format": "jpg", "mime_type": "image/jpeg", "width": 800, "height": 1000, "file_size": 85700},
        "preprocessing": {"status": "completed", "output_format": "jpeg", "output_width": 800, "output_height": 1000, "output_file_size": 1100},
        "enhancement": {"result_id": image_id if enhanced else None, "status": "completed" if enhanced else None, "requested_level": "auto",
                        "applied_level": "medium", "algorithm": "x", "output_format": "jpeg", "output_width": 800, "output_height": 1000, "output_file_size": 1150},
        "quality": {"status": "completed", "decision": status, "overall_score": quality, "brightness_score": 79.5, "sharpness_score": sharpness,
                    "blur_score": None if sharpness is None else round(100 - sharpness, 2), "contrast_score": 80, "noise_score": 78, "exposure_score": 84,
                    "resolution_score": 91, "policy_version": "p"},
        "face_validation": {"decision": "accepted", "face_count": faces, "primary_confidence": 94.0, "detector": "d"},
        "duplicate_analysis": {"status": "completed", "decision": duplicate, "match_count": 0, "policy_version": "p"},
        "identity_group": {"available": False, "group_id": None, "confidence": None},
        "processing_duration_ms": 85,
        "protected_links": {"original": None, "standardized": None, "enhanced": None},
    }


def batch_payload(images, **summary):
    counts = {}
    for item in images:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    base = {
        "total_images": len(images), "accepted_uploads": len(images), "rejected_uploads": 0, "processing_status": "completed", "status_counts": counts,
        "total_input_size": 85700 * len(images), "images_deleted": False, "images_remaining": len(images),
        "enhanced_images": sum(1 for item in images if item["enhancement"]["status"] == "completed"),
    }
    base.update(summary)
    return {
        "schema_version": "processing_report_v1", "report_type": "batch", "generated_at": GENERATED,
        "source": {"batch_id": 7, "name": "Family trip", "upload_type": "zip", "created_at": "2026-10-19T09:12:00+00:00"},
        "summary": base, "images": images,
    }


class LabelTests(SimpleTestCase):
    def test_file_sizes_read_in_kb_and_mb(self):
        self.assertEqual(format_bytes(85700), "83.7 KB")
        self.assertEqual(format_bytes(1024 * 1024), "1.0 MB")
        self.assertEqual(format_bytes(5 * 1024 * 1024 + 512 * 1024), "5.5 MB")
        self.assertEqual(format_bytes(1023), "1023 bytes")
        self.assertEqual(format_bytes(1), "1 byte")
        self.assertEqual(format_bytes(0), "0 bytes")
        self.assertEqual(format_bytes(3 * 1024 ** 3), "3.0 GB")
        self.assertEqual(format_bytes(None), "—")

    def test_the_date_reads_as_a_date_and_a_time_in_utc_with_no_microseconds(self):
        self.assertEqual(format_timestamp(GENERATED), "19 Oct 2026, 09:29 UTC")
        self.assertEqual(format_timestamp("2026-10-05T07:05:00Z"), "5 Oct 2026, 07:05 UTC")
        self.assertEqual(format_timestamp("2026-10-19T14:29:00+05:00"), "19 Oct 2026, 09:29 UTC")            # another zone is converted, not relabelled
        self.assertEqual(format_timestamp("2026-10-19T09:29:00"), "19 Oct 2026, 09:29 UTC")                  # no zone: the server's, which is UTC
        self.assertEqual(format_timestamp("not a date"), "not a date")
        self.assertEqual(format_timestamp(None), "—")

    def test_scores_have_at_most_one_decimal_and_round_half_up(self):
        self.assertEqual(format_score(82.25), "82.3")
        self.assertEqual(format_score(90.0), "90")
        self.assertEqual(format_score(0), "0")
        self.assertEqual(format_score(71.54), "71.5")
        self.assertEqual(format_score(None), "—")

    def test_sharpness_is_a_number_and_a_word(self):
        self.assertEqual(sharpness_words(100), "100 (Sharp)")
        self.assertEqual(sharpness_words(70), "70 (Sharp)")
        self.assertEqual(sharpness_words(69.9), "69.9 (Slightly soft)")
        self.assertEqual(sharpness_words(40), "40 (Slightly soft)")
        self.assertEqual(sharpness_words(39.9), "39.9 (Blurry)")
        self.assertEqual(sharpness_words(0), "0 (Blurry)")
        self.assertEqual(sharpness_words(None), "—")

    def test_codes_become_words_and_unknown_ones_stay_readable(self):
        self.assertEqual(status_label("needs_review"), "Needs review")
        self.assertEqual(status_label("partial"), "Partly completed")
        self.assertEqual(status_label("deleted"), "Deleted")
        self.assertEqual(status_label("brand_new_status"), "Brand new status")
        self.assertEqual(status_label(None), "Not available")
        self.assertEqual(reason_label("severe_blur"), "Blurry photo")
        self.assertEqual(reason_label("face_validation_required"), "Face not clear")
        self.assertEqual(reason_label("Unsupported image"), "Unsupported image")                                # an upload error is shown as recorded
        self.assertEqual(duplicate_label("no_duplicate"), "No duplicate")
        self.assertEqual(duplicate_label("needs_review"), "Possible duplicate")
        self.assertEqual(duplicate_label(None), "Not checked")
        self.assertEqual(humanize("a_b_c"), "A b c")

    def test_every_reason_the_checks_can_write_has_a_label(self):
        codes = [
            "face_too_small", "group_face_too_small", "severe_blur", "borderline_blur", "resolution_too_low", "borderline_resolution",
            "severe_underexposure", "underexposed", "severe_overexposure", "overexposed", "contrast_too_low", "borderline_contrast", "excessive_noise",
            "overall_score_below_acceptance", "no_face_detected", "large_group_detected", "face_off_center", "face_validation_rejected",
            "face_validation_required", "detector_failed",
        ]
        for code in codes:
            self.assertNotEqual(reason_label(code), code, code)
            self.assertNotIn("_", reason_label(code), code)

    def test_a_long_name_breaks_after_separators_and_each_line_fits(self):
        width = PHOTO_COLUMNS[0] - 8
        lines = wrap_filename(LONG_NAME, width)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines), LONG_NAME)                                                             # nothing lost, nothing added
        for line in lines:
            self.assertLessEqual(stringWidth(line, "Helvetica", 7), width, line)
        for line in lines[:-1]:
            self.assertRegex(line, r"[_\-. /]$", line)                                                          # every break is after a separator
        self.assertEqual(lines[-1].rsplit("_", 1)[-1], "original.jpg")
        self.assertFalse(any(line.endswith((".jp", ".jpe", ".j")) for line in lines))                           # never "...original.jp" then "g"

    def test_short_names_stay_on_one_line_and_a_piece_wider_than_a_line_is_cut_last(self):
        self.assertEqual(wrap_filename("portrait.jpg", 130), ["portrait.jpg"])
        self.assertEqual(wrap_filename("", 130), [""])
        piece = "x" * 200
        lines = wrap_filename(piece, 100)
        self.assertEqual("".join(lines), piece)
        self.assertTrue(all(stringWidth(line, "Helvetica", 7) <= 100 for line in lines))
        self.assertEqual(wrap_filename("a_b", 1)[0], "a")                                                       # degenerate width: still terminates


class PdfTextTests(SimpleTestCase):
    def setUp(self):
        self.images = [
            record(1, "portrait.jpg"),
            record(2, LONG_NAME, quality=64.0, sharpness=35.0, duplicate="duplicate"),
            record(3, "blurry_night.jpeg", "rejected", quality=31.2, sharpness=12.0, reasons=["severe_blur", "severe_underexposure"], enhanced=False, faces=0),
            record(4, "holiday photo copy 2.png", "needs_review", quality=55.5, sharpness=48.0, duplicate="needs_review", reasons=["face_validation_required"],
                   enhanced=False),
            record(5, "broken.bmp", "rejected", quality=None, sharpness=None, duplicate=None, reasons=["Unsupported image"], enhanced=False, faces=None),
            record(6, "group.jpg", "rejected", reasons=["face_validation_rejected", "no_face_detected"], enhanced=False, faces=0),
        ]
        self.payload = batch_payload(self.images, accepted_uploads=5, rejected_uploads=1, processing_status="partial")
        self.text = render_text(self.payload)

    def test_the_generated_time_is_a_readable_date_not_an_iso_string(self):
        self.assertIn(" 19 Oct 2026, 09:29 UTC", self.text)
        joined = "\n".join(self.text)
        self.assertNotIn("2026-10-19T", joined)
        self.assertNotIn("123456", joined)

    def test_sizes_are_in_kb_not_bytes(self):
        self.assertIn("502.1 KB", self.text)                                         # 6 x 85,700 bytes
        self.assertNotIn("514200", self.text)

    def test_the_results_are_separate_rows(self):
        for label, count in (("Accepted", "2"), ("Rejected", "3"), ("Needs review", "1")):
            index = self.text.index(label)
            self.assertEqual(self.text[index + 1], count, label)
        self.assertNotIn("accepted: 2", "\n".join(self.text))
        self.assertFalse(any(":" in item and "accepted" in item for item in self.text))

    def test_zero_counts_are_shown_for_the_three_outcomes(self):
        text = render_text(batch_payload([record(1, "a.jpg")]))
        for label in ("Rejected", "Needs review"):
            self.assertEqual(text[text.index(label) + 1], "0", label)

    def test_statuses_reasons_and_duplicate_decisions_are_in_words(self):
        for words in ("No duplicate", "Duplicate", "Possible duplicate", "Not checked", "Needs review", "Blurry photo; Almost black", "Face not clear; No face found",
                      "Unsupported image"):
            self.assertTrue(any(words in item for item in self.text), words)
        joined = "\n".join(self.text)
        for code in ("no_duplicate", "needs_review", "severe_blur", "severe_underexposure", "face_validation_rejected", "no_face_detected"):
            self.assertNotIn(code, joined)

    def test_the_summary_has_needs_review_and_enhanced_counts_and_the_reason_for_each_rejected_photo(self):
        self.assertEqual(self.text[self.text.index("Enhanced") + 1], "2")
        self.assertEqual(self.text[self.text.index("Needs review") + 1], "1")
        self.assertIn("Why photos were rejected", self.text)
        rejected = self.text[self.text.index("Why photos were rejected"):self.text.index("Photos")]
        self.assertIn("blurry_night.jpeg", rejected)
        self.assertIn("Blurry photo; Almost black", rejected)
        self.assertIn("broken.bmp", rejected)

    def test_a_rejected_photo_with_no_recorded_reason_says_so(self):
        text = render_text(batch_payload([record(1, "x.jpg", "rejected", reasons=[], enhanced=False)]))
        self.assertIn("No reason was recorded.", text)

    def test_a_failure_sentence_is_used_when_there_are_no_reason_codes(self):
        text = render_text(batch_payload([record(1, "x.jpg", "rejected", reasons=[], enhanced=False, failure="Image preprocessing failed unexpectedly.")]))
        self.assertIn("Image preprocessing failed unexpectedly.", text)

    def test_the_quality_columns_say_what_the_numbers_are(self):
        for header in ("Quality score", "(0–100)", "Brightness", "Sharpness (0–100)"):
            self.assertTrue(any(header in item for item in self.text), header)
        self.assertNotIn("Blur", self.text)                                         # "Blur 0.0" is gone: a sharp photo no longer reads as 0
        self.assertIn("71.5 (Sharp)", self.text)
        self.assertIn("35 (Blurry)", self.text)
        self.assertIn("82.3", self.text)
        self.assertTrue(any(item.startswith("How to read the scores") for item in self.text))

    def test_a_long_filename_wraps_at_separators_in_every_cell_it_appears_in(self):
        width = PHOTO_COLUMNS[0] - 8
        expected = wrap_filename(LONG_NAME, width)
        self.assertGreater(len(expected), 1)
        start = self.text.index(expected[0])
        self.assertEqual(self.text[start:start + len(expected)], expected)
        self.assertNotIn(LONG_NAME[:-1], self.text)                                  # not left as one overflowing string
        self.assertFalse(any(item.endswith(".jp") for item in self.text))
        joined = "".join(self.text)
        self.assertIn("original.jpg", joined)

    def test_a_long_source_name_wraps_too(self):
        name = "Family_trip_" + "very_long_name_" * 12 + "end.zip"
        payload = copy.deepcopy(self.payload)
        payload["source"]["name"] = name
        text = render_text(payload)
        shown = text[text.index("Source:") + 1:text.index("Generated:")]
        expected = wrap_filename(name, CONTENT_WIDTH - 24 * mm, "Helvetica", 10)
        self.assertGreater(len(expected), 1)
        self.assertEqual([item.strip() for item in shown], expected)
        self.assertEqual("".join(item.strip() for item in shown), name)

    def test_with_no_rows_there_is_a_sentence_not_a_header_only_table(self):
        text = render_text(batch_payload([], total_images=0, accepted_uploads=0, rejected_uploads=0, total_input_size=0, images_remaining=0, enhanced_images=None))
        self.assertIn("There are no photos to list in this report.", text)
        self.assertNotIn("Filename", text)
        self.assertNotIn("Duplicate check", text)

    def test_every_page_has_the_report_type_the_source_and_page_x_of_n(self):
        text = render_text(self.payload)
        self.assertIn("Page 1 of 1", text)
        self.assertIn("Batch report · Family trip", text)
        many = batch_payload([record(i, f"photo_{i:03d}.jpg") for i in range(1, 91)])
        pages = render_text(many)
        totals = {int(match.group(2)) for match in (re.fullmatch(r"Page (\d+) of (\d+)", item) for item in pages) if match}
        self.assertEqual(len(totals), 1)
        total = totals.pop()
        self.assertGreater(total, 1)
        for number in range(1, total + 1):
            self.assertIn(f"Page {number} of {total}", pages)
        self.assertEqual(pages.count("Batch report · Family trip"), total)

    def test_a_photo_report_says_photo_report_in_the_footer(self):
        payload = {
            "schema_version": "processing_report_v1", "report_type": "image", "generated_at": GENERATED,
            "source": {"image_id": 1, "batch_id": 7, "name": "portrait.jpg"}, "summary": {"total_images": 1, "status": "accepted"},
            "images": [record(1, "portrait.jpg")],
        }
        text = render_text(payload)
        self.assertIn("Photo report · portrait.jpg", text)
        self.assertEqual(text[text.index("Result") + 1], "Accepted")
        self.assertEqual(text[text.index("Enhanced") + 1], "Yes")

    def test_the_deleted_photos_sentence_and_the_totals_are_printed(self):
        payload = batch_payload([], total_images=3, accepted_uploads=2, rejected_uploads=1, status_counts={"deleted": 3}, images_deleted=True,
                                images_remaining=0, enhanced_images=None, total_input_size=2500)
        text = render_text(payload)
        self.assertIn(DELETED_ALL, text)
        self.assertEqual(text[text.index("Total images") + 1], "3")
        self.assertEqual(text[text.index("Accepted uploads") + 1], "2")
        self.assertEqual(text[text.index("Photos deleted") + 1], "3")
        self.assertEqual(text[text.index("Total input size") + 1], "2.4 KB")
        self.assertNotIn("Enhanced", text)                                          # unknown once the photos are gone, so not claimed as 0
        self.assertNotIn("Filename", text)
        self.assertIn("There are no photos to list in this report.", text)

    def test_when_only_some_photos_are_deleted_the_table_lists_those_that_remain(self):
        payload = batch_payload([record(1, "kept.jpg")], total_images=3, accepted_uploads=3, status_counts={"accepted": 1, "deleted": 2},
                                images_deleted=True, images_remaining=1)
        text = render_text(payload)
        self.assertTrue(any(item.startswith("Some photos of this upload have been deleted. 1 of 3 remain") for item in text))
        self.assertEqual(text[text.index("Deleted") + 1], "2")
        self.assertIn("kept.jpg", text)

    def test_an_older_stored_payload_without_the_new_fields_still_renders(self):
        payload = batch_payload(self.images)
        for key in ("images_deleted", "images_remaining", "enhanced_images"):
            payload["summary"].pop(key)
        text = render_text(payload)
        self.assertEqual(text[text.index("Enhanced") + 1], "2")                    # worked out from the photos
        self.assertNotIn("Photos still stored", text)

    def test_special_characters_in_names_and_reasons_are_escaped(self):
        text = render_text(batch_payload([record(1, "a&b <c>.jpg", "rejected", reasons=["x < y & z"], enhanced=False)]))
        cell = text[text.index("Why it was rejected") + 1:text.index("x < y & z")]
        self.assertEqual("".join(cell), "a&b <c>.jpg")                              # reportlab draws the pieces around an escaped character separately
        self.assertIn("x < y & z", text)


class DeletedPhotosPayloadTests(APITestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="facecloud-reports-")
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        self.user = User.objects.create_user(username="rr-owner", email="rr-owner@example.com", password="StrongPass123!", is_email_verified=True)
        self.batch = UploadBatch.objects.create(
            owner=self.user, upload_type="zip", display_name="Family trip", status="partial", total_files=3, accepted_files=2, rejected_files=1,
            total_size=2500, processing_status="completed", processed_files=2, current_stage="completed",
        )

    def make_report(self, report_format="json", batch=None):
        return ProcessingReport.objects.create(
            owner=self.user, report_type="batch", format=report_format, batch=batch or self.batch, status="queued", task_id="rr-task",
        )

    def add_rejected_photo(self, name="bad.jpg"):
        image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name=name, file_extension=".jpg", mime_type="image/jpeg", file_size=900, width=10, height=10,
            checksum_sha256="b" * 64, status="rejected", rejection_reason="Unsupported image",
        )
        image.original_file.save(name, ContentFile(b"x"), save=True)
        return image

    def test_after_every_photo_is_deleted_the_totals_come_from_the_batch_and_agree(self):
        summary = build_report_payload(self.make_report())["summary"]
        self.assertEqual(summary["total_images"], 3)
        self.assertEqual((summary["accepted_uploads"], summary["rejected_uploads"]), (2, 1))
        self.assertEqual(summary["status_counts"], {"deleted": 3})
        self.assertEqual(sum(summary["status_counts"].values()), summary["total_images"])
        self.assertEqual((summary["images_deleted"], summary["images_remaining"]), (True, 0))
        self.assertIsNone(summary["enhanced_images"])                                # unknown, not 0
        self.assertEqual(summary["total_input_size"], 2500)
        self.assertFalse(summary["total_images"] == 0 and summary["accepted_uploads"] > 0)

    def test_the_report_is_still_produced_in_every_format(self):
        for report_format, content_type in (("json", "application/json"), ("csv", "text/csv; charset=utf-8"), ("pdf", "application/pdf")):
            report = process_processing_report(self.make_report(report_format).pk, "rr-task")
            self.assertEqual((report.status, report.content_type), ("ready", content_type), report_format)

    def test_the_pdf_of_such_a_batch_states_it_and_shows_the_totals(self):
        payload = build_report_payload(self.make_report("pdf"))
        text = render_text(payload)
        self.assertIn(DELETED_ALL, text)
        self.assertEqual(text[text.index("Total images") + 1], "3")
        self.assertEqual(text[text.index("Accepted uploads") + 1], "2")
        self.assertEqual(text[text.index("Rejected uploads") + 1], "1")
        self.assertEqual(text[text.index("Photos deleted") + 1], "3")
        self.assertIn("2.4 KB", text)
        self.assertTrue(any("Family trip" in item for item in text))

    def test_the_stored_json_snapshot_carries_the_new_fields(self):
        report = process_processing_report(self.make_report("json").pk, "rr-task")
        with report.report_file.open("rb") as handle:
            document = json.loads(handle.read().decode("utf-8"))
        self.assertEqual(document["summary"]["images_deleted"], True)
        self.assertEqual(document, report.payload)
        self.assertEqual(document["images"], [])

    def test_when_some_photos_remain_the_deleted_ones_are_counted_and_the_numbers_add_up(self):
        self.add_rejected_photo()
        summary = build_report_payload(self.make_report())["summary"]
        self.assertEqual(summary["total_images"], 3)
        self.assertEqual(summary["status_counts"], {"rejected": 1, "deleted": 2})
        self.assertEqual(sum(summary["status_counts"].values()), summary["total_images"])
        self.assertEqual((summary["images_deleted"], summary["images_remaining"], summary["enhanced_images"]), (True, 1, 0))

    def test_a_batch_whose_photos_are_all_there_is_unchanged_apart_from_the_added_fields(self):
        batch = UploadBatch.objects.create(
            owner=self.user, upload_type="single", status="uploaded", total_files=1, accepted_files=1, total_size=900, processing_status="completed",
            processed_files=1, current_stage="completed",
        )
        image = ImageUpload.objects.create(
            owner=self.user, batch=batch, original_name="ok.jpg", file_extension=".jpg", mime_type="image/jpeg", file_size=900, width=10, height=10,
            checksum_sha256="c" * 64, status="uploaded",
        )
        summary = build_report_payload(self.make_report(batch=batch))["summary"]
        self.assertEqual(summary["total_images"], 1)
        self.assertNotIn("deleted", summary["status_counts"])
        self.assertEqual((summary["images_deleted"], summary["images_remaining"]), (False, 1))
        self.assertEqual(image.status, "uploaded")

    def test_a_batch_with_no_recorded_totals_never_reports_zero_images_next_to_photos(self):
        batch = UploadBatch.objects.create(owner=self.user, upload_type="single", processing_status="completed")
        ImageUpload.objects.create(
            owner=self.user, batch=batch, original_name="ok.jpg", file_extension=".jpg", mime_type="image/jpeg", file_size=900, width=10, height=10,
            checksum_sha256="d" * 64, status="uploaded",
        )
        summary = build_report_payload(self.make_report(batch=batch))["summary"]
        self.assertEqual((summary["total_images"], summary["images_deleted"]), (1, False))


class ContractTests(SimpleTestCase):
    """The JSON and CSV field names are the same as before; the only thing added is the three batch summary fields."""

    RECORD_KEYS = {
        "image_id", "batch_id", "filename", "status", "rejection_reasons", "failure_reason", "uploaded_at", "original", "preprocessing", "enhancement",
        "quality", "face_validation", "duplicate_analysis", "identity_group", "processing_duration_ms", "protected_links",
    }
    CSV_HEADER = [
        "image_id", "batch_id", "filename", "status", "rejection_reasons", "failure_reason", "enhancement_requested", "enhancement_applied",
        "enhancement_algorithm", "brightness_score", "blur_score", "sharpness_score", "overall_quality_score", "face_count", "face_confidence",
        "duplicate_decision", "duplicate_match_count", "identity_group", "processing_duration_ms", "original_link", "standardized_link", "enhanced_link",
        "uploaded_at",
    ]
    OLD_BATCH_SUMMARY = {"total_images", "accepted_uploads", "rejected_uploads", "processing_status", "status_counts", "total_input_size"}

    def test_the_csv_header_is_exactly_what_it_was(self):
        payload = batch_payload([record(1, "a.jpg")])
        rows = list(csv.reader(io.StringIO(render_csv(payload).decode("utf-8-sig"))))
        self.assertEqual(rows[0], self.CSV_HEADER)
        self.assertEqual(rows[1][2], "a.jpg")
        self.assertEqual(rows[1][10], "28.5")                                      # blur_score is still 100 - sharpness: the PDF no longer shows it, the CSV keeps it

    def test_the_csv_of_a_batch_with_no_photos_is_still_the_short_header(self):
        payload = batch_payload([], total_images=3, images_deleted=True, images_remaining=0)
        rows = list(csv.reader(io.StringIO(render_csv(payload).decode("utf-8-sig"))))
        self.assertEqual(rows, [["image_id", "filename", "status"]])

    def test_the_json_keeps_raw_values_and_every_old_field(self):
        payload = batch_payload([record(1, "a.jpg", sharpness=71.5)])
        document = json.loads(render_json(payload))
        self.assertEqual(document["generated_at"], GENERATED)                      # the ISO string stays in JSON; only the PDF reformats it
        self.assertEqual(document["summary"]["total_input_size"], 85700)           # raw bytes stay in JSON
        self.assertEqual(document["images"][0]["status"], "accepted")              # raw codes stay in JSON
        self.assertEqual(document["images"][0]["quality"]["blur_score"], 28.5)
        self.assertEqual(set(document["images"][0]), self.RECORD_KEYS)
        self.assertTrue(self.OLD_BATCH_SUMMARY <= set(document["summary"]))
        self.assertEqual(set(document["summary"]) - self.OLD_BATCH_SUMMARY, {"images_deleted", "images_remaining", "enhanced_images"})
