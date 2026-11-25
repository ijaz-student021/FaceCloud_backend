"""A photograph a moderator or an administrator rejected leaves My Images' All / Accepted / In review and sits under Rejected only, is not enhanced, and comes back when it is
approved or put back under review. The checks' own verdict keeps its old, softer behaviour."""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from enhancement.eligibility import rejection_of
from moderation.models import ModeratorReview
from moderation.services import create_review_for_quality_decision, decide_review
from operations.image_decision import decide_image
from organizations.models import OrganizationMembership
from organizations.services import create_organization, create_personal_organization
from quality_control.models import ImageQualityDecision
from uploads.display_status import image_display_status, with_status_relations
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

PASSWORD = "ForestRiver!4821"


class PersonRejectionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("rej-admin", "rej-admin@example.com", PASSWORD)
        self.owner = User.objects.create_user("rej-owner", "rej-owner@example.com", PASSWORD, is_email_verified=True)
        self.organization = create_organization(name="Rej Co", owner=self.owner)
        self.moderator = User.objects.create_user("rej-mod", "rej-mod@example.com", PASSWORD, is_email_verified=True, role="moderator")
        create_personal_organization(self.moderator)
        OrganizationMembership.objects.create(organization=self.organization, user=self.moderator, role="moderator", status="active")
        self.member = User.objects.create_user("rej-member", "rej-member@example.com", PASSWORD, is_email_verified=True)
        self.personal = create_personal_organization(self.member)
        OrganizationMembership.objects.create(organization=self.organization, user=self.member, role="member", status="active")
        self.client = APIClient()
        self.client.force_authenticate(self.member)

    def photo(self, name, organization, decision="needs_review"):
        batch = UploadBatch.objects.create(owner=self.member, upload_type="single", organization=organization)
        image = ImageUpload.objects.create(owner=self.member, batch=batch, original_name=name, file_extension=".jpg", mime_type="image/jpeg", status="uploaded")
        pre = ImagePreprocessingResult.objects.create(image=image, owner=self.member, status="completed", output_format="png", output_name="p.png", output_width=400, output_height=300, processed_file_size=1)
        face = FaceValidationResult.objects.create(image=image, owner=self.member, preprocessing_result=pre, decision="accepted", face_count=1)
        quality = ImageQualityDecision.objects.create(image=image, owner=self.member, preprocessing_result=pre, face_validation=face, status="completed", decision=decision,
                                                     overall_score=48.0, rejection_reasons=["underexposed"])
        return image, quality

    def names(self, status=None):
        response = self.client.get("/api/images/my/", {"status": status} if status else {})
        self.assertEqual(response.status_code, 200, response.content)
        return sorted(item["original_name"] for item in response.json()["results"])

    def word(self, image):
        return image_display_status(with_status_relations(ImageUpload.objects.filter(pk=image.pk)).get())

    # -- a moderator rejects a team's photograph -------------------------------------------------------------------------------------------------------------
    def test_a_moderator_rejection_moves_the_photo_to_rejected_only(self):
        image, quality = self.photo("team.jpg", self.organization)
        self.photo("fine.jpg", self.organization, decision="accepted")
        review = create_review_for_quality_decision(quality)
        self.assertEqual(self.names("needs_review"), ["team.jpg"])
        decide_review(review=review, actor=self.moderator, action="reject", reason="Face is not visible")
        self.assertEqual(self.word(image), "rejected")
        self.assertEqual(self.names(), ["fine.jpg"])                                  # All photos
        self.assertEqual(self.names("all"), ["fine.jpg"])
        self.assertEqual(self.names("accepted"), ["fine.jpg"])
        self.assertEqual(self.names("needs_review"), [])
        self.assertEqual(self.names("rejected"), ["team.jpg"])
        image.refresh_from_db()
        self.assertEqual(image.status, "uploaded")                                    # the file is stored: preview, trash and a later approval keep working
        self.assertEqual(image.rejection_reason, "Rejected by a moderator: Face is not visible")
        self.assertIsNotNone(rejection_of(image))                                     # and it is not enhanced, not even on request

    # -- an administrator rejects a personal photograph ------------------------------------------------------------------------------------------------------
    def test_an_administrator_rejection_of_a_personal_photo_does_the_same(self):
        image, _quality = self.photo("mine.jpg", self.personal)
        self.assertEqual(self.names("accepted"), ["mine.jpg"])                        # a doubt in a personal workspace counts as accepted
        decide_image(image=image, actor=self.admin, action="reject", reason="Not allowed here")
        self.assertEqual(self.word(image), "rejected")
        self.assertEqual(self.names(), [])
        self.assertEqual(self.names("accepted"), [])
        self.assertEqual(self.names("rejected"), ["mine.jpg"])
        image.refresh_from_db()
        self.assertEqual(image.rejection_reason, "Rejected by an administrator: Not allowed here")

    # -- it can be undone ------------------------------------------------------------------------------------------------------------------------------------
    def test_putting_it_back_under_review_or_approving_it_brings_the_photo_back(self):
        image, quality = self.photo("undo.jpg", self.organization)
        review = create_review_for_quality_decision(quality)
        decide_review(review=review, actor=self.moderator, action="reject", reason="Too dark")
        self.assertEqual(self.names("rejected"), ["undo.jpg"])

        decide_image(image=image, actor=self.admin, action="keep_in_review", reason="Look again")
        self.assertEqual(self.names("rejected"), [])
        self.assertEqual(self.names("needs_review"), ["undo.jpg"])
        self.assertEqual(self.names(), ["undo.jpg"])
        image.refresh_from_db()
        self.assertEqual(image.rejection_reason, "")                                  # the reviewer's sentence is taken back
        self.assertIsNone(rejection_of(image))

        decide_image(image=image, actor=self.admin, action="approve", reason="Fine after all")
        self.assertEqual(self.names("accepted"), ["undo.jpg"])
        self.assertEqual(self.names("rejected"), [])

    def test_an_administrator_can_reject_it_again_after_approving(self):
        image, _quality = self.photo("again.jpg", self.personal)
        decide_image(image=image, actor=self.admin, action="reject", reason="No")
        decide_image(image=image, actor=self.admin, action="approve", reason="Yes")
        self.assertEqual(self.names("rejected"), [])
        decide_image(image=image, actor=self.admin, action="reject", reason="No again")
        self.assertEqual(self.names("rejected"), ["again.jpg"])

    # -- the checks' own verdicts ------------------------------------------------------------------------------------------------------------------------------
    def test_a_photo_refused_at_upload_is_under_rejected_only(self):
        image, _quality = self.photo("blurry.jpg", self.personal, decision="accepted")
        ImageUpload.objects.filter(pk=image.pk).update(status="rejected", rejection_reason="This photo is too blurry to enhance.")
        self.assertEqual(self.names(), [])
        self.assertEqual(self.names("rejected"), ["blurry.jpg"])

    def test_the_checks_own_rejected_decision_is_still_softened_and_stays_visible(self):
        self.photo("soft.jpg", self.personal, decision="rejected")                  # the checks said "rejected" (QUALITY_CHECKS_REJECT is off): no person decided
        self.assertEqual(self.names(), ["soft.jpg"])
        self.assertEqual(self.names("accepted"), ["soft.jpg"])
        self.assertEqual(self.names("rejected"), [])

    def test_a_photo_with_no_quality_decision_yet_is_still_listed(self):
        batch = UploadBatch.objects.create(owner=self.member, upload_type="single", organization=self.personal)
        ImageUpload.objects.create(owner=self.member, batch=batch, original_name="new.jpg", file_extension=".jpg", mime_type="image/jpeg", status="uploaded")
        self.assertEqual(self.names(), ["new.jpg"])

    # -- photos rejected before the flag existed ---------------------------------------------------------------------------------------------------------------
    def test_the_backfill_marks_old_person_rejections_once(self):
        image, quality = self.photo("old.jpg", self.organization)
        review = create_review_for_quality_decision(quality)
        decide_review(review=review, actor=self.moderator, action="reject", reason="Old rejection")
        ImageQualityDecision.objects.filter(pk=quality.pk).update(decided_by_person=False)       # as it was before this change
        ImageUpload.objects.filter(pk=image.pk).update(rejection_reason="")
        self.assertEqual(self.names(), ["old.jpg"])

        out = StringIO()
        call_command("backfill_person_rejections", stdout=out)
        self.assertIn("1 rejected", out.getvalue())
        self.assertEqual(self.names(), ["old.jpg"])                                   # a dry run writes nothing

        call_command("backfill_person_rejections", "--apply", stdout=StringIO())
        self.assertEqual(self.names(), [])
        self.assertEqual(self.names("rejected"), ["old.jpg"])
        image.refresh_from_db()
        self.assertEqual(image.rejection_reason, "Rejected by a moderator: Old rejection")
        out = StringIO()
        call_command("backfill_person_rejections", "--apply", stdout=out)
        self.assertIn("0 rejected", out.getvalue())
