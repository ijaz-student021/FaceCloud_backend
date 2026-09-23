"""T-007 Phase D: moderator notifications (assignment + escalation only --
deadline/batch-failure alerts have no field/fan-out infrastructure to hook
into yet, see moderation/services.py's docstring history for why they're
deferred rather than attempted here).
"""
from django.urls import reverse
from django.test import TestCase
from rest_framework import status

from notifications.models import NotificationPreference, UserNotification
from organizations.models import OrganizationMembership
from organizations.services import create_organization

from .tests import ModerationFixtureMixin


class ReviewAssignmentNotificationTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("notif-owner")
        self.organization = create_organization(name="Notify Co", owner=self.owner)
        self.moderator_user = self.make_user("notif-moderator")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.moderator_user,
            role=OrganizationMembership.Role.MODERATOR,
        )
        self.review, _ = self.make_quality_review(self.owner, self.organization)
        self.owner_client = self.authenticated_client(self.owner)

    def assign_url(self):
        return reverse(
            "moderation:review-assign",
            kwargs={"organization_id": self.organization.pk, "review_id": self.review.pk},
        )

    def test_assigning_a_review_notifies_the_assigned_moderator(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.owner_client.post(
                self.assign_url(), {"moderator_id": self.moderator_user.pk}, format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        notification = UserNotification.objects.get(
            recipient=self.moderator_user, event_type=UserNotification.EventType.MODERATOR_REVIEW_ASSIGNED,
        )
        self.assertEqual(notification.resource_id, self.review.pk)

    def test_assigning_to_someone_outside_the_organization_is_rejected(self):
        outsider = self.make_user("notif-outsider")

        response = self.owner_client.post(self.assign_url(), {"moderator_id": outsider.pk}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(UserNotification.objects.filter(recipient=outsider).exists())

    def test_assigning_to_a_plain_member_is_rejected(self):
        member = self.make_user("notif-member")
        OrganizationMembership.objects.create(
            organization=self.organization, user=member, role=OrganizationMembership.Role.MEMBER,
        )

        response = self.owner_client.post(self.assign_url(), {"moderator_id": member.pk}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reassigning_to_the_same_moderator_does_not_duplicate_the_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.owner_client.post(self.assign_url(), {"moderator_id": self.moderator_user.pk}, format="json")
        with self.captureOnCommitCallbacks(execute=True):
            self.owner_client.post(self.assign_url(), {"moderator_id": self.moderator_user.pk}, format="json")

        self.assertEqual(
            UserNotification.objects.filter(
                recipient=self.moderator_user, event_type=UserNotification.EventType.MODERATOR_REVIEW_ASSIGNED,
            ).count(),
            1,
        )

    def test_the_preference_toggle_suppresses_the_notification(self):
        NotificationPreference.objects.update_or_create(
            user=self.moderator_user, defaults={"moderator_review_updates": False},
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.owner_client.post(self.assign_url(), {"moderator_id": self.moderator_user.pk}, format="json")

        self.assertFalse(
            UserNotification.objects.filter(
                recipient=self.moderator_user, event_type=UserNotification.EventType.MODERATOR_REVIEW_ASSIGNED,
            ).exists()
        )

    def test_a_plain_member_cannot_assign_a_review(self):
        member = self.make_user("notif-assigner-member")
        OrganizationMembership.objects.create(
            organization=self.organization, user=member, role=OrganizationMembership.Role.MEMBER,
        )

        response = self.authenticated_client(member).post(
            self.assign_url(), {"moderator_id": self.moderator_user.pk}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ReviewEscalationNotificationTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("escalate-owner")
        self.organization = create_organization(name="Escalate Co", owner=self.owner)
        self.second_owner_user = self.make_user("escalate-second-owner")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.second_owner_user, role=OrganizationMembership.Role.OWNER,
        )
        self.moderator_user = self.make_user("escalate-moderator")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.moderator_user, role=OrganizationMembership.Role.MODERATOR,
        )
        self.review, _ = self.make_quality_review(self.owner, self.organization)

    def escalate_url(self):
        return reverse(
            "moderation:review-escalate",
            kwargs={"organization_id": self.organization.pk, "review_id": self.review.pk},
        )

    def test_escalating_notifies_every_active_owner_but_not_moderators_or_the_escalator(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.authenticated_client(self.moderator_user).post(
                self.escalate_url(), {"reason": "needs a second opinion"}, format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        owner_recipients = set(
            UserNotification.objects.filter(
                event_type=UserNotification.EventType.MODERATOR_REVIEW_ESCALATED,
            ).values_list("recipient_id", flat=True)
        )
        self.assertEqual(owner_recipients, {self.owner.pk, self.second_owner_user.pk})
        self.assertNotIn(self.moderator_user.pk, owner_recipients)

    def test_a_removed_owner_is_not_notified(self):
        OrganizationMembership.objects.filter(organization=self.organization, user=self.second_owner_user).update(
            status=OrganizationMembership.Status.REMOVED,
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.authenticated_client(self.moderator_user).post(self.escalate_url(), {}, format="json")

        self.assertFalse(
            UserNotification.objects.filter(
                recipient=self.second_owner_user, event_type=UserNotification.EventType.MODERATOR_REVIEW_ESCALATED,
            ).exists()
        )
