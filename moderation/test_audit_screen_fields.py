"""The organization audit trail as the moderator's Audit and Moderation Logs screens read it."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization, record_audit_event
from uploads.models import UploadBatch

from .services import decide_review
from .tests import ModerationFixtureMixin


class AuditScreenFieldsTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("audit-screen-owner")
        self.organization = create_organization(name="Audit Co", owner=self.owner, moderator_data_scope="organization")
        self.moderator = self.make_user("audit-screen-mod")
        OrganizationMembership.objects.create(organization=self.organization, user=self.moderator,
                                              role=OrganizationMembership.Role.MODERATOR)
        self.member = self.make_user("audit-screen-member")
        OrganizationMembership.objects.create(organization=self.organization, user=self.member)
        self.client = self.authenticated_client(self.moderator)
        self.url = reverse("organizations:organization-audit-list", kwargs={"organization_id": self.organization.pk})

    def rows(self, **query):
        response = self.client.get(self.url, query)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        return body["results"] if isinstance(body, dict) else body

    def decide(self, action="approve", reason="looks fine"):
        review, _ = self.make_quality_review(self.member, self.organization)
        decide_review(review=review, actor=self.moderator, action=action, reason=reason)
        return review

    def test_a_decision_reads_as_a_sentence_about_the_photo_with_old_and_new_status(self):
        review = self.decide("approve")
        row = next(r for r in self.rows() if r["action"] == "moderator_review_decided")
        self.assertEqual(row["actor_name"], "audit-screen-mod")
        self.assertEqual(row["message"], "Approved face.jpg.")
        self.assertEqual((row["target_type"], row["target_id"], row["target_label"]), ("ModeratorReview", review.pk, "face.jpg"))
        self.assertEqual((row["from_status"], row["to_status"]), ("pending", "approved"))
        self.assertEqual(row["reason"], "looks fine")

    def test_a_reject_and_a_correction_use_their_own_words(self):
        self.decide("reject")
        self.decide("correct")
        messages = {r["message"] for r in self.rows(action="moderator_review_decided")}
        self.assertEqual(messages, {"Rejected face.jpg.", "Corrected face.jpg."})

    def test_a_batch_action_names_the_upload(self):
        batch = UploadBatch.objects.create(owner=self.member, organization=self.organization, upload_type="zip", original_archive_name="q3.zip")
        record_audit_event(self.organization, self.moderator, OrganizationAuditEvent.Action.BATCH_CANCELLED_BY_MODERATOR,
                           target_type="UploadBatch", target_id=batch.pk)
        row = self.rows(action="batch_cancelled_by_moderator")[0]
        self.assertEqual((row["message"], row["target_label"]), ("Cancelled q3.zip.", "q3.zip"))
        self.assertEqual((row["from_status"], row["to_status"]), (None, None))

    def test_an_event_about_something_that_is_gone_still_reads_and_has_no_label(self):
        record_audit_event(self.organization, self.moderator, OrganizationAuditEvent.Action.MODERATOR_REVIEW_CLAIMED,
                           target_type="ModeratorReview", target_id=999999)
        row = self.rows(action="moderator_review_claimed")[0]
        self.assertEqual((row["message"], row["target_label"]), ("Took the review of a photo.", None))

    def test_the_old_fields_are_all_still_there(self):
        self.decide()
        row = self.rows()[0]
        for key in ("id", "actor", "action", "target_type", "target_id", "previous_value", "new_value", "reason", "created_at"):
            self.assertIn(key, row)

    def test_filters_by_action_actor_target_and_dates(self):
        review = self.decide()
        self.assertEqual({r["action"] for r in self.rows(action="moderator_review_decided")}, {"moderator_review_decided"})
        self.assertEqual(self.rows(actor=self.member.pk, action="moderator_review_decided"), [])
        self.assertEqual({r["target_id"] for r in self.rows(target_type="ModeratorReview", target_id=review.pk)}, {review.pk})
        today = timezone.now().date().isoformat()
        self.assertTrue(self.rows(date_from=today, date_to=today))                 # a bare date_to includes the whole day
        self.assertEqual(self.rows(date_to=(timezone.now() - timedelta(days=2)).date().isoformat()), [])

    def test_search_finds_the_actor_the_reason_and_the_sentence(self):
        self.decide("reject", reason="blurred beyond use")
        self.assertTrue(self.rows(q="audit-screen-mod"))
        self.assertEqual([r["reason"] for r in self.rows(q="blurred beyond")], ["blurred beyond use"])
        self.assertTrue(self.rows(q="decided"))                                    # the action's name
        self.assertEqual(self.rows(q="zzz-nothing"), [])

    def test_search_finds_a_decision_by_its_wording(self):
        approved = self.decide("approve")
        rejected = self.decide("reject")
        by_word = lambda word: {r["target_id"] for r in self.rows(q=word, action="moderator_review_decided")}      # noqa: E731
        self.assertEqual(by_word("approved"), {approved.pk})
        self.assertEqual(by_word("Rejected"), {rejected.pk})
        self.assertEqual(by_word("appro"), {approved.pk})                          # part of the word
        self.assertEqual(by_word("decided"), {approved.pk, rejected.pk})           # the action's own name

    def test_search_finds_the_sentence_of_other_actions(self):
        review = self.decide("approve")
        record_audit_event(self.organization, self.moderator, OrganizationAuditEvent.Action.MODERATOR_REVIEW_ESCALATED,
                           target_type="ModeratorReview", target_id=review.pk)
        self.assertEqual({r["action"] for r in self.rows(q="escalated")}, {"moderator_review_escalated"})
        self.assertEqual({r["action"] for r in self.rows(q="took the review")}, set())          # not recorded in this test
        self.assertIn("moderator_review_escalated", {r["action"] for r in self.rows(q="the review of")})

    def test_search_does_not_treat_the_placeholders_of_a_sentence_as_text(self):
        self.decide("approve")
        self.assertEqual(self.rows(q="{label}"), [])
        self.assertEqual(self.rows(q="{decision}"), [])

    def test_search_finds_events_by_the_name_of_the_photo_upload_or_folder(self):
        review = self.decide("approve")
        batch = UploadBatch.objects.create(owner=self.member, organization=self.organization, upload_type="zip", original_archive_name="staff_q3.zip")
        record_audit_event(self.organization, self.moderator, OrganizationAuditEvent.Action.BATCH_RETRIED_BY_MODERATOR,
                           target_type="UploadBatch", target_id=batch.pk)
        self.assertEqual({r["target_id"] for r in self.rows(q="face.jpg")}, {review.pk})
        self.assertEqual({r["action"] for r in self.rows(q="staff_q3")}, {"batch_retried_by_moderator"})
        self.assertEqual(self.rows(q="not-a-name-anywhere"), [])

    def test_a_photo_name_search_never_finds_another_organizations_events(self):
        other = create_organization(name="Elsewhere Audit Co", owner=self.make_user("elsewhere-audit-owner"))
        other_review, _ = self.make_quality_review(self.make_user("elsewhere-member"), other)
        record_audit_event(other, self.owner, OrganizationAuditEvent.Action.MODERATOR_REVIEW_CLAIMED,
                           target_type="ModeratorReview", target_id=other_review.pk)
        self.assertEqual(self.rows(q="face.jpg"), [])

    def test_bad_filter_values_are_a_400_naming_the_problem(self):
        for query in ({"action": "nope"}, {"actor": "x"}, {"target_id": "x"}, {"date_from": "yesterday"}, {"q": "x" * 101}):
            response = self.client.get(self.url, query)
            self.assertEqual(response.status_code, 400, query)

    def test_paging_still_works_and_a_page_costs_a_fixed_number_of_queries(self):
        for _ in range(4):
            self.decide()
        paged = self.client.get(self.url, {"page": 1, "page_size": 3}).json()
        self.assertEqual((len(paged["results"]), paged["count"] >= 4), (3, True))
        with self.assertNumQueries(5):                 # user, membership, the count, the page of events, the photo names
            self.client.get(self.url, {"page": 1, "page_size": 10})

    def test_another_organizations_events_are_never_listed(self):
        other = create_organization(name="Other Audit Co", owner=self.make_user("other-audit-owner"))
        record_audit_event(other, self.moderator, OrganizationAuditEvent.Action.MEMBER_REMOVED)
        self.assertNotIn("member_removed", {r["action"] for r in self.rows()})

    def test_a_plain_member_is_refused(self):
        self.assertEqual(self.authenticated_client(self.member).get(self.url).status_code, 403)
