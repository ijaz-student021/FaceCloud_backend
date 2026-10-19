"""POST /api/user/admin/users/: an administrator creates a verified account (a user, or a moderator of an organization)."""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from organizations.models import Organization, OrganizationMembership
from organizations.services import create_organization, create_personal_organization

from .models import AuthEvent
from .services import create_account_as_admin

User = get_user_model()
PASSWORD = "Ocean!Breeze2026x"
URL_NAME = "admin-user-list"


class AdminCreateBase(APITestCase):
    """An administrator, an organization owner and helpers to sign in, create organizations and call the endpoint."""

    def setUp(self):
        self.admin = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        self.owner = User.objects.create_user("founder", "founder@example.com", PASSWORD, is_email_verified=True)

    def organization(self, name="Acme", **fields):
        organization = create_organization(name=name, owner=self.owner)
        if fields:
            Organization.objects.filter(pk=organization.pk).update(**fields)
            organization.refresh_from_db()
        return organization

    def as_user(self, user):
        response = self.client.post(reverse("login"), {"email": user.email, "password": PASSWORD}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        return response.data

    def create(self, **fields):
        payload = {"username": "sara", "email": "sara@example.com", "password": PASSWORD, "role": "user", **fields}
        self.as_user(self.admin)
        return self.client.post(reverse(URL_NAME), {k: v for k, v in payload.items() if v is not None}, format="json")


class AdminCreateUserTests(AdminCreateBase):
    # -- who may call it -------------------------------------------------------------------------------------------------
    def test_only_an_administrator_may_create_accounts(self):
        payload = {"username": "x", "email": "x@example.com", "password": PASSWORD, "role": "user"}
        self.assertEqual(self.client.post(reverse(URL_NAME), payload, format="json").status_code, status.HTTP_401_UNAUTHORIZED)
        for role in ("user", "moderator"):
            person = User.objects.create_user(f"p_{role}", f"p_{role}@example.com", PASSWORD, is_email_verified=True, role=role)
            if role == "moderator":
                OrganizationMembership.objects.create(organization=self.organization(f"Home of {role}"), user=person,
                                                      role=OrganizationMembership.Role.MODERATOR, status=OrganizationMembership.Status.ACTIVE)
            self.as_user(person)
            self.assertEqual(self.client.post(reverse(URL_NAME), payload, format="json").status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(User.objects.filter(username="x").exists())

    # -- a user ----------------------------------------------------------------------------------------------------------
    def test_an_administrator_creates_a_verified_user_with_no_membership(self):
        response = self.create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["role"], "user")
        self.assertEqual(response.data["username"], "sara")
        self.assertEqual(set(response.data), {"id", "username", "email", "full_name", "role", "is_email_verified", "is_suspended",
                                              "suspension_reason", "is_active", "date_joined", "last_login", "uploads_count"})
        user = User.objects.get(username="sara")
        self.assertTrue(user.is_email_verified and user.is_active and not user.is_suspended)
        self.assertEqual(user.role, "user")
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertFalse(OrganizationMembership.objects.filter(user=user).exists())
        self.assertTrue(user.check_password(PASSWORD))
        self.assertNotEqual(user.password, PASSWORD)
        self.assertEqual(len(mail.outbox), 0)                       # nothing is emailed, least of all a password
        self.client.credentials()
        login = self.client.post(reverse("login"), {"email": "sara@example.com", "password": PASSWORD}, format="json")
        self.assertEqual((login.status_code, login.data["role"], login.data["dashboard"]), (200, "user", "user"))

    def test_a_blank_username_is_derived_from_the_email_and_a_full_name_is_stored(self):
        response = self.create(username="", email="Sara.Khan@Example.com", full_name="  Sara   Ahmed Khan ")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        user = User.objects.get(email="sara.khan@example.com")
        self.assertEqual((user.username, user.first_name, user.last_name), ("sara.khan", "Sara", "Ahmed Khan"))
        self.assertEqual(response.data["full_name"], "Sara Ahmed Khan")
        omitted = self.create(username=None, email="lina@example.com")
        self.assertEqual(omitted.data["username"], "lina")

    def test_role_defaults_to_user_and_admin_can_never_be_created(self):
        self.assertEqual(self.create(role=None).data["role"], "user")
        refused = self.create(username="evil", email="evil@example.com", role="admin")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("role", refused.data)
        self.assertFalse(User.objects.filter(username="evil").exists())
        self.assertEqual(self.create(username="evil2", email="evil2@example.com", role="superuser").status_code, 400)

    # -- a moderator -----------------------------------------------------------------------------------------------------
    def test_a_moderator_joins_the_single_shared_organization_and_can_use_the_moderator_panel(self):
        create_personal_organization(self.owner)                     # a personal workspace never counts as "the organization"
        organization = self.organization()
        response = self.create(username="omar", email="omar@example.com", role="moderator")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["role"], "moderator")
        user = User.objects.get(username="omar")
        self.assertEqual(user.role, "moderator")
        membership = OrganizationMembership.objects.get(user=user)
        self.assertEqual((membership.organization_id, membership.role, membership.status),
                         (organization.pk, OrganizationMembership.Role.MODERATOR, OrganizationMembership.Status.ACTIVE))
        self.assertEqual(membership.invited_by_id, self.admin.pk)
        # the list shows the new role, and is still a plain array
        listed = self.client.get(reverse(URL_NAME))
        self.assertIsInstance(listed.data, list)
        self.assertEqual({row["username"]: row["role"] for row in listed.data}["omar"], "moderator")
        # the person can sign in as a moderator ...
        self.client.credentials()
        login = self.client.post(reverse("login"), {"identifier": "omar", "password": PASSWORD}, format="json")
        self.assertEqual((login.status_code, login.data["role"], login.data["dashboard"]), (200, "moderator", "moderator"))
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        # ... find that organization the way the panel does (first non-personal one) ...
        organizations = self.client.get("/api/organizations/").data
        organizations = organizations["results"] if isinstance(organizations, dict) else organizations
        self.assertEqual([o["id"] for o in organizations if not o["is_personal"]], [organization.pk])
        # ... and reach its moderator endpoints
        self.assertEqual(self.client.get(f"/api/organizations/{organization.pk}/moderator/dashboard/").status_code, status.HTTP_200_OK)

    def test_with_no_organization_a_moderator_is_refused_and_nothing_is_created(self):
        create_personal_organization(self.owner)
        response = self.create(username="omar", email="omar@example.com", role="moderator")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "Create an organization first, then try again.")
        self.assertFalse(User.objects.filter(username="omar").exists())

    def test_with_several_organizations_the_choice_must_be_made(self):
        first, second = self.organization("Acme"), self.organization("Globex")
        ambiguous = self.create(username="omar", email="omar@example.com", role="moderator")
        self.assertEqual(ambiguous.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("organization_id", ambiguous.data["detail"])
        self.assertFalse(User.objects.filter(username="omar").exists())
        chosen = self.create(username="omar", email="omar@example.com", role="moderator", organization_id=second.pk)
        self.assertEqual(chosen.status_code, status.HTTP_201_CREATED, chosen.data)
        self.assertEqual(OrganizationMembership.objects.get(user__username="omar").organization_id, second.pk)
        self.assertNotEqual(first.pk, second.pk)

    def test_a_bad_organization_is_refused(self):
        suspended = self.organization("Frozen", status=Organization.Status.SUSPENDED)
        personal = create_personal_organization(self.owner)
        self.organization("Acme")
        for organization_id in (999999, suspended.pk, personal.pk):
            with self.subTest(organization_id=organization_id):
                response = self.create(username=f"m{organization_id}", email=f"m{organization_id}@example.com", role="moderator",
                                       organization_id=organization_id)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("detail", response.data)
        self.assertFalse(User.objects.filter(username__startswith="m9").exists())
        self.assertEqual(User.objects.filter(role="moderator").count(), 0)

    def test_an_organization_is_only_for_moderators(self):
        organization = self.organization()
        response = self.create(role="user", organization_id=organization.pk)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("organization_id", response.data)
        self.assertEqual(self.create(role="user", organization_id=None).status_code, status.HTTP_201_CREATED)

    def test_a_failed_membership_leaves_no_half_created_account(self):
        organization = self.organization()
        with mock.patch("organizations.models.OrganizationMembership.objects.create", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                create_account_as_admin(administrator=self.admin, username="half", email="half@example.com", password=PASSWORD,
                                        role="moderator", organization=organization)
        self.assertFalse(User.objects.filter(username="half").exists())
        self.assertFalse(AuthEvent.objects.filter(event="admin_created_account").exists())

    # -- the same checks as signup ---------------------------------------------------------------------------------------
    def test_duplicates_are_field_errors(self):
        User.objects.create_user("taken", "taken@example.com", PASSWORD, is_email_verified=True)
        email = self.create(username="fresh", email="TAKEN@example.com")
        username = self.create(username="Taken", email="other@example.com")
        self.assertEqual((email.status_code, username.status_code), (400, 400))
        self.assertIn("email", email.data)
        self.assertIn("username", username.data)
        self.assertEqual(User.objects.filter(username__in=["fresh", "Taken"]).count(), 0)

    def test_the_password_rules_apply_and_the_error_is_under_password(self):
        for weak in ("qwertyuiop", "12345678", "short1!", "sara@example.com"):
            with self.subTest(password=weak):
                response = self.create(password=weak)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("password", response.data)
                self.assertNotIn(weak, json.dumps(list(response.data.values())))   # the message never repeats the password
        self.assertFalse(User.objects.filter(username="sara").exists())
        self.assertEqual(self.create(password=None).status_code, status.HTTP_400_BAD_REQUEST)

    # -- secrets and audit -----------------------------------------------------------------------------------------------
    def test_the_password_never_appears_in_a_response_the_log_or_the_audit_event(self):
        with self.assertNoLogs(level="DEBUG"):
            response = self.create(username="omar", email="omar@example.com", role="moderator") if self.organization() else None
        self.assertNotIn(PASSWORD, response.content.decode())
        listing = self.client.get(reverse(URL_NAME))
        self.assertNotIn(PASSWORD, listing.content.decode())
        self.assertNotIn("password", {key for row in listing.data for key in row})
        event = AuthEvent.objects.get(event="admin_created_account")
        self.assertEqual(event.user.username, "omar")
        self.assertEqual(event.identifier, "omar@example.com")
        self.assertEqual((event.details["administrator_id"], event.details["administrator"], event.details["role"]),
                         (self.admin.pk, "boss", "moderator"))
        self.assertNotIn(PASSWORD, json.dumps(event.details))
        self.assertEqual(len(mail.outbox), 0)

    def test_the_list_is_still_a_plain_array_with_the_same_fields(self):
        self.create()
        listed = self.client.get(reverse(URL_NAME))
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertIsInstance(listed.data, list)
        self.assertEqual({row["username"] for row in listed.data}, {"boss", "founder", "sara"})
        self.assertEqual(set(listed.data[0]), {"id", "username", "email", "full_name", "role", "is_email_verified", "is_suspended",
                                               "suspension_reason", "is_active", "date_joined", "last_login", "uploads_count"})

    def test_get_and_patch_on_other_urls_are_unchanged(self):
        self.create()
        sara = User.objects.get(username="sara")
        patched = self.client.patch(reverse("admin-user-update", args=[sara.pk]), {"is_suspended": True, "suspension_reason": "test"},
                                    format="json")
        self.assertEqual(patched.status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.put(reverse(URL_NAME), {}, format="json").status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class AdminCreatedAccountsWorkLikeSignedUpOnesTests(AdminCreateBase):
    """Admin-created accounts get no personal workspace (signup creates one when the email is verified). What that means in practice."""

    def png(self):
        import cv2
        import numpy as np

        return cv2.imencode(".png", np.random.default_rng(1).integers(0, 255, (200, 300, 3), dtype=np.uint8))[1].tobytes()

    def upload_as(self, username):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.credentials()
        login = self.client.post(reverse("login"), {"identifier": username, "password": PASSWORD}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        return self.client.post("/api/images/upload/single/", {"file": SimpleUploadedFile("a.png", self.png(), "image/png")}, format="multipart")

    def test_an_admin_created_user_can_use_the_app_and_their_uploads_belong_to_no_organization(self):
        from uploads.models import UploadBatch

        self.create(username="sara", email="sara@example.com")
        response = self.upload_as("sara")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(UploadBatch.objects.get(owner__username="sara").organization)

    def test_an_admin_created_moderators_own_uploads_are_tagged_to_the_organization_they_moderate(self):
        from uploads.models import UploadBatch

        organization = self.organization()
        self.create(username="omar", email="omar@example.com", role="moderator")
        response = self.upload_as("omar")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(UploadBatch.objects.get(owner__username="omar").organization_id, organization.pk)
