"""An administrator's decision on a photograph: ``POST /api/system/images/{id}/decision/`` (``operations/image_decision.py``).

The pipeline hooks that an approval starts (the automatic enhancement and the person folders) are replaced by mocks in most tests so they can be counted; the two classes at the end
run the real person-folder pipeline.
"""
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from activity.models import ActivityEvent
from identities import test_group_after_verdict as grouping_tests
from identities.models import IdentityAnalysis, IdentityChange, IdentityGroup, IdentityMembership
from identities.test_person_folders import FolderFixtures
from moderation.member_messages import ADMIN_APPROVED_DEFAULT
from moderation.models import ModeratorReview
from moderation.serializers import ModeratorDecisionSerializer
from moderation.services import review_deadline
from moderation.tests import ModerationFixtureMixin
from notifications.models import NotificationPreference, UserNotification
from organizations.audit_reading import describe_event, labels_for
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization, create_personal_organization
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .admin_images import DECISION_ACTIONS, AdminImageDecisionSerializer
from .image_decision import REASON_REQUIRED, decide_image

TOLD = ("photo_review_approved", "photo_review_rejected")
DECIDED = "admin_image_decided"


class DecisionBase(ModerationFixtureMixin, TestCase):
    """An administrator, a team (owner, moderator, member) and photographs in every state a decision meets."""

    def setUp(self):
        self.admin = self.make_user("root", role="admin")
        self.team_owner = self.make_user("team-owner")
        self.organization = create_organization(name="Team", owner=self.team_owner, moderator_data_scope="organization")
        self.moderator = self.make_user("mod", role="moderator")
        OrganizationMembership.objects.create(organization=self.organization, user=self.moderator, role="moderator", status="active")
        self.member = self.make_user("alice")
        OrganizationMembership.objects.create(organization=self.organization, user=self.member, role="member", status="active")
        self.admin_client = self.authenticated_client(self.admin)
        self.moderator_client = self.authenticated_client(self.moderator)
        # What an approval starts after the verdict. Counted here; the real pipeline runs in the two classes at the end.
        for target, name in (("enhancement.auto.enhance_after_verdict", "enhance"), ("identities.verdict.group_when_enhancement_ends", "group")):
            patcher = patch(target)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    # -- fixtures ---------------------------------------------------------------------------------------------------------------------------
    def photo(self, decision="needs_review", *, review=True, owner=None, organization="team", quality_status="completed", status="uploaded", trashed=False, name="photo.jpg"):
        """``(image, quality decision, review)``. ``decision=None``: the photograph has no quality decision at all. A review is opened only in a team."""
        owner = owner or self.member
        organization = self.organization if organization == "team" else organization
        batch = UploadBatch.objects.create(owner=owner, upload_type="single", organization=organization)
        image = ImageUpload.objects.create(
            owner=owner, batch=batch, original_name=name, status=status, file_extension=".jpg", mime_type="image/jpeg", width=320, height=240,
            rejection_reason="Not an image" if status == "rejected" else "", trashed_at=timezone.now() if trashed else None,
        )
        return (image, *self.quality_for(image, decision, review=review, quality_status=quality_status))

    def quality_for(self, image, decision="needs_review", *, review=True, quality_status="completed"):
        if decision is None:
            return None, None
        preprocessing, _ = ImagePreprocessingResult.objects.get_or_create(image=image, defaults={"owner": image.owner, "status": "completed"})
        quality = ImageQualityDecision.objects.create(
            owner=image.owner, image=image, preprocessing_result=preprocessing, status=quality_status,
            decision=decision if quality_status == "completed" else "", overall_score=55.0, sharpness_score=40.0,
        )
        organization = image.batch.organization
        opened = None
        if review and organization is not None and not organization.is_personal:
            opened = ModeratorReview.objects.create(
                organization=organization, image=image, source=ModeratorReview.Source.QUALITY, ai_recommendation=decision,
                quality_decision=quality, due_at=review_deadline(),
            )
        return quality, opened

    # -- calls ------------------------------------------------------------------------------------------------------------------------------
    def decide(self, image, action=None, reason=None, client=None):
        payload = {} if action is None else {"action": action}
        if reason is not None:
            payload["reason"] = reason
        image_id = image if isinstance(image, int) else image.pk
        with self.captureOnCommitCallbacks(execute=True):
            return (client or self.admin_client).post(f"/api/system/images/{image_id}/decision/", payload, format="json")

    def verdict(self, quality):
        return ImageQualityDecision.objects.get(pk=quality.pk).decision

    def told(self, user=None):
        return list(UserNotification.objects.filter(recipient=user or self.member, event_type__in=TOLD).order_by("pk"))

    def decided_events(self):
        return list(ActivityEvent.objects.filter(event=DECIDED).order_by("pk"))

    def team_log(self):
        return list(OrganizationAuditEvent.objects.filter(action="moderator_review_decided").order_by("pk"))

    def queue(self, status=None):
        """The ids of the team's reviews as the moderator's queue lists them."""
        url = reverse("moderation:review-list", kwargs={"organization_id": self.organization.pk})
        rows = self.moderator_client.get(url, {"status": status} if status else {}).json()
        return [row["id"] for row in (rows["results"] if isinstance(rows, dict) else rows)]

    def assertNothingHappened(self, quality, review=None):
        """Nothing was decided, recorded or sent."""
        self.assertEqual(self.decided_events(), [])
        self.assertEqual(self.told(), [])
        self.assertEqual(self.team_log(), [])
        self.enhance.assert_not_called()
        self.group.assert_not_called()
        if quality is not None:
            self.assertEqual(ImageQualityDecision.objects.get(pk=quality.pk).decision, quality.decision)
        if review is not None:
            fresh = ModeratorReview.objects.get(pk=review.pk)
            self.assertEqual((fresh.status, fresh.decided_by_id, fresh.decided_at), (review.status, review.decided_by_id, review.decided_at))


class WhoMayDecideTests(DecisionBase):
    def test_only_an_administrator_may_decide_and_nothing_changes_for_anyone_else(self):
        image, quality, review = self.photo()
        callers = [
            ("the photo's owner", self.authenticated_client(self.member), 403),
            ("an organization moderator", self.moderator_client, 403),
            ("the organization's owner", self.authenticated_client(self.team_owner), 403),
            ("nobody signed in", APIClient(), 401),
        ]
        for label, client, expected in callers:
            for action in ("approve", "reject", "keep_in_review"):
                with self.subTest(caller=label, action=action):
                    self.assertEqual(self.decide(image, action, "because", client=client).status_code, expected)
        self.assertNothingHappened(quality, review)

    def test_the_call_answers_options_with_200_only_to_an_administrator(self):
        """The console asks once with OPTIONS whether the call exists, and switches its buttons on when this is 200."""
        image, _quality, _review = self.photo()
        for photo_id in (image.pk, 987654):                                                    # the photo need not exist: it is a question about the call
            self.assertEqual(self.admin_client.options(f"/api/system/images/{photo_id}/decision/").status_code, 200)
        self.assertEqual(self.authenticated_client(self.member).options(f"/api/system/images/{image.pk}/decision/").status_code, 403)
        self.assertEqual(APIClient().options(f"/api/system/images/{image.pk}/decision/").status_code, 401)

    def test_the_actions_are_the_moderators_own_names_without_correct(self):
        moderator_actions = list(ModeratorDecisionSerializer().fields["action"].choices)
        self.assertEqual(DECISION_ACTIONS, [action for action in moderator_actions if action != "correct"])
        self.assertEqual(DECISION_ACTIONS, ["approve", "reject", "keep_in_review"])


class ApproveAndRejectTests(DecisionBase):
    def test_approving_a_photo_under_review_accepts_it_and_answers_with_the_detail_body(self):
        image, quality, _review = self.photo()
        response = self.decide(image, "approve")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["quality"]["decision"], "accepted")
        self.assertEqual(self.verdict(quality), "accepted")
        self.assertEqual(response.json(), self.admin_client.get(f"/api/system/images/{image.pk}/").json())       # the SAME body as the detail call

    def test_rejecting_with_a_reason_rejects_it(self):
        image, quality, _review = self.photo()
        response = self.decide(image, "reject", "Too blurry to use")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["quality"]["decision"], "rejected")
        self.assertEqual(self.verdict(quality), "rejected")

    def test_holding_a_photo_puts_it_back_under_review(self):
        image, quality, _review = self.photo("accepted", review=False)
        response = self.decide(image, "keep_in_review")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["quality"]["decision"], "needs_review")
        self.assertEqual(self.verdict(quality), "needs_review")

    def test_a_reason_is_optional_except_for_a_rejection(self):
        for action, reason in (("approve", None), ("approve", ""), ("approve", "Looks fine"), ("keep_in_review", None), ("keep_in_review", "Please look again")):
            with self.subTest(action=action, reason=reason):
                image, _quality, _review = self.photo()
                self.assertEqual(self.decide(image, action, reason).status_code, 200)

    def test_an_approval_starts_what_the_pipeline_starts_after_any_accepted_verdict(self):
        image, _quality, _review = self.photo()
        self.decide(image, "approve")
        self.enhance.assert_called_once()
        self.group.assert_called_once()
        for hook in (self.enhance, self.group):
            self.assertEqual(hook.call_args.args[0].pk, image.pk)
            self.assertEqual(hook.call_args.kwargs, {"verdict": "accepted"})

    def test_a_rejection_or_a_hold_starts_neither_the_enhancement_nor_the_grouping(self):
        for action, reason in (("reject", "No"), ("keep_in_review", None)):
            image, _quality, _review = self.photo()
            self.decide(image, action, reason)
        self.enhance.assert_not_called()
        self.group.assert_not_called()

    def test_a_photo_the_checks_decided_on_their_own_can_be_overruled_both_ways(self):
        accepted, accepted_quality, _ = self.photo("accepted", review=False)
        needs_review, needs_review_quality, _ = self.photo("needs_review", review=False)
        self.assertEqual(self.decide(accepted, "reject", "Not a usable photo").json()["quality"]["decision"], "rejected")
        self.assertEqual(self.decide(needs_review, "approve").json()["quality"]["decision"], "accepted")
        self.assertEqual(self.verdict(accepted_quality), "rejected")
        self.assertEqual(self.verdict(needs_review_quality), "accepted")
        self.assertEqual(self.enhance.call_count, 1)                                   # only the approval

    def test_a_photo_in_a_personal_workspace_or_in_no_organization_can_be_decided_too(self):
        personal = create_personal_organization(self.member)
        for organization in (personal, None):
            with self.subTest(personal=organization is not None):
                image, quality, review = self.photo("needs_review", organization=organization)
                self.assertIsNone(review)                                              # nobody could ever decide a review opened there
                self.assertEqual(self.decide(image, "approve").json()["quality"]["decision"], "accepted")
                self.assertEqual(self.verdict(quality), "accepted")
                self.assertEqual(self.decide(image, "reject", "On second thought, no").json()["quality"]["decision"], "rejected")

    def test_the_decision_is_in_the_row_of_the_list_too(self):
        image, _quality, _review = self.photo()
        self.decide(image, "reject", "Too blurry to use")
        rows = {row["id"]: row for row in self.admin_client.get("/api/system/images/").json()["results"]}
        self.assertEqual(rows[image.pk]["quality"]["decision"], "rejected")
        self.assertEqual(self.admin_client.get("/api/system/images/?quality_decision=rejected").json()["count"], 1)


class OpenReviewTests(DecisionBase):
    """An open review is decided by the same ``decide_review`` a moderator uses, with the administrator as the one who decided."""

    def test_approving_closes_the_open_review_and_the_moderators_queue_no_longer_lists_it(self):
        image, _quality, review = self.photo()
        self.assertEqual(self.queue("pending"), [review.pk])
        self.decide(image, "approve", "Looks fine to me")
        fresh = ModeratorReview.objects.get(pk=review.pk)
        self.assertEqual((fresh.status, fresh.decided_by_id), ("approved", self.admin.pk))
        self.assertIsNotNone(fresh.decided_at)
        self.assertEqual((fresh.decision_reason, fresh.message_to_member), ("Looks fine to me", "Looks fine to me"))
        self.assertEqual(self.queue("pending"), [])
        self.assertEqual(self.queue("approved"), [review.pk])

    def test_rejecting_closes_it_as_rejected_with_the_reason(self):
        image, _quality, review = self.photo()
        self.decide(image, "reject", "Too blurry to use")
        fresh = ModeratorReview.objects.get(pk=review.pk)
        self.assertEqual((fresh.status, fresh.decided_by_id, fresh.decision_reason), ("rejected", self.admin.pk, "Too blurry to use"))
        self.assertEqual(self.queue("pending"), [])

    def test_an_escalated_review_can_be_settled_by_the_administrator(self):
        image, _quality, review = self.photo()
        ModeratorReview.objects.filter(pk=review.pk).update(status="escalated", assigned_moderator=self.moderator)
        self.assertEqual(self.decide(image, "approve").status_code, 200)
        self.assertEqual(ModeratorReview.objects.get(pk=review.pk).status, "approved")

    def test_the_decision_log_and_the_overview_figures_count_it_like_a_moderators(self):
        image, _quality, review = self.photo()
        self.decide(image, "approve", "Looks fine to me")
        (entry,) = self.team_log()
        self.assertEqual((entry.organization_id, entry.actor_id, entry.target_type, entry.target_id), (self.organization.pk, self.admin.pk, "ModeratorReview", review.pk))
        self.assertEqual((entry.new_value["action"], entry.new_value["status"], entry.previous_value["status"]), ("approve", "approved", "pending"))
        dashboard = self.moderator_client.get(reverse("moderation:dashboard", kwargs={"organization_id": self.organization.pk})).json()
        self.assertEqual((dashboard["pending"], dashboard["approved_total"], dashboard["completed_today"]), (0, 1, 1))
        overview = self.admin_client.get("/api/system/moderation/").json()
        self.assertEqual((overview["totals"]["pending"], overview["totals"]["approved_total"]), (0, 1))
        self.assertIn({"moderator_id": self.admin.pk, "username": "root", "assigned_open": 0, "decided_total": 1}, overview["moderator_workload"])

    def test_a_moderator_cannot_decide_a_review_the_administrator_already_decided(self):
        image, _quality, review = self.photo()
        self.decide(image, "approve")
        response = self.moderator_client.post(
            reverse("moderation:review-decision", kwargs={"organization_id": self.organization.pk, "review_id": review.pk}), {"action": "reject"}, format="json",
        )
        self.assertEqual(response.status_code, 409)

    def test_a_review_another_moderator_is_working_on_right_now_is_not_taken_from_them(self):
        image, quality, review = self.photo()
        ModeratorReview.objects.filter(pk=review.pk).update(claimed_by=self.moderator, claimed_at=timezone.now())
        claimed = ModeratorReview.objects.get(pk=review.pk)
        for action in ("approve", "reject"):
            response = self.decide(image, action, "because")
            self.assertEqual(response.status_code, 409)
            self.assertIn("mod is reviewing this right now", response.json()["detail"])
        self.assertNothingHappened(quality, claimed)
        self.assertEqual(ModeratorReview.objects.get(pk=review.pk).status, "pending")
        self.assertEqual(ModeratorReview.objects.get(pk=review.pk).claimed_by_id, self.moderator.pk)

    def test_a_claim_that_has_run_out_does_not_stop_the_administrator(self):
        image, _quality, review = self.photo()
        ModeratorReview.objects.filter(pk=review.pk).update(claimed_by=self.moderator, claimed_at=timezone.now() - timezone.timedelta(hours=2))
        self.assertEqual(self.decide(image, "approve").status_code, 200)

    def test_only_the_quality_review_is_closed_not_the_ones_about_other_questions(self):
        image, quality, review = self.photo()
        other = ModeratorReview.objects.create(organization=self.organization, image=image, source="duplicate", ai_recommendation="perceptual")
        self.decide(image, "approve")
        self.assertEqual(ModeratorReview.objects.get(pk=review.pk).status, "approved")
        self.assertEqual(ModeratorReview.objects.get(pk=other.pk).status, "pending")


class HoldTests(DecisionBase):
    """Putting a photograph back under review must leave it where a moderator can see it, or it would wait for ever."""

    def test_a_held_photo_the_checks_accepted_joins_the_moderators_queue(self):
        image, quality, _ = self.photo("accepted", review=False)
        self.assertEqual(self.queue(), [])
        self.decide(image, "keep_in_review", "Please look at the faces again")
        (review,) = ModeratorReview.objects.filter(image=image)
        self.assertEqual((review.source, review.status, review.ai_recommendation, review.decided_at), ("quality", "pending", "accepted", None))
        self.assertEqual(review.decision_reason, "Please look at the faces again")                   # the note the moderators read, as an escalation's reason is
        self.assertGreater(review.due_at, timezone.now())
        self.assertEqual(self.queue("pending"), [review.pk])

    def test_a_held_photo_a_moderator_had_decided_comes_back_to_the_queue(self):
        image, quality, review = self.photo()
        self.moderator_client.post(
            reverse("moderation:review-decision", kwargs={"organization_id": self.organization.pk, "review_id": review.pk}), {"action": "approve"}, format="json",
        )
        self.assertEqual(self.queue("pending"), [])
        self.assertEqual(self.verdict(quality), "accepted")

        self.assertEqual(self.decide(image, "keep_in_review", "A second look").status_code, 200)
        fresh = ModeratorReview.objects.get(pk=review.pk)
        self.assertEqual((fresh.status, fresh.decided_by_id, fresh.decided_at, fresh.assigned_moderator_id, fresh.message_to_member), ("pending", None, None, None, ""))
        self.assertEqual(fresh.decision_reason, "A second look")
        self.assertEqual(self.queue("pending"), [review.pk])
        self.assertEqual(self.verdict(quality), "needs_review")
        reopened = self.team_log()[-1]
        self.assertEqual((reopened.actor_id, reopened.target_id, reopened.previous_value, reopened.new_value["action"]), (self.admin.pk, review.pk, {"status": "approved"}, "keep_in_review"))
        self.assertEqual(len(ModeratorReview.objects.filter(image=image)), 1)                      # one review per quality decision, for good

    def test_a_held_photo_in_a_personal_workspace_opens_no_review(self):
        image, quality, review = self.photo("accepted", organization=create_personal_organization(self.member))
        self.assertEqual(self.decide(image, "keep_in_review").json()["quality"]["decision"], "needs_review")
        self.assertFalse(ModeratorReview.objects.filter(image=image).exists())
        self.assertEqual(self.team_log(), [])

    def test_holding_a_photo_that_is_under_review_with_an_open_review_changes_nothing(self):
        image, quality, review = self.photo()
        response = self.decide(image, "keep_in_review", "again")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"]["decision"], "needs_review")
        self.assertNothingHappened(quality, review)

    def test_holding_a_stuck_photo_gives_it_its_review_back_once(self):
        """A team's photo marked "needs review" that has no review would be waiting for a moderator who cannot see it."""
        image, quality, _ = self.photo("needs_review", review=False)
        self.decide(image, "keep_in_review")
        self.assertEqual(ModeratorReview.objects.filter(image=image, status="pending").count(), 1)
        self.assertEqual(len(self.decided_events()), 1)
        self.decide(image, "keep_in_review")
        self.assertEqual(ModeratorReview.objects.filter(image=image).count(), 1)
        self.assertEqual(len(self.decided_events()), 1)

    def test_a_hold_tells_the_owner_nothing(self):
        for decision in ("accepted", "rejected"):
            image, _quality, _ = self.photo(decision, review=False)
            self.decide(image, "keep_in_review")
        self.assertEqual(self.told(), [])
        self.assertEqual(UserNotification.objects.count(), 0)


class NotificationTests(DecisionBase):
    def test_the_owner_is_told_once_of_an_approval_with_the_reason_as_the_message(self):
        for label, review in (("with a review", True), ("without one", False)):
            with self.subTest(label):
                UserNotification.objects.all().delete()
                image, _quality, _ = self.photo(review=review)
                self.decide(image, "approve", "Looks fine to me")
                (note,) = self.told()
                self.assertEqual((note.event_type, note.severity, note.title), ("photo_review_approved", "success", "Your photo was approved"))
                self.assertEqual((note.message, note.resource_type, note.resource_id, note.action_path), ("Looks fine to me", "image", image.pk, f"/dashboard/images/{image.pk}"))

    def test_the_owner_is_told_once_of_a_rejection_with_the_reason_as_the_message(self):
        for label, review in (("with a review", True), ("without one", False)):
            with self.subTest(label):
                UserNotification.objects.all().delete()
                image, _quality, _ = self.photo(review=review)
                self.decide(image, "reject", "Too blurry to use")
                (note,) = self.told()
                self.assertEqual((note.event_type, note.severity, note.title), ("photo_review_rejected", "warning", "Your photo was not accepted"))
                self.assertEqual((note.message, note.resource_id, note.action_path), ("Too blurry to use", image.pk, f"/dashboard/images/{image.pk}"))

    def test_an_approval_without_a_reason_gets_a_standard_sentence_that_names_the_administrator(self):
        for review in (True, False):
            UserNotification.objects.all().delete()
            image, _quality, _ = self.photo(review=review)
            self.decide(image, "approve")
            self.assertEqual(self.told()[0].message, ADMIN_APPROVED_DEFAULT)

    def test_it_is_the_same_message_a_moderators_decision_sends(self):
        image, _quality, review = self.photo()
        other, _other_quality, other_review = self.photo()
        self.decide(image, "reject", "Too blurry to use")
        with self.captureOnCommitCallbacks(execute=True):
            self.moderator_client.post(
                reverse("moderation:review-decision", kwargs={"organization_id": self.organization.pk, "review_id": other_review.pk}),
                {"action": "reject", "message_to_member": "Too blurry to use"}, format="json",
            )
        by_admin, by_moderator = (UserNotification.objects.get(event_type="photo_review_rejected", resource_id=photo.pk) for photo in (image, other))
        for field in ("event_type", "severity", "title", "message", "resource_type", "email_status"):
            self.assertEqual(getattr(by_admin, field), getattr(by_moderator, field), field)
        self.assertEqual(by_admin.action_path.replace(str(image.pk), "N"), by_moderator.action_path.replace(str(other.pk), "N"))

    def test_deciding_the_same_thing_twice_tells_the_owner_once(self):
        for review in (True, False):
            UserNotification.objects.all().delete()
            image, _quality, _ = self.photo(review=review)
            for _ in range(3):
                self.assertEqual(self.decide(image, "reject", "No").status_code, 200)
            self.assertEqual(len(self.told()), 1)

    def test_every_change_of_mind_is_told_but_a_repeat_is_not(self):
        image, _quality, _ = self.photo("accepted", review=False)
        for action, reason, expected in (("reject", "No", 1), ("reject", "No", 1), ("approve", None, 2), ("approve", None, 2), ("reject", "Final answer", 3)):
            self.decide(image, action, reason)
            self.assertEqual(len(self.told()), expected, (action, reason))
        self.assertEqual([note.event_type for note in self.told()], ["photo_review_rejected", "photo_review_approved", "photo_review_rejected"])

    def test_the_administrators_own_photo_is_decided_without_telling_them(self):
        image, quality, _ = self.photo(owner=self.admin, organization=None)
        self.assertEqual(self.decide(image, "reject", "Mine, and no").status_code, 200)
        self.assertEqual(self.verdict(quality), "rejected")
        self.assertEqual(self.told(self.admin), [])

    def test_the_owners_notification_switches_are_respected(self):
        NotificationPreference.objects.create(user=self.member, photo_accepted=False, photo_rejected=True)
        image, _quality, _ = self.photo(review=False)
        self.decide(image, "approve", "Fine")
        self.assertEqual(self.told(), [])
        self.decide(image, "reject", "Not fine")
        self.assertEqual(len(self.told()), 1)

    def test_a_failure_to_tell_the_owner_does_not_turn_the_decision_into_an_error(self):
        image, quality, _ = self.photo(review=False)
        with patch("operations.image_decision.notify_member_of_image_decision", side_effect=RuntimeError("mail is down")), self.assertLogs("operations.image_decision", "ERROR"):
            response = self.decide(image, "reject", "No")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.verdict(quality), "rejected")
        self.assertEqual(len(self.decided_events()), 1)


class AuditTests(DecisionBase):
    def test_a_decision_writes_one_admin_event_naming_the_photo_its_owner_the_action_the_previous_decision_and_the_reason(self):
        image, _quality, review = self.photo()
        self.decide(image, "reject", "Too blurry to use")
        (event,) = self.decided_events()
        self.assertEqual((event.user_id, event.category, event.object_type, event.object_id), (self.admin.pk, "admin", "ImageUpload", str(image.pk)))
        self.assertEqual(event.details, {
            "owner_id": self.member.pk, "action": "reject", "previous_decision": "needs_review", "decision": "rejected",
            "reason": "Too blurry to use", "review_id": review.pk,
        })

    def test_the_event_is_in_the_platform_audit_next_to_the_one_for_viewing(self):
        image, _quality, _ = self.photo()
        self.admin_client.get(f"/api/system/images/{image.pk}/")
        self.decide(image, "approve")
        rows = self.admin_client.get("/api/system/audit/", {"category": "admin"}).json()["results"]
        events = {row["event"]: row for row in rows}
        self.assertEqual(set(events), {"admin_image_viewed", DECIDED})
        self.assertEqual((events[DECIDED]["actor_name"], events[DECIDED]["resource"], events[DECIDED]["type"]), ("root", f"ImageUpload {image.pk}", "admin"))
        self.assertEqual(events[DECIDED]["details"]["previous_decision"], "needs_review")

    def test_deciding_the_same_thing_again_writes_no_second_row_and_no_second_team_entry(self):
        for review in (True, False):
            ActivityEvent.objects.filter(event=DECIDED).delete()
            image, _quality, _ = self.photo(review=review)
            first = self.decide(image, "reject", "No")
            rows = (len(self.decided_events()), len(self.team_log()))
            second = self.decide(image, "reject", "No, again")
            self.assertEqual((first.status_code, second.status_code), (200, 200))
            self.assertEqual(first.json(), second.json())
            self.assertEqual((len(self.decided_events()), len(self.team_log())), rows)
            self.assertEqual(rows[0], 1)

    def test_every_change_of_mind_is_one_row_with_the_decision_it_replaced(self):
        image, _quality, _ = self.photo("accepted", review=False)
        self.decide(image, "reject", "No")
        self.decide(image, "approve")
        self.decide(image, "keep_in_review")
        steps = [(event.details["previous_decision"], event.details["decision"]) for event in self.decided_events()]
        self.assertEqual(steps, [("accepted", "rejected"), ("rejected", "accepted"), ("accepted", "needs_review")])

    def test_a_refused_request_writes_nothing(self):
        image, quality, review = self.photo()
        self.decide(image, "reject")                                     # no reason
        self.decide(image, "delete")
        self.decide(987654, "approve")
        self.assertNothingHappened(quality, review)

    def test_a_decision_on_a_team_photo_the_checks_decided_is_in_the_teams_own_log(self):
        """With no review to close there is no moderator entry: the team's log gets one about the photograph, so a moderator can see an administrator changed it."""
        image, _quality, _ = self.photo("accepted", review=False)
        self.decide(image, "reject", "Not a usable photo")
        (entry,) = self.team_log()
        self.assertEqual((entry.organization_id, entry.actor_id, entry.target_type, entry.target_id), (self.organization.pk, self.admin.pk, "ImageUpload", image.pk))
        self.assertEqual((entry.previous_value, entry.new_value, entry.reason), ({"decision": "accepted"}, {"action": "reject", "decision": "rejected"}, "Not a usable photo"))
        message, label, _from, _to = describe_event(entry, labels_for([entry]))                 # and the log's screen can word it
        self.assertEqual((message, label), ("Rejected photo.jpg.", "photo.jpg"))

    def test_overruling_a_moderators_decision_is_in_the_log_after_the_moderators_own_entry(self):
        image, quality, review = self.photo()
        with self.captureOnCommitCallbacks(execute=True):
            self.moderator_client.post(
                reverse("moderation:review-decision", kwargs={"organization_id": self.organization.pk, "review_id": review.pk}), {"action": "approve"}, format="json",
            )
        self.assertEqual(self.verdict(quality), "accepted")
        self.assertEqual(len(self.told()), 1)

        self.assertEqual(self.decide(image, "reject", "A second look says no").status_code, 200)

        first, second = self.team_log()
        self.assertEqual((first.actor_id, first.target_type, first.new_value["action"]), (self.moderator.pk, "ModeratorReview", "approve"))
        self.assertEqual((second.actor_id, second.target_type, second.target_id, second.new_value["action"]), (self.admin.pk, "ImageUpload", image.pk, "reject"))
        self.assertEqual(ModeratorReview.objects.get(pk=review.pk).status, "approved")             # what the moderator decided stays as history
        self.assertEqual(self.verdict(quality), "rejected")
        self.assertEqual([note.event_type for note in self.told()], ["photo_review_approved", "photo_review_rejected"])

    def test_a_decision_in_a_personal_workspace_is_in_no_teams_log(self):
        image, _quality, _ = self.photo("needs_review", organization=create_personal_organization(self.member))
        self.decide(image, "approve")
        self.assertEqual(self.team_log(), [])
        self.assertEqual(len(self.decided_events()), 1)


class ErrorTests(DecisionBase):
    def test_a_rejection_needs_a_reason(self):
        image, quality, review = self.photo()
        for reason in (None, "", "   "):
            with self.subTest(reason=reason):
                response = self.decide(image, "reject", reason)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"reason": ["Give a reason for rejecting the photo: the owner is told it."]})
        self.assertNothingHappened(quality, review)

    def test_the_request_serializer_enforces_the_reason_on_its_own(self):
        """The service checks it too (a caller other than the view); each layer is held to the rule by itself."""
        for body in ({"action": "reject"}, {"action": "reject", "reason": ""}, {"action": "reject", "reason": "  "}):
            serializer = AdminImageDecisionSerializer(data=body)
            self.assertFalse(serializer.is_valid(), body)
            self.assertEqual(serializer.errors, {"reason": [REASON_REQUIRED]})
        for body in ({"action": "approve"}, {"action": "keep_in_review", "reason": ""}, {"action": "reject", "reason": "Too blurry"}):
            self.assertTrue(AdminImageDecisionSerializer(data=body).is_valid(), body)

    def test_a_reason_is_at_most_500_characters(self):
        image, quality, review = self.photo()
        self.assertEqual(self.decide(image, "reject", "x" * 501).status_code, 400)
        self.assertEqual(self.decide(image, "approve", "x" * 501).status_code, 400)
        self.assertNothingHappened(quality, review)
        self.assertEqual(self.decide(image, "reject", "x" * 500).status_code, 200)

    def test_an_unknown_or_missing_action_is_a_400_with_a_sentence(self):
        image, quality, review = self.photo()
        for action in ("delete", "correct", "APPROVE", ""):
            with self.subTest(action=action):
                response = self.decide(image, action, "because")
                self.assertEqual(response.status_code, 400)
                self.assertIn("action", response.json())
        self.assertEqual(self.decide(image, None, "because").json(), {"action": ["This field is required."]})
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.admin_client.post(f"/api/system/images/{image.pk}/decision/", ["approve"], format="json").status_code, 400)
        self.assertNothingHappened(quality, review)

    def test_an_unknown_photo_is_a_json_404(self):
        response = self.decide(987654, "approve")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("detail", response.json())

    def test_a_malformed_address_is_a_json_404_too(self):
        response = self.admin_client.post("/api/system/images/abc/decision/", {"action": "approve"}, format="json")
        self.assertEqual((response.status_code, response["Content-Type"]), (404, "application/json"))

    def test_other_methods_are_not_allowed(self):
        image, _quality, _ = self.photo()
        for method in ("get", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.admin_client, method)(f"/api/system/images/{image.pk}/decision/").status_code, 405)


class UndecidableTests(DecisionBase):
    """409 with a sentence: the request is well formed, the photograph's state forbids it. Nothing is written."""

    def refused(self, image, quality=None, *, containing):
        for action in ("approve", "reject", "keep_in_review"):
            with self.subTest(action=action):
                response = self.decide(image, action, "because")
                self.assertEqual(response.status_code, 409, response.content)
                self.assertIn(containing, response.json()["detail"])
        self.assertNothingHappened(quality)

    def test_a_photo_refused_at_upload_cannot_be_decided(self):
        image, _quality, _ = self.photo(None, status="rejected")
        self.refused(image, containing="refused at upload")

    def test_a_photo_whose_quality_check_has_not_finished_cannot_be_decided(self):
        none, _, _ = self.photo(None)
        self.refused(none, containing="has not finished")
        for state in ("pending", "queued", "processing"):
            with self.subTest(state=state):
                image, quality, _ = self.photo("needs_review", quality_status=state, review=False)
                self.refused(image, quality, containing="has not finished")
                self.assertEqual(self.verdict(quality), "")

    def test_a_photo_whose_quality_check_failed_cannot_be_decided(self):
        image, quality, _ = self.photo("needs_review", quality_status="failed", review=False)
        self.refused(image, quality, containing="check of this photo failed")

    def test_a_photo_in_the_trash_cannot_be_decided(self):
        image, quality, review = self.photo(trashed=True)
        self.refused(image, quality, containing="in its owner's trash")
        self.assertEqual(ModeratorReview.objects.get(pk=review.pk).status, "pending")

    def test_the_detail_call_still_shows_these_photos(self):
        refused, _, _ = self.photo(None, status="rejected")
        self.assertEqual(self.admin_client.get(f"/api/system/images/{refused.pk}/").status_code, 200)


class ServiceTests(DecisionBase):
    def test_the_service_says_whether_anything_changed(self):
        image, quality, review = self.photo("accepted", review=False)
        self.assertTrue(decide_image(image=image, actor=self.admin, action="reject", reason="No"))
        self.assertFalse(decide_image(image=image, actor=self.admin, action="reject", reason="No, still"))
        self.assertTrue(decide_image(image=image, actor=self.admin, action="keep_in_review"))
        self.assertFalse(decide_image(image=image, actor=self.admin, action="keep_in_review"))
        self.assertEqual(len(self.decided_events()), 2)

    def test_it_refuses_what_the_serializer_would_so_a_caller_other_than_the_view_is_held_to_the_same_rules(self):
        image, quality, review = self.photo()
        for action, reason in (("reject", ""), ("reject", "   "), ("explode", "x")):
            with self.subTest(action=action, reason=reason), self.assertRaises(ValidationError):
                decide_image(image=image, actor=self.admin, action=action, reason=reason)
        self.assertNothingHappened(quality, review)


class RealFolderTests(DecisionBase):
    """A rejection takes the photograph out of the person folder it was in, as a moderator's does."""

    def test_a_rejected_photo_leaves_its_folder_and_the_history_says_who_did_it(self):
        fixtures = FolderFixtures()
        group = fixtures.make_group(self.member, "Person 001")
        image, membership = fixtures.add_photo(group)
        other, _ = fixtures.add_photo(group, "other.jpg")
        image.batch.organization = self.organization
        image.batch.save(update_fields=["organization"])
        _quality, review = self.quality_for(image, "needs_review")

        self.assertEqual(self.decide(image, "reject", "Too blurry to use").status_code, 200)

        self.assertEqual(IdentityMembership.objects.get(pk=membership.pk).state, IdentityMembership.State.REMOVED)
        group.refresh_from_db()
        self.assertEqual((group.member_count, group.state), (1, IdentityGroup.State.ACTIVE))
        change = IdentityChange.objects.get(membership=membership, action=IdentityChange.Action.REMOVED)
        self.assertEqual(change.reason, "The photo was rejected by an administrator.")
        self.assertEqual(ModeratorReview.objects.get(pk=review.pk).status, "rejected")

    def test_the_same_rejection_by_a_moderator_still_says_a_moderator(self):
        fixtures = FolderFixtures()
        group = fixtures.make_group(self.member, "Person 001")
        image, membership = fixtures.add_photo(group)
        image.batch.organization = self.organization
        image.batch.save(update_fields=["organization"])
        _quality, review = self.quality_for(image, "needs_review")
        response = self.moderator_client.post(
            reverse("moderation:review-decision", kwargs={"organization_id": self.organization.pk, "review_id": review.pk}), {"action": "reject"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IdentityChange.objects.get(membership=membership, action=IdentityChange.Action.REMOVED).reason, "The photo was rejected by a moderator.")


class RealPipelineTests(grouping_tests.Pipeline):
    """The person-folder pipeline, for real: an administrator's approval groups the photograph; a rejection takes it out of its folder again."""

    def setUp(self):
        super().setUp()
        grouping_tests.Provider.vectors = {1: grouping_tests.angle(0), 2: grouping_tests.angle(10)}
        self.admin = self.make_user("pipeline-root", role="admin")
        self.admin_client = self.authenticated_client(self.admin)
        self.organization, self.moderator = self.organization_batch()

    def decide(self, image, action, reason=None):
        body = {"action": action, **({"reason": reason} if reason else {})}
        with self.model_patches(), self.captureOnCommitCallbacks(execute=True):
            return self.admin_client.post(f"/api/system/images/{image.pk}/decision/", body, format="json")

    def test_approving_a_photo_that_waited_for_a_moderator_groups_it(self):
        image = self.photo([grouping_tests.a_face(1)])
        self.check(image, "needs_review")
        self.assertFalse(IdentityAnalysis.objects.exists())
        review = ModeratorReview.objects.get(image=image, source="quality")

        response = self.decide(image, "approve")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["quality"]["decision"], "accepted")
        self.assertEqual(IdentityAnalysis.objects.get(image=image).status, "completed")
        self.assertEqual(self.folders().count(), 1)
        self.assertEqual(ModeratorReview.objects.get(pk=review.pk).decided_by_id, self.admin.pk)

    def test_a_photo_the_admin_rejects_is_taken_out_of_its_folder(self):
        image = self.photo([grouping_tests.a_face(1)])
        self.check(image, "accepted")
        (folder,) = self.folders()
        self.assertEqual(folder.member_count, 1)

        self.assertEqual(self.decide(image, "reject", "Not a usable photo").status_code, 200)

        self.assertEqual(IdentityMembership.objects.get(identity_face__image=image).state, IdentityMembership.State.REMOVED)
        self.assertEqual(self.folders().count(), 0)                                                  # the folder was left empty, so it was archived
        self.assertEqual(ImageQualityDecision.objects.get(image=image).decision, "rejected")

    def test_rejecting_a_photo_that_waited_for_a_moderator_groups_nothing(self):
        image = self.photo([grouping_tests.a_face(1)])
        self.check(image, "needs_review")
        self.decide(image, "reject", "No")
        self.assertFalse(IdentityAnalysis.objects.exists())
        self.assertFalse(IdentityGroup.objects.exists())
