"""POST /api/auth/register/ with an organization invitation's token: the address counts as verified, the account is created, the invitation accepted, tokens returned."""
from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from organizations.services import create_organization, hash_token

from .models import AuthEvent, PendingRegistration, User

PASSWORD = "Ocean!Breeze2026x"
EMAIL = "newcomer@example.com"
SENTENCE = "This invitation is invalid, used or expired."


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RegisterFromInvitationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user("ri-owner", "ri-owner@example.com", PASSWORD, is_email_verified=True)
        self.organization = create_organization(name="Invite Co", owner=self.owner, plan=Organization.Plan.TEAM)
        self.token = "invitation-token-for-register-0123456789abcdef"
        self.invitation = self.invite(EMAIL, "member", self.token)
        self.url = reverse("register")

    def invite(self, email, role, token, **extra):
        return OrganizationInvitation.objects.create(
            organization=self.organization, email=email, role=role, token_hash=hash_token(token), invited_by=self.owner,
            expires_at=extra.pop("expires_at", timezone.now() + timedelta(hours=24)), **extra)

    def register(self, token="__default__", **fields):
        body = {"full_name": "New Comer", "email": EMAIL, "password": PASSWORD, "password_confirm": PASSWORD, **fields}
        if token == "__default__":
            token = self.token
        if token is not None:
            body["invitation_token"] = token
        return APIClient().post(self.url, body, format="json")

    def assert_nothing_created(self, email=EMAIL):
        self.assertFalse(User.objects.filter(email__iexact=email).exists())
        self.assertFalse(PendingRegistration.objects.exists())
        self.assertEqual(mail.outbox, [])
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.status, OrganizationInvitation.Status.PENDING)

    # -- the good path ---------------------------------------------------------------------------------------------------
    def test_a_valid_token_creates_a_verified_account_accepts_the_invitation_and_signs_the_person_in(self):
        response = self.register()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertTrue({"access", "refresh", "user", "role", "dashboard", "organization_roles"} <= set(body))
        self.assertEqual((body["role"], body["dashboard"]), ("user", "user"))
        self.assertEqual(body["organization_roles"], [{"organization_id": self.organization.pk, "name": "Invite Co", "role": "member", "status": "active"}])
        user = User.objects.get(email=EMAIL)
        self.assertTrue(user.is_email_verified)
        self.assertEqual((user.first_name, user.last_name, user.username), ("New", "Comer", "newcomer"))
        membership = OrganizationMembership.objects.get(user=user, organization=self.organization)
        self.assertEqual((membership.role, membership.status, membership.invited_by), ("member", "active", self.owner))
        self.assertTrue(OrganizationMembership.objects.filter(user=user, organization__is_personal=True, role="owner").exists())      # the usual personal workspace
        self.invitation.refresh_from_db()
        self.assertEqual((self.invitation.status, self.invitation.accepted_at is not None), ("accepted", True))
        self.assertEqual(mail.outbox, [])                                               # no verification mail
        self.assertFalse(PendingRegistration.objects.exists())
        self.assertTrue(AuthEvent.objects.filter(user=user, event="registered", details__via="organization_invitation").exists())

    def test_the_returned_tokens_work_and_so_does_signing_in_later(self):
        body = self.register().json()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
        self.assertEqual(client.get(reverse("organizations:organization-list")).status_code, 200)
        login = APIClient().post(reverse("login"), {"identifier": EMAIL, "password": PASSWORD}, format="json")
        self.assertEqual(login.status_code, 200, login.content)

    def test_a_moderator_invitation_makes_a_moderator_who_lands_in_the_moderator_portal(self):
        token = "moderator-invitation-token-0123456789abcdef"
        self.invite("mod-newcomer@example.com", "moderator", token)
        response = self.register(token=token, email="mod-newcomer@example.com")
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual((body["role"], body["dashboard"]), ("moderator", "moderator"))
        self.assertEqual(body["organization_roles"][0]["role"], "moderator")
        membership = OrganizationMembership.objects.get(user__email="mod-newcomer@example.com", organization=self.organization)
        self.assertEqual((membership.role, membership.invited_by), ("moderator", self.owner))

    def test_the_address_may_differ_in_case_and_the_username_may_be_chosen(self):
        self.invitation.email = "NewComer@Example.com"
        self.invitation.save()
        response = self.register(username="chosen_name", email="newcomer@example.com")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(User.objects.get(email=EMAIL).username, "chosen_name")

    def test_an_unfinished_registration_for_the_same_address_is_replaced(self):
        self.assertEqual(APIClient().post(self.url, {"full_name": "New Comer", "email": EMAIL, "password": PASSWORD}, format="json").status_code, 202)
        pending = PendingRegistration.objects.get()
        self.assertEqual(self.register().status_code, 201)
        pending.refresh_from_db()
        self.assertEqual((pending.status, pending.password_hash, pending.token_hash), ("cancelled", "", ""))

    # -- the same single sentence ----------------------------------------------------------------------------------------
    def test_a_token_for_another_address_is_the_same_404_as_the_preview_and_creates_nothing(self):
        response = self.register(email="someone.else@example.com")
        self.assertEqual((response.status_code, response.json()), (404, {"detail": SENTENCE}))
        self.assert_nothing_created("someone.else@example.com")
        preview = APIClient().get(reverse("organizations:organization-invitation-preview", args=["no-such-token"]))
        self.assertEqual((preview.status_code, preview.json()), (404, response.json()))                    # word for word the preview's answer

    def test_an_unknown_token_is_the_same_404(self):
        response = self.register(token="no-such-token")
        self.assertEqual((response.status_code, response.json()), (404, {"detail": SENTENCE}))
        self.assert_nothing_created()

    def test_a_used_cancelled_or_expired_token_is_the_same_404(self):
        for label, change in (
            ("used", {"status": "accepted"}),
            ("cancelled", {"status": "cancelled"}),
            ("expired", {"expires_at": timezone.now() - timedelta(minutes=1)}),
        ):
            with self.subTest(label):
                OrganizationInvitation.objects.filter(pk=self.invitation.pk).update(**change)
                response = self.register()
                self.assertEqual((response.status_code, response.json()), (404, {"detail": SENTENCE}))
                self.assertFalse(User.objects.filter(email=EMAIL).exists())
                OrganizationInvitation.objects.filter(pk=self.invitation.pk).update(status="pending", expires_at=timezone.now() + timedelta(hours=1))

    def test_a_token_used_for_a_registration_cannot_be_used_again(self):
        self.assertEqual(self.register().status_code, 201)
        again = self.register(username="another")
        self.assertEqual((again.status_code, again.json()), (404, {"detail": SENTENCE}))

    def test_an_invitation_to_a_suspended_organization_is_the_same_404(self):
        Organization.objects.filter(pk=self.organization.pk).update(status="suspended")
        self.assertEqual(self.register().status_code, 404)
        self.assertFalse(User.objects.filter(email=EMAIL).exists())

    # -- everything else about registering still holds -------------------------------------------------------------------
    def test_the_password_rules_still_apply_and_nothing_is_used_up(self):
        for fields in ({"password": "12345678", "password_confirm": "12345678"}, {"password_confirm": "Different!Pass123"}):
            with self.subTest(fields=fields):
                response = self.register(**fields)
                self.assertEqual(response.status_code, 400, response.content)
                self.assert_nothing_created()

    def test_an_address_that_already_has_an_account_is_the_usual_400_and_the_invitation_stays_usable(self):
        User.objects.create_user("already", EMAIL, PASSWORD, is_email_verified=True)
        response = self.register()
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json())
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.status, OrganizationInvitation.Status.PENDING)

    def test_a_full_plan_refuses_with_the_plan_sentence_and_creates_nothing(self):
        Organization.objects.filter(pk=self.organization.pk).update(plan=Organization.Plan.FREE)
        for index in range(10):                                                           # the free plan's 10 member seats, taken some other way
            OrganizationMembership.objects.create(
                organization=self.organization, role="member", status="active",
                user=User.objects.create_user(f"ri-filler-{index}", f"ri-filler-{index}@example.com", PASSWORD, is_email_verified=True))
        response = self.register()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["code"], "plan_limit_reached")
        self.assertFalse(User.objects.filter(email=EMAIL).exists())
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.status, OrganizationInvitation.Status.PENDING)

    def test_without_a_token_nothing_changes(self):
        for blank in (None, "", "   "):
            with self.subTest(token=blank):
                PendingRegistration.objects.all().delete()
                response = self.register(token=blank)
                self.assertEqual(response.status_code, 202, response.content)
        self.assertFalse(User.objects.filter(email=EMAIL).exists())
        self.assertTrue(mail.outbox)                                                          # the ordinary verification email

    def test_the_registration_rate_limit_still_applies(self):
        statuses = [self.register(token="no-such-token").status_code for _ in range(11)]
        self.assertEqual(statuses[:10], [404] * 10)
        self.assertEqual(statuses[10], 429)
