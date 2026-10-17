"""Moderator reprocess: a new enhancement version, nothing overwritten, no duplicates."""
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import override_settings

from enhancement.models import EnhancementBatchJob, ImageEnhancementResult
from enhancement.test_pipeline_regressions import NO_MODELS, ResultFixture
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization
from uploads.models import UploadBatch

from .tests import ModerationFixtureMixin


@override_settings(**NO_MODELS)
class ModeratorBatchReprocessTests(ModerationFixtureMixin, ResultFixture):
    def setUp(self):
        super().setUp()  # ResultFixture: self.user (the member) and self.batch
        self.owner = self.make_user("rp-owner")
        self.organization = create_organization(name="Reprocess Co", owner=self.owner)
        OrganizationMembership.objects.create(organization=self.organization, user=self.user)
        UploadBatch.objects.filter(pk=self.batch.pk).update(organization=self.organization)
        self.accepted = self.make_result("1.jpg", status="completed")
        self.accepted.enhanced_file.save("accepted.jpg", ContentFile(b"accepted output bytes"))
        self.url = f"/api/organizations/{self.organization.pk}/moderator/batches/{self.batch.pk}/reprocess/"

    def reprocess(self, user=None, body=None):
        client = self.authenticated_client(user or self.owner)
        with patch("enhancement.tasks.process_enhancement_batch.apply_async"):
            return client.post(self.url, body or {"level": "medium", "reason": "re-run after model fix"},
                               format="json")

    def test_it_creates_a_new_version_and_keeps_the_accepted_output(self):
        original_bytes = self.accepted.image.original_file.read()
        response = self.reprocess()
        self.assertEqual(response.status_code, 201, response.content)
        job = EnhancementBatchJob.objects.get(pk=response.json()["job_id"])
        self.assertEqual((job.owner, job.requested_level, job.status), (self.user, "medium", "queued"))
        results = ImageEnhancementResult.objects.filter(image=self.accepted.image).order_by("pk")
        self.assertEqual(results.count(), 2)
        kept = results.first()
        self.assertEqual((kept.status, kept.enhanced_file.read()), ("completed", b"accepted output bytes"))
        self.assertEqual(results.last().batch_job, job)
        self.accepted.image.refresh_from_db()
        self.assertEqual(self.accepted.image.original_file.read(), original_bytes)

    def test_work_already_in_flight_is_refused(self):
        self.assertEqual(self.reprocess().status_code, 201)
        again = self.reprocess()
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["image_ids"], [self.accepted.image_id])
        self.assertEqual(EnhancementBatchJob.objects.count(), 1)

    def test_it_is_audited(self):
        self.reprocess()
        event = OrganizationAuditEvent.objects.get(action="batch_reprocessed_by_moderator")
        self.assertEqual((event.actor, event.target_id, event.reason), (self.owner, self.batch.pk,
                                                                        "re-run after model fix"))

    def test_access_rules(self):
        self.assertEqual(self.reprocess(user=self.user).status_code, 403)  # a plain member
        outsider = self.make_user("rp-outsider")
        self.assertEqual(self.reprocess(user=outsider).status_code, 404)
        other_owner = self.make_user("rp-other")
        other = create_organization(name="Other RP", owner=other_owner)
        UploadBatch.objects.filter(pk=self.batch.pk).update(organization=other)
        self.assertEqual(self.reprocess().status_code, 404)

    def test_a_personal_space_is_never_reprocessed_by_staff(self):
        personal = create_organization(name="Mine", owner=self.owner, is_personal=True)
        UploadBatch.objects.filter(pk=self.batch.pk).update(organization=personal)
        url = f"/api/organizations/{personal.pk}/moderator/batches/{self.batch.pk}/reprocess/"
        with patch("enhancement.tasks.process_enhancement_batch.apply_async"):
            response = self.authenticated_client(self.owner).post(url, {}, format="json")
        self.assertEqual(response.status_code, 404)
