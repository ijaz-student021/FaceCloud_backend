import hashlib
import tempfile
from unittest.mock import patch

import cv2
import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import DuplicateAnalysis, DuplicateMatch
from .services import (
    create_or_reset_duplicate_analysis,
    hamming_distance,
    perceptual_hash,
    process_duplicate_analysis,
)

User = get_user_model()


@override_settings(
    DUPLICATE_PERCEPTUAL_HASH_DISTANCE=8,
    DUPLICATE_FACE_HASH_DISTANCE=6,
    DUPLICATE_MAX_CANDIDATES=5000,
)
class DuplicateDetectionTests(APITestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.user = User.objects.create_user(
            username="duplicate-owner",
            email="duplicate-owner@example.com",
            password="StrongPass!4729",
            is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="duplicate-other",
            email="duplicate-other@example.com",
            password="StrongPass!4729",
            is_email_verified=True,
        )
        self.authenticate(self.user)

    def tearDown(self):
        self.media_override.disable()
        self.media.cleanup()

    def authenticate(self, user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def create_image(self, owner, name, pixels, *, checksum=None, faces=None):
        success, encoded = cv2.imencode(".png", pixels)
        self.assertTrue(success)
        payload = encoded.tobytes()
        checksum = checksum or hashlib.sha256(payload).hexdigest()
        batch = UploadBatch.objects.create(owner=owner, upload_type="single")
        image = ImageUpload.objects.create(
            owner=owner,
            batch=batch,
            original_name=name,
            file_extension=".png",
            mime_type="image/png",
            file_size=len(payload),
            width=pixels.shape[1],
            height=pixels.shape[0],
            checksum_sha256=checksum,
            status="uploaded",
        )
        image.original_file.save(name, ContentFile(payload))
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=owner,
            image=image,
            status="completed",
            output_format="png",
            output_name=name,
            output_width=pixels.shape[1],
            output_height=pixels.shape[0],
            processed_file_size=len(payload),
        )
        preprocessing.processed_file.save(name, ContentFile(payload))
        if faces is not None:
            FaceValidationResult.objects.create(
                owner=owner,
                image=image,
                preprocessing_result=preprocessing,
                decision="accepted",
                face_count=len(faces),
                faces=faces,
                message="Test face evidence.",
            )
        return image

    def analyze(self, image):
        analysis, _created = create_or_reset_duplicate_analysis(image, force=True)
        return process_duplicate_analysis(analysis.pk)

    @staticmethod
    def patterned_image(offset=0):
        grid = np.indices((160, 160)).sum(axis=0)
        gray = np.where((grid // 16) % 2 == 0, 45 + offset, 190 + offset)
        return np.repeat(np.clip(gray, 0, 255).astype(np.uint8)[:, :, None], 3, axis=2)

    def test_hash_is_deterministic_and_distance_is_bounded(self):
        image = self.patterned_image()
        first = perceptual_hash(image)
        second = perceptual_hash(image.copy())
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)
        self.assertEqual(hamming_distance(first, second), 0)

    def test_exact_checksum_match_is_confirmed_and_idempotent(self):
        pixels = self.patterned_image()
        checksum = "a" * 64
        original = self.create_image(self.user, "original.png", pixels, checksum=checksum)
        duplicate = self.create_image(self.user, "copy.png", pixels, checksum=checksum)
        self.analyze(original)
        result = self.analyze(duplicate)

        self.assertEqual(result.status, DuplicateAnalysis.Status.COMPLETED)
        self.assertEqual(result.decision, DuplicateAnalysis.Decision.DUPLICATE)
        match = DuplicateMatch.objects.get(match_type=DuplicateMatch.MatchType.EXACT)
        self.assertEqual(match.review_status, DuplicateMatch.ReviewStatus.CONFIRMED)
        self.assertEqual(match.similarity_score, 100.0)

        rerun = process_duplicate_analysis(result.pk)
        self.assertEqual(rerun.pk, result.pk)
        self.assertEqual(DuplicateMatch.objects.count(), 1)

    def test_visual_copy_with_different_checksum_requires_review(self):
        first_pixels = self.patterned_image()
        second_pixels = np.clip(first_pixels.astype(np.int16) + 4, 0, 255).astype(np.uint8)
        original = self.create_image(self.user, "visual-a.png", first_pixels)
        copy = self.create_image(self.user, "visual-b.png", second_pixels)
        self.analyze(original)
        result = self.analyze(copy)

        self.assertEqual(result.decision, DuplicateAnalysis.Decision.NEEDS_REVIEW)
        match = DuplicateMatch.objects.get(match_type=DuplicateMatch.MatchType.PERCEPTUAL)
        self.assertLessEqual(match.hash_distance, 8)
        self.assertEqual(match.review_status, DuplicateMatch.ReviewStatus.SUSPECTED)

    def test_matching_face_region_is_review_evidence_not_identity_claim(self):
        first = np.full((200, 240, 3), (20, 60, 110), dtype=np.uint8)
        second = np.full((200, 240, 3), (170, 80, 30), dtype=np.uint8)
        face_patch = self.patterned_image()[:80, :80]
        first[60:140, 80:160] = face_patch
        second[60:140, 80:160] = face_patch
        faces = [{"x": 80, "y": 60, "width": 80, "height": 80, "confidence": 0.9}]
        original = self.create_image(self.user, "face-a.png", first, faces=faces)
        repeated = self.create_image(self.user, "face-b.png", second, faces=faces)
        self.analyze(original)
        result = self.analyze(repeated)

        face_match = DuplicateMatch.objects.get(match_type=DuplicateMatch.MatchType.FACE_REGION)
        self.assertEqual(result.decision, DuplicateAnalysis.Decision.NEEDS_REVIEW)
        self.assertEqual(face_match.evidence["method"], "perceptual_face_region_v1")
        self.assertNotIn("fingerprint", face_match.evidence)

    def test_cross_user_checksum_is_never_reported(self):
        pixels = self.patterned_image()
        checksum = "b" * 64
        self.create_image(self.other, "private.png", pixels, checksum=checksum)
        image = self.create_image(self.user, "owner.png", pixels, checksum=checksum)
        result = self.analyze(image)

        self.assertEqual(result.decision, DuplicateAnalysis.Decision.NO_DUPLICATE)
        self.assertEqual(result.match_count, 0)
        self.assertFalse(DuplicateMatch.objects.exists())

    def test_corrupt_standardized_image_fails_with_safe_reason(self):
        pixels = self.patterned_image()
        image = self.create_image(self.user, "corrupt.png", pixels)
        preprocessing = image.preprocessing_result
        preprocessing.processed_file.delete(save=False)
        preprocessing.processed_file.save("corrupt.png", ContentFile(b"not-an-image"))
        analysis, _created = create_or_reset_duplicate_analysis(image, force=True)

        with patch("duplicates.services.logger.exception") as log_exception:
            result = process_duplicate_analysis(analysis.pk)

        self.assertEqual(result.status, DuplicateAnalysis.Status.FAILED)
        self.assertEqual(
            result.failure_reason,
            "Duplicate analysis could not be completed safely.",
        )
        self.assertNotIn("corrupt.png", result.failure_reason)
        log_exception.assert_called_once()

    @patch("duplicates.tasks.analyze_image_duplicates_task.apply_async")
    def test_api_queues_analysis_and_keeps_internal_hashes_private(self, apply_async):
        image = self.create_image(self.user, "queued.png", self.patterned_image())
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("duplicates:image-analysis", kwargs={"image_id": image.pk}),
                {"force": False},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["status"], DuplicateAnalysis.Status.QUEUED)
        self.assertNotIn("perceptual_hash", response.data)
        self.assertNotIn("face_fingerprints", response.data)
        self.assertNotIn("task_id", response.data)
        apply_async.assert_called_once()

    def test_owner_can_read_and_review_visual_match_but_other_user_cannot(self):
        first = self.create_image(self.user, "review-a.png", self.patterned_image())
        brighter = np.clip(
            self.patterned_image().astype(np.int16) + 3, 0, 255
        ).astype(np.uint8)
        second = self.create_image(self.user, "review-b.png", brighter)
        self.analyze(first)
        result = self.analyze(second)
        match = DuplicateMatch.objects.get(match_type=DuplicateMatch.MatchType.PERCEPTUAL)

        matches = self.client.get(
            reverse("duplicates:image-match-list", kwargs={"image_id": second.pk})
        )
        self.assertEqual(matches.status_code, status.HTTP_200_OK)
        self.assertEqual(len(matches.data), 1)
        self.assertIn("source_image_download_url", matches.data[0])
        self.assertIn("matched_image_download_url", matches.data[0])

        my_images = self.client.get(reverse("my-image-list"))
        self.assertEqual(my_images.status_code, status.HTTP_200_OK)
        image_items = (
            my_images.data["results"]
            if isinstance(my_images.data, dict)
            else my_images.data
        )
        second_summary = next(
            item for item in image_items if item["id"] == second.pk
        )
        self.assertEqual(
            second_summary["duplicate_analysis"]["decision"],
            DuplicateAnalysis.Decision.NEEDS_REVIEW,
        )
        reviewed = self.client.patch(
            reverse("duplicates:match-review", kwargs={"pk": match.pk}),
            {"review_status": "not_duplicate"},
            format="json",
        )
        self.assertEqual(reviewed.status_code, status.HTTP_200_OK)
        result.refresh_from_db()
        self.assertEqual(result.decision, DuplicateAnalysis.Decision.NO_DUPLICATE)

        self.authenticate(self.other)
        hidden_analysis = self.client.get(
            reverse("duplicates:image-analysis", kwargs={"image_id": second.pk})
        )
        hidden_match = self.client.get(
            reverse("duplicates:match-detail", kwargs={"pk": match.pk})
        )
        self.assertEqual(hidden_analysis.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(hidden_match.status_code, status.HTTP_404_NOT_FOUND)

    def test_exact_match_cannot_be_dismissed(self):
        checksum = "c" * 64
        first = self.create_image(
            self.user, "exact-a.png", self.patterned_image(), checksum=checksum
        )
        second = self.create_image(
            self.user, "exact-b.png", self.patterned_image(), checksum=checksum
        )
        self.analyze(first)
        self.analyze(second)
        match = DuplicateMatch.objects.get(match_type=DuplicateMatch.MatchType.EXACT)

        response = self.client.patch(
            reverse("duplicates:match-review", kwargs={"pk": match.pk}),
            {"review_status": "not_duplicate"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        match.refresh_from_db()
        self.assertEqual(match.review_status, DuplicateMatch.ReviewStatus.CONFIRMED)
