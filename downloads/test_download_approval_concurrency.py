"""Download approval under real concurrency (PostgreSQL only).

Each thread uses its own database connection and transaction, so these tests exercise the row locks (``select_for_update``) the way two moderators or two browser tabs
would. They are skipped on SQLite, which cannot lock rows. To make the overlap certain, the first decision is held open for a moment while it still holds its locks: without the
lock the second decision would run in that window and create duplicate or contradictory decisions.
"""
import threading
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from django.db import connection, connections
from django.test import TransactionTestCase, override_settings

from downloads import approval
from downloads.approval import DownloadConflict, DownloadNotAuthorized, ItemsNotEligible
from downloads.models import DownloadGrant, DownloadRequest, DownloadRequestItem
from downloads.test_download_approval import ApprovalFixture
from notifications.models import UserNotification
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.scope import data_scope

Item = DownloadRequestItem


def run_in_parallel(*calls, timeout=30):
    """Start every call at the same instant, each in its own thread (and so its own database connection); return ``("ok", value)`` or ``("error", exception)`` for each."""
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def worker(index, call):
        try:
            barrier.wait(timeout=10)
            results[index] = ("ok", call())
        except BaseException as error:                      # collected and inspected by the test
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


@unittest.skipUnless(connection.vendor == "postgresql", "row locks are only exercised on PostgreSQL")
class ConcurrentApprovalTests(ApprovalFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.require_approval()
        self.second_moderator = self.add_member("ap-moderator-2", OrganizationMembership.Role.MODERATOR)
        self.download_request, self.images = self.make_request(self.photos(3))
        self.items = list(self.download_request.items.order_by("pk"))

    # ---- helpers
    @contextmanager
    def captureOnCommitCallbacks(self, execute=False):
        """The fixture helpers expect it; in a TransactionTestCase every commit is real and runs its on_commit callbacks by itself."""
        yield []

    def hold_the_first_decision_open(self, seconds=0.6):
        """While one decision holds its locks (just before it finishes), the other has time to arrive and has to wait for them."""
        real = approval.recompute_status
        state = {"first": True, "guard": threading.Lock()}

        def slow(items):
            with state["guard"]:
                first, state["first"] = state["first"], False
            if first:
                time.sleep(seconds)
            return real(items)

        return mock.patch.object(approval, "recompute_status", side_effect=slow)

    def decision(self, moderator, action, **kwargs):
        def call():
            return approval.decide(
                download_request_id=self.download_request.pk, actor=moderator, organization=self.organization,
                scope=data_scope(self.organization, moderator), action=action, **kwargs,
            )
        return call

    def codes(self, results):
        return sorted((kind, getattr(getattr(value, "detail", None), "get", lambda _k: None)("code") if kind == "error" else "") for kind, value in results)

    def assert_consistent(self):
        """The invariants that must hold however the decisions interleaved."""
        request = DownloadRequest.objects.get(pk=self.download_request.pk)
        items = list(request.items.all())
        grants = list(DownloadGrant.objects.filter(item__request=request))
        approved = [item for item in items if item.status == Item.Status.APPROVED]
        self.assertEqual(sorted(grant.item_id for grant in grants), sorted(item.pk for item in approved), "exactly one grant per approved item, none for any other")
        self.assertEqual(len({grant.item_id for grant in grants}), len(grants))
        if request.status == DownloadRequest.Status.CANCELLED:
            self.assertEqual([item.status for item in items], [Item.Status.PENDING] * len(items), "a cancelled request decided nothing")
        else:
            self.assertEqual(request.status, approval.recompute_status(items), "the request status follows from its items")
        for item in items:
            if item.status == Item.Status.DENIED:
                self.assertTrue(item.denial_reason_code)
            if item.status != Item.Status.PENDING:
                self.assertIsNotNone(item.reviewed_by_id)
        return request, items, grants

    # ---- tests
    def test_two_moderators_approving_the_same_request_at_the_same_time(self):
        with self.hold_the_first_decision_open():
            results = run_in_parallel(self.decision(self.moderator, "approve_all"), self.decision(self.second_moderator, "approve_all"))
        self.assertEqual(sorted(kind for kind, _ in results), ["error", "ok"], results)
        error = next(value for kind, value in results if kind == "error")
        self.assertIsInstance(error, DownloadConflict)
        self.assertEqual(error.detail["code"], "request_closed")
        request, items, grants = self.assert_consistent()
        self.assertEqual((request.status, request.version, len(grants)), (DownloadRequest.Status.APPROVED, 1, 3))      # three grants, not six
        self.assertEqual(OrganizationAuditEvent.objects.filter(action="download_decided").count(), 1)
        self.assertEqual(UserNotification.objects.filter(recipient=self.member, event_type="download_request_decided").count(), 1)

    def test_an_approval_and_a_denial_that_overlap_on_one_image(self):
        first, second, third = (item.pk for item in self.items)
        reason = {"code": "policy_restriction", "text": ""}
        with self.hold_the_first_decision_open():
            results = run_in_parallel(
                self.decision(self.moderator, "approve_selected", item_ids=[first, second], reason=reason),
                self.decision(self.second_moderator, "deny_selected", item_ids=[second, third], reason=reason),
            )
        self.assertEqual(sorted(kind for kind, _ in results), ["error", "ok"], results)
        error = next(value for kind, value in results if kind == "error")
        self.assertIsInstance(error, DownloadConflict)
        self.assertIn(error.detail["code"], ("request_closed", "item_already_decided"))
        request, items, grants = self.assert_consistent()
        self.assertEqual(sum(1 for item in items if item.status == Item.Status.PENDING) + sum(1 for item in items if item.status != Item.Status.PENDING), 3)

    def test_an_approval_that_races_the_requesters_cancellation(self):
        def cancel():
            return approval.cancel_request(download_request=self.download_request, user=self.member)

        with self.hold_the_first_decision_open():
            results = run_in_parallel(self.decision(self.moderator, "approve_all"), cancel)
        self.assertEqual(sorted(kind for kind, _ in results), ["error", "ok"], results)
        request, items, grants = self.assert_consistent()
        if request.status == DownloadRequest.Status.CANCELLED:
            self.assertEqual(len(grants), 0)                                    # the cancellation won: nothing was approved
        else:
            self.assertEqual((request.status, len(grants)), (DownloadRequest.Status.APPROVED, 3))

    def test_the_same_submission_sent_twice_at_once_makes_one_request(self):
        photos = self.photos(2)
        entries = [{"image_id": image.pk} for image in photos]

        def submit():
            return approval.create_request(user=self.other_member, organization_id=self.organization.pk, entries=entries, client_token="same-token")

        owner_photos = [self.make_photo(self.other_member, f"o{index}.png") for index in range(2)]
        entries[:] = [{"image_id": image.pk} for image in owner_photos]
        results = run_in_parallel(submit, submit)
        self.assertEqual([kind for kind, _ in results], ["ok", "ok"], results)
        self.assertEqual(sorted(value[1] for _, value in results), [False, True])             # one created it, the other got it back
        self.assertEqual(len({value[0].pk for _, value in results}), 1)
        self.assertEqual(DownloadRequest.objects.filter(requester=self.other_member).count(), 1)

    def test_two_different_submissions_for_the_same_image_at_once(self):
        photo = self.make_photo(self.other_member, "dup.png")

        def submit(token):
            return lambda: approval.create_request(user=self.other_member, organization_id=self.organization.pk, entries=[{"image_id": photo.pk}], client_token=token)

        results = run_in_parallel(submit("a"), submit("b"))
        self.assertEqual(sorted(kind for kind, _ in results), ["error", "ok"], results)
        error = next(value for kind, value in results if kind == "error")
        self.assertIsInstance(error, ItemsNotEligible)
        self.assertEqual(error.detail["items"][0]["reason"], "already_requested")
        self.assertEqual(DownloadRequest.objects.filter(requester=self.other_member).count(), 1)

    @override_settings(DOWNLOAD_GRANT_MAX_USES=1)
    def test_a_single_use_grant_cannot_be_used_twice_at_once(self):
        request, images = self.make_request(self.photos(1))
        approval.decide(
            download_request_id=request.pk, actor=self.moderator, organization=self.organization, scope=data_scope(self.organization, self.moderator), action="approve_all",
        )
        image = images[0]

        def use():
            return approval.authorize_download(self.member, image, "enhanced", image.result.pk)

        results = run_in_parallel(use, use, use)
        self.assertEqual(sorted(kind for kind, _ in results), ["error", "error", "ok"], results)
        self.assertTrue(all(isinstance(value, DownloadNotAuthorized) and value.detail["reason"] == "used_up" for kind, value in results if kind == "error"))
        self.assertEqual(DownloadGrant.objects.get(image=image).use_count, 1)
