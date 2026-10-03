"""A photo with no detectable face is a normal result for identity analysis.

It used to end as ``failed`` with a logged traceback, which a frontend showed as
an error and admin queue views counted as a failed job, although nothing had gone
wrong. It now completes with zero faces.
"""
from unittest.mock import patch

import numpy as np
from django.contrib.auth import get_user_model
from django.test import TestCase

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import IdentityAnalysis, IdentityFace, IdentityGroup
from .services import process_identity_analysis
from .test_idempotency import make_provider
from privacy.testing import grant_face_consent

User = get_user_model()
ONE_FACE = [{"x": 0, "y": 0, "width": 10, "height": 10}]


class NoFacesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="noface-owner", email="noface@example.com", password="Password!123")
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="single")
        self.image = ImageUpload.objects.create(owner=self.owner, batch=batch, original_name="scene.jpg", status="uploaded")
        preprocessing = ImagePreprocessingResult.objects.create(owner=self.owner, image=self.image, status="completed")
        self.validation = FaceValidationResult.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=preprocessing,
            decision="accepted", face_count=0, faces=[],
        )
        self.analysis = IdentityAnalysis.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=preprocessing,
            face_validation=self.validation, status="queued", provider="test",
            policy_version="test-v1", task_id="task-1",
        )
        # Facial processing sits behind recorded consent, and production
        # requires it, so grant it here rather than relying on the flag
        # being switched off on this machine.
        grant_face_consent(self.owner)

    def process(self):
        with (
            patch("identities.services.get_identity_provider", return_value=make_provider()),
            patch("identities.services._read_image", return_value=np.zeros((4, 4, 3), dtype=np.uint8)),
        ):
            return process_identity_analysis(self.analysis.pk)

    def test_a_photo_without_faces_completes_with_zero_faces(self):
        with self.assertNoLogs("identities.services", level="ERROR"):
            result = self.process()
        self.assertEqual(result.status, IdentityAnalysis.Status.COMPLETED)
        self.assertEqual((result.face_count, result.assigned_count, result.review_count), (0, 0, 0))
        self.assertEqual(result.failure_reason, "")
        self.assertIsNotNone(result.finished_at)
        self.assertFalse(IdentityFace.objects.exists())
        self.assertFalse(IdentityGroup.objects.exists())

    def test_processing_it_again_does_nothing_more(self):
        self.process()
        again = self.process()
        self.assertEqual(again.status, IdentityAnalysis.Status.COMPLETED)
        self.assertFalse(IdentityFace.objects.exists())

    def test_faces_that_are_no_longer_detected_are_cleared_on_reanalysis(self):
        FaceValidationResult.objects.filter(pk=self.validation.pk).update(face_count=1, faces=ONE_FACE)
        first = self.process()
        self.assertEqual(first.face_count, 1)
        self.assertEqual(IdentityFace.objects.count(), 1)

        FaceValidationResult.objects.filter(pk=self.validation.pk).update(face_count=0, faces=[])
        IdentityAnalysis.objects.filter(pk=self.analysis.pk).update(status="queued", task_id="task-2")
        self.analysis.refresh_from_db()
        second = self.process()

        self.assertEqual(second.status, IdentityAnalysis.Status.COMPLETED)
        self.assertEqual(second.face_count, 0)
        self.assertFalse(IdentityFace.objects.exists())

    def test_a_real_failure_still_fails(self):
        FaceValidationResult.objects.filter(pk=self.validation.pk).update(face_count=1, faces=ONE_FACE)
        provider = make_provider()
        provider.embed.side_effect = RuntimeError("model crashed")
        with (
            patch("identities.services.get_identity_provider", return_value=provider),
            patch("identities.services._read_image", return_value=np.zeros((4, 4, 3), dtype=np.uint8)),
            self.assertLogs("identities.services", level="ERROR"),
        ):
            result = process_identity_analysis(self.analysis.pk)
        self.assertEqual(result.status, IdentityAnalysis.Status.FAILED)
        self.assertTrue(result.failure_reason)
