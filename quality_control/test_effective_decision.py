"""Review only where someone can review: a stored ``needs_review`` is "accepted, with tips" in a personal workspace, and stays a review in an organization.

Every place that shows or counts the verdict is checked for both, so the Python rule (``moderator_can_decide``) and the database rule (``effective_q``) cannot drift.
"""
import tempfile
import zipfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.dashboard import build_user_dashboard
from downloads.archives import build_package_archive
from downloads.models import DownloadPackage
from downloads.services import ensure_package_ready
from notifications.events import notify_quality_result
from notifications.models import UserNotification
from organizations.models import OrganizationMembership
from organizations.services import create_organization, create_personal_organization
from uploads.display_status import image_display_status, with_status_relations
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from uploads.progress import batch_progress_payload
from validation.models import FaceValidationResult

from .effective import TIP_SENTENCES, effective_decision, effective_q, image_reason_codes, moderator_can_decide, review_required, tips_for
from .models import ImageQualityDecision

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix="facecloud-effective-")


@override_settings(MEDIA_ROOT=MEDIA)
class EffectiveDecisionBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="eff-owner", email="eff-owner@example.com", password="Password!123", is_email_verified=True)
        self.personal = create_personal_organization(self.owner)
        self.team = create_organization(name="Eff Co", owner=User.objects.create_user(username="eff-team", email="eff-team@example.com", password="Password!123"))
        OrganizationMembership.objects.create(organization=self.team, user=self.owner, role="member", status="active")
        self.count = 0

    def batch(self, organization="personal"):
        organization = {"personal": self.personal, "team": self.team, "none": None}[organization]
        return UploadBatch.objects.create(owner=self.owner, upload_type="multiple", organization=organization)

    def photo(self, batch, verdict, reasons=(), face="accepted", face_reason=""):
        self.count += 1
        image = ImageUpload.objects.create(owner=self.owner, batch=batch, original_name=f"eff-{self.count}.jpg", status="uploaded", file_extension=".jpg", mime_type="image/jpeg")
        image.original_file.save(f"eff-{self.count}.jpg", ContentFile(b"original-bytes"))
        preprocessing = ImagePreprocessingResult.objects.create(owner=self.owner, image=image, status="completed")
        FaceValidationResult.objects.create(owner=self.owner, image=image, preprocessing_result=preprocessing, decision=face, rejection_reason=face_reason, face_count=1)
        ImageQualityDecision.objects.create(
            owner=self.owner, image=image, preprocessing_result=preprocessing, status="completed", decision=verdict, rejection_reasons=list(reasons), overall_score=70,
        )
        return ImageUpload.objects.get(pk=image.pk)

    def api(self):
        token = RefreshToken.for_user(self.owner)
        token["role"] = self.owner.role
        token["token_version"] = self.owner.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        return client


class TheRuleTests(EffectiveDecisionBase):
    def test_a_moderator_can_decide_only_in_an_organization_that_is_not_personal(self):
        self.assertTrue(moderator_can_decide(self.photo(self.batch("team"), "needs_review")))
        self.assertFalse(moderator_can_decide(self.photo(self.batch("personal"), "needs_review")))
        self.assertFalse(moderator_can_decide(self.photo(self.batch("none"), "needs_review")))

    def test_the_effective_verdict_for_every_combination(self):
        expected = {
            ("team", "accepted"): "accepted", ("team", "needs_review"): "needs_review", ("team", "rejected"): "rejected",
            ("personal", "accepted"): "accepted", ("personal", "needs_review"): "accepted", ("personal", "rejected"): "rejected",
            ("none", "accepted"): "accepted", ("none", "needs_review"): "accepted", ("none", "rejected"): "rejected",
        }
        for (where, verdict), effective in expected.items():
            with self.subTest(where=where, verdict=verdict):
                image = self.photo(self.batch(where), verdict)
                self.assertEqual(effective_decision(image), effective)
                self.assertEqual(effective_decision(image, verdict), effective)
                self.assertEqual(review_required(image), where == "team" and verdict == "needs_review")

    def test_the_database_condition_agrees_with_the_python_rule_for_every_combination(self):
        images = {(where, verdict): self.photo(self.batch(where), verdict) for where in ("team", "personal", "none") for verdict in ("accepted", "needs_review", "rejected")}
        for wanted in ("accepted", "needs_review", "rejected"):
            found = set(ImageUpload.objects.filter(effective_q(wanted)).values_list("pk", flat=True))
            should = {image.pk for image in images.values() if effective_decision(image) == wanted}
            self.assertEqual(found, should, wanted)

    def test_a_photograph_without_a_verdict_is_not_touched(self):
        image = ImageUpload.objects.create(owner=self.owner, batch=self.batch(), original_name="none.jpg", status="uploaded")
        self.assertEqual(effective_decision(image), "")
        self.assertFalse(review_required(image))

    def test_tips_are_plain_sentences_without_repeats_and_unknown_codes_are_skipped(self):
        tips = tips_for(["underexposed", "no_face_detected", "underexposed", "some_future_code", None])
        self.assertEqual(tips, [TIP_SENTENCES["underexposed"], TIP_SENTENCES["no_face_detected"]])
        self.assertTrue(all("review" not in tip.lower() for tip in TIP_SENTENCES.values()))

    def test_the_reason_codes_include_the_face_checks_doubt(self):
        image = self.photo(self.batch(), "needs_review", ["underexposed"], face="needs_review", face_reason="face_off_center")
        self.assertEqual(image_reason_codes(image), ["underexposed", "face_off_center"])


class EverythingThatShowsTheVerdictTests(EffectiveDecisionBase):
    def test_the_status_word(self):
        personal = self.photo(self.batch("personal"), "needs_review", ["underexposed"])
        team = self.photo(self.batch("team"), "needs_review", ["underexposed"])
        rejected = self.photo(self.batch("personal"), "rejected", ["severe_underexposure"])
        accepted = self.photo(self.batch("personal"), "accepted")
        self.assertEqual([image_display_status(image) for image in (personal, team, rejected, accepted)], ["accepted", "needs_review", "rejected", "accepted"])

    def test_a_face_check_that_wanted_review_is_not_a_review_in_a_personal_workspace(self):
        personal = self.photo(self.batch("personal"), "needs_review", ["face_validation_required"], face="needs_review", face_reason="no_face_detected")
        team = self.photo(self.batch("team"), "needs_review", ["face_validation_required"], face="needs_review", face_reason="no_face_detected")
        self.assertEqual((image_display_status(personal), image_display_status(team)), ("accepted", "needs_review"))
        refused = self.photo(self.batch("personal"), "needs_review", face="rejected", face_reason="face_too_small")
        self.assertEqual(image_display_status(refused), "rejected")                      # a rejection is never softened

    def test_the_status_word_costs_no_query_per_photograph(self):
        batch = self.batch("team")
        for _ in range(4):
            self.photo(batch, "needs_review")
        images = list(with_status_relations(ImageUpload.objects.filter(batch=batch)))
        with self.assertNumQueries(0):
            for image in images:
                image_display_status(image)

    def test_my_images_filter_and_rows(self):
        personal = self.photo(self.batch("personal"), "needs_review", ["underexposed"])
        team = self.photo(self.batch("team"), "needs_review", ["underexposed"])
        client = self.api()

        def ids(status):
            return {row["id"] for row in client.get(reverse("my-image-list"), {"status": status}).json()["results"]}

        self.assertEqual(ids("needs_review"), {team.pk})
        self.assertEqual(ids("accepted"), {personal.pk})
        rows = {row["id"]: row for row in client.get(reverse("my-image-list")).json()["results"]}
        self.assertEqual((rows[personal.pk]["display_status"], rows[team.pk]["display_status"]), ("accepted", "needs_review"))
        mine, theirs = rows[personal.pk]["quality_decision"], rows[team.pk]["quality_decision"]
        self.assertEqual((mine["decision"], mine["effective_decision"], mine["review_required"]), ("needs_review", "accepted", False))
        self.assertEqual(mine["tips"], [TIP_SENTENCES["underexposed"]])
        self.assertEqual((theirs["decision"], theirs["effective_decision"], theirs["review_required"]), ("needs_review", "needs_review", True))

    def test_the_quality_endpoint_says_the_same(self):
        image = self.photo(self.batch("personal"), "needs_review", ["borderline_contrast"])
        body = self.api().get(reverse("quality:image-quality", kwargs={"image_id": image.pk})).json()
        self.assertEqual((body["decision"], body["effective_decision"], body["review_required"]), ("needs_review", "accepted", False))
        self.assertEqual(body["tips"], [TIP_SENTENCES["borderline_contrast"]])

    def test_a_batch_of_only_doubted_personal_photographs_completes_it_is_not_partial(self):
        batch = self.batch("personal")
        for _ in range(3):
            self.photo(batch, "needs_review", ["underexposed"])
        payload = batch_progress_payload(batch)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual((payload["counts"]["accepted"], payload["counts"]["needs_review"]), (3, 0))
        self.assertEqual({row["status"] for row in payload["images"]}, {"accepted"})

    def test_an_organizations_batch_stays_partial_as_before(self):
        batch = self.batch("team")
        self.photo(batch, "needs_review")
        self.photo(batch, "accepted")
        payload = batch_progress_payload(batch)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["counts"]["needs_review"], 1)

    def test_the_notification(self):
        personal = self.photo(self.batch("personal"), "needs_review", ["underexposed"])
        team = self.photo(self.batch("team"), "needs_review", ["underexposed"])
        notify_quality_result(personal.quality_decision)
        notify_quality_result(team.quality_decision)
        mine = UserNotification.objects.get(resource_id=personal.pk, resource_type="image")
        theirs = UserNotification.objects.get(resource_id=team.pk, resource_type="image")
        self.assertEqual((mine.event_type, mine.title, mine.severity), ("photo_accepted", "Photo accepted", "success"))
        self.assertIn(TIP_SENTENCES["underexposed"], mine.message)
        self.assertNotIn("review", (mine.title + mine.message).lower())
        self.assertEqual((theirs.event_type, theirs.title), ("photo_needs_review", "Photo needs review"))

    def test_the_dashboard_counts(self):
        self.photo(self.batch("personal"), "needs_review")
        self.photo(self.batch("personal"), "accepted")
        self.photo(self.batch("team"), "needs_review")
        self.photo(self.batch("team"), "rejected", ["severe_underexposure"])
        summary = build_user_dashboard(self.owner)["summary"]
        self.assertEqual((summary["accepted_images"], summary["needs_review_images"]), (2, 1))

    def test_the_accepted_results_package_holds_a_doubted_personal_photograph_but_not_an_organizations(self):
        batch = self.batch("personal")
        personal = self.photo(batch, "needs_review", ["underexposed"])
        batch.processing_status = "completed"
        batch.auto_enhance = True                                                        # it asked for automatic enhancement, so originals stand in where none finished
        batch.save(update_fields=["processing_status", "auto_enhance"])
        package = DownloadPackage.objects.create(owner=self.owner, batch=batch, package_type="accepted_results", report_format="json", status="queued", task_id="t")
        ensure_package_ready(batch, "accepted_results")                                  # accepted photographs exist: it can be built (originals stand in)
        path, manifest, _size, _checksum = build_package_archive(package)
        with zipfile.ZipFile(path):
            pass
        path.unlink()
        self.assertEqual([item["image_id"] for item in manifest["files"]], [personal.pk])

        team_batch = self.batch("team")
        self.photo(team_batch, "needs_review")
        team_batch.processing_status = "completed"
        team_batch.save(update_fields=["processing_status"])
        from downloads.services import PackageNotReadyError

        with self.assertRaises(PackageNotReadyError):
            ensure_package_ready(team_batch, "accepted_results")


class TheTaskTests(EffectiveDecisionBase):
    """Through the real quality task: the activity entry and the review row follow the same rule."""

    def run_task(self, image, verdict):
        from unittest.mock import patch

        import numpy as np

        from .tasks import analyze_image_quality_task

        quality = image.quality_decision
        ImageQualityDecision.objects.filter(pk=quality.pk).update(status="queued", decision="", rejection_reasons=[])
        with (
            patch("quality_control.services._read_standardized", return_value=np.zeros((4, 4, 3), dtype=np.uint8)),
            patch("quality_control.services.analyze_image_quality", return_value={"defects": []}),
            patch("quality_control.services.with_face_sharpness", side_effect=lambda metrics, img, validation: metrics),
            patch("quality_control.services.calculate_scores", return_value={}),
            patch("quality_control.services.make_decision", return_value=(verdict, ["underexposed"], "test")),
        ):
            analyze_image_quality_task.apply(args=[quality.pk])

    def test_a_personal_photograph_records_quality_accepted_and_opens_no_review(self):
        from activity.models import ActivityEvent
        from moderation.models import ModeratorReview

        image = self.photo(self.batch("personal"), "needs_review")
        self.run_task(image, "needs_review")
        events = set(ActivityEvent.objects.filter(user=self.owner, object_id=str(image.pk)).values_list("event", flat=True))
        self.assertIn("quality_accepted", events)
        self.assertNotIn("quality_needs_review", events)
        self.assertFalse(ModeratorReview.objects.filter(image=image).exists())
        self.assertEqual(ImageQualityDecision.objects.get(image=image).decision, "needs_review")          # the evidence is kept

    def test_an_organizations_photograph_still_records_the_review_and_opens_one(self):
        from activity.models import ActivityEvent
        from moderation.models import ModeratorReview

        image = self.photo(self.batch("team"), "needs_review")
        self.run_task(image, "needs_review")
        events = set(ActivityEvent.objects.filter(user=self.owner, object_id=str(image.pk)).values_list("event", flat=True))
        self.assertIn("quality_needs_review", events)
        self.assertTrue(ModeratorReview.objects.filter(image=image, source="quality").exists())


class SimpleRuleTests(SimpleTestCase):
    def test_the_condition_for_a_rejection_is_the_plain_stored_value(self):
        self.assertEqual(str(effective_q("rejected")), "(AND: ('quality_decision__decision', 'rejected'))")
