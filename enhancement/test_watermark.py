import hashlib
import io
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from PIL import Image, ImageChops
from rest_framework.test import APIClient

from downloads.models import DownloadEvent
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import ImageEditVersion, ImageEnhancementResult
from .watermark import (
    MAX_CUSTOM_LENGTH,
    WatermarkError,
    WatermarkSpec,
    apply_watermark,
    validate_spec,
    watermark_label,
)

User = get_user_model()
OWNER = SimpleNamespace(username="alice")


def flat_image(size=(400, 300), colour=(90, 120, 150)):
    return Image.new("RGB", size, colour)


def changed_box(before, after):
    return ImageChops.difference(before, after).getbbox()


class WatermarkSpecTests(SimpleTestCase):
    def test_defaults_are_no_watermark(self):
        spec = validate_spec()
        self.assertFalse(spec.active)

    def test_rejects_unknown_mode_and_position(self):
        with self.assertRaises(WatermarkError) as mode:
            validate_spec(mode="stamp")
        self.assertEqual(mode.exception.field, "watermark")
        with self.assertRaises(WatermarkError) as position:
            validate_spec(mode="facecloud", position="middle")
        self.assertEqual(position.exception.field, "watermark_position")

    def test_opacity_must_be_in_range(self):
        for bad in (0, 0.05, 1.5, "abc"):
            with self.subTest(opacity=bad), self.assertRaises(WatermarkError) as caught:
                validate_spec(mode="facecloud", opacity=bad)
            self.assertEqual(caught.exception.field, "watermark_opacity")
        self.assertEqual(validate_spec(mode="facecloud", opacity="0.3").opacity, 0.3)

    def test_custom_text_rules(self):
        with self.assertRaises(WatermarkError):
            validate_spec(mode="custom", text="   ")
        with self.assertRaises(WatermarkError):
            validate_spec(mode="custom", text="x" * (MAX_CUSTOM_LENGTH + 1))
        with self.assertRaises(WatermarkError) as unsupported:
            validate_spec(mode="custom", text="水印")
        self.assertEqual(unsupported.exception.field, "watermark_text")
        self.assertEqual(validate_spec(mode="custom", text="  © Café 2026  ").text, "© Café 2026")

    def test_text_is_ignored_unless_custom(self):
        self.assertEqual(validate_spec(mode="facecloud", text="ignored").text, "")

    def test_label_per_mode(self):
        self.assertEqual(watermark_label(validate_spec(mode="small"), OWNER), "FaceCloud")
        self.assertEqual(watermark_label(validate_spec(mode="facecloud"), OWNER), "FaceCloud")
        self.assertEqual(watermark_label(validate_spec(mode="username"), OWNER), "@alice")
        self.assertEqual(watermark_label(validate_spec(mode="custom", text="Hi"), OWNER), "Hi")


class WatermarkRenderingTests(SimpleTestCase):
    def test_no_watermark_returns_an_identical_copy(self):
        image = flat_image()
        result = apply_watermark(image, WatermarkSpec(), OWNER)
        self.assertIsNot(result, image)
        self.assertIsNone(changed_box(image, result))

    def test_source_image_is_never_modified(self):
        image = flat_image()
        snapshot = image.copy()
        apply_watermark(image, validate_spec(mode="facecloud"), OWNER)
        self.assertIsNone(changed_box(image, snapshot))

    def test_corner_mark_stays_in_its_corner(self):
        image = flat_image()
        for position, corner in (
            ("bottom_right", lambda b, w, h: b[0] > w // 2 and b[1] > h // 2),
            ("top_left", lambda b, w, h: b[2] < w // 2 and b[3] < h // 2),
            ("top_right", lambda b, w, h: b[0] > w // 2 and b[3] < h // 2),
            ("bottom_left", lambda b, w, h: b[2] < w // 2 and b[1] > h // 2),
        ):
            with self.subTest(position=position):
                stamped = apply_watermark(image, validate_spec(mode="facecloud", position=position), OWNER)
                box = changed_box(image, stamped)
                self.assertIsNotNone(box)
                self.assertTrue(corner(box, *image.size), box)

    def test_centre_mark_is_centred(self):
        image = flat_image()
        box = changed_box(image, apply_watermark(image, validate_spec(mode="facecloud", position="center"), OWNER))
        centre_x, centre_y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        self.assertAlmostEqual(centre_x, image.width / 2, delta=8)
        self.assertAlmostEqual(centre_y, image.height / 2, delta=8)

    def test_tiled_mark_covers_more_of_the_image_than_a_corner_mark(self):
        image = flat_image((800, 600))
        corner = changed_box(image, apply_watermark(image, validate_spec(mode="facecloud"), OWNER))
        tiled = changed_box(
            image, apply_watermark(image, validate_spec(mode="facecloud", position="tile"), OWNER),
        )
        area = lambda b: (b[2] - b[0]) * (b[3] - b[1])
        self.assertGreater(area(tiled), area(corner) * 4)

    def test_small_is_smaller_than_the_brand_mark(self):
        image = flat_image((800, 600))
        small = changed_box(image, apply_watermark(image, validate_spec(mode="small"), OWNER))
        brand = changed_box(image, apply_watermark(image, validate_spec(mode="facecloud"), OWNER))
        self.assertLess(small[2] - small[0], brand[2] - brand[0])

    def test_lower_opacity_changes_pixels_less(self):
        image = flat_image()

        def strongest(opacity):
            stamped = apply_watermark(image, validate_spec(mode="facecloud", opacity=opacity), OWNER)
            return max(ImageChops.difference(image, stamped).convert("L").getdata())

        self.assertLess(strongest(0.2), strongest(1.0))

    def test_tiny_images_do_not_crash(self):
        for position in ("bottom_right", "center", "tile"):
            with self.subTest(position=position):
                apply_watermark(flat_image((24, 24)), validate_spec(mode="username", position=position), OWNER)


class WatermarkEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="mark-owner", email="mark-owner@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="mark-other", email="mark-other@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        image = ImageUpload.objects.create(
            owner=self.user, batch=batch, original_name="a.png", status="uploaded",
        )
        pre = ImagePreprocessingResult.objects.create(
            owner=self.user, image=image, status="completed", output_format="png",
        )
        buffer = io.BytesIO()
        flat_image((1600, 1200)).save(buffer, format="PNG")
        self.png = buffer.getvalue()
        self.result = ImageEnhancementResult.objects.create(
            owner=self.user, image=image, preprocessing_result=pre, status="completed",
            output_name="a_enhanced_auto.png", output_format="png",
            output_file_size=len(self.png),
        )
        self.result.enhanced_file.save("a.png", ContentFile(self.png), save=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("enhancement:result-download", kwargs={"pk": self.result.pk})

    def stored_digest(self):
        self.result.enhanced_file.open("rb")
        try:
            return hashlib.sha256(self.result.enhanced_file.read()).hexdigest()
        finally:
            self.result.enhanced_file.close()

    def body(self, response):
        return b"".join(response.streaming_content)

    def test_download_without_a_watermark_is_unchanged(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.body(response), self.png)

    def test_watermarked_download_is_a_marked_copy_and_the_original_is_untouched(self):
        before = self.stored_digest()

        response = self.client.get(self.url, {"watermark": "username", "watermark_position": "bottom_left"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("a_enhanced_auto_watermarked.png", response["Content-Disposition"])
        stamped = Image.open(io.BytesIO(self.body(response)))
        self.assertEqual(stamped.size, (1600, 1200))
        self.assertIsNotNone(changed_box(flat_image((1600, 1200)), stamped.convert("RGB")))
        self.assertEqual(self.stored_digest(), before)

    def test_download_event_records_the_watermarked_copy(self):
        self.client.get(self.url, {"watermark": "facecloud"})

        event = DownloadEvent.objects.get()
        self.assertEqual(event.filename, "a_enhanced_auto_watermarked.png")
        self.assertNotEqual(event.file_size, len(self.png))

    def test_invalid_options_are_400_with_the_offending_field(self):
        cases = (
            ({"watermark": "banner"}, "watermark"),
            ({"watermark": "facecloud", "watermark_position": "nowhere"}, "watermark_position"),
            ({"watermark": "facecloud", "watermark_opacity": "5"}, "watermark_opacity"),
            ({"watermark": "custom"}, "watermark_text"),
            ({"watermark": "custom", "watermark_text": "水印"}, "watermark_text"),
        )
        for params, field in cases:
            with self.subTest(params=params):
                response = self.client.get(self.url, params)
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
        self.assertEqual(DownloadEvent.objects.count(), 0)

    def test_someone_elses_result_is_404(self):
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.url, {"watermark": "facecloud"}).status_code, 404)

    def test_preview_is_downscaled_inline_and_not_a_download(self):
        response = self.client.get(
            reverse("enhancement:result-watermark-preview", kwargs={"pk": self.result.pk}),
            {"watermark": "custom", "watermark_text": "Draft", "watermark_position": "tile"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))
        preview = Image.open(io.BytesIO(response.content))
        self.assertLessEqual(max(preview.size), 1024)
        self.assertEqual(DownloadEvent.objects.count(), 0)

    def test_preview_needs_a_mode_and_login(self):
        url = reverse("enhancement:result-watermark-preview", kwargs={"pk": self.result.pk})
        self.assertEqual(self.client.get(url).status_code, 400)
        self.assertEqual(self.client.get(url, {"watermark": "none"}).status_code, 400)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(url, {"watermark": "facecloud"}).status_code, 401)

    def test_edited_versions_can_be_watermarked_too(self):
        edit = ImageEditVersion.objects.create(
            owner=self.user, enhancement_result=self.result, mode="manual",
            status="completed", output_name="a_edit.png", output_format="png",
            output_file_size=len(self.png), is_current=True,
        )
        edit.edited_file.save("edit.png", ContentFile(self.png), save=True)

        response = self.client.get(
            reverse("enhancement:edit-download", kwargs={"pk": edit.pk}), {"watermark": "small"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("a_edit_watermarked.png", response["Content-Disposition"])
        self.assertNotEqual(self.body(response), self.png)
