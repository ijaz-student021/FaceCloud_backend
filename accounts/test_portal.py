"""Item 4: the portal (``role`` and ``dashboard`` in the login response) follows organization moderator membership."""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from organizations.models import Organization, OrganizationMembership

from .services import dashboard_for_user, issue_login_tokens

User = get_user_model()
PASSWORD = "StrongPass!4729"


class PortalTests(APITestCase):
    def make(self, username, **fields):
        return User.objects.create_user(username=username, email=f"{username}@example.com", password=PASSWORD, is_email_verified=True, **fields)

    def organization(self, name="Acme", **fields):
        return Organization.objects.create(name=name, slug=name.lower(), **fields)

    def join(self, user, organization, role, status_=OrganizationMembership.Status.ACTIVE):
        return OrganizationMembership.objects.create(user=user, organization=organization, role=role, status=status_)

    def login(self, user):
        return self.client.post(reverse("login"), {"email": user.email, "password": PASSWORD}, format="json")

    def assertPortal(self, user, expected):
        response = self.login(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual((response.data["role"], response.data["dashboard"]), (expected, expected))
        self.assertEqual(response.data["role"], response.data["dashboard"])
        self.assertEqual(dashboard_for_user(user), expected)

    def test_a_plain_user_is_a_user(self):
        self.assertPortal(self.make("plain"), "user")

    def test_an_organization_moderator_gets_the_moderator_portal(self):
        user = self.make("mod")
        self.join(user, self.organization(), OrganizationMembership.Role.MODERATOR)
        self.assertPortal(user, "moderator")
        self.assertEqual(self.login(user).data["user"]["role"], "user")      # what is stored is unchanged

    def test_an_organization_owner_alone_stays_a_user(self):
        user = self.make("owner")
        self.join(user, self.organization(), OrganizationMembership.Role.OWNER)
        self.join(user, self.organization("Second"), OrganizationMembership.Role.MEMBER)
        self.assertPortal(user, "user")

    def test_a_moderator_membership_that_is_not_active_does_not_count(self):
        user = self.make("gone")
        organization = self.organization()
        for state in (OrganizationMembership.Status.INVITED, OrganizationMembership.Status.SUSPENDED, OrganizationMembership.Status.REMOVED):
            OrganizationMembership.objects.filter(user=user).delete()
            self.join(user, organization, OrganizationMembership.Role.MODERATOR, state)
            with self.subTest(state=state):
                self.assertPortal(user, "user")

    def test_a_moderator_of_a_suspended_organization_is_a_user(self):
        user = self.make("frozen")
        self.join(user, self.organization(status=Organization.Status.SUSPENDED), OrganizationMembership.Role.MODERATOR)
        self.assertPortal(user, "user")

    def test_the_stored_roles_still_decide_moderator_and_admin(self):
        self.assertPortal(self.make("storedmod", role="moderator"), "moderator")
        self.assertPortal(self.make("storedadmin", role="admin"), "admin")
        self.assertPortal(self.make("root", is_superuser=True), "admin")

    def test_admin_wins_over_a_moderator_membership(self):
        admin = self.make("both", role="admin")
        self.join(admin, self.organization(), OrganizationMembership.Role.MODERATOR)
        self.assertPortal(admin, "admin")

    def test_the_permission_checks_are_unchanged_for_a_membership_moderator(self):
        user = self.make("limited")
        self.join(user, self.organization(), OrganizationMembership.Role.MODERATOR)
        token = self.login(user).data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(self.client.get(reverse("admin-user-list")).status_code, status.HTTP_403_FORBIDDEN)

    def test_every_login_path_builds_the_same_response(self):
        user = self.make("shape")
        self.join(user, self.organization(), OrganizationMembership.Role.MODERATOR)
        issued = issue_login_tokens(user)
        self.assertEqual((issued["role"], issued["dashboard"]), ("moderator", "moderator"))
