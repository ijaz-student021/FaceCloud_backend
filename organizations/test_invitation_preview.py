"""GET /api/organizations/invitations/{token}/: what an invitation is for, before the person signs in or accepts."""
import re
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from .models import OrganizationInvitation, OrganizationMembership
from .tests import OrgFixtureMixin


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class InvitationPreviewTests(OrgFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("preview-owner")
        self.org = self.make_org(self.owner, "Preview Co")
        invite_url = reverse("organizations:organization-invitation-list-create", kwargs={"organization_id": self.org.pk})
        response = self.authenticated_client(self.owner).post(invite_url, {"email": "Guest@Example.com", "role": "moderator"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.token = re.search(r"/invitations/([\w-]+)", mail.outbox[0].body).group(1)
        self.invitation = OrganizationInvitation.objects.get()
        self.anonymous = APIClient()

    def preview(self, token=None, client=None):
        return (client or self.anonymous).get(reverse("organizations:organization-invitation-preview", kwargs={"token": token or self.token}))

    def test_a_good_token_shows_the_organization_email_role_and_expiry_without_signing_in(self):
        response = self.preview()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        body = response.json()
        self.assertEqual(set(body), {"organization_name", "email", "role", "expires_at"})
        self.assertEqual((body["organization_name"], body["email"], body["role"]), ("Preview Co", "guest@example.com", "moderator"))
        self.assertEqual(body["expires_at"][:16], self.invitation.expires_at.isoformat()[:16].replace("+00:00", ""))
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_nothing_else_about_the_organization_or_the_inviter_is_revealed(self):
        text = self.preview().content.decode()
        for private in (self.owner.email, self.owner.username, "organization_id", "invited_by", "token_hash", "status"):
            self.assertNotIn(private, text)

    def test_unknown_used_cancelled_and_expired_tokens_all_answer_the_same_404(self):
        unknown = self.preview("not-a-real-token")
        answers = {unknown.status_code: unknown.json()}
        for label, change in (
            ("accepted", {"status": OrganizationInvitation.Status.ACCEPTED}),
            ("cancelled", {"status": OrganizationInvitation.Status.CANCELLED}),
            ("expired", {"expires_at": timezone.now() - timedelta(minutes=1)}),
        ):
            with self.subTest(label):
                OrganizationInvitation.objects.filter(pk=self.invitation.pk).update(**change)
                response = self.preview()
                answers[(label, response.status_code)] = response.json()
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(response.json(), unknown.json())
        self.assertEqual(unknown.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_stale_or_wrong_sign_in_does_not_get_in_the_way(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer this-is-not-a-valid-token")
        self.assertEqual(self.preview(client=client).status_code, status.HTTP_200_OK)

    def test_it_changes_nothing_and_accepting_still_works_afterwards(self):
        before = OrganizationInvitation.objects.values_list("status", flat=True).get()
        self.preview()
        self.preview()
        self.assertEqual(OrganizationInvitation.objects.values_list("status", flat=True).get(), before)
        guest = self.make_user("guest", email="guest@example.com")
        response = self.authenticated_client(guest).post(reverse("organizations:organization-invitation-accept"), {"token": self.token}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertTrue(OrganizationMembership.objects.filter(organization=self.org, user=guest, role="moderator").exists())
        self.assertEqual(self.preview().status_code, status.HTTP_404_NOT_FOUND)          # a used link now looks like any other bad one

    def test_guessing_is_rate_limited(self):
        from django.core.cache import cache

        cache.clear()
        rates = {**ScopedRateThrottle.THROTTLE_RATES, "invitation": "3/hour"}
        with patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            codes = [self.preview(f"guess-{i}").status_code for i in range(5)]
        cache.clear()
        self.assertEqual(codes[:3], [404, 404, 404])
        self.assertEqual(codes[3], status.HTTP_429_TOO_MANY_REQUESTS)
