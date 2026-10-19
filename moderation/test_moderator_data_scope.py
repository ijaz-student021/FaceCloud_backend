"""A moderator sees their own members (Organization.moderator_data_scope == "own_members"), enforced on the server.

Fixture: one organization, an owner, two moderators (A and B) and three members: ``ma`` invited by A, ``mb`` invited by B and ``mc`` invited by the platform
administrator. A must see ma and mc and never mb; B must see mb and mc and never ma; the owner sees all three.
"""
from django.test import TestCase
from django.urls import reverse

from identities.models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership
from identities.storage import protect_embedding
from notifications.models import UserNotification
from organizations.models import Organization, OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership
from organizations.services import create_organization, record_audit_event, remove_member
from uploads.models import ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import ModeratorReport, ModeratorReview
from .services import (build_moderator_report_payload, decide_review, flag_overdue_reviews)
from .tests import ModerationFixtureMixin

Role = OrganizationMembership.Role


class ScopeFixture(ModerationFixtureMixin):
    scope_setting = None

    def setUp(self):
        self.admin = self.make_user("scope-admin", role="admin")
        self.owner = self.make_user("scope-owner")
        extra = {"moderator_data_scope": self.scope_setting} if self.scope_setting else {}
        self.organization = create_organization(name="Scope Co", owner=self.owner, **extra)
        self.a = self.make_user("mod-a")
        self.b = self.make_user("mod-b")
        for moderator in (self.a, self.b):
            OrganizationMembership.objects.create(organization=self.organization, user=moderator, role=Role.MODERATOR, invited_by=self.owner)
        self.ma = self.member("ma", self.a)
        self.mb = self.member("mb", self.b)
        self.mc = self.member("mc", self.admin)
        self.content = {name: self.content_for(user) for name, user in (("ma", self.ma), ("mb", self.mb), ("mc", self.mc))}

    def member(self, name, inviter):
        user = self.make_user(f"member-{name}")
        OrganizationMembership.objects.create(organization=self.organization, user=user, role=Role.MEMBER, invited_by=inviter)
        return user

    def content_for(self, user):
        """A batch with one photograph, its review, a person folder and an invitation-free audit trail for ``user``."""
        batch = UploadBatch.objects.create(owner=user, organization=self.organization, upload_type="single", processing_status="completed")
        image = ImageUpload.objects.create(owner=user, batch=batch, original_name=f"{user.username}.jpg", status="uploaded", file_size=10)
        review, _decision = self.make_quality_review(user, self.organization, image=image)
        group = IdentityGroup.objects.create(
            owner=user, display_name=f"Folder of {user.username}", state="active", member_count=1, protected_centroid=protect_embedding([1.0, 0.0, 0.0]),
            embedding_dimension=3, provider="test", policy_version="v1",
        )
        pre = review.quality_decision.preprocessing_result
        validation = FaceValidationResult.objects.create(owner=user, image=image, preprocessing_result=pre, decision="accepted", face_count=1)
        analysis = IdentityAnalysis.objects.create(owner=user, image=image, preprocessing_result=pre, face_validation=validation,
                                                   status="completed", provider="test", policy_version="v1")
        face = IdentityFace.objects.create(owner=user, image=image, analysis=analysis, face_index=0, protected_embedding=protect_embedding([1.0, 0.0, 0.0]),
                                           embedding_dimension=3, provider="test")
        IdentityMembership.objects.create(owner=user, identity_face=face, identity_group=group, state="confirmed", similarity_score=0.9)
        return {"batch": batch, "image": image, "review": review, "group": group}

    def url(self, name, **extra):
        return reverse(name, kwargs={"organization_id": self.organization.pk, **extra})

    def as_(self, user):
        return self.authenticated_client(user)

    def ids(self, response, key="id"):
        body = response.json()
        rows = body["results"] if isinstance(body, dict) and "results" in body else body
        return {row[key] for row in rows}


class OwnMembersTests(ScopeFixture, TestCase):
    """Moderator A against everything that belongs to ma (visible), mb (hidden) and mc (visible: the administrator invited them)."""

    def reviews_of(self, who):
        return {self.content[name]["review"].pk for name in who}

    def test_reviews_list_and_detail(self):
        client = self.as_(self.a)
        self.assertEqual(self.ids(client.get(self.url("moderation:review-list"))), self.reviews_of(("ma", "mc")))
        self.assertEqual(client.get(self.url("moderation:review-detail", review_id=self.content["ma"]["review"].pk)).status_code, 200)
        self.assertEqual(client.get(self.url("moderation:review-detail", review_id=self.content["mc"]["review"].pk)).status_code, 200)
        self.assertEqual(client.get(self.url("moderation:review-detail", review_id=self.content["mb"]["review"].pk)).status_code, 404)

    def test_every_review_action_on_a_hidden_review_is_a_404(self):
        client = self.as_(self.a)
        review_id = self.content["mb"]["review"].pk
        self.assertEqual(client.get(self.url("moderation:review-image", review_id=review_id)).status_code, 404)
        for name, body in (("moderation:review-decision", {"action": "approve"}), ("moderation:review-escalate", {}),
                           ("moderation:review-assign", {"moderator_id": self.a.pk}), ("moderation:review-claim", {}),
                           ("moderation:review-release", {}), ("moderation:review-merge-identity-groups", {})):
            response = client.post(self.url(name, review_id=review_id), body, format="json")
            self.assertEqual(response.status_code, 404, name)
        review = ModeratorReview.objects.get(pk=review_id)
        self.assertEqual((review.status, review.claimed_by_id, review.assigned_moderator_id), ("pending", None, None))      # nothing happened

    def test_a_visible_review_can_be_worked(self):
        client = self.as_(self.a)
        review_id = self.content["ma"]["review"].pk
        self.assertEqual(client.post(self.url("moderation:review-claim", review_id=review_id), {}, format="json").status_code, 200)
        self.assertEqual(client.post(self.url("moderation:review-decision", review_id=review_id), {"action": "approve"}, format="json").status_code, 200)

    def test_a_bulk_decision_skips_hidden_reviews_as_if_they_did_not_exist(self):
        client = self.as_(self.a)
        ids = [self.content[name]["review"].pk for name in ("ma", "mb", "mc")]
        body = client.post(self.url("moderation:review-bulk-decision"), {"review_ids": ids, "action": "approve"}, format="json").json()
        self.assertEqual(set(body["succeeded"]), self.reviews_of(("ma", "mc")))
        self.assertEqual([row["review_id"] for row in body["failed"]], [self.content["mb"]["review"].pk])
        self.assertEqual(body["failed"][0]["detail"], "No such review in this organization.")
        self.assertEqual(ModeratorReview.objects.get(pk=self.content["mb"]["review"].pk).status, "pending")

    def test_a_review_cannot_be_assigned_to_a_moderator_who_may_not_see_it(self):
        response = self.as_(self.owner).post(
            self.url("moderation:review-assign", review_id=self.content["mb"]["review"].pk), {"moderator_id": self.a.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("moderator_id", response.json())
        ok = self.as_(self.owner).post(
            self.url("moderation:review-assign", review_id=self.content["mb"]["review"].pk), {"moderator_id": self.b.pk}, format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.content)

    def test_batches(self):
        client = self.as_(self.a)
        self.assertEqual(self.ids(client.get(self.url("moderation:batch-list"))), {self.content[n]["batch"].pk for n in ("ma", "mc")})
        hidden = self.content["mb"]["batch"].pk
        self.assertEqual(client.get(self.url("moderation:batch-detail", batch_id=hidden)).status_code, 404)
        for name in ("moderation:batch-retry", "moderation:batch-cancel", "moderation:batch-reprocess"):
            self.assertEqual(client.post(self.url(name, batch_id=hidden), {}, format="json").status_code, 404, name)
        self.assertEqual(UploadBatch.objects.get(pk=hidden).cancellation_requested, False)

    def test_images_and_previews(self):
        client = self.as_(self.a)
        self.assertEqual(self.ids(client.get(self.url("moderation:image-list"))), {self.content[n]["image"].pk for n in ("ma", "mc")})
        self.assertEqual(client.get(self.url("moderation:image-preview", image_id=self.content["mb"]["image"].pk)).status_code, 404)

    def test_person_folders_suggestions_and_merge(self):
        client = self.as_(self.a)
        self.assertEqual(self.ids(client.get(self.url("moderation:identity-group-list"))), {self.content[n]["group"].pk for n in ("ma", "mc")})
        hidden = self.content["mb"]["group"]
        other = IdentityGroup.objects.create(owner=self.mb, display_name="Another folder of mb", state="active", member_count=0, embedding_dimension=3,
                                             provider="test", policy_version="v1")
        response = client.post(self.url("moderation:identity-group-merge"), {"source_group_id": other.pk, "target_group_id": hidden.pk}, format="json")
        self.assertEqual(response.status_code, 400)                    # ids in a body are validated, as for another organization's folders
        hidden.refresh_from_db()
        self.assertEqual(hidden.state, "active")
        self.assertEqual(client.get(self.url("moderation:identity-group-suggestions")).status_code, 200)

    def test_dashboard_and_metrics_count_only_visible_members(self):
        a_review = self.content["ma"]["review"]
        decide_review(review=a_review, actor=self.a, action="approve")
        decide_review(review=self.content["mb"]["review"], actor=self.b, action="reject")
        counts = self.as_(self.a).get(self.url("moderation:dashboard")).json()
        self.assertEqual((counts["pending"], counts["approved_total"], counts["rejected_total"]), (1, 1, 0))
        owner_counts = self.as_(self.owner).get(self.url("moderation:dashboard")).json()
        self.assertEqual((owner_counts["pending"], owner_counts["approved_total"], owner_counts["rejected_total"]), (1, 1, 1))
        metrics = self.as_(self.a).get(self.url("moderation:performance-metrics")).json()
        self.assertEqual((metrics["totals"]["decided_count"], [m["username"] for m in metrics["moderators"]]), (1, ["mod-a"]))
        self.assertEqual(self.as_(self.owner).get(self.url("moderation:performance-metrics")).json()["totals"]["decided_count"], 2)

    def test_members_and_invitations(self):
        client = self.as_(self.a)
        usernames = {row["user"]["username"] for row in client.get(self.url("organizations:organization-member-list")).json()}
        self.assertEqual(usernames, {"scope-owner", "mod-a", "mod-b", "member-ma", "member-mc"})        # staff stay listed; mb is not
        hidden_membership = OrganizationMembership.objects.get(user=self.mb)
        response = client.patch(self.url("organizations:organization-member-detail", membership_id=hidden_membership.pk), {"status": "suspended"}, format="json")
        self.assertEqual(response.status_code, 404)
        invitation = OrganizationInvitation.objects.create(organization=self.organization, email="by-b@example.com", token_hash="b" * 64,
                                                           role="member", invited_by=self.b, expires_at="2099-01-01T00:00:00Z")
        mine = OrganizationInvitation.objects.create(organization=self.organization, email="by-a@example.com", token_hash="a" * 64,
                                                     role="member", invited_by=self.a, expires_at="2099-01-01T00:00:00Z")
        listed = client.get(self.url("organizations:organization-invitation-list-create")).json()
        listed = listed["results"] if isinstance(listed, dict) else listed
        self.assertEqual({row["email"] for row in listed}, {"by-a@example.com"})
        self.assertEqual(client.delete(self.url("organizations:organization-invitation-cancel", invitation_id=invitation.pk)).status_code, 404)
        self.assertEqual(client.post(self.url("organizations:organization-invitation-resend", invitation_id=invitation.pk)).status_code, 404)
        self.assertEqual(client.delete(self.url("organizations:organization-invitation-cancel", invitation_id=mine.pk)).status_code, 200)

    def test_the_audit_trail(self):
        record_audit_event(self.organization, self.b, OrganizationAuditEvent.Action.MODERATOR_REVIEW_CLAIMED, target_type="ModeratorReview",
                           target_id=self.content["mb"]["review"].pk)
        record_audit_event(self.organization, self.owner, OrganizationAuditEvent.Action.MODERATOR_REVIEW_ESCALATED, target_type="ModeratorReview",
                           target_id=self.content["mb"]["review"].pk)
        record_audit_event(self.organization, self.b, OrganizationAuditEvent.Action.MODERATOR_BULK_DECISION, target_type="ModeratorReview",
                           new_value={"succeeded": [self.content["mb"]["review"].pk]})
        record_audit_event(self.organization, self.b, OrganizationAuditEvent.Action.BATCH_RETRIED_BY_MODERATOR, target_type="UploadBatch",
                           target_id=self.content["mc"]["batch"].pk)                     # B acted on a member A may also see
        record_audit_event(self.organization, self.a, OrganizationAuditEvent.Action.MODERATOR_REVIEW_CLAIMED, target_type="ModeratorReview",
                           target_id=self.content["ma"]["review"].pk)
        record_audit_event(self.organization, self.owner, OrganizationAuditEvent.Action.ORGANIZATION_DATA_EXPORTED)
        rows = self.as_(self.a).get(self.url("organizations:organization-audit-list")).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        seen = {(row["actor"]["username"] if row["actor"] else None, row["action"], row["target_id"]) for row in rows}
        self.assertNotIn(("mod-b", "moderator_review_claimed", self.content["mb"]["review"].pk), seen)
        self.assertNotIn(("scope-owner", "moderator_review_escalated", self.content["mb"]["review"].pk), seen)       # about a hidden member, whoever did it
        self.assertFalse([row for row in rows if row["action"] == "moderator_bulk_decision"])                     # another moderator's bulk decision
        self.assertIn(("mod-b", "batch_retried_by_moderator", self.content["mc"]["batch"].pk), seen)               # about a member A sees
        self.assertIn(("mod-a", "moderator_review_claimed", self.content["ma"]["review"].pk), seen)               # own action
        self.assertIn(("scope-owner", "organization_data_exported", None), seen)
        owner_rows = self.as_(self.owner).get(self.url("organizations:organization-audit-list")).json()
        owner_rows = owner_rows["results"] if isinstance(owner_rows, dict) else owner_rows
        self.assertEqual(len(owner_rows), len(rows) + 3)

    def test_reports_are_built_from_what_the_requester_sees_and_are_private_to_them(self):
        client_a, client_b = self.as_(self.a), self.as_(self.b)
        for client in (client_a, client_b):
            self.assertEqual(client.post(self.url("moderation:report-list-create"), {"report_type": "review_summary", "format": "json"}, format="json").status_code, 201)
        reports = {report.requested_by_id: report for report in ModeratorReport.objects.all()}
        self.assertEqual(len(reports), 2)                                                  # one each: the in-flight dedupe is per requester
        payload_a = build_moderator_report_payload(reports[self.a.pk])
        self.assertEqual({row["id"] for row in payload_a["rows"]}, self.reviews_of(("ma", "mc")))
        self.assertEqual(payload_a["summary"]["total_reviews"], 2)
        payload_batches = build_moderator_report_payload(ModeratorReport(organization=self.organization, requested_by=self.a, report_type="batches", format="json"))
        self.assertEqual(payload_batches["summary"]["total_batches"], 2)
        listed_a = {row["id"] for row in client_a.get(self.url("moderation:report-list-create")).json()}
        self.assertEqual(listed_a, {reports[self.a.pk].pk})
        self.assertEqual(client_a.get(self.url("moderation:report-detail", report_id=reports[self.b.pk].pk)).status_code, 404)
        self.assertEqual(client_a.get(self.url("moderation:report-download", report_id=reports[self.b.pk].pk)).status_code, 404)
        owner_listed = {row["id"] for row in self.as_(self.owner).get(self.url("moderation:report-list-create")).json()}
        self.assertEqual(owner_listed, {report.pk for report in reports.values()})

    def test_the_organization_wide_export_is_for_the_owner_only(self):
        url = f"/api/organizations/{self.organization.pk}/export/"
        self.assertEqual(self.as_(self.a).get(url).status_code, 403)
        self.assertEqual(self.as_(self.a).post(url).status_code, 403)
        self.assertEqual(self.as_(self.owner).get(url).status_code, 200)

    def test_the_setting_and_the_matching_capability_are_shown(self):
        listed = self.as_(self.a).get("/api/organizations/").json()
        rows = listed["results"] if isinstance(listed, dict) else listed
        row = next(r for r in rows if r["id"] == self.organization.pk)
        self.assertEqual(row["moderator_data_scope"], "own_members")
        self.assertFalse(row["capabilities"]["export_organization"])
        owner_rows = self.as_(self.owner).get("/api/organizations/").json()
        owner_rows = owner_rows["results"] if isinstance(owner_rows, dict) else owner_rows
        self.assertTrue(next(r for r in owner_rows if r["id"] == self.organization.pk)["capabilities"]["export_organization"])


class OwnerAndSettingTests(ScopeFixture, TestCase):
    def test_the_owner_sees_everything(self):
        client = self.as_(self.owner)
        self.assertEqual(self.ids(client.get(self.url("moderation:review-list"))), {c["review"].pk for c in self.content.values()})
        self.assertEqual(self.ids(client.get(self.url("moderation:batch-list"))), {c["batch"].pk for c in self.content.values()})
        self.assertEqual(self.ids(client.get(self.url("moderation:identity-group-list"))), {c["group"].pk for c in self.content.values()})

    def test_the_organization_setting_restores_the_old_behaviour(self):
        Organization.objects.filter(pk=self.organization.pk).update(moderator_data_scope="organization")
        for moderator in (self.a, self.b):
            self.assertEqual(self.ids(self.as_(moderator).get(self.url("moderation:review-list"))), {c["review"].pk for c in self.content.values()})
        self.assertEqual(self.as_(self.a).get(self.url("moderation:review-detail", review_id=self.content["mb"]["review"].pk)).status_code, 200)
        self.assertEqual(self.as_(self.a).get(f"/api/organizations/{self.organization.pk}/export/").status_code, 200)

    def test_new_organizations_start_with_own_members_and_the_migration_keeps_old_ones_open(self):
        self.assertEqual(Organization.objects.get(pk=self.organization.pk).moderator_data_scope, "own_members")
        self.assertEqual(create_organization(name="Fresh Co", owner=self.owner).moderator_data_scope, "own_members")
        self.assertEqual(Organization._meta.get_field("moderator_data_scope").default, "own_members")

    def test_removing_the_inviting_moderator_exposes_their_members_and_frees_their_reviews(self):
        review = self.content["mb"]["review"]
        ModeratorReview.objects.filter(pk=review.pk).update(assigned_moderator=self.b, claimed_by=self.b, status="in_review")
        self.assertEqual(self.as_(self.a).get(self.url("moderation:review-detail", review_id=review.pk)).status_code, 404)
        remove_member(membership=OrganizationMembership.objects.get(organization=self.organization, user=self.b),
                      actor_membership=OrganizationMembership.objects.get(organization=self.organization, user=self.owner))
        self.assertEqual(self.as_(self.a).get(self.url("moderation:review-detail", review_id=review.pk)).status_code, 200)
        review.refresh_from_db()
        self.assertEqual((review.assigned_moderator_id, review.claimed_by_id, review.status), (None, None, "pending"))
        self.assertIn(self.content["mb"]["batch"].pk, self.ids(self.as_(self.a).get(self.url("moderation:batch-list"))))

    def test_demoting_the_inviting_moderator_does_the_same(self):
        membership = OrganizationMembership.objects.get(organization=self.organization, user=self.b)
        OrganizationMembership.objects.filter(pk=membership.pk).update(role=Role.MEMBER)
        self.assertEqual(self.as_(self.a).get(self.url("moderation:review-detail", review_id=self.content["mb"]["review"].pk)).status_code, 200)
        self.assertEqual(self.as_(self.a).get(self.url("moderation:review-detail", review_id=self.content["ma"]["review"].pk)).status_code, 200)

    def test_a_member_nobody_current_invited_is_visible_to_everyone(self):
        lone = self.make_user("lone-member")
        OrganizationMembership.objects.create(organization=self.organization, user=lone, role=Role.MEMBER, invited_by=None)
        left = self.make_user("invited-by-a-left-moderator")
        OrganizationMembership.objects.create(organization=self.organization, user=left, role=Role.MEMBER, invited_by=self.make_user("ex-moderator"))
        for user in (lone, left):
            review = self.content_for(user)["review"]
            for moderator in (self.a, self.b):
                self.assertEqual(self.as_(moderator).get(self.url("moderation:review-detail", review_id=review.pk)).status_code, 200)

    def test_a_second_organization_stays_invisible_throughout(self):
        other_owner = self.make_user("other-scope-owner")
        other = create_organization(name="Elsewhere Co", owner=other_owner, moderator_data_scope="organization")
        stranger = self.make_user("stranger")
        OrganizationMembership.objects.create(organization=other, user=stranger, role=Role.MEMBER)
        batch = UploadBatch.objects.create(owner=stranger, organization=other, upload_type="single")
        image = ImageUpload.objects.create(owner=stranger, batch=batch, original_name="theirs.jpg", status="uploaded")
        foreign_review, _ = self.make_quality_review(stranger, other, image=image)
        for moderator in (self.a, self.b, self.owner):
            client = self.as_(moderator)
            self.assertEqual(client.get(self.url("moderation:review-detail", review_id=foreign_review.pk)).status_code, 404)
            self.assertNotIn(foreign_review.pk, self.ids(client.get(self.url("moderation:review-list"))))
            self.assertEqual(client.get(self.url("moderation:batch-detail", batch_id=batch.pk)).status_code, 404)


class NotificationTests(ScopeFixture, TestCase):
    def test_a_failed_batch_notifies_the_owner_and_only_the_moderators_who_see_that_member(self):
        from notifications.events import notify_moderators_batch_failed

        batch = self.content["mb"]["batch"]
        notify_moderators_batch_failed(batch)
        recipients = set(UserNotification.objects.filter(event_type="moderator_batch_failed").values_list("recipient__username", flat=True))
        self.assertEqual(recipients, {"scope-owner", "mod-b"})

    def test_an_unassigned_overdue_review_goes_to_those_who_may_see_it(self):
        from datetime import timedelta

        from django.utils import timezone

        ModeratorReview.objects.filter(pk=self.content["ma"]["review"].pk).update(due_at=timezone.now() - timedelta(hours=1))
        flag_overdue_reviews()
        recipients = set(UserNotification.objects.filter(event_type="moderator_review_overdue").values_list("recipient__username", flat=True))
        self.assertEqual(recipients, {"scope-owner", "mod-a"})
