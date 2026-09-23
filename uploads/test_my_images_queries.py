from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from enhancement.models import ImageEditVersion, ImageEnhancementResult
from identities.models import (
    IdentityAnalysis,
    IdentityFace,
    IdentityGroup,
    IdentityMembership,
)
from validation.models import FaceValidationResult

from .models import ImagePreprocessingResult, ImageUpload, UploadBatch
from .serializers import MyImageSerializer

User = get_user_model()


class MyImagesQueryCountTests(TestCase):
    """The My Images screen must cost the same number of queries for 2 images or 200."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="queries", email="queries@example.com", password="ForestRiver!4821",
            is_email_verified=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="multiple")
        self.alice = self.group("Alice")
        self.bob = self.group("Bob")

    def group(self, name):
        return IdentityGroup.objects.create(
            owner=self.user, display_name=name, embedding_dimension=3,
            provider="test", policy_version="test-v1",
        )

    def add_image(self, index, memberships=()):
        """An image with a completed enhancement (and an edit) and one face per membership."""
        image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name=f"{index}.png", status="uploaded",
        )
        pre = ImagePreprocessingResult.objects.create(
            owner=self.user, image=image, status="completed", output_format="png",
        )
        result = ImageEnhancementResult.objects.create(
            owner=self.user, image=image, preprocessing_result=pre, status="completed",
            output_name="x.png", output_format="png",
        )
        ImageEditVersion.objects.create(
            owner=self.user, enhancement_result=result, mode="manual", status="completed",
            is_current=True, output_name="e.png", output_format="png",
        )
        validation = FaceValidationResult.objects.create(
            owner=self.user, image=image, preprocessing_result=pre, decision="accepted",
            face_count=len(memberships), faces=[],
        )
        analysis = IdentityAnalysis.objects.create(
            owner=self.user, image=image, preprocessing_result=pre, face_validation=validation,
            status="completed", provider="test", policy_version="test-v1",
        )
        for face_index, (group, state) in enumerate(memberships):
            face = IdentityFace.objects.create(
                owner=self.user, image=image, analysis=analysis, face_index=face_index,
                protected_embedding=b"x", embedding_dimension=3, provider="test",
            )
            IdentityMembership.objects.create(
                owner=self.user, identity_face=face, identity_group=group, state=state,
                decision_method="test",
            )
        return image

    def listing(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("my-image-list"))
        self.assertEqual(response.status_code, 200)
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        return rows, len(queries)

    def test_query_count_does_not_grow_with_the_number_of_images(self):
        pair = [(self.alice, "confirmed"), (self.bob, "auto_matched")]
        for index in range(2):
            self.add_image(index, pair)
        rows, small = self.listing()
        self.assertEqual(len(rows), 2)

        for index in range(2, 14):
            self.add_image(index, pair)
        rows, large = self.listing()

        self.assertEqual(len(rows), 14)
        self.assertEqual(small, large, f"query count grew from {small} to {large} with more images")

    def test_identity_groups_match_the_unoptimised_lookup(self):
        image = self.add_image(0, [
            (self.alice, "confirmed"),
            (self.alice, "new_group"),      # same group twice: listed once
            (self.bob, "rejected"),         # rejected: not listed
            (self.bob, "removed"),          # removed: not listed
        ])
        rows, _ = self.listing()
        optimised = rows[0]["identity_groups"]

        # Serialising a fresh instance has no prefetched data, so it takes the
        # original one-query-per-image path.
        reference = MyImageSerializer(ImageUpload.objects.get(pk=image.pk)).data["identity_groups"]

        self.assertEqual(optimised, [{"id": self.alice.pk, "display_name": "Alice"}])
        self.assertEqual(optimised, reference)

    def test_images_without_faces_still_list_with_no_groups(self):
        self.add_image(0, [])
        rows, _ = self.listing()
        self.assertEqual(rows[0]["identity_groups"], [])
        self.assertEqual(rows[0]["latest_enhancement"]["status"], "completed")
