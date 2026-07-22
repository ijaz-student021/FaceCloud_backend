from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User

from .events import notify_report_result
from .models import NotificationPreference, UserNotification
from .services import create_user_notification, get_notification_preferences
from .tasks import cleanup_expired_notifications, deliver_notification_email


class NotificationApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="notification-user",
            email="notification@example.com",
            password="StrongPass123!",
            is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="other-notification-user",
            email="other-notification@example.com",
            password="StrongPass123!",
            is_email_verified=True,
        )
        self.client.force_authenticate(self.user)

    def create_notification(self, user=None, key="event-1", event_type=None):
        notification, created = create_user_notification(
            recipient=user or self.user,
            event_type=event_type or UserNotification.EventType.UPLOAD_RECEIVED,
            idempotency_key=key,
            title="Upload received",
            message="FaceCloud received one image.",
            resource_type="batch",
            resource_id=12,
            action_path="/dashboard/uploads",
            metadata={
                "batch_id": 12,
                "status": "uploaded",
                "storage_path": "must-not-be-exposed",
            },
        )
        self.assertTrue(created)
        return notification

    def test_preferences_are_created_with_safe_defaults_and_can_be_updated(self):
        response = self.client.get(reverse("notification-preferences"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["photo_rejected"])
        self.assertFalse(response.data["email_notifications"])

        response = self.client.patch(
            reverse("notification-preferences"),
            {"email_notifications": True, "live_session_alerts": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["email_notifications"])
        self.assertTrue(response.data["live_session_alerts"])

    def test_creation_is_idempotent_and_filters_unsafe_metadata(self):
        first = self.create_notification()
        second, created = create_user_notification(
            recipient=self.user,
            event_type=UserNotification.EventType.UPLOAD_RECEIVED,
            idempotency_key="event-1",
            title="Changed title",
            message="Changed message",
        )
        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)
        self.assertNotIn("storage_path", first.metadata)
        self.assertEqual(UserNotification.objects.count(), 1)

    def test_disabled_event_preference_prevents_notification(self):
        preferences = get_notification_preferences(self.user)
        preferences.photo_rejected = False
        preferences.save(update_fields=["photo_rejected", "updated_at"])
        notification, created = create_user_notification(
            recipient=self.user,
            event_type=UserNotification.EventType.PHOTO_REJECTED,
            idempotency_key="image:1:rejected",
            title="Photo rejected",
            message="The image did not pass its checks.",
        )
        self.assertIsNone(notification)
        self.assertFalse(created)

    def test_list_filters_and_unread_count_are_owner_scoped(self):
        own = self.create_notification()
        self.create_notification(user=self.other, key="other-event")
        response = self.client.get(reverse("notification-list"), {"unread": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], own.pk)

        response = self.client.get(reverse("notification-unread-count"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["unread_count"], 1)

    def test_invalid_list_filters_are_rejected(self):
        response = self.client.get(reverse("notification-list"), {"unread": "sometimes"})
        self.assertEqual(response.status_code, 400)
        response = self.client.get(reverse("notification-list"), {"event_type": "secret"})
        self.assertEqual(response.status_code, 400)

    def test_read_mark_all_read_and_delete_actions(self):
        first = self.create_notification(key="first")
        second = self.create_notification(key="second")
        response = self.client.post(reverse("notification-mark-read", kwargs={"pk": first.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["is_read"])

        response = self.client.post(reverse("notification-mark-all-read"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["marked_read"], 1)
        second.refresh_from_db()
        self.assertIsNotNone(second.read_at)

        response = self.client.delete(reverse("notification-detail", kwargs={"pk": first.pk}))
        self.assertEqual(response.status_code, 204)

    def test_other_users_notification_returns_not_found(self):
        notification = self.create_notification(user=self.other, key="other-protected")
        response = self.client.get(
            reverse("notification-detail", kwargs={"pk": notification.pk})
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            reverse("notification-mark-read", kwargs={"pk": notification.pk})
        )
        self.assertEqual(response.status_code, 404)

    @override_settings(
        NOTIFICATION_EMAILS_ENABLED=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_permitted_notification_email_is_delivered(self):
        preferences = get_notification_preferences(self.user)
        preferences.email_notifications = True
        preferences.save(update_fields=["email_notifications", "updated_at"])
        with self.captureOnCommitCallbacks(execute=False):
            notification, _ = create_user_notification(
                recipient=self.user,
                event_type=UserNotification.EventType.PHOTO_ACCEPTED,
                idempotency_key="image:2:accepted",
                title="Photo accepted",
                message="Your photo passed all checks.",
                action_path="/dashboard/images",
            )
        self.assertEqual(notification.email_status, UserNotification.EmailStatus.QUEUED)
        result = deliver_notification_email(notification.pk)
        self.assertEqual(result, UserNotification.EmailStatus.SENT)
        notification.refresh_from_db()
        self.assertEqual(notification.email_status, UserNotification.EmailStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("/media/", mail.outbox[0].body)

    @override_settings(NOTIFICATION_EMAILS_ENABLED=True)
    def test_email_is_skipped_when_global_user_preference_is_disabled(self):
        notification = self.create_notification(
            key="accepted-without-email",
            event_type=UserNotification.EventType.PHOTO_ACCEPTED,
        )
        self.assertEqual(notification.email_status, UserNotification.EmailStatus.SKIPPED)

    @override_settings(NOTIFICATION_RETENTION_DAYS=30)
    def test_cleanup_deletes_only_old_read_notifications(self):
        old_read = self.create_notification(key="old-read")
        old_unread = self.create_notification(key="old-unread")
        recent_read = self.create_notification(key="recent-read")
        old = timezone.now() - timedelta(days=31)
        UserNotification.objects.filter(pk=old_read.pk).update(read_at=old)
        UserNotification.objects.filter(pk=old_unread.pk).update(created_at=old)
        UserNotification.objects.filter(pk=recent_read.pk).update(read_at=timezone.now())
        deleted = cleanup_expired_notifications()
        self.assertEqual(deleted, 1)
        self.assertFalse(UserNotification.objects.filter(pk=old_read.pk).exists())
        self.assertTrue(UserNotification.objects.filter(pk=old_unread.pk).exists())
        self.assertTrue(UserNotification.objects.filter(pk=recent_read.pk).exists())

    def test_account_deletion_cascades_preferences_and_notifications(self):
        self.create_notification()
        get_notification_preferences(self.user)
        user_id = self.user.pk
        self.user.delete()
        self.assertFalse(NotificationPreference.objects.filter(user_id=user_id).exists())
        self.assertFalse(UserNotification.objects.filter(recipient_id=user_id).exists())


class NotificationEventTests(APITestCase):
    def test_report_ready_event_is_supported_without_exposing_payload(self):
        from reports.models import ProcessingReport
        from uploads.models import ImageUpload, UploadBatch

        user = User.objects.create_user(
            username="report-notification-user",
            email="report-notification@example.com",
            password="StrongPass123!",
        )
        batch = UploadBatch.objects.create(owner=user, upload_type="single")
        image = ImageUpload.objects.create(
            owner=user,
            batch=batch,
            original_name="portrait.jpg",
            status="uploaded",
        )
        report = ProcessingReport.objects.create(
            owner=user,
            report_type=ProcessingReport.ReportType.IMAGE,
            format=ProcessingReport.Format.PDF,
            status=ProcessingReport.Status.READY,
            image=image,
            payload={"private": "must-not-appear"},
        )
        notify_report_result(report)
        notification = UserNotification.objects.get(recipient=user)
        self.assertEqual(notification.event_type, UserNotification.EventType.REPORT_READY)
        self.assertNotIn("private", notification.metadata)
