"""Feature 46: a member who can no longer moderate must not keep review work.

Removing, suspending or demoting a moderator returns their undecided reviews to
the queue, so they are not left assigned to someone who cannot act on them.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization

from .models import ModeratorReview
from .tests import ModerationFixtureMixin


class ReleaseReviewsOnMemberChangeTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("release-owner")
        self.organization = create_organization(name="Release Co", owner=self.owner)
        self.moderator = self.make_user("release-mod")
        self.membership = OrganizationMembership.objects.create(
            organization=self.organization, user=self.moderator,
            role=OrganizationMembership.Role.MODERATOR,
        )
        self.other_moderator = self.make_user("release-other-mod")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.other_moderator,
            role=OrganizationMembership.Role.MODERATOR,
        )
        self.client = self.authenticated_client(self.owner)
        self.pending = self.assigned_review(ModeratorReview.Status.PENDING)
        self.in_review = self.assigned_review(ModeratorReview.Status.IN_REVIEW)
        self.escalated = self.assigned_review(ModeratorReview.Status.ESCALATED)
        self.approved = self.assigned_review(ModeratorReview.Status.APPROVED)
        self.others = self.assigned_review(ModeratorReview.Status.IN_REVIEW, user=self.other_moderator)

    def assigned_review(self, status_value, user=None, organization=None):
        organization = organization or self.organization
        review, _decision = self.make_quality_review(self.owner, organization)
        ModeratorReview.objects.filter(pk=review.pk).update(
            status=status_value, assigned_moderator=user or self.moderator,
        )
        return review

    def member_url(self):
        return reverse(
            "organizations:organization-member-detail",
            kwargs={"organization_id": self.organization.pk, "membership_id": self.membership.pk},
        )

    def state(self, review):
        review.refresh_from_db()
        return review.status, review.assigned_moderator_id

    def assert_open_reviews_released(self):
        self.assertEqual(self.state(self.pending), (ModeratorReview.Status.PENDING, None))
        # Work in progress goes back to the queue; an escalation still reaches an owner.
        self.assertEqual(self.state(self.in_review), (ModeratorReview.Status.PENDING, None))
        self.assertEqual(self.state(self.escalated), (ModeratorReview.Status.ESCALATED, None))
        # Decided reviews are history, and other people's work is untouched.
        self.assertEqual(self.state(self.approved), (ModeratorReview.Status.APPROVED, self.moderator.pk))
        self.assertEqual(self.state(self.others), (ModeratorReview.Status.IN_REVIEW, self.other_moderator.pk))

    def test_removing_a_moderator_returns_their_open_reviews_to_the_queue(self):
        response = self.client.delete(self.member_url(), {"reason": "left the team"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_open_reviews_released()
        event = OrganizationAuditEvent.objects.get(action=OrganizationAuditEvent.Action.MEMBER_REMOVED)
        self.assertEqual(event.new_value["released_reviews"], 3)

    def test_suspending_a_moderator_releases_their_reviews(self):
        response = self.client.patch(self.member_url(), {"status": "suspended"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_open_reviews_released()
        event = OrganizationAuditEvent.objects.get(action=OrganizationAuditEvent.Action.MEMBER_STATUS_CHANGED)
        self.assertEqual(event.new_value["released_reviews"], 3)

    def test_demoting_a_moderator_to_member_releases_their_reviews(self):
        response = self.client.patch(self.member_url(), {"role": "member"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_open_reviews_released()

    def test_a_change_that_keeps_them_a_moderator_keeps_their_reviews(self):
        response = self.client.patch(self.member_url(), {"role": "owner"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.state(self.in_review), (ModeratorReview.Status.IN_REVIEW, self.moderator.pk))
        event = OrganizationAuditEvent.objects.get(action=OrganizationAuditEvent.Action.MEMBER_ROLE_CHANGED)
        self.assertNotIn("released_reviews", event.new_value)

    def test_reactivating_does_not_hand_the_reviews_back(self):
        self.client.patch(self.member_url(), {"status": "suspended"}, format="json")
        self.client.patch(self.member_url(), {"status": "active"}, format="json")
        self.assertEqual(self.state(self.pending), (ModeratorReview.Status.PENDING, None))

    def test_only_this_organizations_reviews_are_released(self):
        second = create_organization(name="Second Co", owner=self.owner)
        OrganizationMembership.objects.create(
            organization=second, user=self.moderator, role=OrganizationMembership.Role.MODERATOR,
        )
        elsewhere = self.assigned_review(ModeratorReview.Status.IN_REVIEW, organization=second)
        self.client.delete(self.member_url(), {}, format="json")
        self.assertEqual(self.state(elsewhere), (ModeratorReview.Status.IN_REVIEW, self.moderator.pk))

    def test_removing_a_member_with_no_reviews_reports_zero(self):
        member = self.make_user("release-plain")
        membership = OrganizationMembership.objects.create(
            organization=self.organization, user=member, role=OrganizationMembership.Role.MEMBER,
        )
        url = reverse(
            "organizations:organization-member-detail",
            kwargs={"organization_id": self.organization.pk, "membership_id": membership.pk},
        )
        response = self.client.delete(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        event = OrganizationAuditEvent.objects.filter(
            action=OrganizationAuditEvent.Action.MEMBER_REMOVED, target_id=membership.pk,
        ).get()
        self.assertEqual(event.new_value["released_reviews"], 0)

    def test_released_reviews_show_up_as_unassigned_in_the_queue(self):
        self.client.delete(self.member_url(), {}, format="json")
        listing = self.client.get(
            reverse("moderation:review-list", kwargs={"organization_id": self.organization.pk}),
        )
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        rows = {row["id"]: row for row in listing.data}
        self.assertIsNone(rows[self.pending.pk]["assigned_moderator"])
        self.assertIsNone(rows[self.in_review.pk]["assigned_moderator"])
        self.assertEqual(rows[self.in_review.pk]["status"], "pending")
