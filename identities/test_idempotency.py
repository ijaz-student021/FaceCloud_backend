"""T-005: idempotency of the background recognition job.

Two guards protect ``process_identity_analysis`` (identities/services.py)
against being run twice for the same analysis:

1. A task-level guard (identities/tasks.py): if the row's ``task_id`` no
   longer matches the task that is currently executing, the task returns
   "superseded" without touching the processor at all. This catches a newer
   retry already having been dispatched.
2. A service-level guard: the top of ``process_identity_analysis`` returns
   immediately, doing no work, once the analysis is COMPLETED or
   NEEDS_REVIEW. A second, deeper check re-reads ``task_id`` again, inside a
   row lock, right before results are written -- this catches the row being
   superseded *while* the (expensive) face-embedding work was in flight.

These tests prove both guards actually prevent duplicate work and duplicate
rows, using the same mocked-provider pattern as the existing cancellation
tests (ProcessIdentityAnalysisCancellationTests in tests.py), so no real
model files are needed.

Scoped out on purpose: a CANCELLED analysis being revived by a stale retry.
process_identity_analysis re-checks the *live* batch.cancellation_requested
flag (not the analysis's own stored status) on every call, so if that flag
were cleared and the same analysis reprocessed, it would proceed rather than
staying cancelled. This is not a live bug: nothing in the codebase clears
cancellation_requested and re-triggers identity analysis -- retry_batch()
only re-queues the "preprocessing", "face_validation", "quality_analysis" and
"enhancement" failure stages, never "identity_grouping".
"""
from unittest.mock import MagicMock, patch

import numpy as np
from django.contrib.auth import get_user_model
from django.test import TestCase

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership
from .services import process_identity_analysis
from .tasks import analyze_image_identity_task
from privacy.testing import grant_face_consent

User = get_user_model()


def _cosine_similarity(first, second):
    return float(np.dot(np.asarray(first, dtype=np.float32), np.asarray(second, dtype=np.float32)))


def make_provider(vector=(1.0, 0.0, 0.0)):
    provider = MagicMock()
    provider.name = "test"
    provider.dimension = 3
    provider.model_checksum = "stub"
    provider.healthcheck.return_value = None
    provider.prepare_face.side_effect = lambda image, box, landmarks: image
    provider.embed.return_value = np.array(vector, dtype=np.float32)
    provider.validate_embedding.side_effect = lambda value: value
    provider.similarity.side_effect = _cosine_similarity
    return provider


class RecognitionJobIdempotencyTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="idempotent-owner", email="idempotent-owner@example.com",
            password="Password!123",
        )
        self.batch = UploadBatch.objects.create(owner=self.owner, upload_type="single")
        self.image = ImageUpload.objects.create(
            owner=self.owner, batch=self.batch, original_name="face.jpg", status="uploaded",
        )
        self.preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=self.image, status="completed",
        )
        self.validation = FaceValidationResult.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=self.preprocessing,
            decision="accepted", face_count=1,
            faces=[{"x": 0, "y": 0, "width": 10, "height": 10}],
        )
        self.analysis = IdentityAnalysis.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=self.preprocessing,
            face_validation=self.validation, status="queued", provider="test",
            policy_version="test-v1", task_id="task-1",
        )
        # Facial processing sits behind recorded consent, and production
        # requires it, so grant it here rather than relying on the flag
        # being switched off on this machine.
        grant_face_consent(self.owner)

    def process(self, provider):
        with (
            patch("identities.services.get_identity_provider", return_value=provider),
            patch("identities.services._read_image", return_value=np.zeros((4, 4, 3), dtype=np.uint8)),
        ):
            return process_identity_analysis(self.analysis.pk)

    def snapshot(self):
        faces = list(
            IdentityFace.objects.filter(analysis=self.analysis).order_by("face_index").values_list("pk", flat=True)
        )
        memberships = list(
            IdentityMembership.objects.filter(identity_face__analysis=self.analysis)
            .order_by("pk").values_list("identity_group_id", "state")
        )
        return faces, memberships

    # -- service-level guard: already completed -----------------------------

    def test_reprocessing_a_completed_analysis_does_no_work_and_changes_nothing(self):
        provider = make_provider()
        first = self.process(provider)
        self.assertEqual(first.status, IdentityAnalysis.Status.COMPLETED)
        faces_after_first, memberships_after_first = self.snapshot()
        self.assertEqual(len(faces_after_first), 1)
        group = IdentityGroup.objects.get()
        centroid_after_first = group.protected_centroid

        second = self.process(provider)

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.status, IdentityAnalysis.Status.COMPLETED)
        self.assertEqual(second.finished_at, first.finished_at)  # untouched, not re-stamped
        self.assertEqual(self.snapshot(), (faces_after_first, memberships_after_first))
        group.refresh_from_db()
        self.assertEqual(group.protected_centroid, centroid_after_first)
        # The guard returns before any embedding work, not just before saving.
        provider.embed.assert_called_once()
        provider.prepare_face.assert_called_once()

    def test_reprocessing_a_needs_review_analysis_is_also_a_no_op(self):
        """The top guard treats NEEDS_REVIEW the same as COMPLETED.

        Set up directly rather than by engineering the classifier into
        producing NEEDS_REVIEW (that decision logic -- and how many
        candidate groups it needs to land in the ambiguous band -- is
        already covered by ChooseGroupDecisionTests; this only needs to
        prove the guard itself respects the status).
        """
        IdentityFace.objects.create(
            owner=self.owner, image=self.image, analysis=self.analysis, face_index=0,
            protected_embedding=b"stub", embedding_dimension=3, provider="test",
        )
        self.analysis.status = IdentityAnalysis.Status.NEEDS_REVIEW
        self.analysis.face_count = 1
        self.analysis.review_count = 1
        self.analysis.save()
        snapshot_before = self.snapshot()

        provider = make_provider()
        result = self.process(provider)

        self.assertEqual(result.status, IdentityAnalysis.Status.NEEDS_REVIEW)
        self.assertEqual(self.snapshot(), snapshot_before)
        provider.embed.assert_not_called()
        provider.prepare_face.assert_not_called()

    # -- task-level guard: superseded task id --------------------------------

    def test_the_task_returns_superseded_and_never_calls_the_processor(self):
        IdentityAnalysis.objects.filter(pk=self.analysis.pk).update(task_id="the-real-task")

        with patch("identities.tasks.process_identity_analysis") as processor:
            result = analyze_image_identity_task.apply(
                args=[self.analysis.pk], task_id="a-stale-retry",
            ).get()

        self.assertEqual(result, {"analysis_id": self.analysis.pk, "status": "superseded"})
        processor.assert_not_called()
        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.status, IdentityAnalysis.Status.QUEUED)  # untouched

    def test_the_task_proceeds_when_its_id_matches_the_row(self):
        IdentityAnalysis.objects.filter(pk=self.analysis.pk).update(task_id="matching-task")
        provider = make_provider()

        with (
            patch("identities.services.get_identity_provider", return_value=provider),
            patch("identities.services._read_image", return_value=np.zeros((4, 4, 3), dtype=np.uint8)),
        ):
            result = analyze_image_identity_task.apply(
                args=[self.analysis.pk], task_id="matching-task",
            ).get()

        self.assertEqual(result["status"], IdentityAnalysis.Status.COMPLETED)
        self.assertEqual(IdentityFace.objects.filter(analysis=self.analysis).count(), 1)

    # -- service-level guard: superseded mid-flight --------------------------

    def test_a_task_id_change_during_embedding_is_detected_and_nothing_is_written(self):
        """The row is superseded by a newer retry while this call is still working."""
        analysis_pk = self.analysis.pk

        def embed_side_effect(crop):
            # A newer retry was queued (and started) while this one was busy
            # computing the embedding -- simulated by changing task_id here,
            # the same way the existing cancellation test mutates state from
            # inside a mock side effect.
            IdentityAnalysis.objects.filter(pk=analysis_pk).update(task_id="a-newer-retry")
            return np.array([1.0, 0.0, 0.0], dtype=np.float32)

        provider = MagicMock()
        provider.name = "test"
        provider.dimension = 3
        provider.model_checksum = "stub"
        provider.healthcheck.return_value = None
        provider.prepare_face.side_effect = lambda image, box, landmarks: image
        provider.embed.side_effect = embed_side_effect
        provider.validate_embedding.side_effect = lambda value: value
        provider.similarity.side_effect = _cosine_similarity

        result = self.process(provider)

        # The stale run must not have written anything: no faces, and the
        # status is whatever the (simulated) newer retry left it as, not
        # COMPLETED from this stale run.
        self.assertEqual(IdentityFace.objects.filter(analysis_id=analysis_pk).count(), 0)
        self.assertNotEqual(result.status, IdentityAnalysis.Status.COMPLETED)

    # -- redelivery of the identical task_id (still "processing") -----------

    def test_running_the_same_task_id_twice_before_completion_does_not_duplicate_rows(self):
        """The classic acks_late redelivery case: the same unacked task runs twice.

        By the time a real redelivery like this could happen, the first run
        would normally have already reached COMPLETED and been caught by the
        top guard (see test_reprocessing_a_completed_analysis_...). This
        exercises the deeper property directly: even set up to bypass that
        top guard (status forced back to "processing", same task_id kept),
        the delete-and-rebuild write path itself converges to one clean set
        of rows, not two.
        """
        provider = make_provider()
        self.process(provider)
        self.assertEqual(IdentityFace.objects.filter(analysis=self.analysis).count(), 1)
        first_group = IdentityGroup.objects.get()

        IdentityAnalysis.objects.filter(pk=self.analysis.pk).update(
            status=IdentityAnalysis.Status.PROCESSING,
        )

        second = self.process(provider)

        self.assertEqual(second.status, IdentityAnalysis.Status.COMPLETED)
        self.assertEqual(second.face_count, 1)
        self.assertEqual(IdentityFace.objects.filter(analysis=self.analysis).count(), 1)
        self.assertEqual(IdentityMembership.objects.filter(identity_face__analysis=self.analysis).count(), 1)
        self.assertEqual(IdentityGroup.objects.filter(owner=self.owner).count(), 1)
        first_group.refresh_from_db()
        self.assertEqual(first_group.member_count, 1)
