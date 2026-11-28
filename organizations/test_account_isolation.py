"""Personal accounts and organizations stay apart.

* A public signup creates a personal account with a private workspace and NEVER a membership in a shared organization, whatever its e-mail address matches.
* Membership of a shared organization comes only from accepting an invitation (its token, the invited address), or from an administrator's own explicit action.
* A person who belongs to an organization and has a personal workspace keeps the two apart: the organization's staff never see, list, preview or export what is filed under the personal
  workspace, and what a person uploads without choosing an organization goes to their personal workspace, never to an organization they merely belong to.
"""
import io
import re
from pathlib import Path

import numpy as np
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from PIL import Image
from rest_framework import status

from identities.models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership
from moderation.test_review_image import ReviewImageMixin
from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from organizations.services import (
    create_organization, create_personal_organization, ensure_personal_organization, get_default_organization, personal_organization_of, resolve_upload_organization,
)
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

User = get_user_model()
Role = OrganizationMembership.Role
PASSWORD = "A-long-unique-passphrase-7"


def png_bytes(seed=1):
    buffer = io.BytesIO()
    Image.fromarray(np.random.default_rng(seed).integers(0, 255, (60, 80, 3), dtype=np.uint8), "RGB").save(buffer, format="PNG")
    return buffer.getvalue()


class Fixture(ReviewImageMixin):
    def setUp(self):
        self.admin = self.make_user("iso-admin", role="admin")
        self.admin_client = self.authenticated_client(self.admin)
        self.owner = self.make_user("iso-owner")
        self.organization = create_organization(name="Studio", owner=self.owner, plan="team")
        self.moderator = self.add("iso-moderator", Role.MODERATOR)
        self.person = self.add("iso-person", Role.MEMBER)               # belongs to the organization AND has a personal workspace
        self.personal = create_personal_organization(self.person)
        self.owner_client, self.moderator_client, self.person_client = (self.authenticated_client(u) for u in (self.owner, self.moderator, self.person))

    def add(self, name, role, organization=None, **extra):
        user = self.make_user(name, **extra)
        OrganizationMembership.objects.create(organization=organization or self.organization, user=user, role=role, invited_by=self.owner)
        return user

    def upload(self, client, organization_id=None, seed=1):
        data = {"file": SimpleUploadedFile("a.png", png_bytes(seed), "image/png")}
        if organization_id is not None:
            data["organization_id"] = str(organization_id)
        return client.post(reverse("image-upload-single"), data, format="multipart")

    def memberships_of(self, user):
        return list(OrganizationMembership.objects.filter(user=user).values_list("organization__is_personal", "role", "status"))

    def invite(self, organization, email, role, inviter_client=None):
        with self.captureOnCommitCallbacks(execute=True):
            response = (inviter_client or self.owner_client).post(
                reverse("organizations:organization-invitation-list-create", kwargs={"organization_id": organization.pk}), {"email": email, "role": role}, format="json",
            )
        return response

    def token_from_mail(self):
        return re.search(r"/invitations/([A-Za-z0-9_-]+)", mail.outbox[-1].body).group(1)

    def register_and_verify(self, email, name="Public Person", **extra):
        registered = self.client.post(reverse("register"), {"full_name": name, "email": email, "password": PASSWORD, **extra}, format="json")
        self.assertEqual(registered.status_code, status.HTTP_202_ACCEPTED, registered.content)
        match = re.search(r"verify-email\?registration_id=([^&\s]+)&token=([^\s&\"]+)", mail.outbox[-1].body)
        verified = self.client.post(reverse("verify-email"), {"registration_id": match.group(1), "token": match.group(2)}, format="json")
        self.assertIn(verified.status_code, (status.HTTP_200_OK, status.HTTP_201_CREATED), verified.content)
        return User.objects.get(email__iexact=email)


class SignupIsolationTests(Fixture, TestCase):
    def test_a_public_signup_gets_a_private_workspace_and_no_organization(self):
        user = self.register_and_verify("newcomer@example.com")
        self.assertEqual(self.memberships_of(user), [(True, "owner", "active")])          # only their own private workspace
        workspace = personal_organization_of(user)
        self.assertEqual(list(workspace.memberships.values_list("user_id", flat=True)), [user.pk])
        self.assertEqual(user.role, "user")

    def test_nothing_exists_before_the_address_is_verified(self):
        self.client.post(reverse("register"), {"full_name": "Not Yet", "email": "notyet@example.com", "password": PASSWORD}, format="json")
        self.assertFalse(User.objects.filter(email="notyet@example.com").exists())
        self.assertFalse(OrganizationMembership.objects.filter(user__email="notyet@example.com").exists())

    def test_signing_up_with_the_address_of_a_pending_owner_invitation_does_not_join_the_organization(self):
        with self.captureOnCommitCallbacks(execute=True):
            created = self.admin_client.post(reverse("organizations:organization-admin-list-create"), {"name": "Future Co", "owner_email": "future@example.com", "plan": "free"}, format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        user = self.register_and_verify("future@example.com")                              # the same address, a public signup, no invitation token
        self.assertEqual(self.memberships_of(user), [(True, "owner", "active")])
        organization = Organization.objects.get(pk=created.data["id"])
        self.assertEqual((organization.status, organization.memberships.count()), ("pending_owner", 0))
        self.assertEqual(organization.invitations.get(role="owner").status, "pending")     # still waiting: the address matching is not an acceptance
        again = self.authenticated_client(user).get(reverse("organizations:organization-detail", kwargs={"organization_id": organization.pk}))
        self.assertEqual(again.status_code, status.HTTP_404_NOT_FOUND)

    def test_signing_up_with_the_address_of_a_pending_member_or_moderator_invitation_does_not_join_either(self):
        for index, role in enumerate(("member", "moderator")):
            email = f"invitee{index}@example.com"
            self.assertEqual(self.invite(self.organization, email, role).status_code, status.HTTP_201_CREATED)
            user = self.register_and_verify(email)
            self.assertEqual(self.memberships_of(user), [(True, "owner", "active")], role)
            self.assertEqual(OrganizationInvitation.objects.get(organization=self.organization, email=email).status, "pending")

    def test_a_token_for_another_address_creates_nothing(self):
        self.invite(self.organization, "someone@example.com", "member")
        token = self.token_from_mail()
        response = self.client.post(reverse("register"), {
            "full_name": "Thief", "email": "thief@example.com", "password": PASSWORD, "password_confirm": PASSWORD, "invitation_token": token,
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(User.objects.filter(email="thief@example.com").exists())

    def test_registering_through_an_invitation_joins_only_that_organization_with_only_that_role(self):
        other_owner = self.make_user("iso-other-owner")
        create_organization(name="Elsewhere", owner=other_owner)
        self.invite(self.organization, "joiner@example.com", "member")
        token = self.token_from_mail()
        response = self.client.post(reverse("register"), {
            "full_name": "Joiner", "email": "joiner@example.com", "password": PASSWORD, "password_confirm": PASSWORD, "invitation_token": token,
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        user = User.objects.get(email="joiner@example.com")
        self.assertEqual(sorted(self.memberships_of(user)), [(False, "member", "active"), (True, "owner", "active")])      # the invited organization as a member + their own workspace
        self.assertEqual(OrganizationMembership.objects.filter(user=user, organization__name="Elsewhere").count(), 0)

    def test_a_new_code_path_that_creates_memberships_has_to_be_reviewed(self):
        """A tripwire, not a proof: every place in the code that creates an OrganizationMembership is listed here. A new one fails this test until somebody has checked that it needs
        a token, an owner or an administrator and never just an e-mail address."""
        root = Path(__file__).resolve().parents[1]
        pattern = re.compile(r"OrganizationMembership(\.objects)?\.(create|update_or_create|get_or_create|bulk_create)\(|OrganizationMembership\(")
        found = set()
        for path in root.rglob("*.py"):
            relative = path.relative_to(root).as_posix()
            if relative.startswith(("venv/", ".venv/", "node_modules/")) or "/migrations/" in relative or "test" in path.name or relative.startswith("scripts/"):
                continue
            if pattern.search(path.read_text(errors="ignore")):
                found.add(relative)
        self.assertEqual(found, {
            "accounts/admin.py",              # the Django admin site (staff)
            "accounts/invitations.py",        # an administrator's account invitation for a moderator, accepted with its token
            "accounts/services.py",           # an administrator creating a moderator account
            "accounts/views.py",              # an administrator promoting an account to moderator
            "organizations/models.py",        # the model itself
            "organizations/services.py",      # create_organization (owner), invitations accepted with their token
        })


class InvitationAcceptanceTests(Fixture, TestCase):
    def test_an_e_mail_match_alone_gives_no_membership_and_no_access(self):
        existing = self.make_user("iso-existing", email="match@example.com")
        self.invite(self.organization, "match@example.com", "member")
        self.assertEqual(self.memberships_of(existing), [])                               # nothing happened to the account
        response = self.authenticated_client(existing).get(reverse("organizations:organization-detail", kwargs={"organization_id": self.organization.pk}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_accepting_needs_the_token_and_the_invited_address(self):
        existing = self.make_user("iso-existing", email="match@example.com")
        stranger = self.make_user("iso-stranger", email="stranger@example.com")
        self.invite(self.organization, "match@example.com", "member")
        token = self.token_from_mail()
        accept = reverse("organizations:organization-invitation-accept")
        self.assertIn(self.authenticated_client(existing).post(accept, {"token": "wrong"}, format="json").status_code, (status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT))
        self.assertEqual(self.authenticated_client(stranger).post(accept, {"token": token}, format="json").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.memberships_of(stranger), [])
        ok = self.authenticated_client(existing).post(accept, {"token": token}, format="json")
        self.assertEqual((ok.status_code, ok.data["role"]), (status.HTTP_200_OK, "member"))

    def test_the_role_granted_is_exactly_the_role_invited_and_a_member_cannot_raise_it(self):
        member = self.make_user("iso-grow", email="grow@example.com")
        self.invite(self.organization, "grow@example.com", "member")
        self.authenticated_client(member).post(reverse("organizations:organization-invitation-accept"), {"token": self.token_from_mail()}, format="json")
        membership = OrganizationMembership.objects.get(user=member, organization=self.organization)
        self.assertEqual(membership.role, "member")
        raise_it = self.authenticated_client(member).patch(
            reverse("organizations:organization-member-detail", kwargs={"organization_id": self.organization.pk, "membership_id": membership.pk}), {"role": "owner"}, format="json",
        )
        self.assertEqual(raise_it.status_code, status.HTTP_403_FORBIDDEN)
        membership.refresh_from_db()
        self.assertEqual(membership.role, "member")

    def test_the_chain_of_invitations_owner_moderator_member(self):
        # the owner may invite a moderator and a member; a moderator only a member; a member nobody; nobody can invite an owner
        self.assertEqual(self.invite(self.organization, "m1@example.com", "moderator", self.owner_client).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.invite(self.organization, "p1@example.com", "member", self.owner_client).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.invite(self.organization, "p2@example.com", "member", self.moderator_client).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.invite(self.organization, "m2@example.com", "moderator", self.moderator_client).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.invite(self.organization, "p3@example.com", "member", self.person_client).status_code, status.HTTP_403_FORBIDDEN)
        for client in (self.owner_client, self.moderator_client):
            self.assertEqual(self.invite(self.organization, "o2@example.com", "owner", client).status_code, status.HTTP_400_BAD_REQUEST)

    def test_accepting_an_invitation_moves_none_of_the_persons_existing_data(self):
        joiner = self.make_user("iso-joiner", email="joiner@example.com")
        workspace = create_personal_organization(joiner)
        before = self.make_stored_image(joiner, workspace, "mine.jpg")
        self.invite(self.organization, "joiner@example.com", "member")
        self.authenticated_client(joiner).post(reverse("organizations:organization-invitation-accept"), {"token": self.token_from_mail()}, format="json")
        before.batch.refresh_from_db()
        self.assertEqual(before.batch.organization_id, workspace.pk)                       # still in the private workspace: nothing is moved into the organization
        self.assertEqual(UploadBatch.objects.filter(owner=joiner, organization=self.organization).count(), 0)

    def test_removing_a_member_leaves_their_personal_workspace_alone(self):
        image = self.make_stored_image(self.person, self.personal, "private.jpg")
        membership = OrganizationMembership.objects.get(user=self.person, organization=self.organization)
        removed = self.owner_client.delete(reverse("organizations:organization-member-detail", kwargs={"organization_id": self.organization.pk, "membership_id": membership.pk}), format="json")
        self.assertIn(removed.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT), removed.content)
        self.assertEqual(self.person_client.get(reverse("image-upload-detail", kwargs={"pk": image.pk})).status_code, status.HTTP_200_OK)
        self.assertTrue(OrganizationMembership.objects.filter(user=self.person, organization=self.personal, status="active").exists())


class DefaultWorkspaceTests(Fixture, TestCase):
    def test_an_upload_without_a_choice_goes_to_the_personal_workspace_not_the_organization(self):
        response = self.upload(self.person_client)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(UploadBatch.objects.get(owner=self.person).organization_id, self.personal.pk)

    def test_an_upload_with_an_explicit_organization_goes_there(self):
        response = self.upload(self.person_client, self.organization.pk)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(UploadBatch.objects.get(owner=self.person).organization_id, self.organization.pk)

    def test_an_organization_the_person_does_not_belong_to_or_that_is_not_active_cannot_be_chosen(self):
        other = create_organization(name="Not Mine", owner=self.make_user("iso-not-mine"))
        self.assertEqual(self.upload(self.person_client, other.pk).status_code, status.HTTP_400_BAD_REQUEST)
        with self.captureOnCommitCallbacks(execute=True):
            waiting = self.admin_client.post(reverse("organizations:organization-admin-list-create"), {"name": "Waiting", "owner_email": "w@example.com"}, format="json")
        self.assertEqual(self.upload(self.person_client, waiting.data["id"]).status_code, status.HTTP_400_BAD_REQUEST)
        Organization.objects.filter(pk=self.organization.pk).update(status="suspended")
        self.assertEqual(self.upload(self.person_client, self.organization.pk).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(UploadBatch.objects.filter(owner=self.person).count(), 0)

    def test_somebody_without_a_personal_workspace_is_never_filed_under_the_organization_they_belong_to(self):
        legacy = self.add("iso-legacy", Role.MODERATOR)                                   # an account from before workspaces were ensured: a membership, no personal workspace
        self.assertIsNone(get_default_organization(legacy))
        self.assertIsNone(resolve_upload_organization(legacy, None))
        response = self.upload(self.authenticated_client(legacy))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(UploadBatch.objects.get(owner=legacy).organization_id)         # "no organization", exactly like a private photograph
        self.assertNotIn(UploadBatch.objects.get(owner=legacy).pk, [None])
        self.assertEqual(self.moderator_client.get(reverse("moderation:batch-detail", kwargs={"organization_id": self.organization.pk, "batch_id": UploadBatch.objects.get(owner=legacy).pk})).status_code, status.HTTP_404_NOT_FOUND)

    def test_the_organization_staff_do_not_see_the_uploads_of_somebody_without_a_choice(self):
        legacy = self.add("iso-legacy2", Role.MEMBER)
        self.upload(self.authenticated_client(legacy))
        mine = ImageUpload.objects.get(owner=legacy)
        listing = self.owner_client.get(reverse("moderation:image-list", kwargs={"organization_id": self.organization.pk}))
        rows = listing.data["results"] if isinstance(listing.data, dict) else listing.data
        self.assertNotIn(mine.pk, {row["id"] for row in rows})

    def test_ensure_personal_organization_is_idempotent(self):
        legacy = self.add("iso-ensure", Role.MEMBER)
        first = ensure_personal_organization(legacy)
        second = ensure_personal_organization(legacy)
        self.assertEqual((first.pk, first.is_personal), (second.pk, True))
        self.assertEqual(OrganizationMembership.objects.filter(user=legacy, organization__is_personal=True).count(), 1)
        self.assertEqual(get_default_organization(legacy), first)

    def test_the_command_gives_every_account_a_workspace_and_a_dry_run_changes_nothing(self):
        legacy = self.add("iso-cmd", Role.MEMBER)
        out = io.StringIO()
        call_command("ensure_personal_workspaces", stdout=out)
        self.assertIsNone(personal_organization_of(legacy))
        self.assertIn("dry run", out.getvalue())
        before = self.make_stored_image(legacy, self.organization, "org.jpg")
        call_command("ensure_personal_workspaces", "--apply", stdout=io.StringIO())
        self.assertIsNotNone(personal_organization_of(legacy))
        before.batch.refresh_from_db()
        self.assertEqual(before.batch.organization_id, self.organization.pk)              # nothing already filed was moved
        call_command("ensure_personal_workspaces", "--apply", stdout=io.StringIO())
        self.assertEqual(OrganizationMembership.objects.filter(user=legacy, organization__is_personal=True).count(), 1)

    def test_an_administrator_created_account_gets_its_own_workspace_with_the_account(self):
        created = self.admin_client.post(reverse("admin-user-list"), {"username": "madeby", "email": "madeby@example.com", "role": "moderator", "organization_id": self.organization.pk, "reason": "Pilot organization needs a moderator before its owner can invite one"}, format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.content)
        user = User.objects.get(username="madeby")
        self.assertIsNotNone(personal_organization_of(user))
        self.assertEqual(sorted(self.memberships_of(user)), [(False, "moderator", "active"), (True, "owner", "active")])


class CrossWorkspaceIsolationTests(Fixture, TestCase):
    """The person belongs to the organization and has a private workspace. Staff see what is filed under the organization, never what is filed under the private workspace."""

    def setUp(self):
        super().setUp()
        self.private_image = self.make_stored_image(self.person, self.personal, "private.jpg")
        self.shared_image = self.make_stored_image(self.person, self.organization, "shared.jpg")
        self.stranger = self.make_user("iso-stranger")
        self.stranger_client = self.authenticated_client(self.stranger)

    def staff_rows(self, client):
        response = client.get(reverse("moderation:image-list", kwargs={"organization_id": self.organization.pk}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["results"] if isinstance(response.data, dict) else response.data
        return {row["id"] for row in data}

    def test_the_staff_list_shows_the_shared_image_and_never_the_private_one(self):
        for client in (self.owner_client, self.moderator_client):
            rows = self.staff_rows(client)
            self.assertIn(self.shared_image.pk, rows)
            self.assertNotIn(self.private_image.pk, rows)

    def test_the_staff_cannot_preview_or_open_the_private_image_or_its_upload(self):
        for client in (self.owner_client, self.moderator_client):
            preview = lambda image: client.get(reverse("moderation:image-preview", kwargs={"organization_id": self.organization.pk, "image_id": image.pk}))
            self.assertEqual(preview(self.shared_image).status_code, status.HTTP_200_OK)
            self.assertEqual(preview(self.private_image).status_code, status.HTTP_404_NOT_FOUND)
            self.assertEqual(client.get(reverse("moderation:batch-detail", kwargs={"organization_id": self.organization.pk, "batch_id": self.private_image.batch_id})).status_code, status.HTTP_404_NOT_FOUND)
            self.assertEqual(client.get(reverse("moderation:batch-detail", kwargs={"organization_id": self.organization.pk, "batch_id": self.shared_image.batch_id})).status_code, status.HTTP_200_OK)
            for name in ("image-upload-detail", "image-upload-download"):                  # the person's own routes are theirs alone
                self.assertEqual(client.get(reverse(name, kwargs={"pk": self.private_image.pk})).status_code, status.HTTP_404_NOT_FOUND, name)
            self.assertEqual(client.get(reverse("image-thumbnail", kwargs={"pk": self.private_image.pk})).status_code, status.HTTP_404_NOT_FOUND)

    def test_the_staff_cannot_open_the_private_image_through_an_organization_they_are_not_in(self):
        other = create_organization(name="Rival", owner=self.make_user("iso-rival"))
        response = self.owner_client.get(reverse("moderation:image-preview", kwargs={"organization_id": other.pk, "image_id": self.shared_image.pk}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_private_image_stays_the_persons_own_and_is_unchanged_for_them(self):
        self.assertEqual(self.person_client.get(reverse("image-upload-download", kwargs={"pk": self.private_image.pk})).status_code, status.HTTP_200_OK)       # personal downloads work as always
        self.assertEqual(self.person_client.get(reverse("image-upload-detail", kwargs={"pk": self.private_image.pk})).status_code, status.HTTP_200_OK)
        self.assertEqual(self.stranger_client.get(reverse("image-upload-download", kwargs={"pk": self.private_image.pk})).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.stranger_client.get(reverse("image-thumbnail", kwargs={"pk": self.private_image.pk})).status_code, status.HTTP_404_NOT_FOUND)

    def test_the_organization_export_holds_the_shared_image_and_not_the_private_one(self):
        from privacy.services import build_organization_export_payload

        payload = build_organization_export_payload(self.organization)
        text = repr(payload)
        self.assertIn("shared.jpg", text)
        self.assertNotIn("private.jpg", text)

    def test_a_person_folder_that_holds_both_shows_staff_only_the_shared_photographs(self):
        def face(image, name):
            pre = ImagePreprocessingResult.objects.get(image=image) if ImagePreprocessingResult.objects.filter(image=image).exists() else ImagePreprocessingResult.objects.create(owner=self.person, image=image, status="completed")
            validation = FaceValidationResult.objects.create(owner=self.person, image=image, preprocessing_result=pre, decision="accepted", face_count=1, faces=[])
            analysis = IdentityAnalysis.objects.create(owner=self.person, image=image, preprocessing_result=pre, face_validation=validation, status="completed", provider="t", policy_version="v1")
            return IdentityFace.objects.create(owner=self.person, image=image, analysis=analysis, face_index=0, protected_embedding=b"e", embedding_dimension=3, provider="t")

        group = IdentityGroup.objects.create(owner=self.person, display_name="Alex", member_count=2, protected_centroid=b"c", embedding_dimension=3, provider="t", policy_version="v1")
        for image in (self.shared_image, self.private_image):
            IdentityMembership.objects.create(owner=self.person, identity_face=face(image, "x"), identity_group=group, state=IdentityMembership.State.NEW_GROUP, decision_method="t")
        listing = self.moderator_client.get(reverse("moderation:identity-group-list", kwargs={"organization_id": self.organization.pk}))
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        rows = listing.data["results"] if isinstance(listing.data, dict) else listing.data
        row = next(item for item in rows if item["id"] == group.pk)
        self.assertEqual(row["photos_in_organization"], 1)
        self.assertEqual([cover["id"] for cover in row["cover_images"]], [self.shared_image.pk])        # only the shared photograph is shown
        self.assertEqual(row["member_count"], 1)                                                            # and only the shared ones are counted for staff

    def test_a_personal_account_with_no_organization_is_invisible_to_every_organization(self):
        loner = self.make_user("iso-loner")
        create_personal_organization(loner)
        image = self.make_stored_image(loner, personal_organization_of(loner), "loner.jpg")
        self.assertNotIn(image.pk, self.staff_rows(self.owner_client))
        response = self.owner_client.get(reverse("moderation:image-preview", kwargs={"organization_id": self.organization.pk, "image_id": image.pk}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class AdministratorSeparationTests(Fixture, TestCase):
    def users(self, **params):
        response = self.admin_client.get(reverse("admin-user-list"), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        data = response.data["results"] if isinstance(response.data, dict) else response.data
        return {row["username"]: row for row in data}

    def test_the_user_list_separates_personal_users_from_organization_users(self):
        personal = self.make_user("iso-personal-only")
        create_personal_organization(personal)
        everyone = self.users()
        self.assertEqual((everyone["iso-personal-only"]["account_type"], everyone["iso-personal-only"]["shared_organization_count"]), ("personal", 0))
        self.assertEqual((everyone["iso-person"]["account_type"], everyone["iso-person"]["shared_organization_count"]), ("organization", 1))
        self.assertEqual(everyone["iso-owner"]["account_type"], "organization")
        only_personal = self.users(account_type="personal")
        self.assertIn("iso-personal-only", only_personal)
        self.assertNotIn("iso-person", only_personal)
        self.assertNotIn("iso-owner", only_personal)
        only_organization = self.users(account_type="organization")
        self.assertTrue({"iso-owner", "iso-moderator", "iso-person"} <= set(only_organization))
        self.assertNotIn("iso-personal-only", only_organization)
        self.assertEqual(self.admin_client.get(reverse("admin-user-list"), {"account_type": "bogus"}).status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_organizations_list_can_leave_the_private_workspaces_out(self):
        listing = lambda kind: self.admin_client.get(reverse("organizations:organization-admin-list-create"), {"kind": kind})
        rows = lambda response: response.data["results"] if isinstance(response.data, dict) else response.data
        shared = rows(listing("organization"))
        self.assertTrue(shared and not any(row["is_personal"] for row in shared))
        personal = rows(listing("personal"))
        self.assertTrue(personal and all(row["is_personal"] for row in personal))
        everything = rows(listing("all"))
        self.assertEqual(len(everything), len(shared) + len(personal))
        self.assertEqual(len(rows(self.admin_client.get(reverse("organizations:organization-admin-list-create")))), len(everything))          # no parameter: unchanged
        self.assertEqual(listing("bogus").status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_account_detail_marks_the_private_workspace_and_shows_storage(self):
        response = self.admin_client.get(reverse("admin-user-update", kwargs={"pk": self.person.pk}))
        flags = {row["name"]: row["is_personal"] for row in response.data["organizations"]}
        self.assertEqual(flags[self.organization.name], False)
        self.assertEqual(flags[self.personal.name], True)
        self.make_stored_image(self.person, self.personal, "sized.jpg")
        ImageUpload.objects.filter(owner=self.person).update(file_size=1234)
        again = self.admin_client.get(reverse("admin-user-update", kwargs={"pk": self.person.pk}))
        self.assertEqual(again.data["usage"]["storage_bytes"], 1234)

    def test_an_administrator_creates_an_account_without_ever_knowing_a_password(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.admin_client.post(reverse("admin-user-list"), {"username": "nopw", "email": "nopw@example.com", "role": "user"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        user = User.objects.get(username="nopw")
        self.assertFalse(user.has_usable_password())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/reset-password/", mail.outbox[0].body)                                  # a link to choose their own
        self.assertNotIn("password:", mail.outbox[0].body.lower())
        self.assertEqual([key for key in response.data if "password" in key], ["password_setup_pending"])        # a yes/no flag; never a password, hash or token
        self.assertIs(response.data["password_setup_pending"], True)
        self.assertTrue(user.is_email_verified)

    def test_an_administrator_can_send_a_reset_link_and_never_sees_a_password_or_a_token(self):
        target = self.person
        with self.captureOnCommitCallbacks(execute=True):
            response = self.admin_client.post(reverse("admin-user-password-reset", kwargs={"pk": target.pk}))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.data["email"], target.email)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [target.email])
        link = re.search(r"/reset-password/([^/\s]+)/([^/\s]+)", mail.outbox[0].body)
        self.assertIsNotNone(link)
        self.assertNotIn(link.group(2), str(response.content))                                  # the token is in the person's inbox only
        self.assertNotIn("/reset-password/", str(response.content))
        from accounts.models import AuthEvent

        self.assertTrue(AuthEvent.objects.filter(event="admin_password_reset_sent", user=target).exists())

    def test_the_reset_link_is_for_the_administrator_only_and_for_resettable_accounts(self):
        url = reverse("admin-user-password-reset", kwargs={"pk": self.person.pk})
        self.assertEqual(self.person_client.post(url).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.owner_client.post(url).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.post(url).status_code, status.HTTP_401_UNAUTHORIZED)
        User.objects.filter(pk=self.person.pk).update(is_suspended=True)
        self.assertEqual(self.admin_client.post(url).status_code, status.HTTP_409_CONFLICT)
        other_admin = self.make_user("iso-admin-2", role="admin")
        self.assertEqual(self.admin_client.post(reverse("admin-user-password-reset", kwargs={"pk": other_admin.pk})).status_code, status.HTTP_403_FORBIDDEN)      # an administrator account needs a superuser
        self.assertEqual(self.admin_client.post(reverse("admin-user-password-reset", kwargs={"pk": 999999})).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(len(mail.outbox), 0)

    def test_no_plaintext_password_is_stored_or_returned_anywhere(self):
        self.admin_client.post(reverse("admin-user-list"), {"username": "withpw", "email": "withpw@example.com", "password": PASSWORD, "role": "user"}, format="json")
        user = User.objects.get(username="withpw")
        self.assertNotEqual(user.password, PASSWORD)
        self.assertTrue(user.check_password(PASSWORD))
        for name in ("admin-user-list",):
            self.assertNotIn(PASSWORD, str(self.admin_client.get(reverse(name)).content))
        self.assertNotIn(PASSWORD, str(self.admin_client.get(reverse("admin-user-update", kwargs={"pk": user.pk})).content))
        self.assertTrue(all(PASSWORD not in message.body for message in mail.outbox))
