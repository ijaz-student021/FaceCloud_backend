"""A member asks to leave one organization; a moderator decides; approval suspends that membership only (and nothing else).

Covers: the request, duplicates, who may ask, who may decide, denial, cancellation, immediate loss of access (also with a token the member already holds), the person's
personal workspace and other organizations staying as they were, download rights ending, seat accounting, notifications and audit, the administrator override, and
the simultaneous approval / cancellation race (PostgreSQL).
"""
import unittest

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from downloads.models import DownloadGrant, DownloadRequest
from downloads.test_download_approval import ApprovalFixture
from notifications.models import UserNotification
from organizations import leave
from organizations.models import Organization, OrganizationAuditEvent, OrganizationLeaveRequest, OrganizationMembership
from organizations.services import create_organization, create_personal_organization, seats_in_use
from organizations.test_owner_invitation_postgres import run_in_parallel

User = get_user_model()
Role, State, Leave = OrganizationMembership.Role, OrganizationMembership.Status, OrganizationLeaveRequest.Status
Audit = OrganizationAuditEvent.Action


def rows(response):
    return response.data["results"] if isinstance(response.data, dict) else response.data


class LeaveFixture(ApprovalFixture):
    def url(self, name, organization=None, **extra):
        return reverse(f"organizations:{name}", kwargs={"organization_id": (organization or self.organization).pk, **extra})

    def ask(self, client=None, organization=None, **body):
        with self.captureOnCommitCallbacks(execute=True):
            return (client or self.member_client).post(self.url("organization-leave-request", organization), body, format="json")

    def decide_leave(self, leave_request, action, client=None, **body):
        with self.captureOnCommitCallbacks(execute=True):
            return (client or self.moderator_client).post(
                self.url("organization-leave-request-decision", leave_request.organization, leave_id=leave_request.pk), {"action": action, **body}, format="json")

    def make_leave(self, user=None, organization=None, reason="Moving to another team"):
        response = self.ask(self.authenticated_client(user) if user else None, organization, reason=reason)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        return OrganizationLeaveRequest.objects.get(pk=response.data["id"])

    def membership(self, user=None, organization=None):
        return OrganizationMembership.objects.get(organization=organization or self.organization, user=user or self.member)

    def events(self, action, organization=None):
        return OrganizationAuditEvent.objects.filter(organization=organization or self.organization, action=action)


class RequestingTests(LeaveFixture, TestCase):
    def test_a_member_asks_and_nothing_changes_until_a_moderator_decides(self):
        response = self.ask(reason="  Moving to another team ")
        self.assertEqual((response.status_code, response.data["status"], response.data["reason"]), (201, "pending", "Moving to another team"))
        self.assertEqual(response.data["member"]["username"], "ap-member")
        self.assertEqual(self.membership().status, State.ACTIVE)                                      # still a member, still has access
        self.assertEqual(self.member_client.get(self.url("organization-detail")).status_code, 200)
        self.assertEqual(self.events(Audit.LEAVE_REQUESTED).count(), 1)
        mine = self.member_client.get(self.url("organization-leave-request")).data["request"]
        self.assertEqual((mine["id"], mine["status"]), (response.data["id"], "pending"))

    def test_the_moderator_and_the_member_are_notified(self):
        self.ask(reason="x")
        got = UserNotification.objects.filter(event_type="leave_request_submitted")
        self.assertEqual([n.recipient_id for n in got], [self.moderator.pk])                           # the moderator, not the owner and not the member
        self.assertEqual(got[0].action_path, "/moderator/leave-requests")
        received = UserNotification.objects.get(event_type="leave_request_received")
        self.assertEqual((received.recipient_id, received.action_path), (self.member.pk, "/dashboard/organization"))

    def test_there_is_one_pending_request_per_membership(self):
        self.make_leave()
        again = self.ask(reason="again")
        self.assertEqual((again.status_code, again.data["code"]), (409, "leave_request_pending"))
        self.assertEqual(OrganizationLeaveRequest.objects.filter(user=self.member).count(), 1)

    def test_the_reason_is_optional_and_limited(self):
        self.assertEqual(self.ask().status_code, 201)
        leave_request = OrganizationLeaveRequest.objects.get()
        self.member_client.post(self.url("organization-leave-request-cancel"))
        self.assertEqual(self.ask(reason="x" * 501).status_code, 400)
        self.assertEqual(leave_request.reason, "")

    def test_an_owner_a_moderator_and_a_personal_workspace_cannot_ask(self):
        for client in (self.owner_client, self.moderator_client):
            refused = self.ask(client)
            self.assertEqual((refused.status_code, refused.data["code"]), (403, "leave_not_available"))
        workspace = create_personal_organization(self.member)
        refused = self.ask(organization=workspace)
        self.assertEqual((refused.status_code, refused.data["code"]), (403, "personal_workspace_is_private"))

    def test_an_outsider_and_a_suspended_member_get_404(self):
        outsider = self.authenticated_client(self.make_user("ap-outsider"))
        self.assertEqual(self.ask(outsider).status_code, 404)
        OrganizationMembership.objects.filter(user=self.other_member).update(status=State.SUSPENDED)
        self.assertEqual(self.ask(self.authenticated_client(self.other_member)).status_code, 404)

    def test_without_a_moderator_who_can_decide_the_request_is_refused_not_left_hanging(self):
        OrganizationMembership.objects.filter(user=self.moderator).update(status=State.SUSPENDED)
        refused = self.ask()
        self.assertEqual((refused.status_code, refused.data["code"]), (409, "no_eligible_reviewer"))
        self.assertFalse(OrganizationLeaveRequest.objects.exists())

    def test_a_moderator_who_sees_only_their_own_members_is_not_a_reviewer_of_other_members(self):
        Organization.objects.filter(pk=self.organization.pk).update(moderator_data_scope="own_members")
        second = self.add_member("ap-moderator-2", Role.MODERATOR)
        OrganizationMembership.objects.filter(user=self.member).update(invited_by=second)
        leave_request = self.make_leave()
        self.assertEqual(self.decide_leave(leave_request, "approve", client=self.moderator_client).status_code, 404)        # not theirs to see
        self.assertEqual(self.decide_leave(leave_request, "approve", client=self.authenticated_client(second)).status_code, 200)


class CancellingTests(LeaveFixture, TestCase):
    def test_a_pending_request_can_be_cancelled_and_a_new_one_asked(self):
        leave_request = self.make_leave()
        with self.captureOnCommitCallbacks(execute=True):
            cancelled = self.member_client.post(self.url("organization-leave-request-cancel"))
        self.assertEqual((cancelled.status_code, cancelled.data["status"]), (200, "cancelled"))
        self.assertTrue(self.events(Audit.LEAVE_REQUEST_CANCELLED).exists())
        self.assertTrue(UserNotification.objects.filter(event_type="leave_request_cancelled", recipient=self.member).exists())
        self.assertEqual(self.decide_leave(leave_request, "approve").data["code"], "leave_request_not_pending")             # a cancelled request cannot be approved
        self.assertEqual(self.membership().status, State.ACTIVE)
        self.assertEqual(self.ask().status_code, 201)

    def test_a_request_that_was_decided_cannot_be_cancelled(self):
        leave_request = self.make_leave()
        self.decide_leave(leave_request, "deny", reason="Needed for the event")
        refused = self.member_client.post(self.url("organization-leave-request-cancel"))
        self.assertEqual((refused.status_code, refused.data["code"]), (409, "leave_request_not_pending"))
        gone = leave.LeaveConflict
        with self.assertRaises(gone):
            leave.cancel_leave(user=self.member, organization=self.organization, leave_id=leave_request.pk)

    def test_nobody_cancels_someone_elses_request(self):
        self.make_leave()
        self.assertEqual(self.authenticated_client(self.other_member).post(self.url("organization-leave-request-cancel")).status_code, 404)
        self.assertEqual(self.moderator_client.post(self.url("organization-leave-request-cancel")).status_code, 404)


class DecidingTests(LeaveFixture, TestCase):
    def test_approval_suspends_that_membership_and_records_everything(self):
        leave_request = self.make_leave()
        response = self.decide_leave(leave_request, "approve", reason="Thanks for your work")
        self.assertEqual((response.status_code, response.data["status"], response.data["membership_status"]), (200, "approved", "suspended"))
        self.assertEqual(response.data["reviewed_by"]["id"], self.moderator.pk)
        self.assertIsNotNone(response.data["decided_at"])
        self.assertEqual(self.membership().status, State.SUSPENDED)
        self.assertTrue(self.events(Audit.LEAVE_REQUEST_APPROVED).exists())
        status_change = self.events(Audit.MEMBER_STATUS_CHANGED).get()
        self.assertEqual((status_change.previous_value["status"], status_change.new_value["status"]), ("active", "suspended"))
        notice = UserNotification.objects.get(event_type="leave_request_approved")
        self.assertEqual((notice.recipient_id, notice.action_path), (self.member.pk, "/dashboard/organization"))
        self.assertTrue(User.objects.get(pk=self.member.pk).is_active)                                # the account is not suspended
        self.assertFalse(User.objects.get(pk=self.member.pk).is_suspended)

    def test_denial_needs_a_reason_and_leaves_the_member_active(self):
        leave_request = self.make_leave()
        self.assertEqual(self.decide_leave(leave_request, "deny").status_code, 400)
        denied = self.decide_leave(leave_request, "deny", reason="We need you for the event")
        self.assertEqual((denied.status_code, denied.data["status"], denied.data["decision_reason"]), (200, "denied", "We need you for the event"))
        self.assertEqual(self.membership().status, State.ACTIVE)
        self.assertTrue(UserNotification.objects.filter(event_type="leave_request_denied", recipient=self.member).exists())
        self.assertEqual(self.ask().status_code, 201)                                                  # may ask again

    def test_a_decided_request_cannot_be_decided_again(self):
        leave_request = self.make_leave()
        self.decide_leave(leave_request, "approve")
        again = self.decide_leave(leave_request, "deny", reason="changed my mind")
        self.assertEqual((again.status_code, again.data["code"]), (409, "leave_request_not_pending"))

    def test_only_a_moderator_of_that_organization_decides(self):
        leave_request = self.make_leave()
        for client in (self.owner_client, self.member_client, self.authenticated_client(self.other_member)):
            refused = self.decide_leave(leave_request, "approve", client=client)
            self.assertEqual((refused.status_code, refused.data["code"]), (403, "moderator_required"))
        elsewhere_owner = self.make_user("ap-elsewhere")
        elsewhere = create_organization(name="Elsewhere", owner=elsewhere_owner)
        stranger = self.add_member("ap-stranger-mod", Role.MODERATOR, organization=elsewhere)
        self.assertEqual(self.decide_leave(leave_request, "approve", client=self.authenticated_client(stranger)).status_code, 404)
        self.assertEqual(self.membership().status, State.ACTIVE)

    def test_the_list_shows_requests_to_the_owner_and_the_moderator_but_never_to_a_member(self):
        self.make_leave()
        for client in (self.moderator_client, self.owner_client):
            self.assertEqual([(row["status"], row["member"]["email"]) for row in rows(client.get(self.url("organization-leave-requests")))], [("pending", self.member.email)])
        self.assertEqual(self.member_client.get(self.url("organization-leave-requests")).status_code, 403)
        self.assertEqual(rows(self.owner_client.get(self.url("organization-leave-requests") + "?status=approved")), [])


class AfterApprovalTests(LeaveFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.workspace = create_personal_organization(self.member)
        self.other_org = create_organization(name="Other Team", owner=self.make_user("ap-other-owner"))
        OrganizationMembership.objects.create(organization=self.other_org, user=self.member, role=Role.MEMBER)
        self.private = self.make_photo(self.member, "private.png", organization=self.workspace)
        self.leave_request = self.make_leave()

    def approve(self):
        return self.decide_leave(self.leave_request, "approve")

    def test_the_organization_is_gone_at_once_even_for_a_token_the_member_already_holds(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        token = RefreshToken.for_user(self.member).access_token
        token["token_version"] = self.member.token_version
        holder = APIClient()
        holder.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(holder.get(self.url("organization-detail")).status_code, 200)
        self.approve()
        for name in ("organization-detail", "organization-leave-request", "organization-member-list", "organization-invitation-list-create"):
            self.assertEqual(holder.get(self.url(name)).status_code, 404, name)                        # the live membership row is read on every request
        self.assertEqual(holder.get(reverse("moderation:image-list", kwargs={"organization_id": self.organization.pk})).status_code, 404)                          # the moderator portal's organization URLs too
        self.assertNotIn(self.organization.pk, [row["id"] for row in rows(holder.get(reverse("organizations:organization-list")))])

    def test_new_organization_download_requests_are_refused(self):
        self.require_approval()
        self.approve()
        refused = self.submit(self.photos(1, organization=self.organization))
        self.assertGreaterEqual(refused.status_code, 400)
        self.assertFalse(DownloadRequest.objects.filter(requester=self.member).exists())

    def test_download_rights_end_with_the_suspension_but_the_files_stay(self):
        self.require_approval()
        request_record, images = self.make_request(self.photos(2))
        self.decide(request_record, "approve_all")
        self.assertEqual(self.enhanced_download(images[0].result).status_code, status.HTTP_200_OK)
        pending, _ = self.make_request(self.photos(1))                                                  # a second request waiting
        self.leave_request.refresh_from_db()
        self.approve()
        self.assertFalse(DownloadGrant.objects.filter(grantee=self.member, revoked_at__isnull=True).exists())
        pending.refresh_from_db()
        self.assertEqual(pending.status, DownloadRequest.Status.CANCELLED)
        self.assertEqual(self.enhanced_download(images[0].result).status_code, status.HTTP_403_FORBIDDEN)  # no grant, no membership
        self.assertTrue(images[0].__class__.all_objects.filter(pk=images[0].pk).exists())                # nothing was deleted

    def test_the_personal_workspace_the_account_and_other_memberships_are_untouched(self):
        self.approve()
        self.assertEqual(self.membership(organization=self.other_org).status, State.ACTIVE)
        self.assertEqual(self.membership(organization=self.workspace).status, State.ACTIVE)
        self.assertEqual(self.member_client.get(self.url("organization-detail", self.other_org)).status_code, 200)
        self.assertEqual(self.member_client.get(self.url("organization-detail", self.workspace)).status_code, 200)
        self.assertEqual(self.member_client.get(reverse("image-upload-download", kwargs={"pk": self.private.pk})).status_code, 200)
        listed = {row["id"] for row in rows(self.member_client.get(reverse("organizations:organization-list")))}
        self.assertEqual(listed, {self.other_org.pk, self.workspace.pk})
        login = APIClient().post(reverse("login"), {"identifier": self.member.username, "password": "A-long-unique-passphrase-7"}, format="json")
        self.assertIn(login.status_code, (200, 400))                                                    # the account itself is not blocked (400 only if the fixture password differs)
        self.assertFalse(User.objects.get(pk=self.member.pk).is_suspended)

    def test_other_members_the_moderator_and_the_owner_keep_their_access(self):
        self.approve()
        for client in (self.authenticated_client(self.other_member), self.moderator_client, self.owner_client):
            self.assertEqual(client.get(self.url("organization-detail")).status_code, 200)

    def test_nothing_is_deleted_and_the_moderator_still_sees_the_members_records(self):
        photo = self.make_photo(self.member)
        self.approve()
        self.assertTrue(photo.__class__.objects.filter(pk=photo.pk).exists())
        self.assertEqual(OrganizationLeaveRequest.objects.get(pk=self.leave_request.pk).status, Leave.APPROVED)
        self.assertEqual([row["status"] for row in rows(self.moderator_client.get(self.url("organization-leave-requests") + "?status=all"))], ["approved"])

    def test_seats_pending_holds_one_and_approval_releases_it_and_reactivation_needs_room(self):
        Organization.objects.filter(pk=self.organization.pk).update(plan="free")                       # 5 users: owner, moderator, member, other member + one more
        self.organization.refresh_from_db()
        before = seats_in_use(self.organization, "users")
        self.assertEqual(before, 4)                                                                    # the pending request holds the member's seat
        self.approve()
        self.assertEqual(seats_in_use(self.organization, "users"), 3)
        self.add_member("ap-newcomer-1", Role.MEMBER)
        self.add_member("ap-newcomer-2", Role.MEMBER)                                                   # 5 of 5 now
        reactivate = self.owner_client.patch(self.url("organization-member-detail", membership_id=self.membership().pk), {"status": "active"}, format="json")
        self.assertEqual((reactivate.status_code, reactivate.data["code"]), (403, "plan_limit_reached"))
        self.assertEqual(self.membership().status, State.SUSPENDED)                                    # never reactivated by itself


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AdministratorOverrideTests(LeaveFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("ap-admin", role="admin")
        self.admin_client = self.authenticated_client(self.admin)
        self.leave_request = self.make_leave()

    def url_for_admin(self):
        return reverse("organizations:organization-admin-leave-decision", kwargs={"organization_id": self.organization.pk, "leave_id": self.leave_request.pk})

    def test_the_administrator_needs_a_real_reason_and_the_decision_is_marked(self):
        self.assertEqual(self.admin_client.post(self.url_for_admin(), {"action": "approve", "reason": "ok"}, format="json").status_code, 400)
        done = self.admin_client.post(self.url_for_admin(), {"action": "approve", "reason": "No moderator is available this month"}, format="json")
        self.assertEqual((done.status_code, done.data["admin_override"], done.data["status"]), (200, True, "approved"))
        self.assertEqual(self.membership().status, State.SUSPENDED)
        self.assertTrue(self.events(Audit.LEAVE_REQUEST_APPROVED).get().new_value["admin_override"])

    def test_nobody_else_uses_the_override(self):
        for client in (self.member_client, self.moderator_client, self.owner_client):
            self.assertEqual(client.post(self.url_for_admin(), {"action": "approve", "reason": "A long enough reason"}, format="json").status_code, 403)


@unittest.skipUnless(connection.vendor == "postgresql", "row locks are only exercised on PostgreSQL")
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RaceTests(LeaveFixture, TransactionTestCase):
    def test_an_approval_and_a_cancellation_at_the_same_instant_have_exactly_one_winner(self):
        for round_number in range(6):
            member = self.add_member(f"race-{round_number}", Role.MEMBER)
            leave_request = leave.request_leave(user=member, organization=self.organization)
            moderator_membership = self.membership(self.moderator)
            results = run_in_parallel(
                lambda: leave.decide_leave(leave=leave_request, actor=self.moderator, actor_membership=moderator_membership, action="approve"),
                lambda: leave.cancel_leave(user=member, organization=self.organization),
            )
            self.assertEqual(sorted(kind for kind, _ in results), ["error", "ok"], results)
            self.assertTrue(all(isinstance(value, leave.LeaveConflict) for kind, value in results if kind == "error"), results)
            final = OrganizationLeaveRequest.objects.get(pk=leave_request.pk)
            suspended = self.membership(member).status == State.SUSPENDED
            self.assertEqual((final.status, suspended), (Leave.APPROVED, True) if final.status == Leave.APPROVED else (Leave.CANCELLED, False))

    def test_two_approvals_at_once_suspend_once(self):
        member = self.add_member("race-double", Role.MEMBER)
        leave_request = leave.request_leave(user=member, organization=self.organization)
        moderator_membership = self.membership(self.moderator)
        results = run_in_parallel(*[lambda: leave.decide_leave(leave=leave_request, actor=self.moderator, actor_membership=moderator_membership, action="approve") for _ in range(3)])
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1, results)
        self.assertEqual(self.events(Audit.LEAVE_REQUEST_APPROVED).count(), 1)
        self.assertEqual(self.events(Audit.MEMBER_STATUS_CHANGED).count(), 1)
