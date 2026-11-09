"""When a photograph was taken: read from EXIF at upload, parsed defensively, filled in for old photographs by a command, and usable as a gallery filter."""
import io
import tempfile
from datetime import datetime, timedelta, timezone
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .capture import capture_time, parse_exif_datetime
from .models import ImageUpload, UploadBatch

User = get_user_model()
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def jpeg(taken=None, offset=None, digitized=None, generic=None, fmt="JPEG", size=(64, 48)):
    """A small picture with the given EXIF times (``taken``: DateTimeOriginal, ``digitized``, ``generic``: DateTime)."""
    image = Image.new("RGB", size, (90, 120, 150))
    exif = Image.Exif()
    inner = {}
    if taken:
        inner[36867] = taken
    if offset:
        inner[36881] = offset
    if digitized:
        inner[36868] = digitized
    if inner:
        exif[0x8769] = inner
    if generic:
        exif[306] = generic
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, exif=exif)
    return buffer.getvalue()


class ParseTests(SimpleTestCase):
    def test_a_plain_time_is_taken_as_utc(self):
        self.assertEqual(parse_exif_datetime("2023:10:13 21:33:05", now=NOW), datetime(2023, 10, 13, 21, 33, 5, tzinfo=timezone.utc))

    def test_an_offset_makes_it_exact(self):
        self.assertEqual(parse_exif_datetime("2023:10:13 21:33:05", "+05:00", now=NOW), datetime(2023, 10, 13, 16, 33, 5, tzinfo=timezone.utc))
        self.assertEqual(parse_exif_datetime("2023:10:13 21:33:05", "-03:30", now=NOW), datetime(2023, 10, 14, 1, 3, 5, tzinfo=timezone.utc))

    def test_bytes_padding_and_a_bad_offset(self):
        self.assertEqual(parse_exif_datetime(b"2023:10:13 21:33:05\x00", now=NOW), datetime(2023, 10, 13, 21, 33, 5, tzinfo=timezone.utc))
        self.assertEqual(parse_exif_datetime("2023:10:13 21:33:05", "garbage", now=NOW), datetime(2023, 10, 13, 21, 33, 5, tzinfo=timezone.utc))

    def test_nonsense_gives_none(self):
        for raw in (None, 12345, "", "not a date", "0000:00:00 00:00:00", "2023-10-13 21:33:05", "2023:13:40 25:61:61", "1980:01:01 00:00:00", "2031:01:01 00:00:00"):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_exif_datetime(raw, now=NOW))

    def test_the_day_after_now_is_allowed_for_clock_skew_and_two_days_is_not(self):
        self.assertIsNotNone(parse_exif_datetime((NOW + timedelta(days=1)).strftime("%Y:%m:%d %H:%M:%S"), now=NOW))
        self.assertIsNone(parse_exif_datetime((NOW + timedelta(days=3)).strftime("%Y:%m:%d %H:%M:%S"), now=NOW))


class ReadTests(SimpleTestCase):
    def read(self, data):
        with Image.open(io.BytesIO(data)) as image:
            return capture_time(image)

    def test_the_original_time_wins_over_the_digitized_and_the_generic_one(self):
        data = jpeg(taken="2022:05:01 10:00:00", digitized="2022:06:01 10:00:00", generic="2022:07:01 10:00:00")
        self.assertEqual(self.read(data), datetime(2022, 5, 1, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(self.read(jpeg(digitized="2022:06:01 10:00:00", generic="2022:07:01 10:00:00")), datetime(2022, 6, 1, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(self.read(jpeg(generic="2022:07:01 10:00:00")), datetime(2022, 7, 1, 10, 0, tzinfo=timezone.utc))

    def test_a_bad_original_falls_through_to_the_next_source(self):
        self.assertEqual(self.read(jpeg(taken="0000:00:00 00:00:00", digitized="2022:06:01 10:00:00")), datetime(2022, 6, 1, 10, 0, tzinfo=timezone.utc))

    def test_no_exif_gives_none_and_so_does_an_unreadable_image(self):
        self.assertIsNone(self.read(jpeg()))
        self.assertIsNone(capture_time(object()))

    def test_png_carries_it_too(self):
        self.assertEqual(self.read(jpeg(taken="2021:03:04 05:06:07", fmt="PNG")), datetime(2021, 3, 4, 5, 6, 7, tzinfo=timezone.utc))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="facecloud-capture-"))
class UploadAndFilterTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="cap-owner", email="cap-owner@example.com", password="Password!123", is_email_verified=True)
        token = RefreshToken.for_user(self.owner)
        token["role"] = self.owner.role
        token["token_version"] = self.owner.token_version
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def upload(self, data, name="shot.jpg"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        with self.captureOnCommitCallbacks(execute=False):
            response = self.client.post("/api/images/upload/single/", {"file": SimpleUploadedFile(name, data, content_type="image/jpeg")}, format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["images"][0]

    def test_an_upload_records_the_capture_time_and_the_row_shows_it(self):
        row = self.upload(jpeg(taken="2023:10:13 21:33:05", offset="+05:00", size=(480, 640)))
        self.assertEqual(row["taken_at"], "2023-10-13T16:33:05Z")
        self.assertEqual(ImageUpload.objects.get(pk=row["id"]).taken_at, datetime(2023, 10, 13, 16, 33, 5, tzinfo=timezone.utc))
        listing = self.client.get(reverse("my-image-list")).json()["results"]
        self.assertEqual(listing[0]["taken_at"], "2023-10-13T16:33:05Z")

    def test_a_photograph_without_exif_has_none_and_is_dated_by_its_upload(self):
        row = self.upload(jpeg(size=(480, 640)))
        self.assertIsNone(row["taken_at"])
        image = ImageUpload.objects.get(pk=row["id"])
        self.assertEqual(image.captured_at, image.created_at)

    def test_the_gallery_can_be_filtered_by_the_day_it_was_taken(self):
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple")
        make = lambda name, taken: ImageUpload.objects.create(owner=self.owner, batch=batch, original_name=name, status="uploaded", taken_at=taken)   # noqa: E731
        old = make("old.jpg", datetime(2023, 10, 13, 20, 0, tzinfo=timezone.utc))
        mid = make("mid.jpg", datetime(2024, 1, 5, 9, 0, tzinfo=timezone.utc))
        undated = make("undated.jpg", None)                                                                 # falls back to the upload time (now)

        def ids(**params):
            return {row["id"] for row in self.client.get(reverse("my-image-list"), params).json()["results"]}

        self.assertEqual(ids(taken_from="2023-10-13", taken_to="2023-10-13"), {old.pk})
        self.assertEqual(ids(taken_from="2024-01-01"), {mid.pk, undated.pk})
        self.assertEqual(ids(taken_to="2023-12-31"), {old.pk})
        self.assertEqual(ids(taken_from="2023-01-01", taken_to="2024-12-31"), {old.pk, mid.pk})
        self.assertEqual(self.client.get(reverse("my-image-list"), {"taken_from": "13/10/2023"}).status_code, 400)
        self.assertEqual(self.client.get(reverse("my-image-list"), {"taken_to": "x"}).status_code, 400)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="facecloud-backfill-"))
class BackfillTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="fill-owner", email="fill-owner@example.com", password="Password!123", is_email_verified=True)
        self.batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple")

    def photo(self, data, name):
        image = ImageUpload.objects.create(owner=self.owner, batch=self.batch, original_name=name, status="uploaded")
        image.original_file.save(name, ContentFile(data))
        return image

    def run_command(self, *args):
        out = StringIO()
        call_command("backfill_taken_at", *args, stdout=out)
        return out.getvalue()

    def test_it_fills_the_ones_with_an_exif_time_and_leaves_the_rest(self):
        with_time = self.photo(jpeg(taken="2020:02:03 04:05:06"), "a.jpg")
        without = self.photo(jpeg(), "b.jpg")
        broken = self.photo(b"not an image", "c.jpg")
        already = ImageUpload.objects.create(owner=self.owner, batch=self.batch, original_name="d.jpg", status="uploaded", taken_at=datetime(2019, 1, 1, tzinfo=timezone.utc))
        already.original_file.save("d.jpg", ContentFile(jpeg(taken="2020:02:03 04:05:06")))

        report = self.run_command("--dry-run")
        self.assertIn("would set the capture time of 1", report)
        self.assertIsNone(ImageUpload.objects.get(pk=with_time.pk).taken_at)                      # a dry run changes nothing

        report = self.run_command()
        self.assertIn("set the capture time of 1, 1 could not be read, 1 have no EXIF time", report)
        self.assertEqual(ImageUpload.objects.get(pk=with_time.pk).taken_at, datetime(2020, 2, 3, 4, 5, 6, tzinfo=timezone.utc))
        self.assertIsNone(ImageUpload.objects.get(pk=without.pk).taken_at)
        self.assertIsNone(ImageUpload.objects.get(pk=broken.pk).taken_at)
        self.assertEqual(ImageUpload.objects.get(pk=already.pk).taken_at, datetime(2019, 1, 1, tzinfo=timezone.utc))      # never overwritten

    def test_running_it_again_does_nothing_new_and_a_limit_is_honoured(self):
        first, second = self.photo(jpeg(taken="2020:02:03 04:05:06"), "a.jpg"), self.photo(jpeg(taken="2021:02:03 04:05:06"), "b.jpg")
        self.assertIn("Looked at 1 photograph(s)", self.run_command("--limit", "1"))
        self.assertIsNotNone(ImageUpload.objects.get(pk=first.pk).taken_at)
        self.assertIsNone(ImageUpload.objects.get(pk=second.pk).taken_at)
        self.run_command()
        self.assertIn("Looked at 0 photograph(s)", self.run_command())
