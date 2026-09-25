"""T-007: an organization's owners and moderators hear when one of its batches fails.

Only a batch that failed entirely alerts them (partial results and cancellations
are routine), the uploader is skipped because they get their own notification,
and the alert is in-app only.
"""
from django.test import TestCase

from notifications.events import notify_batch_completed, notify_moderators_batch_failed
from notifications.models import NotificationPreference, UserNotification
from organizations.models import OrganizationMembership
from organizations.services import create_organization
from uploads.models import BatchImageProgress, ImageUpload, UploadBatch
from uploads.progress import recompute_batch_progress

from .tests import ModerationFixtureMixin

ALERT = UserNotification.EventType.MODERATOR_BATCH_FAILED


class BatchFailureAlertTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("alert-owner")
        self.organization = create_organization(name="Alert Co", owner=self.owner)
        self.uploader = self.make_user("alert-uploader")
        self.member(self.uploader, OrganizationMembership.Role.MEMBER)
        self.moderator = self.make_user("alert-mod")
        self.member(self.moderator, OrganizationMembership.Role.MODERATOR)
        self.batch = UploadBatch.objects.create(
            owner=self.uploader, upload_type="multiple", organization=self.organization,
            processing_status="failed",
        )

    def member(self, user, role, status_value=OrganizationMembership.Status.ACTIVE):
        return OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=role, status=status_value,
        )

    def alerted(self):
        return set(UserNotification.objects.filter(event_type=ALERT).values_list("recipient__username", flat=True))

    def test_owners_and_moderators_are_alerted_but_not_the_uploader_or_plain_members(self):
        bystander = self.make_user("alert-bystander")
        self.member(bystander, OrganizationMembership.Role.MEMBER)

        notify_batch_completed(self.batch.pk, {"failed": 3})

        self.assertEqual(self.alerted(), {"alert-owner", "alert-mod"})
        # The uploader still gets their own notification, and only that one.
        self.assertTrue(UserNotification.objects.filter(
            recipient=self.uploader, event_type=UserNotification.EventType.BATCH_COMPLETED,
        ).exists())
        self.assertFalse(UserNotification.objects.filter(recipient=self.uploader, event_type=ALERT).exists())

    def test_the_alert_points_at_the_batch_and_is_an_error_without_an_email(self):
        notify_batch_completed(self.batch.pk, {"failed": 1})
        alert = UserNotification.objects.get(recipient=self.moderator, event_type=ALERT)
        self.assertEqual(alert.severity, UserNotification.Severity.ERROR)
        self.assertEqual((alert.resource_type, alert.resource_id), ("batch", self.batch.pk))
        self.assertEqual(alert.action_path, "/moderator/batches")
        self.assertEqual(
            alert.metadata, {"batch_id": self.batch.pk, "organization_id": self.organization.pk, "status": "failed"},
        )
        self.assertEqual(alert.email_status, UserNotification.EmailStatus.SKIPPED)

    def test_suspended_or_removed_staff_are_not_alerted(self):
        for name, status_value in (("alert-suspended", "suspended"), ("alert-removed", "removed"), ("alert-invited", "invited")):
            self.member(self.make_user(name), OrganizationMembership.Role.MODERATOR, status_value)
        notify_batch_completed(self.batch.pk, {"failed": 1})
        self.assertEqual(self.alerted(), {"alert-owner", "alert-mod"})

    def test_only_a_fully_failed_batch_alerts(self):
        for status_value in ("completed", "partial", "cancelled", "processing", "queued"):
            UploadBatch.objects.filter(pk=self.batch.pk).update(processing_status=status_value)
            notify_batch_completed(self.batch.pk, {"failed": 1})
        self.assertEqual(self.alerted(), set())

    def test_the_same_failure_alerts_once_but_a_failure_after_a_retry_alerts_again(self):
        notify_batch_completed(self.batch.pk, {"failed": 1})
        notify_batch_completed(self.batch.pk, {"failed": 1})
        self.assertEqual(UserNotification.objects.filter(event_type=ALERT, recipient=self.moderator).count(), 1)

        UploadBatch.objects.filter(pk=self.batch.pk).update(retry_count=1)
        notify_batch_completed(self.batch.pk, {"failed": 1})
        self.assertEqual(UserNotification.objects.filter(event_type=ALERT, recipient=self.moderator).count(), 2)

    def test_the_moderator_preference_switches_it_off_for_that_person_only(self):
        NotificationPreference.objects.update_or_create(user=self.moderator, defaults={"moderator_review_updates": False})
        notify_batch_completed(self.batch.pk, {"failed": 1})
        self.assertEqual(self.alerted(), {"alert-owner"})

    def test_an_uploader_who_is_also_the_owner_is_not_alerted_about_their_own_batch(self):
        batch = UploadBatch.objects.create(
            owner=self.owner, upload_type="single", organization=self.organization, processing_status="failed",
        )
        notify_batch_completed(batch.pk, {"failed": 1})
        recipients = set(UserNotification.objects.filter(
            event_type=ALERT, resource_id=batch.pk,
        ).values_list("recipient__username", flat=True))
        self.assertEqual(recipients, {"alert-mod"})

    def test_a_batch_without_an_organization_is_ignored_safely(self):
        loose = UploadBatch.objects.create(owner=self.uploader, upload_type="single", processing_status="failed")
        self.assertEqual(notify_moderators_batch_failed(loose), [])
        notify_batch_completed(loose.pk, {"failed": 1})
        self.assertFalse(UserNotification.objects.filter(event_type=ALERT, resource_id=loose.pk).exists())

    def test_another_organizations_staff_are_never_alerted(self):
        other_owner = self.make_user("alert-other-owner")
        create_organization(name="Elsewhere", owner=other_owner)
        notify_batch_completed(self.batch.pk, {"failed": 1})
        self.assertNotIn("alert-other-owner", self.alerted())

    def test_it_fires_when_the_real_progress_calculation_finds_every_image_failed(self):
        batch = UploadBatch.objects.create(
            owner=self.uploader, upload_type="multiple", organization=self.organization,
            processing_status="processing",
        )
        for name in ("a.jpg", "b.jpg"):
            image = ImageUpload.objects.create(owner=self.uploader, batch=batch, original_name=name, status="uploaded")
            BatchImageProgress.objects.filter(image=image).update(status="failed", failure_reason="boom")

        summary = recompute_batch_progress(batch.pk)

        self.assertEqual(summary["status"], "failed")
        recipients = set(UserNotification.objects.filter(
            event_type=ALERT, resource_id=batch.pk,
        ).values_list("recipient__username", flat=True))
        self.assertEqual(recipients, {"alert-owner", "alert-mod"})

    def test_a_partially_failed_batch_from_the_real_calculation_does_not_alert(self):
        batch = UploadBatch.objects.create(
            owner=self.uploader, upload_type="multiple", organization=self.organization,
            processing_status="processing",
        )
        for name, state in (("a.jpg", "failed"), ("b.jpg", "completed")):
            image = ImageUpload.objects.create(owner=self.uploader, batch=batch, original_name=name, status="uploaded")
            BatchImageProgress.objects.filter(image=image).update(status=state)

        summary = recompute_batch_progress(batch.pk)

        self.assertEqual(summary["status"], "partial")
        self.assertFalse(UserNotification.objects.filter(event_type=ALERT, resource_id=batch.pk).exists())
