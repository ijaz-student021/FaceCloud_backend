"""Accepting an organization invitation as an existing user must work in autocommit, which is how production runs.

``accept_invitation`` locks the invitation row with ``select_for_update()``, which PostgreSQL only allows inside a transaction. A ``TestCase`` wraps every test in one (and SQLite has no row
locks), so the endpoint answered 500 for every existing user on PostgreSQL while the tests passed. This test runs without a wrapping transaction and is skipped on SQLite.
"""
import unittest
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from moderation.tests import ModerationFixtureMixin
from organizations.models import OrganizationInvitation, OrganizationMembership
from organizations.services import create_organization, hash_token

User = get_user_model()


@unittest.skipUnless(connection.vendor == "postgresql", "select_for_update is only enforced on PostgreSQL")
class AcceptInvitationInAutocommitTests(ModerationFixtureMixin, TransactionTestCase):
    def test_an_existing_user_accepts_a_member_and_a_moderator_invitation(self):
        owner = self.make_user("tx-owner")
        organization = create_organization(name="Acme", owner=owner, plan="team")
        for role in ("member", "moderator"):
            person = self.make_user(f"tx-{role}", email=f"tx-{role}@example.com")
            OrganizationInvitation.objects.create(
                organization=organization, email=person.email, role=role, token_hash=hash_token(f"token-{role}"), invited_by=owner,
                expires_at=timezone.now() + timedelta(hours=5),
            )
            response = self.authenticated_client(person).post(reverse("organizations:organization-invitation-accept"), {"token": f"token-{role}"}, format="json")
            self.assertEqual(response.status_code, status.HTTP_200_OK, getattr(response, "data", None))
            self.assertTrue(OrganizationMembership.objects.filter(organization=organization, user=person, role=role, status="active").exists())
