from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from enhancement.models import ImageEnhancementResult
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .analytics import InvalidAnalyticsFilter, build_admin_analytics, resolve_window

User = get_user_model()


class ResolveWindowTests(TestCase):
    def test_defaults_to_the_last_30_days(self):
        window_from, window_to = resolve_window()
        self.assertAlmostEqual((window_to - window_from).days, 30, delta=1)

    def test_explicit_range_is_honoured(self):
        window_from, window_to = resolve_window("2026-01-01", "2026-01-31")
        self.assertEqual(window_from.date().isoformat(), "2026-01-01")
        self.assertEqual(window_to.date().isoformat(), "2026-01-31")

    def test_from_after_to_is_rejected(self):
        with self.assertRaises(InvalidAnalyticsFilter):
            resolve_window("2026-02-01", "2026-01-01")

    def test_a_range_longer_than_the_maximum_is_rejected(self):
        with self.assertRaises(InvalidAnalyticsFilter):
            resolve_window("2020-01-01", "2026-01-01")

    def test_malformed_dates_are_rejected(self):
        with self.assertRaises(InvalidAnalyticsFilter):
            resolve_window("not-a-date", None)


class BuildAdminAnalyticsFixtureMixin:
    def make_user(self, name, **extra):
        return User.objects.create_user(
            username=name, email=f"{name}@example.com", password="ForestRiver!4821",
            is_email_verified=True, **extra,
        )

    def make_pipeline(
        self, owner, *, when=None, enhancement_status="completed", decision="accepted",
        rejection_reasons=None, algorithm="opencv_adaptive_restoration_router_v2",
        original_size=1000, output_size=1500, duration_ms=200,
    ):
        when = when or timezone.now()
        batch = UploadBatch.objects.create(owner=owner, upload_type="single")
        UploadBatch.objects.filter(pk=batch.pk).update(created_at=when)
        image = ImageUpload.objects.create(
            owner=owner, batch=batch, original_name="a.png", status="uploaded",
            file_size=original_size,
        )
        ImageUpload.objects.filter(pk=image.pk).update(created_at=when)
        pre = ImagePreprocessingResult.objects.create(
            owner=owner, image=image, status="completed", output_format="png",
        )
        quality = ImageQualityDecision.objects.create(
            owner=owner, image=image, preprocessing_result=pre,
            status=ImageQualityDecision.Status.COMPLETED, decision=decision,
            rejection_reasons=rejection_reasons or [],
            processing_duration_ms=duration_ms,
        )
        ImageQualityDecision.objects.filter(pk=quality.pk).update(created_at=when)
        result = ImageEnhancementResult.objects.create(
            owner=owner, image=image, preprocessing_result=pre, status=enhancement_status,
            output_file_size=output_size, processing_duration_ms=duration_ms,
            applied_parameters={"algorithm": algorithm} if algorithm else {},
        )
        ImageEnhancementResult.objects.filter(pk=result.pk).update(created_at=when)
        return image


class BuildAdminAnalyticsTests(BuildAdminAnalyticsFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("analytics-owner")
        self.other = self.make_user("analytics-other")

    def test_only_rows_inside_the_window_are_counted(self):
        now = timezone.now()
        self.make_pipeline(self.owner, when=now - timedelta(days=5))
        self.make_pipeline(self.owner, when=now - timedelta(days=90))

        result = build_admin_analytics(date_from=(now - timedelta(days=10)).date().isoformat())

        self.assertEqual(result["totals"]["images"]["total"], 1)
        self.assertEqual(result["totals"]["enhancements"]["total"], 1)

    def test_success_rate_uses_only_decided_images(self):
        self.make_pipeline(self.owner, decision="accepted")
        self.make_pipeline(self.owner, decision="accepted")
        self.make_pipeline(self.owner, decision="rejected", rejection_reasons=["severe_blur"])

        result = build_admin_analytics()

        self.assertAlmostEqual(result["success_rate_percent"], 200 / 3, places=1)

    def test_success_rate_is_none_with_no_decided_images(self):
        result = build_admin_analytics()
        self.assertIsNone(result["success_rate_percent"])

    def test_rejection_reasons_are_counted_across_decisions_and_ranked(self):
        self.make_pipeline(self.owner, decision="rejected", rejection_reasons=["severe_blur", "low_contrast"])
        self.make_pipeline(self.owner, decision="rejected", rejection_reasons=["severe_blur"])
        self.make_pipeline(self.owner, decision="needs_review", rejection_reasons=["borderline_blur"])
        self.make_pipeline(self.owner, decision="accepted", rejection_reasons=["should_not_count"])

        reasons = {row["value"]: row["count"] for row in build_admin_analytics()["top_rejection_reasons"]}

        self.assertEqual(reasons["severe_blur"], 2)
        self.assertEqual(reasons["low_contrast"], 1)
        self.assertEqual(reasons["borderline_blur"], 1)
        self.assertNotIn("should_not_count", reasons)

    def test_compound_algorithm_strings_are_split_and_counted_separately(self):
        self.make_pipeline(self.owner, algorithm="zero_dce_hybrid_v1+private_gfpgan_v1_4")
        self.make_pipeline(self.owner, algorithm="zero_dce_hybrid_v1")

        usage = {row["value"]: row["count"] for row in build_admin_analytics()["model_usage"]}

        self.assertEqual(usage["zero_dce_hybrid_v1"], 2)
        self.assertEqual(usage["private_gfpgan_v1_4"], 1)

    def test_storage_and_average_processing_time_are_exact(self):
        self.make_pipeline(self.owner, original_size=1000, output_size=2000, duration_ms=100)
        self.make_pipeline(self.owner, original_size=3000, output_size=4000, duration_ms=300)

        result = build_admin_analytics()

        self.assertEqual(result["storage_bytes"], {"originals": 4000, "enhanced_outputs": 6000, "total": 10000})
        self.assertEqual(result["average_processing_time_ms"]["enhancement"], 200.0)

    def test_status_filter_narrows_enhancement_totals_only(self):
        self.make_pipeline(self.owner, enhancement_status="completed")
        self.make_pipeline(self.owner, enhancement_status="failed")

        result = build_admin_analytics(status="failed")

        self.assertEqual(result["totals"]["enhancements"]["total"], 1)
        self.assertEqual(result["totals"]["images"]["total"], 2)  # images are not status-filtered

    def test_an_unknown_status_filter_is_rejected(self):
        with self.assertRaises(InvalidAnalyticsFilter):
            build_admin_analytics(status="not-a-real-status")

    def test_model_filter_matches_by_substring(self):
        self.make_pipeline(self.owner, algorithm="zero_dce_hybrid_v1")
        self.make_pipeline(self.owner, algorithm="opencv_adaptive_restoration_router_v2")

        result = build_admin_analytics(model="zero_dce")

        self.assertEqual(result["totals"]["enhancements"]["total"], 1)

    def test_owner_filter_restricts_every_total_to_one_user(self):
        self.make_pipeline(self.owner)
        self.make_pipeline(self.other)

        result = build_admin_analytics(owner=str(self.owner.pk))

        self.assertEqual(result["totals"]["images"]["total"], 1)
        self.assertEqual(result["filters_applied"]["owner"], self.owner.pk)

    def test_a_non_numeric_owner_is_rejected(self):
        with self.assertRaises(InvalidAnalyticsFilter):
            build_admin_analytics(owner="not-an-id")

    def test_daily_charts_are_zero_filled_for_days_with_no_activity(self):
        now = timezone.now()
        self.make_pipeline(self.owner, when=now)

        result = build_admin_analytics(
            date_from=(now - timedelta(days=3)).date().isoformat(),
            date_to=now.date().isoformat(),
        )

        series = result["charts"]["uploads_per_day"]
        self.assertEqual(len(series), 4)
        self.assertEqual(sum(row["count"] for row in series), 1)
        self.assertEqual([row["count"] for row in series[:3]], [0, 0, 0])

    def test_user_totals_are_never_windowed_or_owner_filtered(self):
        """User counts describe the platform, not the activity window."""
        self.make_user("bystander-with-no-activity")

        result = build_admin_analytics(owner=str(self.owner.pk))

        self.assertGreaterEqual(result["totals"]["users"]["total"], 3)


class AdminAnalyticsEndpointTests(BuildAdminAnalyticsFixtureMixin, TestCase):
    def setUp(self):
        self.admin = self.make_user("analytics-admin", role="admin")
        self.plain = self.make_user("analytics-plain")
        self.client = APIClient()
        self.url = reverse("operations:admin-analytics")

    def test_requires_authentication_and_admin_role(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_authenticate(self.plain)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_admin_gets_the_real_payload(self):
        self.make_pipeline(self.admin)
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["totals"]["images"]["total"], 1)

    def test_query_params_reach_the_builder(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.url, {"status": "not-a-real-status"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.data)
