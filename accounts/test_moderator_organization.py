"""A moderator belongs to an organization: no organization, no moderator sign-in (and the ways one is attached)."""
from io import StringIO

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationMembership
from organizations.services import create_organization, create_personal_organization

from .admin import FaceCloudUserChangeForm, FaceCloudUserCreationForm
from .models import AccountInvitation, AuthEvent, User
from .services import dashboard_for_user, has_moderator_organization

PASSWORD = "ForestRiver!4821"
Role = OrganizationMembership.Role
State = OrganizationMembership.Status


class Fixtures:
    def setUp(self):
        cache.clear()
        self.admin = self.person("mo-admin", role="admin")
        self.owner = self.person("mo-owner")
        self.organization = create_organization(name="Mod Co", owner=self.owner)

    def person(self, name, **extra):
        return User.objects.create_user(username=name, email=f"{name}@example.com", password=PASSWORD, is_email_verified=True, **extra)

    def join(self, user, organization=None, role=Role.MODERATOR, status_value=State.ACTIVE):
        return OrganizationMembership.objects.create(organization=organization or self.organization, user=user, role=role, status=status_value)

    def sign_in(self, user, password=PASSWORD):
        return APIClient().post(reverse("login"), {"identifier": user.username, "password": password}, format="json")

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client


class SignInTests(Fixtures, TestCase):
    def test_a_moderator_account_with_no_organization_is_refused_with_a_clear_code(self):
        stranded = self.person("stranded", role="moderator")
        response = self.sign_in(stranded)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json(), {"detail": "Your moderator account is not assigned to an organization yet.", "code": "no_organization"})
        self.assertTrue(AuthEvent.objects.filter(user=stranded, event="login_failed", details__reason="no_organization").exists())

    def test_the_wrong_password_still_reads_as_a_wrong_password(self):
        stranded = self.person("stranded2", role="moderator")
        response = self.sign_in(stranded, "Wrong!Password9")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)          # nothing about organizations is revealed without the password

    def test_with_an_active_moderator_membership_the_account_signs_in_to_the_moderator_portal(self):
        moderator = self.person("placed", role="moderator")
        self.join(moderator)
        response = self.sign_in(moderator)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["role"], response.json()["dashboard"]), ("moderator", "moderator"))

    def test_an_organization_that_is_gone_suspended_or_a_personal_workspace_does_not_count(self):
        for label, prepare in (
            ("suspended", lambda: Organization.objects.filter(pk=self.organization.pk).update(status=Organization.Status.SUSPENDED)),
            ("membership suspended", lambda: OrganizationMembership.objects.filter(user=moderator).update(status=State.SUSPENDED)),
            ("membership removed", lambda: OrganizationMembership.objects.filter(user=moderator).update(status=State.REMOVED)),
        ):
            with self.subTest(label):
                moderator = self.person(f"m-{label.replace(' ', '-')}", role="moderator")
                self.join(moderator)
                prepare()
                self.assertEqual(self.sign_in(moderator).status_code, status.HTTP_403_FORBIDDEN)
                Organization.objects.filter(pk=self.organization.pk).update(status=Organization.Status.ACTIVE)
        personal_only = self.person("personal-only", role="moderator")
        create_personal_organization(personal_only)
        self.assertEqual(self.sign_in(personal_only).status_code, status.HTTP_403_FORBIDDEN)

    def test_a_stored_moderator_who_owns_an_organization_may_sign_in_but_the_owner_is_not_in_the_moderator_portal(self):
        owner_moderator = self.person("owner-mod", role="moderator")
        self.join(owner_moderator, role=Role.OWNER)
        response = self.sign_in(owner_moderator)
        self.assertEqual((response.status_code, response.json()["dashboard"]), (200, "user"))

    def test_a_user_who_moderates_an_organization_is_a_moderator_without_the_stored_role(self):
        member = self.person("membership-only")
        self.join(member)
        self.assertEqual(dashboard_for_user(member), "moderator")

    def test_administrators_and_ordinary_users_are_unaffected(self):
        self.assertEqual(self.sign_in(self.admin).json()["dashboard"], "admin")
        self.assertEqual(self.sign_in(self.person("plain")).json()["dashboard"], "user")

    def test_a_token_refresh_is_refused_too_once_the_organization_is_lost(self):
        moderator = self.person("loses-org", role="moderator")
        membership = self.join(moderator)
        tokens = self.sign_in(moderator).json()
        OrganizationMembership.objects.filter(pk=membership.pk).update(status=State.REMOVED)
        response = APIClient().post(reverse("refresh"), {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["code"], "no_organization")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", FRONTEND_URL="https://app.facecloud.test")
class InvitationTests(Fixtures, TestCase):
    def invite(self, **body):
        body = {"email": "new.moderator@example.org", "role": "moderator", **body}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client_for(self.admin).post("/api/user/admin/invitations/", body, format="json")

    def token(self):
        import re

        return re.search(r"token=([\w-]+)", mail.outbox[-1].body).group(1)

    def accept(self, token, username="newmod"):
        return APIClient().post("/api/auth/invitations/accept/", {"token": token, "username": username, "password": PASSWORD}, format="json")

    def test_a_moderator_invitation_must_name_an_organization(self):
        response = self.invite()
        self.assertEqual(response.status_code, 400)
        self.assertIn("organization_id", response.json())
        self.assertFalse(AccountInvitation.objects.exists())

    def test_only_a_moderator_invitation_may_name_one(self):
        response = self.invite(role="user", organization_id=self.organization.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("organization_id", response.json())
        self.assertEqual(self.invite(role="user").status_code, 201)

    def test_the_organization_must_be_a_real_active_shared_one(self):
        personal = create_personal_organization(self.person("someone-else"))
        suspended = create_organization(name="Frozen Co", owner=self.person("frozen-owner"))
        Organization.objects.filter(pk=suspended.pk).update(status=Organization.Status.SUSPENDED)
        for organization_id in (999999, personal.pk, suspended.pk):
            self.assertEqual(self.invite(organization_id=organization_id).status_code, 400, organization_id)
        self.assertFalse(AccountInvitation.objects.exists())

    def test_the_invitation_shows_its_organization_and_the_email_names_it(self):
        response = self.invite(organization_id=self.organization.pk)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["organization"], {"id": self.organization.pk, "name": "Mod Co"})
        self.assertIn("for Mod Co", mail.outbox[-1].body)
        preview = APIClient().post("/api/auth/invitations/inspect/", {"token": self.token()}, format="json").json()
        self.assertEqual((preview["role"], preview["organization_name"]), ("moderator", "Mod Co"))
        listed = self.client_for(self.admin).get("/api/user/admin/invitations/").json()
        rows = listed if isinstance(listed, list) else listed["results"]
        self.assertEqual(rows[0]["organization"]["name"], "Mod Co")

    def test_accepting_creates_the_account_and_the_moderator_membership_together(self):
        self.invite(organization_id=self.organization.pk)
        accepted = self.accept(self.token())
        self.assertEqual(accepted.status_code, 201, accepted.content)
        user = User.objects.get(username="newmod")
        membership = OrganizationMembership.objects.get(user=user, organization=self.organization)
        self.assertEqual((membership.role, membership.status, membership.invited_by_id), (Role.MODERATOR, State.ACTIVE, self.admin.pk))
        self.assertEqual(accepted.json()["dashboard"], "moderator")
        self.assertEqual(self.sign_in(user).status_code, 200)

    def test_if_the_organization_cannot_take_the_moderator_any_more_nothing_is_created(self):
        self.invite(organization_id=self.organization.pk)
        token = self.token()
        Organization.objects.filter(pk=self.organization.pk).update(status=Organization.Status.SUSPENDED)
        response = self.accept(token)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username="newmod").exists())
        self.assertEqual(AccountInvitation.objects.get().status, AccountInvitation.Status.PENDING)       # still usable once the organization is back
        Organization.objects.filter(pk=self.organization.pk).update(status=Organization.Status.ACTIVE)
        self.assertEqual(self.accept(token).status_code, 201)

    def test_an_ordinary_invitation_creates_no_membership_beyond_the_personal_workspace(self):
        self.invite(role="user")
        self.assertEqual(self.accept(self.token(), "plainuser").status_code, 201)
        self.assertFalse(OrganizationMembership.objects.filter(user__username="plainuser", organization=self.organization).exists())


class AdminRoleChangeTests(Fixtures, TestCase):
    def patch(self, target, **body):
        return self.client_for(self.admin).patch(f"/api/user/admin/users/{target.pk}/", body, format="json")

    def test_making_someone_a_moderator_needs_an_organization_when_there_is_a_choice(self):
        second = create_organization(name="Second Co", owner=self.person("second-owner"))
        target = self.person("promoted")
        response = self.patch(target, role="moderator")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "There is more than one organization: send organization_id to choose one.")
        target.refresh_from_db()
        self.assertEqual(target.role, "user")                                       # nothing half-done
        done = self.patch(target, role="moderator", organization_id=second.pk)
        self.assertEqual(done.status_code, 200, done.content)
        membership = OrganizationMembership.objects.get(user=target)
        self.assertEqual((membership.organization_id, membership.role, membership.status, membership.invited_by_id),
                         (second.pk, Role.MODERATOR, State.ACTIVE, self.admin.pk))
        self.assertEqual(self.sign_in(target).json()["dashboard"], "moderator")

    def test_with_exactly_one_organization_it_is_chosen_for_you_and_with_none_there_is_a_clear_message(self):
        target = self.person("single-choice")
        self.assertEqual(self.patch(target, role="moderator").status_code, 200)
        self.assertTrue(OrganizationMembership.objects.filter(user=target, organization=self.organization, role=Role.MODERATOR).exists())
        Organization.objects.filter(pk=self.organization.pk).delete()
        other = self.person("no-organizations")
        response = self.patch(other, role="moderator")
        self.assertEqual((response.status_code, response.json()["detail"]), (400, "Create an organization first, then try again."))

    def test_a_personal_workspace_or_an_unknown_organization_is_refused(self):
        target = self.person("bad-choice")
        personal = create_personal_organization(self.person("elsewhere"))
        self.assertEqual(self.patch(target, role="moderator", organization_id=personal.pk).status_code, 400)
        self.assertEqual(self.patch(target, role="moderator", organization_id=999999).status_code, 400)

    def test_someone_who_already_moderates_somewhere_needs_no_organization_to_be_given_the_role(self):
        target = self.person("already-in")
        self.join(target)
        self.assertEqual(self.patch(target, role="moderator").status_code, 200)

    def test_other_changes_do_not_touch_organizations(self):
        target = self.person("just-suspended")
        self.assertEqual(self.patch(target, is_suspended=True, suspension_reason="x", organization_id=self.organization.pk).status_code, 200)
        self.assertFalse(OrganizationMembership.objects.filter(user=target).exists())


class DjangoAdminFormTests(Fixtures, TestCase):
    def change_data(self, user, **extra):
        data = {"username": user.username, "email": user.email, "role": user.role, "password": user.password, "is_active": "on",
                "date_joined": "2026-01-01 00:00:00", "token_version": user.token_version}
        data.update(extra)
        return data

    def test_a_moderator_cannot_be_saved_without_an_organization(self):
        user = self.person("form-mod")
        form = FaceCloudUserChangeForm(self.change_data(user, role="moderator"), instance=user)
        self.assertFalse(form.is_valid())
        self.assertIn("moderator_organization", form.errors)

    def test_choosing_an_organization_creates_the_membership_on_save(self):
        user = self.person("form-mod2")
        form = FaceCloudUserChangeForm(self.change_data(user, role="moderator", moderator_organization=self.organization.pk), instance=user)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertTrue(has_moderator_organization(user))

    def test_an_account_that_already_moderates_can_be_saved_without_choosing_again(self):
        user = self.person("form-mod3", role="moderator")
        self.join(user)
        form = FaceCloudUserChangeForm(self.change_data(user), instance=user)
        self.assertTrue(form.is_valid(), form.errors)

    def test_users_and_administrators_are_unaffected_and_personal_workspaces_are_not_offered(self):
        user = self.person("form-user")
        self.assertTrue(FaceCloudUserChangeForm(self.change_data(user), instance=user).is_valid())
        admin_form = FaceCloudUserChangeForm(self.change_data(user, role="admin"), instance=user)
        self.assertTrue(admin_form.is_valid(), admin_form.errors)
        personal = create_personal_organization(self.person("form-personal"))
        offered = list(FaceCloudUserChangeForm(instance=user).fields["moderator_organization"].queryset)
        self.assertIn(self.organization, offered)
        self.assertNotIn(personal, offered)

    def test_the_admin_pages_render(self):
        root = User.objects.create_superuser("django-root", "django-root@example.com", PASSWORD)
        self.client.force_login(root)
        user = self.person("rendered", role="moderator")
        self.assertEqual(self.client.get(f"/admin/accounts/user/{user.pk}/change/").status_code, 200)
        self.assertEqual(self.client.get("/admin/accounts/user/add/").status_code, 200)

    def test_the_add_form_asks_for_the_same(self):
        data = {"username": "added-mod", "email": "added-mod@example.com", "password1": PASSWORD, "password2": PASSWORD, "role": "moderator"}
        self.assertFalse(FaceCloudUserCreationForm(data).is_valid())
        form = FaceCloudUserCreationForm({**data, "moderator_organization": self.organization.pk})
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertTrue(has_moderator_organization(user))


class ReportCommandTests(Fixtures, TestCase):
    def run_report(self):
        out = StringIO()
        call_command("report_moderators_without_organization", stdout=out)
        return out.getvalue()

    def test_it_lists_only_the_stranded_moderators_and_changes_nothing(self):
        stranded = self.person("zed-stranded", role="moderator")
        placed = self.person("placed-mod", role="moderator")
        self.join(placed)
        text = self.run_report()
        self.assertIn("zed-stranded\tzed-stranded@example.com", text)
        self.assertNotIn("placed-mod", text)
        self.assertIn("1 moderator account(s) without an organization", text)
        stranded.refresh_from_db()
        self.assertEqual(stranded.role, "moderator")                              # no demotion
        self.assertEqual(OrganizationMembership.objects.filter(user=stranded).count(), 0)

    def test_when_there_are_none_it_says_so(self):
        self.assertIn("Every moderator account belongs to an organization.", self.run_report())
