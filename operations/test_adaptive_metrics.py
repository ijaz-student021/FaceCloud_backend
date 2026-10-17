from datetime import timedelta

from django.core.management import call_command
from django.test import SimpleTestCase as TestCase
from django.utils import timezone
from io import StringIO
import json

from enhancement.models import ImageEnhancementResult
from enhancement.test_pipeline_regressions import ResultFixture
from operations.adaptive_metrics import adaptive_metrics, percentile
from uploads.models import ImagePreprocessingResult, ImageUpload


class PercentileTests(TestCase):
    def test_percentiles_interpolate_and_handle_empty(self):
        self.assertIsNone(percentile([], 95))
        self.assertEqual(percentile([1, 2, 3, 4, 100], 50), 3)
        self.assertAlmostEqual(percentile(list(range(1, 101)), 95), 95.05, places=1)


class AdaptiveMetricsTests(ResultFixture):
    def setUp(self):
        super().setUp()
        self.upload = ImageUpload.objects.create(owner=self.user, batch=self.batch, original_name="a.png", file_extension=".png",
                                                 mime_type="image/png", file_size=1, width=8, height=8, status="uploaded")
        self.preprocessing = ImagePreprocessingResult.objects.create(owner=self.user, image=self.upload, status="completed",
                                                                     output_format="png", output_name="a.png", output_width=8,
                                                                     output_height=8, processed_file_size=1)

    def make(self, outcome, status="completed", ms=2000, stages=None, state="verified"):
        now = timezone.now()
        return ImageEnhancementResult.objects.create(
            owner=self.user, image=self.upload, preprocessing_result=self.preprocessing, requested_level="auto", requested_style="natural", status=status, processing_duration_ms=ms,
            started_at=now, applied_parameters={"adaptive": {"outcome": outcome, "stages": stages or []},
                                                "identity_verification": {"state": state}, "warnings": []})

    def test_counts_rates_and_reasons(self):
        # one active job per image is enforced, so finished rows only
        self.make("enhanced", stages=[{"operation": "low_light_enhancement", "status": "accepted", "seconds": 3.0, "models_run": ["retinexformer"]}])
        self.make("original_preserved", ms=9000, stages=[{"operation": "motion_deblurring", "status": "rejected", "seconds": 5.0,
                                                          "rejection_reasons": ["halo_or_ringing", "identity_changed"], "models_run": ["nafnet_gopro"]}])
        self.make("original_preserved", status="failed", ms=0)
        metrics = adaptive_metrics(7)
        self.assertEqual(metrics["outcomes"], {"enhanced": 1, "original_preserved": 2})
        self.assertEqual(metrics["original_preserved_rate"], 0.667)
        self.assertEqual(metrics["failed_jobs_in_window"], 1)
        self.assertEqual(metrics["rejection_reasons"]["motion_deblurring:halo_or_ringing"], 1)
        self.assertEqual(metrics["models_run"], {"retinexformer": 1, "nafnet_gopro": 1})
        self.assertEqual(metrics["job_seconds"]["max"], 9.0)

    def test_the_command_prints_json_and_old_rows_are_outside_the_window(self):
        row = self.make("enhanced")
        ImageEnhancementResult.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(days=30))
        out = StringIO()
        call_command("adaptive_report", "--days", "7", stdout=out)
        self.assertEqual(json.loads(out.getvalue())["adaptive_results"], 0)
