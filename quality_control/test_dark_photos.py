"""A dark photograph is lifted, not refused; only one with no usable signal is refused (BE-005)."""
import importlib.util
import io
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings, tag
from PIL import Image
from rest_framework.test import APIClient

from enhancement.models import ImageEnhancementResult
from uploads.models import ImageUpload, UploadBatch
from uploads.services import preprocess_image
from validation.models import FaceValidationResult

from .models import ImageQualityDecision
from .services import calculate_scores, enqueue_quality_analysis, make_decision

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix="facecloud-dark-")


def synthetic_photo(mean=71.0, spread=48.0, size=(480, 360), seed=3):
    """A textured, smoothly lit RGB scene with the given mean brightness and spread; not a face, but enough signal for every measure the checks take."""
    rng = np.random.default_rng(seed)
    height, width = size[1], size[0]
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    field = np.zeros((height, width), np.float32)
    for _ in range(40):
        cx, cy, radius, amplitude = rng.uniform(0, width), rng.uniform(0, height), rng.uniform(20, 90), rng.uniform(-1, 1)
        field += amplitude * np.exp(-(((x - cx) ** 2 + (y - cy) ** 2) / (2 * radius**2)))
    field += rng.normal(0, 0.15, field.shape)
    field = (field - field.mean()) / field.std()
    gray = np.clip(mean + spread * field, 0, 255)
    rgb = np.stack([gray * 1.05, gray, gray * 0.92], axis=-1)
    return np.clip(rgb, 0, 255).astype(np.uint8)


def png(array):
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, "PNG")
    return buffer.getvalue()


def luminance(array):
    return float((0.299 * array[..., 0] + 0.587 * array[..., 1] + 0.114 * array[..., 2]).mean())


def metrics(mean, spread, sharpness=120.0, size=(640, 480)):
    return {
        "width": size[0], "height": size[1], "mean_luminance": mean, "contrast_stddev": spread, "laplacian_sharpness": sharpness, "estimated_noise": 2.0,
        "shadow_clipping_percent": 0.0, "highlight_clipping_percent": 0.0,
    }


def decide(mean, spread, face=True, **extra):
    measured = metrics(mean, spread, **extra)
    validation = SimpleNamespace(decision="accepted", face_count=1 if face else 0, rejection_reason="") if face else SimpleNamespace(decision="accepted", face_count=0, rejection_reason="")
    return make_decision(measured, calculate_scores(measured), validation)


class RuleTests(SimpleTestCase):
    def test_a_dark_photograph_is_lifted_not_refused(self):
        for mean, spread in ((28.0, 20.0), (22.0, 14.0), (18.0, 12.0), (10.0, 6.0), (6.5, 4.0)):
            with self.subTest(mean=mean):
                verdict, reasons, _message = decide(mean, spread)
                self.assertNotEqual(verdict, "rejected", reasons)
                self.assertIn("underexposed", reasons)
                self.assertNotIn("severe_underexposure", reasons)
                self.assertNotIn("contrast_too_low", reasons)

    def test_a_photograph_with_no_usable_signal_is_still_refused(self):
        verdict, reasons, _ = decide(0.0, 0.0)                                                    # fully black
        self.assertEqual(verdict, "rejected")
        self.assertIn("severe_underexposure", reasons)
        verdict, reasons, _ = decide(5.9, 5.0)                                                    # practically black
        self.assertEqual((verdict, "severe_underexposure" in reasons), ("rejected", True))

    def test_almost_no_contrast_still_refuses_a_dark_photograph(self):
        verdict, reasons, _ = decide(20.0, 1.0)
        self.assertEqual(verdict, "rejected")
        self.assertIn("contrast_too_low", reasons)
        verdict, reasons, _ = decide(20.0, 6.0)                                                   # the same darkness with some spread is fine
        self.assertNotEqual(verdict, "rejected", reasons)

    def test_the_contrast_floor_does_not_change_for_a_photograph_of_ordinary_brightness(self):
        verdict, reasons, _ = decide(135.0, 5.0)
        self.assertEqual((verdict, "contrast_too_low" in reasons), ("rejected", True))
        verdict, reasons, _ = decide(135.0, 11.9)
        self.assertEqual((verdict, "contrast_too_low" in reasons), ("rejected", True))
        verdict, reasons, _ = decide(135.0, 12.5)
        self.assertNotIn("contrast_too_low", reasons)

    def test_a_clean_photograph_is_still_accepted_without_reasons(self):
        self.assertEqual(decide(135.0, 55.0)[:2], ("accepted", []))

    def test_the_other_rejection_reasons_are_unchanged(self):
        self.assertIn("severe_overexposure", decide(240.0, 40.0)[1])
        self.assertIn("resolution_too_low", decide(135.0, 55.0, size=(200, 150))[1])
        self.assertIn("severe_blur", decide(135.0, 55.0, face=False, sharpness=5.0)[1])

    @override_settings(QUALITY_REJECT_MIN_LUMINANCE=25.0)
    def test_the_line_is_still_a_setting(self):
        self.assertEqual(decide(18.0, 14.0)[0], "rejected")


@override_settings(MEDIA_ROOT=MEDIA, IMAGE_ENCRYPTION_ENABLED=False)
class EndToEndTests(TestCase):
    """Through the real quality check and the real enhancement request, with the face check stood in for (a synthetic scene has no face)."""

    def setUp(self):
        self.user = User.objects.create_user(username="dark-owner", email="dark-owner@example.com", password="Password!123", is_email_verified=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        self.count = 0

    def stored(self, array):
        self.count += 1
        image = ImageUpload.objects.create(owner=self.user, batch=self.batch, original_name=f"dark-{self.count}.png", status="uploaded", file_extension=".png", mime_type="image/png")
        image.original_file.save(image.original_name, ContentFile(png(array)))
        with self.captureOnCommitCallbacks(execute=False):
            preprocessing, _ = preprocess_image(ImageUpload.objects.get(pk=image.pk))
        height, width = array.shape[:2]
        # Preparing the photograph already ran the real face check, which finds no face in a synthetic scene: stand a detected face in for it.
        FaceValidationResult.objects.update_or_create(
            image=image, defaults=dict(
                owner=self.user, preprocessing_result=preprocessing, decision="accepted", face_count=1, primary_confidence=0.95, rejection_reason="", message="",
                faces=[{"x": width // 4, "y": height // 6, "width": width // 2, "height": height * 2 // 3, "confidence": 0.95, "area_ratio": 0.33}],
            ),
        )
        with self.captureOnCommitCallbacks(execute=True):
            enqueue_quality_analysis(ImageUpload.objects.get(pk=image.pk))
        return ImageUpload.objects.get(pk=image.pk)

    def enhance(self, image):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/api/enhancements/images/{image.pk}/", {"level": "auto", "style": "natural"}, format="json")
        return response

    def enhanced_luminance(self, response):
        result = ImageEnhancementResult.objects.get(pk=response.json()["id"])
        self.assertEqual(result.status, "completed", result.failure_reason)
        return luminance(np.asarray(Image.open(result.enhanced_file).convert("RGB")))

    def test_the_demos_dark_photograph_is_accepted_for_enhancement(self):
        dark = (synthetic_photo(mean=71.0).astype(np.float32) * 0.40).astype(np.uint8)            # the demo's x0.40
        image = self.stored(dark)
        decision = ImageQualityDecision.objects.get(image=image)
        self.assertNotEqual(decision.decision, "rejected", decision.rejection_reasons)
        self.assertIn("underexposed", decision.rejection_reasons)
        response = self.enhance(image)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(ImageEnhancementResult.objects.get(pk=response.json()["id"]).status, "completed")

    def test_the_older_x025_darkening_is_no_longer_refused_either(self):
        dark = (synthetic_photo(mean=71.0).astype(np.float32) * 0.25).astype(np.uint8)
        image = self.stored(dark)
        self.assertNotEqual(ImageQualityDecision.objects.get(image=image).decision, "rejected")
        self.assertEqual(self.enhance(image).status_code, 202)

    def test_a_fully_black_photograph_is_still_refused_and_not_enhanced(self):
        image = self.stored(np.zeros((360, 480, 3), np.uint8))
        decision = ImageQualityDecision.objects.get(image=image)
        self.assertEqual(decision.decision, "rejected")
        self.assertIn("severe_underexposure", decision.rejection_reasons)
        self.assertEqual(self.enhance(image).status_code, 400)

    def test_a_clean_photograph_is_still_left_alone(self):
        clean = synthetic_photo(mean=120.0, spread=50.0)
        image = self.stored(clean)
        decision = ImageQualityDecision.objects.get(image=image)
        self.assertEqual((decision.decision, decision.rejection_reasons), ("accepted", []))
        response = self.enhance(image)
        self.assertEqual(response.status_code, 202)
        result = ImageEnhancementResult.objects.get(pk=response.json()["id"])
        self.assertEqual(result.status, "completed", result.failure_reason)
        self.assertAlmostEqual(self.enhanced_luminance(response), luminance(clean), delta=3.0)


SAMPLES = sorted(Path(os.environ["FACECLOUD_SAMPLE_PHOTOS"]).glob("*.jp*g")) if os.environ.get("FACECLOUD_SAMPLE_PHOTOS") else []


# The settings scripts/defense_demo.sh runs the demo with.
DEMO_SETTINGS = dict(
    ENHANCEMENT_RENDER_POLICY="adaptive_v1", ZERO_DCE_ENABLED=True, RETINEXFORMER_ENABLED=False, NAFNET_DENOISE_ENABLED=False, NAFNET_DEBLUR_ENABLED=False, HD_RESTORATION_ENABLED=False,
)


@tag("slow")
@override_settings(MEDIA_ROOT=MEDIA, IMAGE_ENCRYPTION_ENABLED=False, **DEMO_SETTINGS)
class RealPortraitTests(TestCase):
    """The demo's dark photograph, end to end, on a real portrait: needs a folder of portraits (FACECLOUD_SAMPLE_PHOTOS, never committed) and the Zero-DCE model, else skipped.

        FACECLOUD_SAMPLE_PHOTOS=/path/to/portraits python manage.py test quality_control.test_dark_photos.RealPortraitTests
    """

    def setUp(self):
        if not SAMPLES:
            self.skipTest("FACECLOUD_SAMPLE_PHOTOS is not set (a folder of real portraits).")
        if not Path(settings.ZERO_DCE_MODEL_PATH).is_file():
            self.skipTest("The Zero-DCE checkpoint is not installed.")
        self.user = User.objects.create_user(username="real-dark", email="real-dark@example.com", password="Password!123", is_email_verified=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def seed_demo(self):
        specification = importlib.util.spec_from_file_location("seed_demo", Path(settings.BASE_DIR) / "scripts" / "seed_demo.py")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module

    def test_the_demos_dark_portrait_is_accepted_and_more_than_twice_as_bright_after_automatic_enhancement(self):
        import cv2

        photo = cv2.imread(str(SAMPLES[0]), cv2.IMREAD_COLOR)
        scale = 1280 / max(photo.shape[:2])
        photo = cv2.resize(photo, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else photo
        dark, factor = self.seed_demo().darken(photo)
        brightness = lambda array: float(cv2.cvtColor(array, cv2.COLOR_BGR2GRAY).mean())   # noqa: E731
        self.assertLessEqual(factor, 0.40)
        with self.captureOnCommitCallbacks(execute=True):
            upload = self.client.post("/api/images/upload/single/", {"file": SimpleUploadedFile("dark.png", cv2.imencode(".png", dark)[1].tobytes(), "image/png")}, format="multipart")
        self.assertEqual(upload.status_code, 201, upload.content)
        image_id = upload.json()["images"][0]["id"]
        verdict = self.client.get(f"/api/quality/images/{image_id}/").json()
        self.assertNotEqual(verdict["decision"], "rejected", verdict["rejection_reasons"])
        with self.captureOnCommitCallbacks(execute=True):
            queued = self.client.post(f"/api/enhancements/images/{image_id}/", {"level": "auto", "style": "natural"}, format="json")
        self.assertEqual(queued.status_code, 202, queued.content)
        result = ImageEnhancementResult.objects.get(pk=queued.json()["id"])
        self.assertEqual(result.status, "completed", result.failure_reason)
        lifted = cv2.imdecode(np.frombuffer(result.enhanced_file.read(), np.uint8), cv2.IMREAD_COLOR)
        self.assertGreaterEqual(brightness(lifted), 2 * brightness(dark), f"{brightness(dark):.1f} -> {brightness(lifted):.1f}")


def load_seed_demo():
    specification = importlib.util.spec_from_file_location("seed_demo", Path(settings.BASE_DIR) / "scripts" / "seed_demo.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class DemoScriptTests(SimpleTestCase):
    def test_a_normal_portrait_is_darkened_to_forty_percent_and_a_bright_one_further(self):
        import cv2

        seed = load_seed_demo()
        normal = np.full((90, 120, 3), 71, np.uint8)
        dark, factor = seed.darken(normal)
        self.assertEqual(factor, 0.40)
        self.assertAlmostEqual(float(cv2.cvtColor(dark, cv2.COLOR_BGR2GRAY).mean()), 28.4, delta=1.0)
        bright = np.full((90, 120, 3), 134, np.uint8)
        darker, factor = seed.darken(bright)
        self.assertLess(factor, 0.40)
        self.assertAlmostEqual(float(cv2.cvtColor(darker, cv2.COLOR_BGR2GRAY).mean()), 30.0, delta=1.0)
        black, factor = seed.darken(np.zeros((10, 10, 3), np.uint8))
        self.assertEqual((factor, int(black.max())), (0.40, 0))                                   # nothing to divide by

    def test_the_darkened_demo_photograph_is_not_one_the_checks_refuse(self):
        seed = load_seed_demo()
        for base in (45.0, 71.0, 110.0, 134.0, 180.0):
            with self.subTest(base=base):
                import cv2

                image = np.clip(synthetic_photo(mean=base, spread=base * 0.6)[..., ::-1], 0, 255).astype(np.uint8)
                dark, _factor = seed.darken(image)
                mean = float(cv2.cvtColor(dark, cv2.COLOR_BGR2GRAY).mean())
                spread = float(cv2.cvtColor(dark, cv2.COLOR_BGR2GRAY).std())
                self.assertNotEqual(decide(mean, spread)[0], "rejected", (mean, spread))

    def test_a_photograph_the_checks_refuse_is_reported_and_the_run_carries_on(self):
        """The whole script, in a throwaway database: a black photograph is refused (HTTP 400 on the enhancement) and the script still ends normally."""
        import subprocess
        import sys

        folder = Path(tempfile.mkdtemp(prefix="facecloud-seed-"))
        black = folder / "black.png"
        black.write_bytes(png(np.zeros((360, 480, 3), np.uint8)))
        environment = {
            **os.environ, "DB_NAME": str(folder / "demo.sqlite3"), "MEDIA_ROOT": str(folder / "media"), "DJANGO_DEBUG": "True", "DB_ENGINE": "django.db.backends.sqlite3",
            "IMAGE_ENCRYPTION_ENABLED": "False", "LOG_FILE": str(folder / "log.txt"),
        }
        root = str(settings.BASE_DIR)
        subprocess.run([sys.executable, "manage.py", "migrate", "-v", "0"], cwd=root, env=environment, check=True, capture_output=True, timeout=300)
        done = subprocess.run([sys.executable, "scripts/seed_demo.py", "--photo", str(black)], cwd=root, env=environment, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        self.assertIn("verdict rejected", done.stdout)
        self.assertIn("severe_underexposure", done.stdout)
        self.assertIn("not enhanced: HTTP 400", done.stdout)
        self.assertIn("database :", done.stdout)                                                  # it got to the end
