"""The Platform Administrator creates an organization from an owner e-mail address; the owner needs no account and accepts an invitation (organizations/services.py: create_pending_organization).

Nothing is granted until the invited address accepts: the organization waits (``pending_owner``), nobody belongs to it, and it becomes active in the same transaction in which the
accepting account becomes its owner.
"""
import logging
import re
import smtplib
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from moderation.tests import ModerationFixtureMixin
from organizations.models import Organization, OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership
from organizations.services import (
    OrganizationPendingOwner, assert_organization_active, cancel_invitation, create_organization, get_default_organization, hash_token, resend_invitation,
)

User = get_user_model()
Role = OrganizationMembership.Role
FIXED_TOKEN = "OWNER-TOKEN-FOR-TESTS-0123456789abcdefghijk"
PASSWORD = "A-long-unique-passphrase-7"


class OwnerInvitationFixture(ModerationFixtureMixin):
    def setUp(self):
        self.admin = self.make_user("oi-admin", role="admin")
        self.admin_client = self.authenticated_client(self.admin)

    # ---- calls
    def create(self, name="Event Planners", email="newowner@example.com", plan="team", client=None, **extra):
        with self.captureOnCommitCallbacks(execute=True):
            return (client or self.admin_client).post(reverse("organizations:organization-admin-list-create"), {"name": name, "owner_email": email, "plan": plan, **extra}, format="json")

    def created(self, **kwargs):
        response = self.create(**kwargs)
        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_201_CREATED), response.content)
        return Organization.objects.get(pk=response.data["id"]), response

    def token_from_mail(self, index=-1):
        match = re.search(r"/invitations/([A-Za-z0-9_-]+)", mail.outbox[index].body)
        self.assertIsNotNone(match, mail.outbox[index].body)
        return match.group(1)

    def invitation_of(self, organization):
        return organization.invitations.get(role="owner")

    def preview(self, token):
        return self.client.get(reverse("organizations:organization-invitation-preview", kwargs={"token": token}))

    def accept(self, user, token):
        return self.authenticated_client(user).post(reverse("organizations:organization-invitation-accept"), {"token": token}, format="json")

    def register(self, token, email, username=None, **extra):
        body = {"full_name": "New Owner", "email": email, "password": PASSWORD, "password_confirm": PASSWORD, "invitation_token": token, **extra}
        if username:
            body["username"] = username
        return self.client.post("/api/auth/register/", body, format="json")

    def admin_get(self, name, organization, **kwargs):
        return self.admin_client.get(reverse(f"organizations:{name}", kwargs={"organization_id": organization.pk, **kwargs}))

    def admin_post(self, name, organization, client=None, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return (client or self.admin_client).post(reverse(f"organizations:{name}", kwargs={"organization_id": organization.pk, **kwargs}), format="json")


class CreateWithoutAnAccountTests(OwnerInvitationFixture, TestCase):
    def test_the_administrator_creates_an_organization_for_an_email_with_no_account(self):
        organization, response = self.created()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual((organization.status, organization.plan, organization.name), ("pending_owner", "team", "Event Planners"))
        self.assertTrue(response.data["created"])
        self.assertFalse(response.data["owner_account_exists"])
        self.assertIsNone(response.data["owner"])                                         # nobody owns it yet
        self.assertFalse(User.objects.filter(email="newowner@example.com").exists())      # no dummy account, no credentials
        self.assertFalse(organization.memberships.exists())                               # not even the administrator belongs to it
        invitation = response.data["owner_invitation"]
        self.assertEqual((invitation["email"], invitation["status"], invitation["can_resend"], invitation["accepted_by"]), ("newowner@example.com", "pending", True, None))

    def test_a_secure_invitation_is_created_and_the_e_mail_is_queued_after_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.admin_client.post(reverse("organizations:organization-admin-list-create"), {"name": "Acme", "owner_email": "NewOwner@Example.com", "plan": "free"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(mail.outbox), 0)                                            # nothing is sent before the transaction commits
        self.assertEqual(len(callbacks), 1)
        invitation = OrganizationInvitation.objects.get(organization_id=response.data["id"])
        self.assertEqual((invitation.role, invitation.status, invitation.email, invitation.email_status), ("owner", "pending", "newowner@example.com", "queued"))
        self.assertEqual(len(invitation.token_hash), 64)                                 # only the SHA-256 of the token is stored
        for callback in callbacks:
            callback()
        invitation.refresh_from_db()
        self.assertEqual((len(mail.outbox), invitation.email_status), (1, "sent"))
        self.assertIsNotNone(invitation.email_sent_at)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["newowner@example.com"])
        token = self.token_from_mail()
        self.assertEqual(invitation.token_hash, hash_token(token))
        self.assertGreater(len(token), 30)                                                # a long random token, not a guessable one
        for expected in ("Acme", "newowner@example.com", "expires on", "/invitations/"):
            self.assertIn(expected, message.body)
        self.assertNotIn("password", message.body.lower().replace("never sends a password", ""))            # no credentials of any kind
        self.assertNotIn(token, str(invitation.__dict__.values()))
        self.assertTrue(invitation.expires_at > timezone.now() + timedelta(hours=100))

    def test_the_creation_is_audited(self):
        organization, _ = self.created()
        actions = list(organization.audit_events.values_list("action", flat=True))
        self.assertIn(OrganizationAuditEvent.Action.ORGANIZATION_CREATED, actions)
        self.assertIn(OrganizationAuditEvent.Action.OWNER_INVITED, actions)
        created = organization.audit_events.get(action=OrganizationAuditEvent.Action.ORGANIZATION_CREATED)
        self.assertEqual((created.actor, created.new_value["pending_owner_email"]), (self.admin, "newowner@example.com"))

    def test_the_invitation_preview_names_the_organization_owner_email_and_expiry(self):
        organization, _ = self.created()
        token = self.token_from_mail()
        response = self.preview(token)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual((response.data["organization_name"], response.data["email"], response.data["role"]), ("Event Planners", "newowner@example.com", "owner"))
        self.assertIn("expires_at", response.data)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(self.preview("not-a-token").status_code, status.HTTP_404_NOT_FOUND)

    def test_an_existing_account_gets_an_invitation_too_and_is_not_made_owner_silently(self):
        existing = self.make_user("oi-existing", email="existing@example.com")
        organization, response = self.created(email="existing@example.com")
        self.assertTrue(response.data["owner_account_exists"])
        self.assertEqual(response.data["status"], "pending_owner")
        self.assertFalse(OrganizationMembership.objects.filter(user=existing).exists())     # nothing is granted before the person accepts
        self.assertEqual(len(mail.outbox), 1)

    def test_the_legacy_owner_id_request_still_makes_that_account_the_owner_at_once(self):
        owner = self.make_user("oi-legacy-owner")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.admin_client.post(reverse("organizations:organization-admin-list-create"), {"name": "Legacy Co", "plan": "free", "owner_id": owner.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual((response.data["status"], response.data["owner"]["id"], response.data["owner_invitation"], response.data["owner_account_exists"]), ("active", owner.pk, None, None))
        self.assertTrue(OrganizationMembership.objects.filter(user=owner, role="owner", status="active").exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_the_request_needs_exactly_one_way_to_name_the_owner(self):
        url = reverse("organizations:organization-admin-list-create")
        owner = self.make_user("oi-both")
        self.assertEqual(self.admin_client.post(url, {"name": "X", "plan": "free"}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.admin_client.post(url, {"name": "X", "owner_email": "a@example.com", "owner_id": owner.pk}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.admin_client.post(url, {"name": "X", "owner_email": "not-an-email"}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Organization.objects.filter(name="X").count(), 0)


class AcceptanceTests(OwnerInvitationFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.organization, _ = self.created()
        self.token = self.token_from_mail()

    def test_a_new_user_registers_with_the_invitation_and_becomes_the_owner(self):
        response = self.register(self.token, "newowner@example.com", username="newowner")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        user = User.objects.get(email="newowner@example.com")
        self.assertTrue(user.is_email_verified)                                          # the link went to this mailbox
        self.assertEqual(user.role, "user")                                              # an organization owner is not a platform administrator
        membership = OrganizationMembership.objects.get(organization=self.organization, user=user)
        self.assertEqual((membership.role, membership.status), ("owner", "active"))
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.status, "active")
        invitation = self.invitation_of(self.organization)
        self.assertEqual((invitation.status, invitation.accepted_at is not None), ("accepted", True))
        self.assertTrue(Organization.objects.filter(is_personal=True, memberships__user=user).exists())      # their own workspace, as for every account
        actions = list(self.organization.audit_events.values_list("action", flat=True))
        self.assertIn(OrganizationAuditEvent.Action.INVITATION_ACCEPTED, actions)
        self.assertIn(OrganizationAuditEvent.Action.ORGANIZATION_ACTIVATED, actions)

    def test_registering_for_another_address_with_the_token_is_refused_and_creates_nothing(self):
        response = self.register(self.token, "someone-else@example.com")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(User.objects.filter(email="someone-else@example.com").exists())
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.status, "pending_owner")

    def test_an_existing_user_signs_in_and_accepts(self):
        user = self.make_user("oi-owner-to-be", email="newowner@example.com")
        response = self.accept(user, self.token)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual((response.data["role"], response.data["status"]), ("owner", "active"))
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.status, "active")
        self.assertEqual(self.invitation_of(self.organization).status, "accepted")

    def test_the_e_mail_comparison_ignores_case(self):
        user = self.make_user("oi-mixed", email="NewOwner@Example.com")
        self.assertEqual(self.accept(user, self.token).status_code, status.HTTP_200_OK)

    def test_somebody_else_cannot_accept_it_even_with_the_link(self):
        stranger = self.make_user("oi-stranger", email="stranger@example.com")
        response = self.accept(stranger, self.token)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(OrganizationMembership.objects.filter(user=stranger).exists())
        self.organization.refresh_from_db()
        self.assertEqual((self.organization.status, self.invitation_of(self.organization).status), ("pending_owner", "pending"))

    def test_an_account_whose_address_is_not_verified_cannot_accept(self):
        from organizations.services import accept_invitation
        from rest_framework.exceptions import PermissionDenied

        unverified = self.make_user("oi-unverified", email="newowner@example.com", is_email_verified=False)
        self.assertIn(self.accept(unverified, self.token).status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))      # refused at sign-in already
        with self.assertRaises(PermissionDenied):                                                                                       # and by the service itself, whoever calls it
            accept_invitation(token=self.token, user=unverified)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.status, "pending_owner")
        self.assertFalse(OrganizationMembership.objects.filter(user=unverified).exists())

    def test_a_suspended_account_cannot_accept(self):
        from organizations.services import accept_invitation
        from rest_framework.exceptions import PermissionDenied

        suspended = self.make_user("oi-suspended", email="newowner@example.com", is_suspended=True)
        self.assertIn(self.accept(suspended, self.token).status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        with self.assertRaises(PermissionDenied):
            accept_invitation(token=self.token, user=suspended)
        self.assertFalse(OrganizationMembership.objects.filter(user=suspended).exists())

    def test_an_expired_invitation_is_rejected_everywhere(self):
        OrganizationInvitation.objects.filter(organization=self.organization).update(expires_at=timezone.now() - timedelta(seconds=1))
        user = self.make_user("oi-late", email="newowner@example.com")
        self.assertEqual(self.preview(self.token).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.accept(user, self.token).status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.register(self.token, "newowner@example.com").status_code, status.HTTP_404_NOT_FOUND)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.status, "pending_owner")
        self.assertEqual(self.admin_get("organization-admin-owner-invitation", self.organization).data["status"], "expired")

    def test_a_cancelled_invitation_is_rejected(self):
        response = self.admin_post("organization-admin-owner-invitation-cancel", self.organization)
        self.assertEqual((response.status_code, response.data["status"]), (status.HTTP_200_OK, "cancelled"))
        user = self.make_user("oi-cancelled", email="newowner@example.com")
        self.assertEqual(self.preview(self.token).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.accept(user, self.token).status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.register(self.token, "newowner@example.com").status_code, status.HTTP_404_NOT_FOUND)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.status, "pending_owner")                      # the organization is not broken: it still waits for an owner

    def test_the_token_works_once(self):
        user = self.make_user("oi-once", email="newowner@example.com")
        self.assertEqual(self.accept(user, self.token).status_code, status.HTTP_200_OK)
        again = self.accept(user, self.token)
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.preview(self.token).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.register(self.token, "newowner@example.com").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(OrganizationMembership.objects.filter(organization=self.organization, role="owner").count(), 1)       # never a second owner membership

    def test_a_suspended_organization_does_not_accept_its_owner(self):
        Organization.objects.filter(pk=self.organization.pk).update(status="suspended")
        user = self.make_user("oi-susp-org", email="newowner@example.com")
        self.assertEqual(self.preview(self.token).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.accept(user, self.token).status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(OrganizationMembership.objects.filter(user=user).exists())

    def test_the_admin_sees_who_accepted(self):
        user = self.make_user("oi-seen", email="newowner@example.com")
        self.accept(user, self.token)
        response = self.admin_get("organization-admin-owner-invitation", self.organization)
        self.assertEqual((response.data["status"], response.data["accepted_by"], response.data["can_resend"]), ("accepted", {"id": user.pk, "username": "oi-seen"}, False))
        detail = self.admin_client.get(reverse("organizations:organization-admin-detail", kwargs={"organization_id": self.organization.pk}))
        self.assertEqual((detail.data["status"], detail.data["owner"]["id"], detail.data["owner_invitation"]), ("active", user.pk, None))


class PendingOwnerHasNoPowerTests(OwnerInvitationFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.organization, _ = self.created(email="future@example.com")
        self.future = self.make_user("oi-future", email="future@example.com")           # has an account, has not accepted
        self.future_client = self.authenticated_client(self.future)

    def test_the_invited_person_is_not_a_member_and_every_organization_route_answers_404(self):
        org = self.organization.pk
        self.assertEqual(self.future_client.get(reverse("organizations:organization-detail", kwargs={"organization_id": org})).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.future_client.get(reverse("organizations:organization-member-list", kwargs={"organization_id": org})).status_code, status.HTTP_404_NOT_FOUND)
        invite = self.future_client.post(reverse("organizations:organization-invitation-list-create", kwargs={"organization_id": org}), {"email": "x@example.com", "role": "member"}, format="json")
        self.assertEqual(invite.status_code, status.HTTP_404_NOT_FOUND)                  # no member invitations before the owner exists
        self.assertFalse(OrganizationInvitation.objects.filter(organization=self.organization, role="member").exists())
        listing = self.future_client.get(reverse("organizations:organization-list"))
        self.assertNotIn(org, [row["id"] for row in (listing.data["results"] if isinstance(listing.data, dict) else listing.data)])

    def test_nothing_active_can_happen_in_the_organization(self):
        with self.assertRaises(OrganizationPendingOwner):
            assert_organization_active(self.organization)
        self.assertNotEqual(get_default_organization(self.future), self.organization)
        upload = self.future_client.post(reverse("image-upload-single"), {"organization_id": self.organization.pk}, format="multipart")
        self.assertEqual(upload.status_code, status.HTTP_400_BAD_REQUEST)               # not a member of it: uploads cannot be filed under it

    def test_the_owner_invitation_cannot_be_managed_through_the_organizations_own_routes(self):
        invitation = self.invitation_of(self.organization)
        membership = mock.Mock(user=self.admin)
        with self.assertRaises(Exception) as caught:
            cancel_invitation(invitation=invitation, actor_membership=membership)
        self.assertEqual(caught.exception.status_code, 403)
        with self.assertRaises(Exception) as caught:
            resend_invitation(invitation=invitation, actor_membership=membership)
        self.assertEqual(caught.exception.status_code, 403)

    def test_an_owner_role_cannot_be_invited_through_the_member_invitation_route(self):
        owner = self.make_user("oi-real-owner")
        active = create_organization(name="Active Co", owner=owner)
        response = self.authenticated_client(owner).post(reverse("organizations:organization-invitation-list-create", kwargs={"organization_id": active.pk}), {"email": "o2@example.com", "role": "owner"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_active_owner_can_invite_a_moderator_only_after_the_organization_is_active(self):
        token = self.token_from_mail()
        self.accept(self.future, token)
        response = self.future_client.post(reverse("organizations:organization-invitation-list-create", kwargs={"organization_id": self.organization.pk}), {"email": "mod@example.com", "role": "moderator"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        listing = self.future_client.get(reverse("organizations:organization-detail", kwargs={"organization_id": self.organization.pk}))
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data["membership"]["role"], "owner")
        self.assertTrue(listing.data["capabilities"]["invite_moderators"])

    def test_the_administrator_cannot_activate_it_by_hand_and_a_reactivation_goes_back_to_waiting(self):
        url = reverse("organizations:organization-admin-detail", kwargs={"organization_id": self.organization.pk})
        refused = self.admin_client.patch(url, {"status": "active"}, format="json")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.admin_client.patch(url, {"status": "pending_owner"}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.admin_client.patch(url, {"status": "suspended"}, format="json").status_code, status.HTTP_200_OK)
        back = self.admin_client.patch(url, {"status": "active"}, format="json")
        self.assertEqual((back.status_code, back.data["status"]), (status.HTTP_200_OK, "pending_owner"))       # never "active" without an owner
        self.assertFalse(OrganizationMembership.objects.filter(organization=self.organization).exists())

    def test_cross_organization_access_stays_blocked(self):
        token = self.token_from_mail()
        self.accept(self.future, token)
        other_owner = self.make_user("oi-other-owner")
        other = create_organization(name="Rival", owner=other_owner)
        self.assertEqual(self.future_client.get(reverse("organizations:organization-detail", kwargs={"organization_id": other.pk})).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.authenticated_client(other_owner).get(reverse("organizations:organization-detail", kwargs={"organization_id": self.organization.pk})).status_code, status.HTTP_404_NOT_FOUND)


class DeliveryResendAndIdempotencyTests(OwnerInvitationFixture, TestCase):
    def test_a_delivery_failure_leaves_a_working_organization_and_a_resend_fixes_it(self):
        with mock.patch("organizations.services.send_mail", side_effect=smtplib.SMTPException("mail server is down")):
            organization, response = self.created(email="late@example.com")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)                  # the organization and invitation exist: nothing is rolled back
        invitation = self.invitation_of(organization)
        self.assertEqual((invitation.email_status, invitation.status), ("failed", "pending"))
        self.assertIn("SMTPException", invitation.email_failure_reason)
        status_response = self.admin_get("organization-admin-owner-invitation", organization)
        self.assertEqual((status_response.data["email_status"], status_response.data["status"], status_response.data["can_resend"]), ("failed", "pending", True))
        self.assertEqual(len(mail.outbox), 0)
        resent = self.admin_post("organization-admin-owner-invitation-resend", organization)
        self.assertEqual((resent.status_code, resent.data["status"]), (status.HTTP_200_OK, "pending"))
        after = self.admin_get("organization-admin-owner-invitation", organization)          # (the test harness runs the on-commit send after the response; in production it has run before it)
        self.assertEqual((after.data["email_status"], after.data["email_failure_reason"]), ("sent", ""))
        self.assertEqual(len(mail.outbox), 1)
        user = self.make_user("oi-late-owner", email="late@example.com")
        self.assertEqual(self.accept(user, self.token_from_mail()).status_code, status.HTTP_200_OK)

    def test_a_resend_gives_a_new_link_and_the_old_one_stops_working(self):
        organization, _ = self.created()
        old = self.token_from_mail()
        self.admin_post("organization-admin-owner-invitation-resend", organization)
        self.assertEqual(len(mail.outbox), 2)
        new = self.token_from_mail()
        self.assertNotEqual(old, new)
        self.assertEqual(self.preview(old).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.preview(new).status_code, status.HTTP_200_OK)
        self.assertEqual(organization.invitations.filter(role="owner").count(), 1)       # one invitation row, renewed

    def test_a_resend_reopens_an_expired_or_cancelled_invitation(self):
        organization, _ = self.created()
        OrganizationInvitation.objects.filter(organization=organization).update(expires_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(self.admin_post("organization-admin-owner-invitation-resend", organization).data["status"], "pending")
        self.admin_post("organization-admin-owner-invitation-cancel", organization)
        reopened = self.admin_post("organization-admin-owner-invitation-resend", organization)
        self.assertEqual((reopened.status_code, reopened.data["status"]), (status.HTTP_200_OK, "pending"))
        self.assertEqual(self.preview(self.token_from_mail()).status_code, status.HTTP_200_OK)

    def test_an_accepted_invitation_cannot_be_resent_or_cancelled(self):
        organization, _ = self.created()
        user = self.make_user("oi-done", email="newowner@example.com")
        self.accept(user, self.token_from_mail())
        self.assertEqual(self.admin_post("organization-admin-owner-invitation-resend", organization).status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.admin_post("organization-admin-owner-invitation-cancel", organization).status_code, status.HTTP_409_CONFLICT)

    def test_an_organization_without_an_owner_invitation_answers_404(self):
        owner = self.make_user("oi-plain-owner")
        plain = create_organization(name="Plain", owner=owner)
        self.assertEqual(self.admin_get("organization-admin-owner-invitation", plain).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.admin_post("organization-admin-owner-invitation-resend", plain).status_code, status.HTTP_404_NOT_FOUND)

    def test_the_same_request_twice_makes_one_organization_and_one_e_mail(self):
        first = self.create(name="Acme Events", email="same@example.com")
        again = self.create(name="  acme   events ", email="SAME@example.com")
        self.assertEqual((first.status_code, again.status_code), (status.HTTP_201_CREATED, status.HTTP_200_OK))
        self.assertTrue(first.data["created"])
        self.assertFalse(again.data["created"])
        self.assertEqual(first.data["id"], again.data["id"])
        self.assertEqual(Organization.objects.filter(name__iexact="acme events").count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        different = self.create(name="Acme Events", email="other@example.com")
        self.assertEqual(different.status_code, status.HTTP_201_CREATED)                 # another owner is another organization
        self.assertNotEqual(different.data["id"], first.data["id"])

    def test_the_token_is_never_logged_stored_or_returned(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture(level=logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        try:
            with mock.patch("organizations.services.secrets.token_urlsafe", return_value=FIXED_TOKEN):
                with mock.patch("organizations.services.send_mail", side_effect=smtplib.SMTPException(f"cannot deliver {FIXED_TOKEN}")):
                    organization, response = self.created(email="log@example.com")
                status_response = self.admin_get("organization-admin-owner-invitation", organization)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        invitation = self.invitation_of(organization)
        self.assertEqual(invitation.token_hash, hash_token(FIXED_TOKEN))
        everything = " ".join(records) + str(response.content) + str(status_response.content) + invitation.email_failure_reason
        everything += " ".join(str(event.new_value) + str(event.previous_value) + event.reason for event in organization.audit_events.all())
        self.assertNotIn(FIXED_TOKEN, everything)
        self.assertNotIn("/invitations/", everything)
        self.assertIn("[removed]", invitation.email_failure_reason)


class AdministratorOnlyTests(OwnerInvitationFixture, TestCase):
    def test_only_the_platform_administrator_can_create_or_manage_an_owner_invitation(self):
        organization, _ = self.created()
        owner = self.make_user("oi-an-owner")
        real = create_organization(name="Real", owner=owner)
        moderator = self.make_user("oi-a-mod", role="moderator")
        callers = [self.authenticated_client(self.make_user("oi-plain")), self.authenticated_client(moderator), self.authenticated_client(owner)]
        for client in callers:
            created = self.create(name="Hijack", email="h@example.com", client=client)
            self.assertEqual(created.status_code, status.HTTP_403_FORBIDDEN)
            for name in ("organization-admin-owner-invitation",):
                self.assertEqual(client.get(reverse(f"organizations:{name}", kwargs={"organization_id": organization.pk})).status_code, status.HTTP_403_FORBIDDEN)
            for name in ("organization-admin-owner-invitation-resend", "organization-admin-owner-invitation-cancel"):
                self.assertEqual(client.post(reverse(f"organizations:{name}", kwargs={"organization_id": organization.pk})).status_code, status.HTTP_403_FORBIDDEN)
        anonymous = self.client.post(reverse("organizations:organization-admin-list-create"), {"name": "X", "owner_email": "x@example.com"}, format="json")
        self.assertEqual(anonymous.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(Organization.objects.filter(name="Hijack").exists())
        self.assertEqual(real.status, "active")

    def test_the_administrator_list_shows_a_waiting_organization_with_its_invitation(self):
        organization, _ = self.created()
        listing = self.admin_client.get(reverse("organizations:organization-admin-list-create"))
        rows = listing.data["results"] if isinstance(listing.data, dict) else listing.data
        row = next(item for item in rows if item["id"] == organization.pk)
        self.assertEqual((row["status"], row["owner"], row["owner_invitation"]["status"], row["owner_invitation"]["email_status"]), ("pending_owner", None, "pending", "sent"))
        self.assertNotIn("token", str(row))

    def test_existing_organization_flows_are_unchanged(self):
        owner = self.make_user("oi-flow-owner")
        organization = create_organization(name="Flow Co", owner=owner)
        client = self.authenticated_client(owner)
        member = self.make_user("oi-flow-member", email="flowmember@example.com")
        with self.captureOnCommitCallbacks(execute=True):
            invited = client.post(reverse("organizations:organization-invitation-list-create", kwargs={"organization_id": organization.pk}), {"email": "flowmember@example.com", "role": "member"}, format="json")
        self.assertEqual(invited.status_code, status.HTTP_201_CREATED)
        token = re.search(r"/invitations/([A-Za-z0-9_-]+)", mail.outbox[-1].body).group(1)
        self.assertEqual(self.preview(token).data["role"], "member")
        self.assertEqual(self.accept(member, token).status_code, status.HTTP_200_OK)
        self.assertTrue(OrganizationMembership.objects.filter(organization=organization, user=member, role="member", status="active").exists())
        resend = client.post(reverse("organizations:organization-invitation-resend", kwargs={"organization_id": organization.pk, "invitation_id": 999}))
        self.assertEqual(resend.status_code, status.HTTP_404_NOT_FOUND)
        rename = self.admin_client.patch(reverse("organizations:organization-admin-detail", kwargs={"organization_id": organization.pk}), {"name": "Flow Company"}, format="json")
        self.assertEqual((rename.status_code, rename.data["status"]), (status.HTTP_200_OK, "active"))
