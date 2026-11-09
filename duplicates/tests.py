import hashlib
import tempfile
from datetime import timedelta
from unittest.mock import patch

import cv2
import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from moderation.models import ModeratorReview
from organizations.services import create_organization
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
class DuplicateFixtures(APITestCase):
    """Two users, stored images and the helpers every duplicate test uses. It has no tests of its own."""

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

    def create_image(self, owner, name, pixels, *, checksum=None, faces=None, organization=None):
        success, encoded = cv2.imencode(".png", pixels)
        self.assertTrue(success)
        payload = encoded.tobytes()
        checksum = checksum or hashlib.sha256(payload).hexdigest()
        batch = UploadBatch.objects.create(owner=owner, upload_type="single", organization=organization)
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


class DuplicateDetectionTests(DuplicateFixtures):
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


class DuplicateMatchFixtures(DuplicateFixtures):
    """Helpers for the tests that read or re-check stored matches. It has no tests of its own."""

    def similar_image(self, owner, name, shift=0, **kwargs):
        """A photograph the check reads as the same picture as every other ``similar_image`` (a small brightness change), with a checksum of its own."""
        pixels = np.clip(self.patterned_image().astype(np.int16) + shift, 0, 255).astype(np.uint8)
        return self.create_image(owner, name, pixels, **kwargs)

    def replace_standardized_file(self, image, pixels):
        """What a new preprocessing of the photograph does to the check: the file it reads now holds other pixels."""
        preprocessing = image.preprocessing_result
        preprocessing.processed_file.delete(save=False)
        success, encoded = cv2.imencode(".png", pixels)
        self.assertTrue(success)
        preprocessing.processed_file.save("replaced.png", ContentFile(encoded.tobytes()))

    def make_match(self, source, matched, *, match_type="perceptual", review_status="suspected", owner=None):
        """A stored match written straight to the database (no check is run)."""
        owner = owner or source.owner
        analysis, _created = DuplicateAnalysis.objects.get_or_create(
            image=source,
            defaults={
                "owner": owner, "preprocessing_result": source.preprocessing_result, "status": "completed",
                "decision": "needs_review", "perceptual_hash": "0" * 16,
            },
        )
        return DuplicateMatch.objects.create(
            owner=owner, analysis=analysis, source_image=source, matched_image=matched, match_type=match_type,
            similarity_score=97.5, hash_distance=2, review_status=review_status,
        )

    def decide(self, match, review_status):
        """What the person's Confirm / Not a duplicate buttons send."""
        response = self.client.patch(
            reverse("duplicates:match-review", kwargs={"pk": match.pk}), {"review_status": review_status}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        match.refresh_from_db()
        return match

    def stored(self):
        """Every stored match, row by row: which photographs, what kind, who decided what and when, and when the row last changed."""
        return {
            match.pk: (
                match.source_image_id, match.matched_image_id, match.match_type, match.review_status,
                match.reviewed_by_id, match.reviewed_at, match.created_at, match.updated_at,
            )
            for match in DuplicateMatch.objects.all()
        }

    def summary(self, image):
        analysis = DuplicateAnalysis.objects.get(image=image)
        return analysis.status, analysis.decision, analysis.match_count


class DuplicateMatchListTests(DuplicateMatchFixtures):
    def setUp(self):
        super().setUp()
        self.url = reverse("duplicates:match-list")
        self.photos = [self.similar_image(self.user, f"listed-{number}.png", shift=number) for number in range(5)]

    def listed(self, query="", expected_status=status.HTTP_200_OK):
        response = self.client.get(f"{self.url}?{query}")
        self.assertEqual(response.status_code, expected_status, response.content)
        return response

    def listed_ids(self, query=""):
        return [row["id"] for row in self.listed(f"page=1&page_size=100&{query}").json()["results"]]

    def test_the_list_is_paged_newest_first_and_equal_times_are_ordered_by_the_newest_id(self):
        oldest = self.make_match(self.photos[1], self.photos[0])
        tied_first = self.make_match(self.photos[2], self.photos[0])
        tied_second = self.make_match(self.photos[3], self.photos[0])
        newest = self.make_match(self.photos[4], self.photos[0])
        now = timezone.now()
        for match, age in ((oldest, 3), (tied_first, 2), (tied_second, 2), (newest, 1)):
            DuplicateMatch.objects.filter(pk=match.pk).update(created_at=now - timedelta(hours=age))
        # The model's own order is the best similarity first: give the oldest the best, to show that it is not what decides here.
        DuplicateMatch.objects.filter(pk=oldest.pk).update(similarity_score=100.0)

        first = self.listed("page=1&page_size=2").json()
        self.assertEqual((first["count"], first["previous"]), (4, None))
        self.assertIsNotNone(first["next"])
        self.assertEqual([row["id"] for row in first["results"]], [newest.pk, tied_second.pk])
        second = self.listed("page=2&page_size=2").json()
        self.assertEqual([row["id"] for row in second["results"]], [tied_first.pk, oldest.pk])
        self.assertIsNone(second["next"])
        self.assertIsNotNone(second["previous"])

    def test_each_row_is_exactly_what_the_detail_call_returns(self):
        self.make_match(self.photos[1], self.photos[0])
        self.make_match(self.photos[2], self.photos[0], match_type="face_region", review_status="confirmed")
        rows = self.listed("page=1&page_size=10").json()["results"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            detail = self.client.get(reverse("duplicates:match-detail", kwargs={"pk": row["id"]}))
            self.assertEqual(row, detail.json())
        self.assertIn("source_filename", rows[0])
        self.assertIn("matched_image_download_url", rows[0])

    def test_a_page_never_holds_more_than_one_hundred_rows(self):
        batch = UploadBatch.objects.create(owner=self.user, upload_type="multiple")
        ImageUpload.objects.bulk_create(
            [ImageUpload(owner=self.user, batch=batch, original_name=f"bulk-{number}.png", status="uploaded") for number in range(110)]
        )
        bulk = list(ImageUpload.objects.filter(batch=batch).order_by("pk"))
        analysis = DuplicateAnalysis.objects.create(
            owner=self.user, image=bulk[0], preprocessing_result=self.photos[0].preprocessing_result, status="completed",
        )
        DuplicateMatch.objects.bulk_create(
            [
                DuplicateMatch(
                    owner=self.user, analysis=analysis, source_image=image, matched_image=bulk[0],
                    match_type="perceptual", similarity_score=99.0, hash_distance=1,
                )
                for image in bulk[1:]
            ]
        )
        body = self.listed("page=1&page_size=500").json()
        self.assertEqual((body["count"], len(body["results"])), (109, 100))
        self.assertEqual(len(self.listed("page=2&page_size=500").json()["results"]), 9)
        self.assertEqual(len(self.listed("page=1").json()["results"]), 20)

    def test_review_status_filter(self):
        suspected = self.make_match(self.photos[1], self.photos[0])
        confirmed = self.make_match(self.photos[2], self.photos[0], review_status="confirmed")
        dismissed = self.make_match(self.photos[3], self.photos[0], review_status="not_duplicate")
        self.assertEqual(sorted(self.listed_ids()), sorted([suspected.pk, confirmed.pk, dismissed.pk]))
        self.assertEqual(self.listed_ids("review_status=suspected"), [suspected.pk])
        self.assertEqual(self.listed_ids("review_status=confirmed"), [confirmed.pk])
        self.assertEqual(self.listed_ids("review_status=not_duplicate"), [dismissed.pk])

    def test_match_type_filter(self):
        exact = self.make_match(self.photos[1], self.photos[0], match_type="exact", review_status="confirmed")
        perceptual = self.make_match(self.photos[2], self.photos[0], match_type="perceptual")
        face = self.make_match(self.photos[3], self.photos[0], match_type="face_region")
        self.assertEqual(self.listed_ids("match_type=exact"), [exact.pk])
        self.assertEqual(self.listed_ids("match_type=perceptual"), [perceptual.pk])
        self.assertEqual(self.listed_ids("match_type=face_region"), [face.pk])

    def test_image_filter_finds_the_pairs_that_have_the_photograph_on_either_side(self):
        as_source = self.make_match(self.photos[2], self.photos[1])
        as_matched = self.make_match(self.photos[1], self.photos[0])
        elsewhere = self.make_match(self.photos[4], self.photos[3])
        self.assertEqual(sorted(self.listed_ids(f"image={self.photos[1].pk}")), sorted([as_source.pk, as_matched.pk]))
        self.assertEqual(self.listed_ids(f"image={self.photos[3].pk}"), [elsewhere.pk])
        self.assertEqual(self.listed_ids(f"image={self.photos[4].pk}"), [elsewhere.pk])

    def test_filters_combine(self):
        wanted = self.make_match(self.photos[1], self.photos[0], match_type="face_region", review_status="not_duplicate")
        self.make_match(self.photos[2], self.photos[0], match_type="face_region", review_status="suspected")
        self.make_match(self.photos[3], self.photos[0], match_type="perceptual", review_status="not_duplicate")
        self.make_match(self.photos[4], self.photos[3], match_type="face_region", review_status="not_duplicate")
        query = f"review_status=not_duplicate&match_type=face_region&image={self.photos[0].pk}"
        self.assertEqual(self.listed_ids(query), [wanted.pk])

    def test_a_value_that_is_not_one_of_the_choices_is_a_400_with_a_plain_sentence(self):
        self.make_match(self.photos[1], self.photos[0])
        refused = {
            "review_status=maybe": "review_status", "review_status=Suspected": "review_status", "match_type=fuzzy": "match_type",
            "image=abc": "image", "image=0": "image", "image=-3": "image", "image=1.5": "image",
            "image=%C2%B2": "image",            # the digit "²": True for str.isdigit(), but not a number
        }
        for query, field in refused.items():
            with self.subTest(query=query):
                body = self.listed(query, status.HTTP_400_BAD_REQUEST).json()
                self.assertEqual(list(body), [field])
                self.assertIsInstance(body[field], str)
        self.assertEqual(
            self.listed("review_status=maybe", status.HTTP_400_BAD_REQUEST).json(),
            {"review_status": "Choose a valid review status: suspected, confirmed or not_duplicate."},
        )
        self.assertEqual(
            self.listed("match_type=fuzzy", status.HTTP_400_BAD_REQUEST).json(),
            {"match_type": "Choose a valid match type: exact, perceptual or face_region."},
        )
        self.assertEqual(self.listed("image=abc", status.HTTP_400_BAD_REQUEST).json(), {"image": "Image must be a positive integer."})
        # An empty value is no filter, as in the analyses list.
        self.assertEqual(len(self.listed("review_status=&match_type=&image=").json()), 1)

    def test_an_id_too_large_to_belong_to_any_photograph_is_an_empty_list_not_an_error(self):
        self.make_match(self.photos[1], self.photos[0])
        self.assertEqual(self.listed(f"image={10 ** 40}").json(), [])
        self.assertEqual(self.listed(f"page=1&image={10 ** 40}").json()["count"], 0)

    def test_an_empty_list_is_a_200_with_a_count_of_zero(self):
        body = self.listed("page=1&page_size=5").json()
        self.assertEqual((body["count"], body["results"], body["next"], body["previous"]), (0, [], None, None))

    def test_a_dismissed_pair_is_listed_though_no_summary_counts_it_any_more(self):
        first, second = self.photos[0], self.photos[1]
        self.analyze(first)
        self.analyze(second)
        match = DuplicateMatch.objects.get()
        self.decide(match, "not_duplicate")

        # Dismissed, the pair drops out of both photographs' summaries, so no filter of the analyses list leads to it ...
        for decision in ("needs_review", "duplicate"):
            analyses = self.client.get(reverse("duplicates:analysis-list"), {"decision": decision}).json()
            self.assertEqual(analyses, [])
        self.assertEqual(self.summary(second), ("completed", "no_duplicate", 0))
        # ... but this list still has it.
        self.assertEqual(self.listed_ids("review_status=not_duplicate"), [match.pk])
        self.assertEqual(self.listed_ids("review_status=suspected"), [])

    def test_another_persons_matches_are_never_listed(self):
        theirs_a = self.similar_image(self.other, "theirs-a.png", shift=1)
        theirs_b = self.similar_image(self.other, "theirs-b.png", shift=2)
        mine = self.make_match(self.photos[1], self.photos[0])
        theirs = self.make_match(theirs_b, theirs_a, owner=self.other)
        self.assertEqual(self.listed_ids(), [mine.pk])
        self.assertEqual(self.listed_ids(f"image={theirs_a.pk}"), [])
        self.authenticate(self.other)
        self.assertEqual(self.listed_ids(), [theirs.pk])

    def test_a_pair_with_a_photograph_in_the_trash_is_left_out_until_it_is_restored(self):
        source_trashed = self.make_match(self.photos[1], self.photos[0])
        matched_trashed = self.make_match(self.photos[3], self.photos[2])
        kept = self.make_match(self.photos[4], self.photos[0])
        everything = sorted([source_trashed.pk, matched_trashed.pk, kept.pk])
        self.assertEqual(sorted(self.listed_ids()), everything)
        ImageUpload.all_objects.filter(pk__in=[self.photos[1].pk, self.photos[2].pk]).update(trashed_at=timezone.now())
        self.assertEqual(self.listed_ids(), [kept.pk])
        self.assertEqual(self.listed_ids(f"image={self.photos[2].pk}"), [])
        self.assertEqual(self.listed("page=1").json()["count"], 1)
        ImageUpload.all_objects.filter(pk__in=[self.photos[1].pk, self.photos[2].pk]).update(trashed_at=None)
        self.assertEqual(sorted(self.listed_ids()), everything)

    def test_the_list_needs_a_sign_in(self):
        self.client.credentials()
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_the_number_of_queries_does_not_grow_with_the_number_of_rows(self):
        self.make_match(self.photos[1], self.photos[0])
        self.make_match(self.photos[2], self.photos[0])
        with CaptureQueriesContext(connection) as few:
            self.assertEqual(len(self.listed("page=1&page_size=50").json()["results"]), 2)
        for number in range(3, 5):
            self.make_match(self.photos[number], self.photos[0])
            self.make_match(self.photos[number], self.photos[1], match_type="face_region")
        with CaptureQueriesContext(connection) as many:
            self.assertEqual(len(self.listed("page=1&page_size=50").json()["results"]), 6)
        self.assertEqual(len(many), len(few))


class DuplicateRecheckTests(DuplicateMatchFixtures):
    """"Find duplicates again" (POST with force=true) keeps what a person decided about a pair that the check finds again."""

    def setUp(self):
        super().setUp()
        self.first = self.similar_image(self.user, "first.png", shift=0)
        self.second = self.similar_image(self.user, "second.png", shift=3)
        self.third = self.similar_image(self.user, "third.png", shift=6)

    def queue_recheck(self, image):
        """The button's request. Returns the id of the queued analysis; the worker has not run yet."""
        with patch("duplicates.tasks.analyze_image_duplicates_task.apply_async") as apply_async:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("duplicates:image-analysis", kwargs={"image_id": image.pk}), {"force": True}, format="json",
                )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.content)
        apply_async.assert_called_once()
        return response.data["id"]

    def recheck(self, image):
        """The whole button: the request, then the job the worker takes from the queue."""
        return process_duplicate_analysis(self.queue_recheck(image))

    def three_photographs_in_one_library(self):
        """third~first, third~second and second~first, all suspected. Returns those three matches in that order."""
        for image in (self.first, self.second, self.third):
            self.analyze(image)
        by_pair = {(match.source_image_id, match.matched_image_id): match for match in DuplicateMatch.objects.all()}
        self.assertEqual(len(by_pair), 3)
        return (
            by_pair[(self.third.pk, self.first.pk)],
            by_pair[(self.third.pk, self.second.pk)],
            by_pair[(self.second.pk, self.first.pk)],
        )

    def test_a_forced_check_keeps_every_decision_and_every_match_id(self):
        third_first, third_second, second_first = self.three_photographs_in_one_library()
        self.decide(third_first, "confirmed")
        self.decide(third_second, "not_duplicate")
        before = self.stored()
        self.assertEqual(before[third_first.pk][3:5], ("confirmed", self.user.pk))
        self.assertEqual(before[third_second.pk][3:5], ("not_duplicate", self.user.pk))
        self.assertIsNotNone(before[third_first.pk][5])

        result = self.recheck(self.third)

        self.assertEqual(result.status, "completed")
        # The same three rows: ids, pairs, decisions, who and when. Not even updated_at moved, because nothing was written to them.
        self.assertEqual(self.stored(), before)
        # The summaries are recounted from the kept decisions: a dismissed pair is not counted, a confirmed one makes the photograph a duplicate.
        self.assertEqual(self.summary(self.third), ("completed", "duplicate", 1))
        self.assertEqual(self.summary(self.first), ("completed", "duplicate", 2))
        self.assertEqual(self.summary(self.second), ("completed", "needs_review", 1))
        self.assertEqual(DuplicateMatch.objects.get(pk=second_first.pk).review_status, "suspected")

    def test_the_same_holds_for_the_pairs_of_the_photograph_that_is_checked_again_on_the_matched_side(self):
        third_first, _third_second, second_first = self.three_photographs_in_one_library()
        self.decide(third_first, "not_duplicate")
        self.decide(second_first, "confirmed")
        before = self.stored()
        self.recheck(self.first)                 # the oldest photograph: its pairs have it as the matched_image
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.summary(self.first), ("completed", "duplicate", 1))

    def test_asking_for_a_new_check_deletes_nothing_before_the_check_runs(self):
        third_first, third_second, _second_first = self.three_photographs_in_one_library()
        self.decide(third_first, "confirmed")
        self.decide(third_second, "not_duplicate")
        before = self.stored()

        analysis_id = self.queue_recheck(self.third)

        self.assertEqual(DuplicateAnalysis.objects.get(pk=analysis_id).status, "queued")
        self.assertEqual(self.stored(), before)
        listed = self.client.get(reverse("duplicates:image-match-list", kwargs={"image_id": self.third.pk})).json()
        self.assertEqual({row["id"]: row["review_status"] for row in listed}, {third_first.pk: "confirmed", third_second.pk: "not_duplicate"})

    def test_a_pair_found_again_gets_fresh_evidence_but_keeps_its_decision_and_its_age(self):
        third_first, _third_second, _second_first = self.three_photographs_in_one_library()
        self.decide(third_first, "confirmed")
        DuplicateMatch.objects.filter(pk=third_first.pk).update(similarity_score=71.0, hash_distance=18, evidence={"method": "an_older_method"})
        created_at = DuplicateMatch.objects.get(pk=third_first.pk).created_at

        self.recheck(self.third)

        refreshed = DuplicateMatch.objects.get(pk=third_first.pk)
        self.assertEqual((refreshed.similarity_score, refreshed.hash_distance), (100.0, 0))
        self.assertEqual(refreshed.evidence, {"method": "phash_dct_64_v1"})
        self.assertEqual((refreshed.review_status, refreshed.reviewed_by_id), ("confirmed", self.user.pk))
        self.assertEqual(refreshed.created_at, created_at)

    def test_a_pair_that_is_no_longer_found_is_deleted_and_the_other_photograph_is_recounted(self):
        self.analyze(self.first)
        self.analyze(self.second)
        self.decide(DuplicateMatch.objects.get(), "confirmed")
        self.assertEqual(self.summary(self.first), ("completed", "duplicate", 1))

        self.replace_standardized_file(self.second, 255 - self.patterned_image())        # it is no longer like the other photograph
        self.recheck(self.second)

        self.assertFalse(DuplicateMatch.objects.exists())
        self.assertEqual(self.summary(self.second), ("completed", "no_duplicate", 0))
        self.assertEqual(self.summary(self.first), ("completed", "no_duplicate", 0))      # the other one does not keep reporting a pair that is gone

    def test_only_the_pairs_that_are_gone_are_deleted_and_the_others_keep_their_decisions(self):
        third_first, third_second, _second_first = self.three_photographs_in_one_library()
        self.decide(third_first, "confirmed")
        self.decide(third_second, "not_duplicate")
        self.replace_standardized_file(self.first, 255 - self.patterned_image())         # first is no longer like second and third
        self.recheck(self.first)
        self.assertEqual({match.pk: match.review_status for match in DuplicateMatch.objects.all()}, {third_second.pk: "not_duplicate"})
        self.assertEqual(self.summary(self.third), ("completed", "no_duplicate", 0))      # recounted: its only active pair was the one that went

    def test_a_pair_found_again_from_the_other_side_keeps_its_row_and_is_not_stored_twice(self):
        pixels = self.patterned_image()
        checksum = "d" * 64
        older = self.create_image(self.user, "same-older.png", pixels, checksum=checksum)
        newer = self.create_image(self.user, "same-newer.png", pixels, checksum=checksum)
        self.analyze(older)                      # the newer photograph has no finished check yet, so the pair is stored as (older, newer)
        stored = DuplicateMatch.objects.get()
        self.assertEqual((stored.source_image_id, stored.matched_image_id), (older.pk, newer.pk))
        self.decide(stored, "confirmed")
        before = self.stored()

        self.analyze(newer)                      # the newer photograph's own check finds the same pair as (newer, older)

        self.assertEqual(DuplicateMatch.objects.count(), 1)
        self.assertEqual(self.stored(), before)
        self.recheck(older)
        self.recheck(newer)
        self.assertEqual(self.stored(), before)

    def test_a_stored_pair_is_the_same_pair_whichever_photograph_is_stored_as_its_source(self):
        self.analyze(self.first)
        self.analyze(self.second)
        match = DuplicateMatch.objects.get()
        self.assertEqual((match.source_image_id, match.matched_image_id), (self.second.pk, self.first.pk))
        DuplicateMatch.objects.filter(pk=match.pk).update(
            source_image=self.first, matched_image=self.second, analysis=DuplicateAnalysis.objects.get(image=self.first),
        )
        self.decide(match, "not_duplicate")
        before = self.stored()
        for image in (self.second, self.first):
            self.recheck(image)
            self.assertEqual(DuplicateMatch.objects.count(), 1)
            self.assertEqual(self.stored(), before)
        self.assertEqual(self.summary(self.second), ("completed", "no_duplicate", 0))

    def test_a_pair_with_a_photograph_in_the_trash_is_never_deleted_by_a_check(self):
        self.analyze(self.first)
        self.analyze(self.second)
        match = DuplicateMatch.objects.get()
        self.decide(match, "confirmed")
        before = self.stored()
        ImageUpload.all_objects.filter(pk=self.first.pk).update(trashed_at=timezone.now())

        self.recheck(self.second)                # found again (the trashed photograph's finished check is still there): kept
        self.assertEqual(self.stored(), before)
        self.replace_standardized_file(self.second, 255 - self.patterned_image())
        self.recheck(self.second)                # no longer found, but a photograph in the trash can be restored, and the pair comes back with it as it was
        self.assertEqual(self.stored(), before)

        ImageUpload.all_objects.filter(pk=self.first.pk).update(trashed_at=None)
        listed = self.client.get(reverse("duplicates:match-list"), {"page": 1}).json()
        self.assertEqual([row["id"] for row in listed["results"]], [match.pk])
        self.recheck(self.second)                # restored, and the pair really is gone: now it is deleted
        self.assertFalse(DuplicateMatch.objects.exists())

    def test_a_pair_whose_other_photograph_is_being_checked_at_the_same_moment_is_left_alone(self):
        self.analyze(self.first)
        self.analyze(self.second)
        match = DuplicateMatch.objects.get()
        self.decide(match, "confirmed")
        before = self.stored()

        # Both photographs are checked again at once. While the older one's check is running it has no finished result, so the newer one's check
        # cannot compare with it: that is not the same as the pair being gone.
        DuplicateAnalysis.objects.filter(image=self.first).update(status="processing")
        self.recheck(self.second)
        self.assertEqual(self.stored(), before)
        DuplicateAnalysis.objects.filter(image=self.first).update(status="completed")
        self.recheck(self.first)
        self.assertEqual(self.stored(), before)

    def test_a_pair_beyond_the_candidate_limit_is_left_alone_not_deleted(self):
        third_first, _third_second, _second_first = self.three_photographs_in_one_library()
        self.decide(third_first, "confirmed")
        before = self.stored()
        with override_settings(DUPLICATE_MAX_CANDIDATES=1):         # the check compares with the newest finished photograph only: second, not first
            self.recheck(self.third)
        self.assertEqual(self.stored(), before)

    def test_two_checks_in_a_row_and_a_job_delivered_twice_change_nothing(self):
        third_first, third_second, _second_first = self.three_photographs_in_one_library()
        self.decide(third_first, "confirmed")
        self.decide(third_second, "not_duplicate")
        before = self.stored()

        self.recheck(self.third)
        self.recheck(self.third)
        self.assertEqual(self.stored(), before)

        # The queue delivers the same job to a second worker: the analysis is "processing" again and the job runs once more.
        analysis = DuplicateAnalysis.objects.get(image=self.third)
        DuplicateAnalysis.objects.filter(pk=analysis.pk).update(status="processing")
        process_duplicate_analysis(analysis.pk)
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.summary(self.third), ("completed", "duplicate", 1))

    def test_a_new_pair_found_by_the_check_is_suspected_and_a_deleted_pair_does_not_come_back_decided(self):
        self.analyze(self.first)
        self.analyze(self.second)
        old = DuplicateMatch.objects.get()
        self.decide(old, "confirmed")
        self.replace_standardized_file(self.second, 255 - self.patterned_image())
        self.recheck(self.second)
        self.assertFalse(DuplicateMatch.objects.exists())

        self.replace_standardized_file(self.second, self.patterned_image(offset=2))      # like the other photograph again
        self.recheck(self.second)

        new = DuplicateMatch.objects.get()
        self.assertNotEqual(new.pk, old.pk)
        self.assertEqual((new.review_status, new.reviewed_by_id, new.reviewed_at), ("suspected", None, None))
        self.assertEqual(self.summary(self.second), ("completed", "needs_review", 1))

    def test_each_kind_of_match_of_a_pair_keeps_its_own_decision(self):
        faces = [{"x": 20, "y": 20, "width": 80, "height": 80, "confidence": 0.9}]
        first = self.similar_image(self.user, "face-first.png", shift=10, faces=faces)
        second = self.similar_image(self.user, "face-second.png", shift=13, faces=faces)
        self.analyze(first)
        self.analyze(second)
        by_type = {match.match_type: match for match in DuplicateMatch.objects.all()}
        self.assertEqual(set(by_type), {"perceptual", "face_region"})
        self.decide(by_type["perceptual"], "confirmed")
        self.decide(by_type["face_region"], "not_duplicate")
        before = self.stored()

        self.recheck(second)

        self.assertEqual(self.stored(), before)
        self.assertEqual(self.summary(second), ("completed", "duplicate", 1))

    def test_an_exact_match_stays_confirmed_and_still_cannot_be_dismissed(self):
        checksum = "e" * 64
        pixels = self.patterned_image()
        older = self.create_image(self.user, "exact-older.png", pixels, checksum=checksum)
        newer = self.create_image(self.user, "exact-newer.png", pixels, checksum=checksum)
        self.analyze(older)
        self.analyze(newer)
        match = DuplicateMatch.objects.get()
        before = self.stored()
        self.recheck(newer)
        self.assertEqual(self.stored(), before)
        self.assertEqual(DuplicateMatch.objects.get().review_status, "confirmed")

        refused = self.client.patch(
            reverse("duplicates:match-review", kwargs={"pk": match.pk}), {"review_status": "not_duplicate"}, format="json",
        )
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)

        # An exact pair that is stored as anything else (it cannot be, through the API) is put right by the next check.
        DuplicateMatch.objects.filter(pk=match.pk).update(review_status="suspected")
        self.recheck(newer)
        self.assertEqual(DuplicateMatch.objects.get().pk, match.pk)
        self.assertEqual(DuplicateMatch.objects.get().review_status, "confirmed")

    def test_a_check_that_fails_leaves_the_decisions_where_they_were(self):
        third_first, third_second, _second_first = self.three_photographs_in_one_library()
        self.decide(third_first, "confirmed")
        self.decide(third_second, "not_duplicate")
        before = self.stored()
        with patch("duplicates.services._read_standardized", side_effect=RuntimeError("the file is unreadable")):
            with patch("duplicates.services.logger.exception"):
                result = self.recheck(self.third)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.stored(), before)

    def test_the_moderators_review_of_a_pair_survives_the_check(self):
        team = create_organization(name="Duplicate Co", owner=self.user)
        first = self.similar_image(self.user, "team-first.png", shift=10, organization=team)
        second = self.similar_image(self.user, "team-second.png", shift=13, organization=team)
        self.analyze(first)
        self.analyze(second)
        match = DuplicateMatch.objects.get()
        review = ModeratorReview.objects.get(duplicate_match=match)
        self.decide(match, "not_duplicate")

        self.recheck(second)

        self.assertEqual(DuplicateMatch.objects.get().pk, match.pk)
        self.assertEqual(list(ModeratorReview.objects.filter(duplicate_match=match).values_list("pk", flat=True)), [review.pk])

    def test_a_pair_somebody_already_decided_does_not_go_back_to_the_moderators_queue(self):
        team = create_organization(name="Duplicate Co", owner=self.user)
        first = self.similar_image(self.user, "queue-first.png", shift=10, organization=team)
        second = self.similar_image(self.user, "queue-second.png", shift=13, organization=team)
        self.analyze(first)
        self.analyze(second)
        match = DuplicateMatch.objects.get()

        ModeratorReview.objects.filter(duplicate_match=match).delete()
        self.recheck(second)                     # still suspected: it is in the queue, once
        self.assertEqual(ModeratorReview.objects.filter(duplicate_match=match).count(), 1)

        self.decide(match, "confirmed")
        ModeratorReview.objects.filter(duplicate_match=match).delete()
        self.recheck(second)                     # decided: the queue does not get it back
        self.assertEqual(ModeratorReview.objects.filter(duplicate_match=match).count(), 0)
