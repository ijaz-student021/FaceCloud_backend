"""T-008 Phase B: a newly created identity group is tagged with the owner's organization.

Owner-scoped access is unchanged (see IdentityGroup.organization's docstring) --
this only proves the field a future org-scoped moderator view will query is
actually populated.
"""
from unittest.mock import MagicMock, patch

import numpy as np
from django.contrib.auth import get_user_model
from django.test import TestCase

from organizations.services import create_personal_organization
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import IdentityAnalysis, IdentityGroup
from .services import process_identity_analysis

User = get_user_model()


def make_provider():
    provider = MagicMock()
    provider.name = "test"
    provider.dimension = 3
    provider.model_checksum = "stub"
    provider.healthcheck.return_value = None
    provider.prepare_face.side_effect = lambda image, box, landmarks: image
    provider.embed.return_value = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    provider.validate_embedding.side_effect = lambda value: value
    return provider


class GroupOrganizationTaggingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="group-owner", email="group-owner@example.com", password="Password!123",
        )
        self.personal_org = create_personal_organization(self.owner)
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="single")
        image = ImageUpload.objects.create(
            owner=self.owner, batch=batch, original_name="face.jpg", status="uploaded",
        )
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=image, status="completed",
        )
        validation = FaceValidationResult.objects.create(
            owner=self.owner, image=image, preprocessing_result=preprocessing,
            decision="accepted", face_count=1,
            faces=[{"x": 0, "y": 0, "width": 10, "height": 10}],
        )
        self.analysis = IdentityAnalysis.objects.create(
            owner=self.owner, image=image, preprocessing_result=preprocessing,
            face_validation=validation, status="queued", provider="test",
            policy_version="test-v1", task_id="task-1",
        )

    def test_a_freshly_created_group_is_tagged_with_the_owners_organization(self):
        with (
            patch("identities.services.get_identity_provider", return_value=make_provider()),
            patch("identities.services._read_image", return_value=np.zeros((4, 4, 3), dtype=np.uint8)),
        ):
            process_identity_analysis(self.analysis.pk)

        group = IdentityGroup.objects.get(owner=self.owner)
        self.assertEqual(group.organization, self.personal_org)
