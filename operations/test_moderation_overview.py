from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from moderation.models import ModeratorReview
from moderation.services import dashboard_summary
from organizations.models import OrganizationMembership
from organizations.services import create_organization
from uploads.models import ImageUpload, UploadBatch

from .moderation_overview import InvalidModerationOverviewFilter, build_admin_moderation_overview

User = get_user_model()


class BuildAdminModerationOverviewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="modover-owner", email="modover-owner@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.organization = create_organization(name="Moderation Overview Co", owner=self.owner)
        self.mod1 = User.objects.create_user(
            username="modover-mod1", email="modover-mod1@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.mod2 = User.objects.create_user(
            username="modover-mod2", email="modover-mod2@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.mod1,
            role=OrganizationMembership.Role.MODERATOR, status=OrganizationMembership.Status.ACTIVE,
        )
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.mod2,
            role=OrganizationMembership.Role.MODERATOR, status=OrganizationMembership.Status.ACTIVE,
        )
        self.batch = UploadBatch.objects.create(
            owner=self.owner, organization=self.organization, upload_type="single",
            status="uploaded", total_files=5, accepted_files=5, processing_status="completed",
        )
        self._next_image = 0

    def make_image(self):
        self._next_image += 1
        image = ImageUpload.objects.create(
            owner=self.owner, batch=self.batch, original_name=f"p{self._next_image}.jpg",
            file_extension=".jpg", mime_type="image/jpeg", file_size=14, status="uploaded",
        )
        image.original_file.save(f"p{self._next_image}.jpg", ContentFile(b"x"), save=True)
        return image

    def make_review(self, status, *, created_at=None, assigned_moderator=None, decided_by=None, decided_at=None):
        review = ModeratorReview.objects.create(
            organization=self.organization, image=self.make_image(), status=status,
            source=ModeratorReview.Source.QUALITY, assigned_moderator=assigned_moderator,
            decided_by=decided_by, decided_at=decided_at,
        )
        if created_at is not None:
            ModeratorReview.objects.filter(pk=review.pk).update(created_at=created_at)
        return review

    def test_totals_count_every_status(self):
        self.make_review(ModeratorReview.Status.PENDING)
        self.make_review(ModeratorReview.Status.IN_REVIEW)
        self.make_review(ModeratorReview.Status.ESCALATED)
        self.make_review(ModeratorReview.Status.APPROVED, decided_at=timezone.now())
        self.make_review(ModeratorReview.Status.REJECTED, decided_at=timezone.now())
        self.make_review(ModeratorReview.Status.CORRECTED, decided_at=timezone.now())

        totals = build_admin_moderation_overview()["totals"]

        self.assertEqual(totals["pending"], 1)
        self.assertEqual(totals["in_review"], 1)
        self.assertEqual(totals["escalated"], 1)
        self.assertEqual(totals["approved_total"], 1)
        self.assertEqual(totals["rejected_total"], 1)
        self.assertEqual(totals["corrected_total"], 1)
        self.assertEqual(totals["completed_today"], 3)

    def test_stale_open_counts_only_open_reviews_past_the_cutoff(self):
        old = timezone.now() - timezone.timedelta(hours=100)
        recent = timezone.now() - timezone.timedelta(hours=1)
        self.make_review(ModeratorReview.Status.PENDING, created_at=old)
        self.make_review(ModeratorReview.Status.IN_REVIEW, created_at=recent)
        self.make_review(ModeratorReview.Status.APPROVED, created_at=old, decided_at=timezone.now())

        overview = build_admin_moderation_overview(stale_hours=48)

        self.assertEqual(overview["stale_hours"], 48)
        self.assertEqual(overview["totals"]["stale_open"], 1)

    def test_organizations_needing_attention_only_lists_orgs_with_open_reviews(self):
        quiet_org = create_organization(name="Quiet Co", owner=self.owner)
        self.make_review(ModeratorReview.Status.PENDING)

        rows = build_admin_moderation_overview()["organizations_needing_attention"]

        org_ids = {row["organization_id"] for row in rows}
        self.assertIn(self.organization.pk, org_ids)
        self.assertNotIn(quiet_org.pk, org_ids)

    def test_organizations_needing_attention_orders_by_escalated_then_stale_then_pending(self):
        other = create_organization(name="Other Attention Co", owner=self.owner)
        other_batch = UploadBatch.objects.create(
            owner=self.owner, organization=other, upload_type="single",
            status="uploaded", total_files=1, accepted_files=1, processing_status="completed",
        )
        other_image = ImageUpload.objects.create(
            owner=self.owner, batch=other_batch, original_name="o.jpg",
            file_extension=".jpg", mime_type="image/jpeg", file_size=14, status="uploaded",
        )
        other_image.original_file.save("o.jpg", ContentFile(b"x"), save=True)
        ModeratorReview.objects.create(
            organization=other, image=other_image, status=ModeratorReview.Status.PENDING,
            source=ModeratorReview.Source.QUALITY,
        )
        self.make_review(ModeratorReview.Status.ESCALATED)

        rows = build_admin_moderation_overview()["organizations_needing_attention"]

        self.assertEqual(rows[0]["organization_id"], self.organization.pk)

    def test_moderator_workload_counts_assigned_open_and_decided(self):
        self.make_review(ModeratorReview.Status.IN_REVIEW, assigned_moderator=self.mod1)
        self.make_review(ModeratorReview.Status.ESCALATED, assigned_moderator=self.mod1)
        self.make_review(ModeratorReview.Status.APPROVED, decided_by=self.mod2, decided_at=timezone.now())
        self.make_review(ModeratorReview.Status.REJECTED, decided_by=self.mod2, decided_at=timezone.now())

        rows = {row["moderator_id"]: row for row in build_admin_moderation_overview()["moderator_workload"]}

        self.assertEqual(rows[self.mod1.pk]["assigned_open"], 2)
        self.assertEqual(rows[self.mod1.pk]["decided_total"], 0)
        self.assertEqual(rows[self.mod1.pk]["username"], self.mod1.username)
        self.assertEqual(rows[self.mod2.pk]["assigned_open"], 0)
        self.assertEqual(rows[self.mod2.pk]["decided_total"], 2)

    def test_moderator_workload_is_not_inflated_by_the_other_moderators_counts(self):
        # Regression guard for the join-fan-out bug this module was built to avoid:
        # one moderator with many assigned-open reviews and a different moderator
        # with many decided reviews must not multiply each other's counts.
        for _ in range(3):
            self.make_review(ModeratorReview.Status.IN_REVIEW, assigned_moderator=self.mod1)
        for _ in range(4):
            self.make_review(ModeratorReview.Status.APPROVED, decided_by=self.mod2, decided_at=timezone.now())

        rows = {row["moderator_id"]: row for row in build_admin_moderation_overview()["moderator_workload"]}

        self.assertEqual(rows[self.mod1.pk]["assigned_open"], 3)
        self.assertEqual(rows[self.mod2.pk]["decided_total"], 4)

    def test_stale_hours_rejects_non_numeric_values(self):
        with self.assertRaises(InvalidModerationOverviewFilter):
            build_admin_moderation_overview(stale_hours="not-a-number")

    def test_stale_hours_rejects_out_of_range_values(self):
        with self.assertRaises(InvalidModerationOverviewFilter):
            build_admin_moderation_overview(stale_hours=0)
        with self.assertRaises(InvalidModerationOverviewFilter):
            build_admin_moderation_overview(stale_hours=24 * 90 + 1)

    def test_stale_hours_defaults_when_blank(self):
        self.assertEqual(build_admin_moderation_overview(stale_hours=None)["stale_hours"], 48)
        self.assertEqual(build_admin_moderation_overview(stale_hours="")["stale_hours"], 48)

    def test_the_platform_totals_count_reviews_past_their_deadline(self):
        late = self.make_review(ModeratorReview.Status.PENDING)
        ModeratorReview.objects.filter(pk=late.pk).update(
            due_at=timezone.now() - timezone.timedelta(hours=3),
        )
        in_time = self.make_review(ModeratorReview.Status.PENDING)
        ModeratorReview.objects.filter(pk=in_time.pk).update(
            due_at=timezone.now() + timezone.timedelta(hours=10),
        )

        self.assertEqual(build_admin_moderation_overview()["totals"]["overdue"], 1)

    def test_a_decided_review_is_not_counted_as_overdue_platform_wide(self):
        decided = self.make_review(ModeratorReview.Status.APPROVED, decided_at=timezone.now())
        ModeratorReview.objects.filter(pk=decided.pk).update(
            due_at=timezone.now() - timezone.timedelta(days=2),
        )

        self.assertEqual(build_admin_moderation_overview()["totals"]["overdue"], 0)

    def test_each_organization_row_reports_its_own_overdue_count(self):
        late = self.make_review(ModeratorReview.Status.PENDING)
        ModeratorReview.objects.filter(pk=late.pk).update(
            due_at=timezone.now() - timezone.timedelta(hours=3),
        )

        row = next(
            row for row in build_admin_moderation_overview()["organizations_needing_attention"]
            if row["organization_id"] == self.organization.pk
        )

        self.assertEqual(row["overdue"], 1)

    def test_agrees_with_the_organizations_own_dashboard_summary(self):
        self.make_review(ModeratorReview.Status.PENDING)
        self.make_review(ModeratorReview.Status.IN_REVIEW)
        self.make_review(ModeratorReview.Status.APPROVED, decided_at=timezone.now())

        org_summary = dashboard_summary(self.organization)
        platform_totals = build_admin_moderation_overview()["totals"]

        # Every count the organization dashboard reports, the platform view
        # reports the same way. "oldest_open_age_hours" is excluded because it
        # is an age measured from the moment it is asked for, so the two calls
        # above would legitimately differ in the last decimal; the platform view
        # reports the absolute "oldest_open_at" per organization instead.
        for key in set(org_summary) - {"oldest_open_age_hours"}:
            self.assertEqual(platform_totals[key], org_summary[key], key)


class AdminModerationOverviewEndpointTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="modover-admin", email="modover-admin@example.com",
            password="ForestRiver!4821", is_email_verified=True, role="admin",
        )
        self.plain = User.objects.create_user(
            username="modover-plain", email="modover-plain@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.client = APIClient()
        self.url = reverse("operations:admin-moderation-overview")

    def test_requires_authentication_and_admin_role(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_authenticate(self.plain)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_admin_gets_the_overview_payload(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIn("totals", response.data)
        self.assertIn("organizations_needing_attention", response.data)
        self.assertIn("moderator_workload", response.data)

    def test_stale_hours_is_honoured(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.url, {"stale_hours": "10"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["stale_hours"], 10)

    def test_invalid_stale_hours_is_400(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.url, {"stale_hours": "not-a-number"})

        self.assertEqual(response.status_code, 400)
