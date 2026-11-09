"""The member is told what a moderator decided about their photograph: approve, reject and correct notify; keep_in_review does not."""
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from notifications.models import NotificationPreference, UserNotification
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization

from .member_messages import APPROVED_DEFAULT, REASON_SENTENCES, REJECTED_DEFAULT
from .models import ModeratorReview
from .tests import ModerationFixtureMixin


class NotifyTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("notify-owner")
        self.organization = create_organization(name="Notify Co", owner=self.owner, moderator_data_scope="organization")
        self.moderator = self.make_user("notify-mod")
        OrganizationMembership.objects.create(organization=self.organization, user=self.moderator, role="moderator", status="active")
        self.member = self.make_user("notify-member")
        OrganizationMembership.objects.create(organization=self.organization, user=self.member, role="member", status="active", invited_by=self.moderator)
        self.client = self.authenticated_client(self.moderator)

    def review(self, member=None, reasons=None):
        review, decision = self.make_quality_review(member or self.member, self.organization)
        if reasons is not None:
            decision.rejection_reasons = reasons
            decision.save()
        return review

    def decide(self, review, action, **body):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("moderation:review-decision", kwargs={"organization_id": self.organization.pk, "review_id": review.pk}),
                                        {"action": action, **body}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def told(self, user=None, event_type=None):
        rows = UserNotification.objects.filter(recipient=user or self.member, event_type__in=[
            "photo_review_approved", "photo_review_rejected"] if event_type is None else [event_type])
        return list(rows)

    def test_approving_tells_the_member_in_plain_words_and_the_audit_trail_records_it(self):
        review = self.review()
        self.decide(review, "approve", reason="internal: face is fine", notes="internal notes")
        (note,) = self.told()
        self.assertEqual((note.event_type, note.severity, note.title), ("photo_review_approved", "success", "Your photo was approved"))
        self.assertEqual((note.message, note.action_path, note.resource_type, note.resource_id), (APPROVED_DEFAULT, f"/dashboard/images/{review.image_id}", "image", review.image_id))
        event = OrganizationAuditEvent.objects.get(action="member_notified_of_review")
        self.assertEqual((event.target_type, event.target_id, event.actor_id, event.new_value), ("ModeratorReview", review.pk, self.moderator.pk, {"outcome": "approved"}))

    def test_the_internal_reason_and_notes_never_reach_the_member(self):
        review = self.review()
        self.decide(review, "reject", reason="SECRET-REASON looks like a screenshot", notes="SECRET-NOTES call the uploader")
        (note,) = self.told()
        everything = f"{note.title} {note.message} {note.metadata}"
        self.assertNotIn("SECRET", everything)

    def test_rejecting_with_a_message_uses_it_and_without_one_uses_the_sentence_for_the_reason_code(self):
        typed = self.review()
        self.decide(typed, "reject", message_to_member="Please retake this one in daylight.")
        self.assertEqual(self.told()[0].message, "Please retake this one in daylight.")
        self.assertEqual((self.told()[0].event_type, self.told()[0].severity, self.told()[0].title),
                         ("photo_review_rejected", "warning", "Your photo was not accepted"))
        UserNotification.objects.all().delete()
        standard = self.review(reasons=["severe_blur", "borderline_resolution"])
        self.decide(standard, "reject")
        self.assertEqual(self.told()[0].message, REASON_SENTENCES["severe_blur"])
        UserNotification.objects.all().delete()
        unknown = self.review(reasons=["a_code_nobody_wrote_a_sentence_for"])
        self.decide(unknown, "reject")
        self.assertEqual(self.told()[0].message, REJECTED_DEFAULT)

    def test_every_reason_code_the_checks_can_produce_has_a_sentence(self):
        produced = {"face_too_small", "group_face_too_small", "severe_blur", "borderline_blur", "resolution_too_low", "borderline_resolution",
                    "severe_underexposure", "underexposed", "severe_overexposure", "overexposed", "contrast_too_low", "borderline_contrast",
                    "excessive_noise", "overall_score_below_acceptance", "no_face_detected", "large_group_detected", "face_off_center",
                    "face_validation_rejected", "face_validation_required", "detector_failed"}
        self.assertEqual(produced - set(REASON_SENTENCES), set())

    def test_correct_is_an_approval_the_moderator_adjusted(self):
        self.decide(self.review(), "correct")
        self.assertEqual(self.told()[0].event_type, "photo_review_approved")

    def test_correct_tells_the_member_in_the_moderators_own_words(self):
        review = self.review()
        self.decide(review, "correct", reason="internal: wrong person", message_to_member="I fixed the details on this photo.")
        (note,) = self.told()
        self.assertEqual((note.event_type, note.severity, note.title), ("photo_review_approved", "success", "Your photo was approved"))
        self.assertEqual(note.message, "I fixed the details on this photo.")
        self.assertEqual((note.recipient_id, note.resource_type, note.resource_id, note.action_path),
                         (self.member.pk, "image", review.image_id, f"/dashboard/images/{review.image_id}"))
        self.assertEqual(note.metadata["review_id"], review.pk)
        self.assertNotIn("internal", f"{note.title} {note.message} {note.metadata}")
        event = OrganizationAuditEvent.objects.get(action="member_notified_of_review")
        self.assertEqual((event.target_id, event.new_value), (review.pk, {"outcome": "approved"}))

    def test_correct_without_a_message_uses_the_standard_sentence(self):
        self.decide(self.review(), "correct")
        self.assertEqual(self.told()[0].message, APPROVED_DEFAULT)

    def test_correct_is_told_once_even_if_the_same_decision_is_repeated(self):
        review = self.review()
        self.decide(review, "correct", message_to_member="Done.")
        response = self.client.post(reverse("moderation:review-decision", kwargs={"organization_id": self.organization.pk, "review_id": review.pk}),
                                    {"action": "correct"}, format="json")
        self.assertEqual(response.status_code, 409)                                  # a decided review cannot be decided again
        self.assertEqual(len(self.told()), 1)

    def test_correct_follows_the_same_switches_as_approve(self):
        NotificationPreference.objects.create(user=self.member, photo_accepted=False)
        self.decide(self.review(), "correct", message_to_member="Done.")
        self.assertEqual(self.told(), [])
        self.assertFalse(OrganizationAuditEvent.objects.filter(action="member_notified_of_review").exists())
        NotificationPreference.objects.filter(user=self.member).update(photo_accepted=True, photo_rejected=False)
        self.decide(self.review(), "correct", message_to_member="Done again.")
        self.assertEqual(len(self.told()), 1)                                        # "photo_rejected" off does not silence a correction

    @override_settings(NOTIFICATION_EMAILS_ENABLED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_correct_sends_the_email_copy_when_the_member_wants_emails(self):
        NotificationPreference.objects.create(user=self.member, email_notifications=True)
        self.decide(self.review(), "correct", message_to_member="Corrected for you.")
        (note,) = self.told()
        self.assertEqual(note.email_status, "sent")
        self.assertIn("Corrected for you.", mail.outbox[-1].body)

    def test_correct_on_a_duplicate_review_tells_the_member_too(self):
        review, _match = self.make_duplicate_review(self.member, self.organization)
        self.decide(review, "correct", message_to_member="Not a copy, it is fine.")
        (note,) = self.told()
        self.assertEqual(note.message, "Not a copy, it is fine.")

    def test_correct_on_the_moderators_own_photograph_notifies_nobody(self):
        self.decide(self.review(member=self.moderator), "correct")
        self.assertEqual(UserNotification.objects.filter(event_type__in=["photo_review_approved", "photo_review_rejected"]).count(), 0)

    def test_keep_in_review_tells_nobody_and_the_final_decision_still_does(self):
        review = self.review()
        self.decide(review, "keep_in_review", message_to_member="still looking")
        self.assertEqual(self.told(), [])
        self.assertFalse(OrganizationAuditEvent.objects.filter(action="member_notified_of_review").exists())
        self.decide(review, "approve")
        self.assertEqual(len(self.told()), 1)

    def test_the_members_preferences_are_respected(self):
        preferences = NotificationPreference.objects.create(user=self.member, photo_accepted=False)
        self.decide(self.review(), "approve")
        self.assertEqual(self.told(), [])
        self.assertFalse(OrganizationAuditEvent.objects.filter(action="member_notified_of_review").exists())      # nothing was sent, so nothing is recorded
        self.decide(self.review(), "reject")
        self.assertEqual(len(self.told()), 1)                                                                     # photo_rejected is still on
        NotificationPreference.objects.filter(pk=preferences.pk).update(photo_accepted=True, photo_rejected=False)
        UserNotification.objects.all().delete()
        self.decide(self.review(), "reject")
        self.assertEqual(self.told(), [])

    @override_settings(NOTIFICATION_EMAILS_ENABLED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_the_email_copy_goes_out_when_the_member_wants_emails(self):
        NotificationPreference.objects.create(user=self.member, email_notifications=True)
        self.decide(self.review(), "approve", message_to_member="Looks good, thank you.")
        (note,) = self.told()
        self.assertEqual(note.email_status, "sent")
        self.assertEqual(mail.outbox[-1].to, [self.member.email])
        self.assertIn("Looks good, thank you.", mail.outbox[-1].body)

    def test_no_email_copy_when_the_member_has_not_asked(self):
        self.decide(self.review(), "approve")
        self.assertEqual(self.told()[0].email_status, "skipped")

    def test_a_bulk_decision_tells_each_member_once_about_their_own_photo(self):
        other = self.make_user("notify-other-member")
        OrganizationMembership.objects.create(organization=self.organization, user=other, role="member", status="active")
        reviews = [self.review(), self.review(), self.review(other)]
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("moderation:review-bulk-decision", kwargs={"organization_id": self.organization.pk}),
                                        {"review_ids": [r.pk for r in reviews], "action": "approve", "message_to_member": "All fine."}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.told(self.member)), 2)                       # one per photograph, not one per member
        self.assertEqual(len(self.told(other)), 1)
        self.assertEqual({n.message for n in self.told(self.member) + self.told(other)}, {"All fine."})
        self.assertEqual(OrganizationAuditEvent.objects.filter(action="member_notified_of_review").count(), 3)

    def test_a_bulk_escalation_tells_nobody(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("moderation:review-bulk-decision", kwargs={"organization_id": self.organization.pk}),
                             {"review_ids": [self.review().pk], "action": "escalate"}, format="json")
        self.assertEqual(self.told(), [])

    def test_the_moderators_own_photograph_notifies_nobody(self):
        own = self.review(member=self.moderator)
        self.decide(own, "approve")
        self.assertEqual(UserNotification.objects.filter(event_type__in=["photo_review_approved", "photo_review_rejected"]).count(), 0)

    def test_the_moderator_who_decided_is_not_notified(self):
        self.decide(self.review(), "approve")
        self.assertEqual(self.told(self.moderator), [])

    def test_the_message_is_limited_to_500_characters(self):
        review = self.review()
        response = self.client.post(reverse("moderation:review-decision", kwargs={"organization_id": self.organization.pk, "review_id": review.pk}),
                                    {"action": "approve", "message_to_member": "x" * 501}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ModeratorReview.objects.get(pk=review.pk).status, "pending")

    def test_the_detail_returns_what_the_member_was_told(self):
        review = self.review()
        self.decide(review, "reject", message_to_member="Too dark, sorry.")
        body = self.client.get(reverse("moderation:review-detail", kwargs={"organization_id": self.organization.pk, "review_id": review.pk})).json()
        self.assertEqual(body["message_to_member"], "Too dark, sorry.")

    def test_another_organizations_moderator_can_neither_trigger_nor_read_anything(self):
        review = self.review()
        elsewhere_owner = self.make_user("elsewhere-notify-owner")
        elsewhere = create_organization(name="Elsewhere Notify Co", owner=elsewhere_owner)
        stranger = self.make_user("elsewhere-notify-mod")
        OrganizationMembership.objects.create(organization=elsewhere, user=stranger, role="moderator", status="active")
        stranger_client = self.authenticated_client(stranger)
        for organization_id in (self.organization.pk, elsewhere.pk):
            response = stranger_client.post(reverse("moderation:review-decision", kwargs={"organization_id": organization_id, "review_id": review.pk}),
                                            {"action": "approve"}, format="json")
            self.assertEqual(response.status_code, 404, organization_id)
        self.assertEqual(UserNotification.objects.count(), 0)
        self.decide(review, "approve")
        listed = stranger_client.get("/api/notifications/").json()
        rows = listed["results"] if isinstance(listed, dict) else listed
        self.assertEqual([row for row in rows if row["event_type"].startswith("photo_review")], [])

    def test_a_member_sees_it_in_their_own_notifications(self):
        self.decide(self.review(), "approve")
        member_client = self.authenticated_client(self.member)
        listed = member_client.get("/api/notifications/").json()
        rows = listed["results"] if isinstance(listed, dict) else listed
        row = next(r for r in rows if r["event_type"] == "photo_review_approved")
        self.assertEqual((row["title"], row["severity"]), ("Your photo was approved", "success"))
