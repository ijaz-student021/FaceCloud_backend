import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Organization, OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership
from .services import create_organization, create_personal_organization, hash_token, invite_member

User = get_user_model()


class OrgFixtureMixin:
    def make_user(self, name, **extra):
        defaults = {"is_email_verified": True, "email": f"{name}@example.com"}
        defaults.update(extra)
        return User.objects.create_user(username=name, password="ForestRiver!4821", **defaults)

    def authenticated_client(self, user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        return client

    def make_org(self, owner, name="Acme"):
        return create_organization(name=name, owner=owner)

    def add_member(self, organization, user, role=OrganizationMembership.Role.MEMBER, **extra):
        defaults = {"status": OrganizationMembership.Status.ACTIVE}
        defaults.update(extra)
        return OrganizationMembership.objects.create(organization=organization, user=user, role=role, **defaults)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PersonalOrganizationCreationTests(OrgFixtureMixin, TestCase):
    def test_create_personal_organization_makes_the_user_an_active_owner(self):
        user = self.make_user("solo")

        organization = create_personal_organization(user)

        self.assertTrue(organization.is_personal)
        membership = OrganizationMembership.objects.get(organization=organization, user=user)
        self.assertEqual(membership.role, OrganizationMembership.Role.OWNER)
        self.assertEqual(membership.status, OrganizationMembership.Status.ACTIVE)
        self.assertTrue(
            OrganizationAuditEvent.objects.filter(
                organization=organization, action=OrganizationAuditEvent.Action.ORGANIZATION_CREATED,
            ).exists()
        )

    def test_registering_a_new_account_creates_its_personal_organization(self):
        started = self.client.post(
            reverse("register"),
            {"username": "new-org-user", "email": "new-org-user@example.com", "password": "StrongPass!4729"},
            format="json",
        )
        self.assertEqual(started.status_code, status.HTTP_202_ACCEPTED)
        code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

        verified = self.client.post(
            reverse("verify-email"),
            {"registration_id": started.data["registration_id"], "code": code},
            format="json",
        )

        self.assertEqual(verified.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username="new-org-user")
        membership = OrganizationMembership.objects.get(user=user)
        self.assertEqual(membership.role, OrganizationMembership.Role.OWNER)
        self.assertTrue(membership.organization.is_personal)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OrganizationIsolationTests(OrgFixtureMixin, TestCase):
    """Cross-organization visibility must be a 404, never a 403 (spec section 5.1)."""

    def setUp(self):
        self.owner_a = self.make_user("owner-a")
        self.org_a = self.make_org(self.owner_a, "Org A")
        self.owner_b = self.make_user("owner-b")
        self.org_b = self.make_org(self.owner_b, "Org B")
        self.client_a = self.authenticated_client(self.owner_a)

    def test_a_member_of_one_org_cannot_see_another_orgs_detail(self):
        response = self.client_a.get(
            reverse("organizations:organization-detail", kwargs={"organization_id": self.org_b.pk}),
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_member_of_one_org_cannot_list_another_orgs_members(self):
        response = self.client_a.get(
            reverse("organizations:organization-member-list", kwargs={"organization_id": self.org_b.pk}),
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_member_of_one_org_cannot_read_another_orgs_audit_trail(self):
        response = self.client_a.get(
            reverse("organizations:organization-audit-list", kwargs={"organization_id": self.org_b.pk}),
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_an_unrelated_user_gets_404_not_403(self):
        outsider = self.authenticated_client(self.make_user("outsider"))
        response = outsider.get(
            reverse("organizations:organization-detail", kwargs={"organization_id": self.org_a.pk}),
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_listing_my_organizations_only_shows_my_own(self):
        response = self.client_a.get(reverse("organizations:organization-list"))
        names = {org["name"] for org in response.data["results"]} if "results" in response.data else {
            org["name"] for org in response.data
        }
        self.assertIn("Org A", names)
        self.assertNotIn("Org B", names)

    def test_a_plain_member_cannot_list_members(self):
        member = self.make_user("plain-member")
        self.add_member(self.org_a, member)
        client = self.authenticated_client(member)

        response = client.get(
            reverse("organizations:organization-member-list", kwargs={"organization_id": self.org_a.pk}),
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class MembershipProtectionTests(OrgFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("protect-owner")
        self.org = self.make_org(self.owner, "Protect Co")
        self.owner_membership = OrganizationMembership.objects.get(organization=self.org, user=self.owner)
        self.moderator_user = self.make_user("protect-mod")
        self.moderator_membership = self.add_member(self.org, self.moderator_user, OrganizationMembership.Role.MODERATOR)
        self.member_user = self.make_user("protect-member")
        self.member_membership = self.add_member(self.org, self.member_user, OrganizationMembership.Role.MEMBER)
        self.owner_client = self.authenticated_client(self.owner)
        self.moderator_client = self.authenticated_client(self.moderator_user)

    def member_url(self, membership):
        return reverse(
            "organizations:organization-member-detail",
            kwargs={"organization_id": self.org.pk, "membership_id": membership.pk},
        )

    def test_the_last_owner_cannot_be_removed(self):
        response = self.owner_client.delete(self.member_url(self.owner_membership))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.owner_membership.refresh_from_db()
        self.assertEqual(self.owner_membership.status, OrganizationMembership.Status.ACTIVE)

    def test_the_last_owner_cannot_be_demoted(self):
        response = self.owner_client.patch(self.member_url(self.owner_membership), {"role": "member"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_owner_is_not_the_last_owner_once_a_second_owner_exists(self):
        second_owner_user = self.make_user("second-owner")
        second_owner = self.add_member(self.org, second_owner_user, OrganizationMembership.Role.OWNER)

        response = self.owner_client.delete(self.member_url(second_owner))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        second_owner.refresh_from_db()
        self.assertEqual(second_owner.status, OrganizationMembership.Status.REMOVED)

    def test_a_moderator_cannot_manage_the_owner(self):
        response = self.moderator_client.patch(
            self.member_url(self.owner_membership), {"status": "suspended"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_moderator_cannot_manage_another_moderator(self):
        other_mod_user = self.make_user("protect-mod-2")
        other_mod = self.add_member(self.org, other_mod_user, OrganizationMembership.Role.MODERATOR)

        response = self.moderator_client.delete(self.member_url(other_mod))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_moderator_can_remove_a_plain_member(self):
        response = self.moderator_client.delete(self.member_url(self.member_membership))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.member_membership.refresh_from_db()
        self.assertEqual(self.member_membership.status, OrganizationMembership.Status.REMOVED)
        self.assertTrue(
            OrganizationAuditEvent.objects.filter(
                organization=self.org, action=OrganizationAuditEvent.Action.MEMBER_REMOVED,
                target_id=self.member_membership.pk,
            ).exists()
        )

    def test_a_moderator_cannot_grant_the_owner_role(self):
        response = self.moderator_client.patch(
            self.member_url(self.member_membership), {"role": "owner"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.member_membership.refresh_from_db()
        self.assertEqual(self.member_membership.role, OrganizationMembership.Role.MEMBER)

    def test_an_owner_can_promote_a_member_to_owner(self):
        response = self.owner_client.patch(
            self.member_url(self.member_membership), {"role": "owner"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.member_membership.refresh_from_db()
        self.assertEqual(self.member_membership.role, OrganizationMembership.Role.OWNER)

    def test_a_moderator_cannot_demote_themselves_to_member(self):
        """The moderator-on-moderator rule applies to acting on your own row too."""
        response = self.moderator_client.patch(
            self.member_url(self.moderator_membership), {"role": "member"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_sole_owner_demoting_themself_hits_the_last_owner_rule_not_a_generic_denial(self):
        response = self.owner_client.patch(
            self.member_url(self.owner_membership), {"role": "moderator"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_removing_an_already_removed_member_is_a_conflict_not_a_silent_success(self):
        self.moderator_client.delete(self.member_url(self.member_membership))

        response = self.moderator_client.delete(self.member_url(self.member_membership))

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class InvitationLifecycleTests(OrgFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("invite-owner")
        self.org = self.make_org(self.owner, "Invite Co")
        self.owner_client = self.authenticated_client(self.owner)
        self.invite_url = reverse(
            "organizations:organization-invitation-list-create", kwargs={"organization_id": self.org.pk},
        )

    def test_inviting_a_member_sends_an_email_and_stores_only_the_token_hash(self):
        response = self.owner_client.post(self.invite_url, {"email": "NewPerson@Example.com"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        invitation = OrganizationInvitation.objects.get()
        self.assertEqual(invitation.email, "newperson@example.com")
        self.assertEqual(len(mail.outbox), 1)
        token_match = re.search(r"/invitations/([\w-]+)", mail.outbox[0].body)
        self.assertIsNotNone(token_match)
        self.assertEqual(invitation.token_hash, hash_token(token_match.group(1)))

    def test_inviting_an_existing_active_member_is_a_conflict(self):
        member = self.make_user("already-in")
        self.add_member(self.org, member)

        response = self.owner_client.post(self.invite_url, {"email": member.email}, format="json")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_a_second_invitation_to_the_same_pending_email_is_a_conflict(self):
        self.owner_client.post(self.invite_url, {"email": "dup@example.com"}, format="json")

        response = self.owner_client.post(self.invite_url, {"email": "dup@example.com"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_an_invitation_cannot_grant_the_owner_role(self):
        response = self.owner_client.post(
            self.invite_url, {"email": "wannabe-owner@example.com", "role": "owner"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_moderator_cannot_invite_another_moderator(self):
        moderator_user = self.make_user("plain-mod")
        self.add_member(self.org, moderator_user, OrganizationMembership.Role.MODERATOR)
        moderator_client = self.authenticated_client(moderator_user)

        response = moderator_client.post(
            self.invite_url, {"email": "new-mod@example.com", "role": "moderator"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_resending_an_invitation_issues_a_new_token_and_the_old_one_stops_working(self):
        invitation = invite_member(
            organization=self.org, actor_membership=OrganizationMembership.objects.get(organization=self.org, user=self.owner),
            email="resend@example.com", role=OrganizationInvitation.Role.MEMBER,
        )
        old_hash = invitation.token_hash
        old_token_match = re.search(r"/invitations/([\w-]+)", mail.outbox[0].body)

        resend_url = reverse(
            "organizations:organization-invitation-resend",
            kwargs={"organization_id": self.org.pk, "invitation_id": invitation.pk},
        )
        response = self.owner_client.post(resend_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invitation.refresh_from_db()
        self.assertNotEqual(invitation.token_hash, old_hash)
        accept_response = self.owner_client.post(
            reverse("organizations:organization-invitation-accept"),
            {"token": old_token_match.group(1)}, format="json",
        )
        self.assertIn(accept_response.status_code, (status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT))

    def test_cancelling_a_pending_invitation_prevents_acceptance(self):
        invitation = invite_member(
            organization=self.org, actor_membership=OrganizationMembership.objects.get(organization=self.org, user=self.owner),
            email="cancel-me@example.com", role=OrganizationInvitation.Role.MEMBER,
        )
        token_match = re.search(r"/invitations/([\w-]+)", mail.outbox[0].body)
        cancel_url = reverse(
            "organizations:organization-invitation-cancel",
            kwargs={"organization_id": self.org.pk, "invitation_id": invitation.pk},
        )

        cancel_response = self.owner_client.delete(cancel_url)

        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)
        invitee = self.make_user("cancel-me")
        invitee_client = self.authenticated_client(invitee)
        accept_response = invitee_client.post(
            reverse("organizations:organization-invitation-accept"),
            {"token": token_match.group(1)}, format="json",
        )
        self.assertEqual(accept_response.status_code, status.HTTP_409_CONFLICT)

    def test_accepting_with_the_invited_email_creates_an_active_membership(self):
        invitation = invite_member(
            organization=self.org, actor_membership=OrganizationMembership.objects.get(organization=self.org, user=self.owner),
            email="accept-me@example.com", role=OrganizationInvitation.Role.MODERATOR,
        )
        token_match = re.search(r"/invitations/([\w-]+)", mail.outbox[0].body)
        invitee = self.make_user("accept-me", email="accept-me@example.com")
        invitee_client = self.authenticated_client(invitee)

        response = invitee_client.post(
            reverse("organizations:organization-invitation-accept"),
            {"token": token_match.group(1)}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        membership = OrganizationMembership.objects.get(organization=self.org, user=invitee)
        self.assertEqual(membership.role, OrganizationMembership.Role.MODERATOR)
        self.assertEqual(membership.status, OrganizationMembership.Status.ACTIVE)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, OrganizationInvitation.Status.ACCEPTED)

    def test_accepting_with_a_different_account_than_the_invited_email_is_forbidden(self):
        invite_member(
            organization=self.org, actor_membership=OrganizationMembership.objects.get(organization=self.org, user=self.owner),
            email="only-for-them@example.com", role=OrganizationInvitation.Role.MEMBER,
        )
        token_match = re.search(r"/invitations/([\w-]+)", mail.outbox[0].body)
        wrong_person = self.authenticated_client(self.make_user("wrong-person"))

        response = wrong_person.post(
            reverse("organizations:organization-invitation-accept"),
            {"token": token_match.group(1)}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_accepting_an_unknown_token_is_not_found(self):
        response = self.owner_client.post(
            reverse("organizations:organization-invitation-accept"),
            {"token": "not-a-real-token"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_accepting_an_expired_invitation_is_a_conflict(self):
        invitation = invite_member(
            organization=self.org, actor_membership=OrganizationMembership.objects.get(organization=self.org, user=self.owner),
            email="expired@example.com", role=OrganizationInvitation.Role.MEMBER,
        )
        token_match = re.search(r"/invitations/([\w-]+)", mail.outbox[0].body)
        invitation.expires_at = timezone.now() - timezone.timedelta(hours=1)
        invitation.save(update_fields=["expires_at"])
        invitee = self.authenticated_client(self.make_user("expired", email="expired@example.com"))

        response = invitee.post(
            reverse("organizations:organization-invitation-accept"),
            {"token": token_match.group(1)}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AdminOrganizationCreationTests(OrgFixtureMixin, TestCase):
    def test_an_admin_can_create_an_organization_with_a_named_owner(self):
        admin = self.make_user("org-admin", role="admin")
        target_owner = self.make_user("named-owner")
        client = self.authenticated_client(admin)

        response = client.post(
            reverse("organizations:organization-admin-list-create"),
            {"name": "New Co", "owner_id": target_owner.pk}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        organization = Organization.objects.get(name="New Co")
        self.assertEqual(
            OrganizationMembership.objects.get(organization=organization, user=target_owner).role,
            OrganizationMembership.Role.OWNER,
        )

    def test_a_non_admin_cannot_create_an_organization(self):
        regular = self.make_user("not-an-admin")
        client = self.authenticated_client(regular)

        response = client.post(
            reverse("organizations:organization-admin-list-create"),
            {"name": "Nope Co", "owner_id": regular.pk}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
