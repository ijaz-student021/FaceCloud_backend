"""The owner invitation on PostgreSQL, outside a wrapping test transaction (what production does), and under real concurrency (separate connections).

* Accepting an organization invitation used to answer 500 on PostgreSQL: ``select_for_update`` outside a transaction. ``TestCase`` (one big transaction) and SQLite hid it.
* Two identical "create the organization for this owner" requests at the same instant make one organization and send one e-mail; two acceptances of one token make one owner.
"""
import re
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection, connections
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from moderation.tests import ModerationFixtureMixin
from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from organizations.services import OrganizationConflict, accept_invitation, create_organization, create_pending_organization, hash_token

User = get_user_model()


def run_in_parallel(*calls, timeout=30):
    """Start every call at the same instant, each in its own thread and so its own database connection; ``("ok", value)`` or ``("error", exception)`` for each."""
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def worker(index, call):
        try:
            barrier.wait(timeout=10)
            results[index] = ("ok", call())
        except BaseException as error:
            results[index] = ("error", error)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(index, call)) for index, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
        if thread.is_alive():
            raise AssertionError("a concurrent call did not finish: a lock was never released")
    return results


@unittest.skipUnless(connection.vendor == "postgresql", "row and advisory locks are only exercised on PostgreSQL")
class OwnerInvitationOnPostgresTests(ModerationFixtureMixin, TransactionTestCase):
    def setUp(self):
        self.admin = self.make_user("pg-admin", role="admin")

    @staticmethod
    def captureOnCommitCallbacks(execute=False):                  # not needed here: every commit is real and runs its callbacks
        from contextlib import nullcontext

        return nullcontext([])

    def test_an_existing_user_can_accept_an_organization_invitation_outside_a_test_transaction(self):
        """The regression: this answered 500 (TransactionManagementError) on PostgreSQL."""
        owner = self.make_user("pg-owner")
        organization = create_organization(name="Acme", owner=owner)
        member = self.make_user("pg-member", email="pg-member@example.com")
        OrganizationInvitation.objects.create(
            organization=organization, email="pg-member@example.com", role="member", token_hash=hash_token("member-token"), invited_by=owner,
            expires_at=timezone.now() + timedelta(hours=5),
        )
        response = self.authenticated_client(member).post(reverse("organizations:organization-invitation-accept"), {"token": "member-token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, getattr(response, "data", None))
        self.assertTrue(OrganizationMembership.objects.filter(organization=organization, user=member, role="member", status="active").exists())

    def test_the_whole_owner_flow_works_in_autocommit(self):
        client = self.authenticated_client(self.admin)
        response = client.post(reverse("organizations:organization-admin-list-create"), {"name": "Auto Co", "owner_email": "auto-owner@example.com", "plan": "team"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(len(mail.outbox), 1)                                            # the real on-commit send ran
        invitation = OrganizationInvitation.objects.get(organization_id=response.data["id"])
        self.assertEqual(invitation.email_status, "sent")
        token = re.search(r"/invitations/([A-Za-z0-9_-]+)", mail.outbox[0].body).group(1)
        owner = self.make_user("pg-auto-owner", email="auto-owner@example.com")
        accepted = self.authenticated_client(owner).post(reverse("organizations:organization-invitation-accept"), {"token": token}, format="json")
        self.assertEqual(accepted.status_code, status.HTTP_200_OK, getattr(accepted, "data", None))
        self.assertEqual(Organization.objects.get(pk=response.data["id"]).status, "active")

    def test_two_identical_creations_at_the_same_instant_make_one_organization_and_one_e_mail(self):
        def create():
            return create_pending_organization(name="Race Co", owner_email="race@example.com", plan="free", actor=self.admin)

        real_slug = __import__("organizations.services", fromlist=["_unique_slug"])._unique_slug
        state = {"first": True, "guard": threading.Lock()}

        def slow_slug(base):                                                              # holds the first request between its check and its commit, so the second one arrives in that window
            with state["guard"]:
                first, state["first"] = state["first"], False
            if first:
                time.sleep(0.6)
            return real_slug(base)

        with mock.patch("organizations.services._unique_slug", side_effect=slow_slug):
            results = run_in_parallel(create, create)
        self.assertEqual([kind for kind, _ in results], ["ok", "ok"], results)
        self.assertEqual(sorted(value[2] for _, value in results), [False, True])        # one created it, the other got it back
        self.assertEqual(len({value[0].pk for _, value in results}), 1)
        self.assertEqual(Organization.objects.filter(name="Race Co").count(), 1)
        self.assertEqual(OrganizationInvitation.objects.filter(organization__name="Race Co").count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_two_acceptances_of_one_token_make_one_owner(self):
        organization, invitation, _ = create_pending_organization(name="Twice Co", owner_email="twice@example.com", plan="free", actor=self.admin)
        token = re.search(r"/invitations/([A-Za-z0-9_-]+)", mail.outbox[0].body).group(1)
        user = self.make_user("pg-twice", email="twice@example.com")
        real_audit = __import__("organizations.services", fromlist=["record_audit_event"]).record_audit_event
        state = {"first": True, "guard": threading.Lock()}

        def slow_audit(*args, **kwargs):                                                  # keeps the first acceptance inside its transaction, holding the locks
            with state["guard"]:
                first, state["first"] = state["first"], False
            if first:
                time.sleep(0.6)
            return real_audit(*args, **kwargs)

        with mock.patch("organizations.services.record_audit_event", side_effect=slow_audit):
            results = run_in_parallel(lambda: accept_invitation(token=token, user=user), lambda: accept_invitation(token=token, user=user))
        self.assertEqual(sorted(kind for kind, _ in results), ["error", "ok"], results)
        error = next(value for kind, value in results if kind == "error")
        self.assertIsInstance(error, OrganizationConflict)
        self.assertEqual(OrganizationMembership.objects.filter(organization=organization, role="owner").count(), 1)
        organization.refresh_from_db()
        self.assertEqual(organization.status, "active")
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, "accepted")

    def test_a_resend_racing_an_acceptance_leaves_a_consistent_organization(self):
        from organizations.services import resend_owner_invitation

        organization, _, _ = create_pending_organization(name="Race Two", owner_email="race2@example.com", plan="free", actor=self.admin)
        token = re.search(r"/invitations/([A-Za-z0-9_-]+)", mail.outbox[0].body).group(1)
        user = self.make_user("pg-race2", email="race2@example.com")
        results = run_in_parallel(lambda: accept_invitation(token=token, user=user), lambda: resend_owner_invitation(organization=organization, actor=self.admin))
        organization.refresh_from_db()
        owners = OrganizationMembership.objects.filter(organization=organization, role="owner", status="active").count()
        invitation = organization.invitations.get(role="owner")
        if owners:                                                                         # the acceptance won: active, one owner, invitation accepted, the resend refused or ran before it
            self.assertEqual((organization.status, invitation.status, owners), ("active", "accepted", 1))
        else:                                                                              # the resend won first: the old token no longer works, nobody is owner, still waiting
            self.assertEqual((organization.status, invitation.status), ("pending_owner", "pending"))
            self.assertEqual([kind for kind, _ in results].count("ok"), 1)
