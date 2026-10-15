import csv
import io
from datetime import timedelta
from unittest.mock import patch

import cv2
import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from operations.jobs import reap_stuck_jobs
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .comparison import (
    ORIGINAL,
    PROFILES,
    build_report_csv,
    process_model_comparison,
    recommend,
    validate_profiles,
)
from .models import ModelComparison, ModelComparisonOutput
from .services import compare_image_quality
from .tasks import cleanup_old_comparisons

User = get_user_model()


def metrics(clipping=0.0):
    return {"shadow_clipping_percent": clipping, "highlight_clipping_percent": 0.0}


def candidate(profile, score, guard=None, faces=1, clipping=0.0):
    return {
        "profile": profile, "overall_score": score, "metrics": metrics(clipping),
        "faces_detected": faces, "identity_guard": {"status": guard} if guard else {},
    }


def fake_enhance(image, level, style="natural", **kwargs):
    """A fast stand-in for the pipeline that still returns the real contract."""
    brighter = cv2.convertScaleAbs(image, alpha=1.0, beta=40)
    return brighter, "high", {
        "algorithm": f"fake_{style}",
        "quality_comparison": compare_image_quality(image, brighter),
        "identity_guard": {"status": "skipped"},
        "warnings": [],
    }


SOURCE = {"overall_score": 50.0, "metrics": metrics(), "faces_detected": 1}


class RecommendationRuleTests(SimpleTestCase):
    def test_highest_adjusted_score_wins_when_the_gap_is_meaningful(self):
        profile, reason, details = recommend(
            SOURCE, [candidate("natural", 60), candidate("ai_max", 75)],
        )
        self.assertEqual(profile, "ai_max")
        self.assertIn("ai_max scored 75.0 against 50.0", reason)
        self.assertEqual(details["ai_max"]["penalty"], 0)

    def test_a_near_tie_prefers_the_more_conservative_profile(self):
        profile, reason, _ = recommend(
            SOURCE, [candidate("ai_max", 80.4), candidate("natural", 79.8), candidate("dynamic", 80.0)],
        )
        self.assertEqual(profile, "natural")
        self.assertIn("more conservative", reason)

    def test_no_meaningful_improvement_recommends_keeping_the_original(self):
        profile, reason, _ = recommend(SOURCE, [candidate("natural", 50.4), candidate("dynamic", 49.0)])
        self.assertEqual(profile, ORIGINAL)
        self.assertIn("keep the original", reason)

    def test_an_unresolved_identity_guard_can_demote_the_top_scorer(self):
        profile, _, details = recommend(
            SOURCE, [candidate("natural", 70), candidate("ai_max", 85, guard="unresolved")],
        )
        self.assertEqual(profile, "natural")
        self.assertEqual(details["ai_max"]["penalty"], 20.0)
        self.assertIn("identity guard", details["ai_max"]["notes"][0])

    def test_a_reduced_strength_guard_costs_a_little(self):
        _, _, details = recommend(SOURCE, [candidate("natural", 70, guard="strength_reduced")])
        self.assertEqual(details["natural"]["penalty"], 2.0)

    def test_losing_every_detectable_face_is_penalised(self):
        _, _, details = recommend(SOURCE, [candidate("natural", 70, faces=0)])
        self.assertEqual(details["natural"]["penalty"], 15.0)
        self.assertIn("no longer detected", details["natural"]["notes"][0])

    def test_faces_are_not_required_when_the_source_had_none(self):
        source = {**SOURCE, "faces_detected": 0}
        _, _, details = recommend(source, [candidate("natural", 70, faces=0)])
        self.assertEqual(details["natural"]["penalty"], 0)

    def test_a_jump_in_clipping_is_penalised_beyond_a_tolerance(self):
        _, _, tolerated = recommend(SOURCE, [candidate("natural", 70, clipping=4.0)])
        _, _, penalised = recommend(SOURCE, [candidate("natural", 70, clipping=12.0)])
        self.assertEqual(tolerated["natural"]["penalty"], 0)
        self.assertEqual(penalised["natural"]["penalty"], 7.0)

    def test_nothing_to_recommend_without_candidates(self):
        profile, reason, details = recommend(SOURCE, [])
        self.assertEqual((profile, details), ("", {}))
        self.assertIn("nothing can be recommended", reason)

    def test_profile_validation(self):
        self.assertEqual(validate_profiles(None), list(PROFILES))
        self.assertEqual(validate_profiles(["natural", "natural", "dynamic"]), ["natural", "dynamic"])
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            validate_profiles(["natural", "magic"])


class ComparisonFixtureMixin:
    def make_user(self, name):
        return User.objects.create_user(
            username=name, email=f"{name}@example.com", password="ForestRiver!4821",
            is_email_verified=True,
        )

    def make_image(self, owner, preprocessed=True):
        rng = np.random.default_rng(7)
        pixels = rng.integers(0, 22, size=(150, 200, 3), dtype=np.uint8)
        pixels[40:110, 60:140] = 70  # a dim lit patch
        success, encoded = cv2.imencode(".png", pixels)
        self.assertTrue(success)
        payload = encoded.tobytes()
        batch = UploadBatch.objects.create(owner=owner, upload_type="single")
        image = ImageUpload.objects.create(
            owner=owner, batch=batch, original_name="dark.png", status="uploaded",
            file_extension=".png", mime_type="image/png", file_size=len(payload),
            width=200, height=150,
        )
        image.original_file.save("dark.png", ContentFile(payload))
        pre = ImagePreprocessingResult.objects.create(
            owner=owner, image=image, status="completed" if preprocessed else "pending",
            output_format="png", output_name="dark.png",
        )
        if preprocessed:
            pre.processed_file.save("dark.png", ContentFile(payload))
        return image


class ComparisonProcessingTests(ComparisonFixtureMixin, TestCase):
    def setUp(self):
        self.user = self.make_user("compare-owner")
        self.image = self.make_image(self.user)

    def comparison(self, profiles=None):
        return ModelComparison.objects.create(
            owner=self.user, image=self.image, status="queued",
            profiles=profiles or ["natural", "dynamic"],
        )

    @tag("slow")
    def test_every_requested_profile_and_the_original_get_metrics_and_previews(self):
        comparison = process_model_comparison(self.comparison())

        self.assertEqual(comparison.status, "completed")
        outputs = {output.profile: output for output in comparison.outputs.all()}
        self.assertEqual(set(outputs), {ORIGINAL, "natural", "dynamic"})
        for name, output in outputs.items():
            with self.subTest(profile=name):
                self.assertEqual(output.outcome, "completed")
                self.assertTrue(output.preview_file)
                self.assertGreater(output.overall_score, 0)
                self.assertIn("mean_luminance", output.metrics)
        self.assertEqual(comparison.reference_dimensions, [200, 150])
        self.assertGreater(comparison.total_duration_ms, 0)
        self.assertTrue(comparison.recommended_profile)
        self.assertTrue(comparison.recommendation_reason)
        # Adjusted scores are stored next to the raw ones for transparency.
        self.assertIn("adjusted_score", outputs["natural"].scores)

    def test_the_retired_baseline_profile_is_refused_with_its_reason_not_run_as_something_else(self):
        from rest_framework.exceptions import ValidationError

        from .comparison import RETIRED_PROFILES, validate_profiles

        with self.assertRaises(ValidationError) as caught:
            validate_profiles(["opencv_baseline", "natural"])
        message = str(caught.exception.detail["profiles"][0])
        self.assertIn("opencv_baseline", message)
        self.assertIn("retired", message)
        self.assertIn("Zero-DCE", message)
        self.assertEqual(set(RETIRED_PROFILES), {"opencv_baseline"})
        self.assertEqual(validate_profiles(None), ["natural", "dynamic", "ai_max"])

    def test_a_failing_profile_is_recorded_without_losing_the_others(self):
        def flaky(image, level, style="natural", **kwargs):
            if style == "dynamic":
                raise ValueError("boom")
            return fake_enhance(image, level, style=style, **kwargs)

        with patch("enhancement.services.enhance_bgr", side_effect=flaky):
            comparison = process_model_comparison(self.comparison(["natural", "dynamic"]))

        self.assertEqual(comparison.status, "completed")
        failed = comparison.outputs.get(profile="dynamic")
        self.assertEqual(failed.outcome, "failed")
        self.assertFalse(failed.preview_file)
        self.assertNotIn("boom", failed.failure_reason)
        self.assertEqual(comparison.outputs.get(profile="natural").outcome, "completed")
        self.assertEqual(comparison.recommended_profile in {"natural", ORIGINAL}, True)

    def test_when_every_profile_fails_the_comparison_fails(self):
        with patch("enhancement.services.enhance_bgr", side_effect=ValueError("boom")):
            comparison = process_model_comparison(self.comparison(["natural", "dynamic"]))

        self.assertEqual(comparison.status, "failed")
        self.assertIn("No profile", comparison.failure_reason)

    def test_an_unreadable_source_fails_cleanly_and_leaves_no_partial_outputs(self):
        self.image.original_file.delete(save=True)
        pre = self.image.preprocessing_result
        pre.processed_file.delete(save=True)

        comparison = process_model_comparison(self.comparison())

        self.assertEqual(comparison.status, "failed")
        self.assertEqual(comparison.outputs.count(), 0)

    def test_a_finished_comparison_is_not_reprocessed(self):
        with patch("enhancement.services.enhance_bgr", side_effect=fake_enhance):
            comparison = process_model_comparison(self.comparison(["natural"]))
        stamp = comparison.finished_at

        again = process_model_comparison(comparison)

        self.assertEqual(again.finished_at, stamp)
        self.assertEqual(again.outputs.count(), 2)

    @override_settings(COMPARISON_MAX_EDGE=100)
    def test_profiles_run_on_a_downscaled_copy(self):
        with patch("enhancement.services.enhance_bgr", side_effect=fake_enhance):
            comparison = process_model_comparison(self.comparison(["natural"]))

        self.assertEqual(comparison.reference_dimensions, [100, 75])

    def test_the_csv_report_has_one_row_per_output_and_marks_the_recommendation(self):
        with patch("enhancement.services.enhance_bgr", side_effect=fake_enhance):
            comparison = process_model_comparison(self.comparison())

        rows = list(csv.DictReader(io.StringIO(build_report_csv(comparison))))

        self.assertEqual([row["profile"] for row in rows], [ORIGINAL, "natural", "dynamic"])
        self.assertEqual(rows[0]["label"], "Original (untouched)")
        marked = [row["profile"] for row in rows if row["recommended"] == "yes"]
        self.assertEqual(marked, [comparison.recommended_profile])
        self.assertTrue(all(row["overall_score"] for row in rows))


class ComparisonApiTests(ComparisonFixtureMixin, TestCase):
    def setUp(self):
        self.user = self.make_user("compare-api")
        self.other = self.make_user("compare-other")
        self.image = self.make_image(self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("enhancement:image-compare", kwargs={"image_id": self.image.pk})

    def start(self, **body):
        with patch("enhancement.views.run_model_comparison.apply_async") as queued:
            response = self.client.post(self.url, body, format="json")
        return response, queued

    def test_starting_a_comparison_queues_it_and_returns_202(self):
        response, queued = self.start(profiles=["natural", "dynamic"])

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], "queued")
        self.assertEqual(response.data["profiles"], ["natural", "dynamic"])
        queued.assert_called_once()
        comparison = ModelComparison.objects.get()
        self.assertEqual(queued.call_args.kwargs["task_id"], comparison.task_id)

    def test_all_profiles_are_used_by_default(self):
        response, _ = self.start()
        self.assertEqual(response.data["profiles"], list(PROFILES))

    def test_bad_requests_are_400(self):
        response, _ = self.start(profiles=["magic"])
        self.assertEqual(response.status_code, 400)
        response, _ = self.start(level="extreme")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ModelComparison.objects.count(), 0)

    def test_a_retired_profile_is_refused_with_the_reason_and_nothing_is_queued(self):
        response, queued = self.start(profiles=["opencv_baseline", "natural"])

        self.assertEqual(response.status_code, 400)
        self.assertIn("retired", str(response.data["profiles"]))
        self.assertIn("Zero-DCE", str(response.data["profiles"]))
        queued.assert_not_called()
        self.assertEqual(ModelComparison.objects.count(), 0)

    def test_an_earlier_comparison_that_used_the_retired_profile_still_reads(self):
        comparison = ModelComparison.objects.create(
            owner=self.user, image=self.image, status="completed", profiles=["opencv_baseline", "natural"],
        )
        ModelComparisonOutput.objects.create(
            comparison=comparison, profile="opencv_baseline", outcome="completed",
            algorithm="opencv_adaptive_restoration_router_v2", overall_score=50.0,
        )

        response = self.client.get(reverse("enhancement:comparison-detail", kwargs={"pk": comparison.pk}))

        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.data["outputs"] if item["profile"] == "opencv_baseline")
        self.assertIn("retired", row["label"])
        self.assertIn("Zero-DCE", row["description"])

    def test_the_image_must_be_preprocessed_and_owned(self):
        unprocessed = self.make_image(self.user, preprocessed=False)
        response = self.client.post(
            reverse("enhancement:image-compare", kwargs={"image_id": unprocessed.pk}), {}, format="json",
        )
        self.assertEqual(response.status_code, 400)

        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 404)

    @override_settings(MAX_ACTIVE_COMPARISONS_PER_USER=1)
    def test_concurrent_comparisons_are_limited_per_user(self):
        self.assertEqual(self.start()[0].status_code, 202)
        blocked, queued = self.start()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("limit 1", blocked.data["detail"])
        queued.assert_not_called()

    def test_an_unavailable_queue_is_reported_as_a_failed_comparison(self):
        with patch("enhancement.views.run_model_comparison.apply_async", side_effect=OSError("down")):
            response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], "failed")

    def finished(self):
        comparison = ModelComparison.objects.create(
            owner=self.user, image=self.image, status="queued", profiles=["natural"],
        )
        with patch("enhancement.services.enhance_bgr", side_effect=fake_enhance):
            return process_model_comparison(comparison)

    def test_detail_lists_outputs_with_preview_urls_and_a_report_link(self):
        comparison = self.finished()

        response = self.client.get(reverse("enhancement:comparison-detail", kwargs={"pk": comparison.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["profile"] for row in response.data["outputs"]}, {ORIGINAL, "natural"})
        natural = next(row for row in response.data["outputs"] if row["profile"] == "natural")
        self.assertIn(f"/comparisons/{comparison.pk}/outputs/natural/preview/", natural["preview_url"])
        self.assertEqual(natural["label"], "Natural (default pipeline)")
        self.assertTrue(response.data["report_url"].endswith(f"/comparisons/{comparison.pk}/report/"))
        self.assertTrue(response.data["recommendation_reason"])

    def test_the_list_is_a_lightweight_summary_of_your_own_comparisons_only(self):
        self.finished()
        listing = self.client.get(reverse("enhancement:comparison-list"))
        self.assertEqual(len(listing.data), 1)
        self.assertNotIn("outputs", listing.data[0])

        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(reverse("enhancement:comparison-list")).data, [])

    def test_previews_are_private_inline_jpegs(self):
        comparison = self.finished()
        url = reverse("enhancement:comparison-preview", kwargs={"pk": comparison.pk, "profile": "natural"})

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        decoded = cv2.imdecode(np.frombuffer(b"".join(response.streaming_content), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape[:2], (150, 200))  # the fake keeps the source size
        self.assertEqual(self.client.get(url.replace("natural", "nonexistent")).status_code, 404)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_report_downloads_as_csv_for_the_owner_only(self):
        comparison = self.finished()
        url = reverse("enhancement:comparison-report", kwargs={"pk": comparison.pk})

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("recommended", response.content.decode().splitlines()[0])
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_deleting_a_comparison_removes_its_preview_files(self):
        comparison = self.finished()
        names = [output.preview_file.name for output in comparison.outputs.all()]
        storage = comparison.outputs.first().preview_file.storage
        self.assertTrue(all(storage.exists(name) for name in names))

        response = self.client.delete(reverse("enhancement:comparison-detail", kwargs={"pk": comparison.pk}))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(any(storage.exists(name) for name in names))
        self.assertEqual(ModelComparisonOutput.objects.count(), 0)

    def test_login_is_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 401)
        self.assertEqual(self.client.get(reverse("enhancement:comparison-list")).status_code, 401)


class ComparisonMediaTests(ComparisonFixtureMixin, TestCase):
    def test_comparison_previews_are_known_media_not_orphans(self):
        from privacy.cleanup import orphan_media_files, referenced_media_names

        user = self.make_user("compare-media")
        image = self.make_image(user)
        comparison = ModelComparison.objects.create(
            owner=user, image=image, status="queued", profiles=["natural"],
        )
        with patch("enhancement.services.enhance_bgr", side_effect=fake_enhance):
            comparison = process_model_comparison(comparison)
        names = {output.preview_file.name for output in comparison.outputs.all()}
        storage = comparison.outputs.first().preview_file.storage
        stray = f"users/{user.pk}/comparisons/stray.jpg"
        storage.save(stray, ContentFile(b"not referenced"))
        self.addCleanup(storage.delete, stray)

        later = timezone.now() + timedelta(days=30)
        orphans = {entry[0] if isinstance(entry, tuple) else entry for entry in orphan_media_files(now=later)}
        normalised = {str(name).replace("\\", "/") for name in orphans}

        self.assertTrue(names <= referenced_media_names())
        self.assertFalse(names & normalised)
        self.assertIn(stray, normalised)


class ComparisonHousekeepingTests(ComparisonFixtureMixin, TestCase):
    def setUp(self):
        self.user = self.make_user("compare-housekeeping")
        self.image = self.make_image(self.user)

    def make(self, status, age):
        comparison = ModelComparison.objects.create(
            owner=self.user, image=self.image, status=status, profiles=["natural"],
        )
        ModelComparison.objects.filter(pk=comparison.pk).update(
            created_at=timezone.now() - age, updated_at=timezone.now() - age,
        )
        return comparison

    @override_settings(COMPARISON_RETENTION_DAYS=7)
    def test_old_finished_comparisons_are_deleted_but_recent_and_active_ones_stay(self):
        old = self.make("completed", timedelta(days=8))
        old_failed = self.make("failed", timedelta(days=9))
        recent = self.make("completed", timedelta(days=2))
        running = self.make("processing", timedelta(days=30))

        summary = cleanup_old_comparisons()

        self.assertEqual(summary, {"deleted_comparisons": 2})
        remaining = set(ModelComparison.objects.values_list("pk", flat=True))
        self.assertEqual(remaining, {recent.pk, running.pk})
        self.assertNotIn(old.pk, remaining)
        self.assertNotIn(old_failed.pk, remaining)

    @override_settings(JOB_PROCESSING_STALE_SECONDS=3600)
    def test_a_stuck_comparison_is_recovered_by_the_reaper(self):
        stuck = self.make("processing", timedelta(hours=3))

        summary = reap_stuck_jobs()

        stuck.refresh_from_db()
        self.assertEqual(stuck.status, "failed")
        self.assertEqual(summary["by_kind"].get("model_comparison"), 1)
