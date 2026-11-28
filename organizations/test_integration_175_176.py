"""The three changes together (owner invitation, account isolation, download approval): one flow end to end, and a personal workspace is never a shared organization anywhere."""
import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from downloads.approval import eligible_reviewers, organization_requires_approval
from downloads.models import DownloadGrant, DownloadRequest
from downloads.test_download_approval import ApprovalFixture
from moderation.tests import ModerationFixtureMixin
from organizations.models import Organization, OrganizationMembership
from organizations.services import create_organization, create_personal_organization

User = get_user_model()
PASSWORD = "A-long-unique-passphrase-7"
REASON = "Pilot organization needs a second moderator before its owner can invite one"


def token_from(message):
    return re.search(r"/invitations/([A-Za-z0-9_-]+)", message.body).group(1)


class EndToEndTests(ModerationFixtureMixin, TestCase):
    """Platform Admin creates the organization -> the owner accepts -> invites a moderator and a member -> approval is enabled -> a download is requested, decided and delivered."""

    def register(self, email, token, username):
        response = self.client.post("/api/auth/register/", {"full_name": username.title(), "email": email, "password": PASSWORD, "password_confirm": PASSWORD, "invitation_token": token, "username": username}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        return User.objects.get(username=username)

    def test_the_whole_workflow(self):
        admin = self.make_user("e2e-admin", role="admin")
        admin_client = self.authenticated_client(admin)

        # 1. the Platform Admin creates the organization from an e-mail address: it waits for its owner
        with self.captureOnCommitCallbacks(execute=True):
            created = admin_client.post(reverse("organizations:organization-admin-list-create"), {"name": "Event Planners", "owner_email": "owner@example.com", "plan": "team"}, format="json")
        self.assertEqual((created.status_code, created.data["status"], created.data["owner"]), (status.HTTP_201_CREATED, "pending_owner", None))
        organization = Organization.objects.get(pk=created.data["id"])
        # approval cannot be switched on for an organization nobody owns or moderates yet
        refused = admin_client.patch(reverse("organizations:organization-admin-detail", kwargs={"organization_id": organization.pk}), {"download_approval_required": True}, format="json")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)

        # 2. the owner registers through the invitation: owner + active organization
        owner = self.register("owner@example.com", token_from(mail.outbox[-1]), "e2e-owner")
        organization.refresh_from_db()
        self.assertEqual((organization.status, OrganizationMembership.objects.get(organization=organization, user=owner).role), ("active", "owner"))
        owner_client = self.authenticated_client(owner)

        # 3. the owner invites a moderator and a member; they join by their invitations
        for email, role in (("moderator@example.com", "moderator"), ("member@example.com", "member")):
            with self.captureOnCommitCallbacks(execute=True):
                invited = owner_client.post(reverse("organizations:organization-invitation-list-create", kwargs={"organization_id": organization.pk}), {"email": email, "role": role}, format="json")
            self.assertEqual(invited.status_code, status.HTTP_201_CREATED, invited.content)
        moderator = self.register("moderator@example.com", token_from(mail.outbox[-2]), "e2e-moderator")
        member = self.register("member@example.com", token_from(mail.outbox[-1]), "e2e-member")
        moderator_client, member_client = self.authenticated_client(moderator), self.authenticated_client(member)

        # 4. only now can the administrator switch download approval on (an active moderator exists)
        enabled = admin_client.patch(reverse("organizations:organization-admin-detail", kwargs={"organization_id": organization.pk}), {"download_approval_required": True}, format="json")
        self.assertEqual((enabled.status_code, enabled.data["download_approval_required"]), (status.HTTP_200_OK, True))
        organization.refresh_from_db()

        # 5. the member's photograph (filed under the organization by their explicit choice) cannot be taken without approval
        photo = ApprovalFixture.make_photo(self, member, "event.png", organization=organization)
        direct = member_client.get(reverse("enhancement:result-download", kwargs={"pk": photo.result.pk}))
        self.assertEqual((direct.status_code, direct.data["reason"]), (status.HTTP_403_FORBIDDEN, "no_grant"))

        # 6. the member asks; the moderator (not the owner) is told and decides; the owner can only watch
        with self.captureOnCommitCallbacks(execute=True):
            asked = member_client.post(reverse("downloads:request-list-create"), {"organization_id": organization.pk, "items": [{"image_id": photo.pk}], "client_token": "e2e-1"}, format="json")
        self.assertEqual(asked.status_code, status.HTTP_201_CREATED, asked.content)
        request_id = asked.data["request"]["id"]
        decision = reverse("org-download-request-decision", kwargs={"organization_id": organization.pk, "request_id": request_id})
        self.assertEqual(owner_client.post(decision, {"action": "approve_all"}, format="json").data["code"], "moderator_required")
        self.assertEqual(owner_client.get(reverse("org-download-request-detail", kwargs={"organization_id": organization.pk, "request_id": request_id})).status_code, status.HTTP_200_OK)
        with self.captureOnCommitCallbacks(execute=True):
            approved = moderator_client.post(decision, {"action": "approve_all"}, format="json")
        self.assertEqual((approved.status_code, approved.data["status"]), (status.HTTP_200_OK, "approved"))

        # 7. the member now downloads it; a personal photograph was never subject to any of this
        self.assertEqual(member_client.get(reverse("enhancement:result-download", kwargs={"pk": photo.result.pk})).status_code, status.HTTP_200_OK)
        workspace = create_personal_organization(member)
        private = ApprovalFixture.make_photo(self, member, "private.png", organization=workspace)
        self.assertEqual(member_client.get(reverse("enhancement:result-download", kwargs={"pk": private.result.pk})).status_code, status.HTTP_200_OK)
        staff_rows = moderator_client.get(reverse("moderation:image-list", kwargs={"organization_id": organization.pk}))
        listed = staff_rows.data["results"] if isinstance(staff_rows.data, dict) else staff_rows.data
        self.assertEqual({row["id"] for row in listed}, {photo.pk})                              # the moderator sees the organization's photograph, never the private one

        # 8. the administrator's exceptional way to add a second moderator needs a reason and is audited
        url = reverse("admin-user-list")
        body = {"username": "e2e-extra", "email": "extra@example.com", "role": "moderator", "organization_id": organization.pk}
        self.assertEqual(admin_client.post(url, body, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(admin_client.post(url, {**body, "reason": REASON}, format="json").status_code, status.HTTP_201_CREATED)
        self.assertEqual(organization.audit_events.filter(action="admin_moderator_override").count(), 1)
        self.assertEqual(DownloadRequest.objects.count(), 1)
        self.assertEqual(DownloadGrant.objects.count(), 1)


class PersonalWorkspaceIsNeverASharedOrganizationTests(ApprovalFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("pw-admin", role="admin")
        self.admin_client = self.authenticated_client(self.admin)
        self.workspace = create_personal_organization(self.member)
        Organization.objects.filter(pk=self.workspace.pk).update(download_approval_required=True)          # even set by hand, it must never count

    def test_download_approval_never_applies_to_a_personal_workspace(self):
        self.assertFalse(organization_requires_approval(Organization.objects.get(pk=self.workspace.pk)))
        photo = self.make_photo(self.member, "mine.png", organization=self.workspace)
        self.assertEqual(self.member_client.get(reverse("enhancement:result-download", kwargs={"pk": photo.result.pk})).status_code, status.HTTP_200_OK)
        self.assertEqual(self.member_client.get(reverse("image-upload-download", kwargs={"pk": photo.pk})).status_code, status.HTTP_200_OK)
        asked = self.member_client.post(reverse("downloads:request-list-create"), {"organization_id": self.workspace.pk, "items": [{"image_id": photo.pk}]}, format="json")
        self.assertEqual((asked.status_code, asked.data["code"]), (status.HTTP_409_CONFLICT, "approval_not_required"))
        thumb = self.member_client.get(reverse("image-thumbnail", kwargs={"pk": photo.pk}) + "?size=256")
        self.assertNotIn("X-Preview-Restricted", thumb)
        row = next(item for item in self.member_client.get(reverse("my-image-list")).data["results"] if item["id"] == photo.pk) if isinstance(self.member_client.get(reverse("my-image-list")).data, dict) else None
        if row is not None:
            self.assertEqual((row["organization_id"], row["download_approval_required"]), (self.workspace.pk, False))

    def test_nobody_can_switch_approval_on_for_it_or_make_it_wait_for_an_owner(self):
        refused = self.admin_client.patch(reverse("organizations:organization-admin-detail", kwargs={"organization_id": self.workspace.pk}), {"download_approval_required": True}, format="json")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        suspend = self.admin_client.patch(reverse("organizations:organization-admin-detail", kwargs={"organization_id": self.workspace.pk}), {"status": "suspended"}, format="json")
        self.assertEqual(suspend.status_code, status.HTTP_400_BAD_REQUEST)
        owner_patch = self.member_client.patch(reverse("organizations:organization-detail", kwargs={"organization_id": self.workspace.pk}), {"download_approval_required": True}, format="json")
        self.assertEqual(owner_patch.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_personal_workspace_has_no_reviewers_and_takes_no_invitations(self):
        self.assertEqual(eligible_reviewers(self.workspace, self.member.pk), [])
        invite = self.member_client.post(reverse("organizations:organization-invitation-list-create", kwargs={"organization_id": self.workspace.pk}), {"email": "intruder@example.com", "role": "moderator"}, format="json")
        self.assertEqual((invite.status_code, invite.data["code"]), (status.HTTP_403_FORBIDDEN, "personal_workspace_is_private"))
        moderator = self.admin_client.post(reverse("admin-user-list"), {"username": "pw-mod", "email": "pw-mod@example.com", "role": "moderator", "organization_id": self.workspace.pk, "reason": "A reason long enough to count"}, format="json")
        self.assertEqual(moderator.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_organization_that_waits_for_its_owner_cannot_have_approval_or_reviewers(self):
        with self.captureOnCommitCallbacks(execute=True):
            created = self.admin_client.post(reverse("organizations:organization-admin-list-create"), {"name": "Waiting", "owner_email": "wait@example.com", "plan": "team"}, format="json")
        waiting = Organization.objects.get(pk=created.data["id"])
        self.assertEqual(self.admin_client.patch(reverse("organizations:organization-admin-detail", kwargs={"organization_id": waiting.pk}), {"download_approval_required": True}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(eligible_reviewers(waiting, self.member.pk), [])
        asked = self.member_client.post(reverse("downloads:request-list-create"), {"organization_id": waiting.pk, "items": [{"image_id": 1}]}, format="json")
        self.assertEqual(asked.status_code, status.HTTP_400_BAD_REQUEST)                      # the member is not a member of it
        shared = create_organization(name="Plain Shared", owner=self.make_user("pw-shared-owner"))
        self.assertTrue(shared.is_personal is False)
