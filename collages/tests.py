import tempfile
from datetime import timedelta
from unittest.mock import patch

import cv2
import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from identities.models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .layouts import CANVAS_HEIGHT, CANVAS_WIDTH, evenly_spaced, layout_cells, smart_crop
from .models import Collage
from .rendering import render_collage
from .services import expire_due_collages
from .tasks import process_collage
from privacy.testing import grant_face_consent, withdraw_face_consent

User = get_user_model()


class LayoutTests(SimpleTestCase):
    def test_every_layout_fills_the_canvas_without_overlap(self):
        for count in (3, 4, 5, 6):
            cells = layout_cells(count)
            self.assertEqual(len(cells), count)
            covered = np.zeros((CANVAS_HEIGHT, CANVAS_WIDTH), dtype=np.uint8)
            for x, y, width, height in cells:
                self.assertGreater(width, 100)
                self.assertGreater(height, 100)
                self.assertGreaterEqual(x, 0)
                self.assertLessEqual(x + width, CANVAS_WIDTH)
                self.assertLessEqual(y + height, CANVAS_HEIGHT)
                covered[y:y + height, x:x + width] += 1
            self.assertEqual(int(covered.max()), 1, f"cells overlap for {count} photos")

    def test_unsupported_counts_are_rejected(self):
        for count in (0, 1, 2, 7):
            with self.assertRaises(ValueError):
                layout_cells(count)

    def test_evenly_spaced_keeps_order_and_ends(self):
        self.assertEqual(evenly_spaced([1, 2, 3], 6), [1, 2, 3])
        picked = evenly_spaced(list(range(100)), 6)
        self.assertEqual(len(picked), 6)
        self.assertEqual((picked[0], picked[-1]), (0, 99))
        self.assertEqual(picked, sorted(picked))


class SmartCropTests(SimpleTestCase):
    def face(self, x, y, width, height):
        return {"standardized_box": {"x": x, "y": y, "width": width, "height": height}}

    def test_a_face_near_the_edge_stays_inside_a_narrower_crop(self):
        # A 1000x500 photo cropped to a square would centre-crop the face at x=900 away.
        crop = smart_crop(1000, 500, 1.0, [self.face(880, 200, 80, 100)])
        self.assertEqual((crop["width"], crop["height"]), (500, 500))
        self.assertTrue(crop["faces_in_frame"])
        self.assertLessEqual(crop["x"] + crop["width"], 1000)
        self.assertLessEqual(crop["x"], 880)
        self.assertGreaterEqual(crop["x"] + crop["width"], 960)

    def test_every_face_is_kept_when_they_fit_together(self):
        faces = [self.face(100, 100, 60, 60), self.face(400, 120, 60, 60)]
        crop = smart_crop(1000, 500, 1.0, faces)
        self.assertTrue(crop["faces_in_frame"])

    def test_faces_that_cannot_fit_report_it_and_keep_the_largest(self):
        faces = [self.face(0, 100, 100, 100), self.face(900, 100, 40, 40)]
        crop = smart_crop(1000, 500, 1.0, faces)
        self.assertFalse(crop["faces_in_frame"])
        self.assertLessEqual(crop["x"], 0)

    def test_no_faces_centres_the_crop(self):
        crop = smart_crop(1000, 500, 1.0, [])
        self.assertEqual(crop["x"], 250)
        self.assertEqual(crop["y"], 0)
        self.assertTrue(crop["faces_in_frame"])

    def test_malformed_boxes_are_ignored_not_fatal(self):
        bad = [{"nonsense": 1}, None, {"x": "a", "y": 1, "width": 2, "height": 2}, {"x": 5, "y": 5, "width": 0, "height": 9}]
        crop = smart_crop(400, 400, 1.0, bad)
        self.assertEqual((crop["x"], crop["y"], crop["width"], crop["height"]), (0, 0, 400, 400))

    def test_the_crop_always_matches_the_target_shape_and_stays_inside(self):
        for aspect in (0.5, 0.75, 1.0, 1.5, 2.5):
            crop = smart_crop(640, 480, aspect, [self.face(600, 400, 60, 70)])
            self.assertGreaterEqual(crop["x"], 0)
            self.assertGreaterEqual(crop["y"], 0)
            self.assertLessEqual(crop["x"] + crop["width"], 640)
            self.assertLessEqual(crop["y"] + crop["height"], 480)
            self.assertAlmostEqual(crop["width"] / crop["height"], aspect, delta=0.02)

    def test_rendering_produces_a_canvas_sized_jpeg(self):
        tiles = [(np.full((300, 400, 3), 128 + index * 10, dtype=np.uint8), []) for index in range(4)]
        payload, crops = render_collage(tiles)
        decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape[:2], (CANVAS_HEIGHT, CANVAS_WIDTH))
        self.assertEqual(len(crops), 4)


class CollageTestCase(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        media_override = override_settings(MEDIA_ROOT=self.media.name)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.addCleanup(self.media.cleanup)
        self.owner = User.objects.create_user(username="collage-owner", email="collage-owner@example.com", password="pass-12345")
        self.other = User.objects.create_user(username="collage-other", email="collage-other@example.com", password="pass-12345")
        self.base = timezone.now() - timedelta(days=5)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        # Facial processing sits behind recorded consent, and production
        # requires it, so grant it here rather than relying on the flag
        # being switched off on this machine.
        grant_face_consent(self.owner, self.other)

    def photo(self, owner, hours, *, faces=None, processed=True, color=None):
        pixels = np.full((240, 320, 3), color if color is not None else 60 + int(hours) % 150, dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", pixels)
        payload = encoded.tobytes()
        batch = UploadBatch.objects.create(owner=owner, upload_type="single")
        image = ImageUpload.objects.create(
            owner=owner, batch=batch, original_name=f"p{hours}.png", file_extension=".png",
            mime_type="image/png", width=320, height=240, status="uploaded",
        )
        image.original_file.save("p.png", ContentFile(payload))
        ImageUpload.objects.filter(pk=image.pk).update(created_at=self.base + timedelta(hours=hours))
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=owner, image=image, status="completed" if processed else "pending",
            output_format="png", output_name="p.png", output_width=320, output_height=240,
        )
        if processed:
            preprocessing.processed_file.save("p.png", ContentFile(payload))
        if faces is not None:
            FaceValidationResult.objects.create(
                owner=owner, image=image, preprocessing_result=preprocessing, decision="accepted",
                face_count=len(faces), faces=faces,
            )
        return image

    def event_photos(self, count, owner=None):
        return [self.photo(owner or self.owner, hours) for hours in range(count)]

    def create(self, **payload):
        with patch("collages.tasks.generate_collage_task.apply_async") as queue:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse("collage-list"), payload, format="json")
        return response, queue

    def make_group(self, owner, images, *, name="Sara"):
        group = IdentityGroup.objects.create(
            owner=owner, display_name=name, embedding_dimension=4, provider="test", policy_version="v1",
        )
        for index, image in enumerate(images):
            analysis = IdentityAnalysis.objects.create(
                owner=owner, image=image, preprocessing_result=image.preprocessing_result,
                face_validation=image.face_validation, status="completed", provider="test", policy_version="v1",
            )
            face = IdentityFace.objects.create(
                owner=owner, image=image, analysis=analysis, face_index=0,
                bounding_box={"x": 10, "y": 10, "width": 20, "height": 20},
                protected_embedding=b"x", embedding_dimension=4, provider="test",
            )
            IdentityMembership.objects.create(
                owner=owner, identity_face=face, identity_group=group, state="confirmed",
            )
        return group


class EventCollageTests(CollageTestCase):
    def test_login_is_required(self):
        self.assertEqual(APIClient().get(reverse("collage-list")).status_code, 401)
        self.assertEqual(APIClient().post(reverse("collage-list"), {}, format="json").status_code, 401)

    def test_creating_an_event_collage_queues_a_job_and_returns_202(self):
        photos = self.event_photos(4)
        response, queue = self.create(source_type="event", event_id=photos[0].pk, title="Trip")
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["status"], "queued")
        self.assertEqual(response.data["title"], "Trip")
        self.assertEqual(response.data["photo_count"], 4)
        self.assertIsNone(response.data["download_url"])
        queue.assert_called_once()

    def test_the_job_produces_a_downloadable_jpeg_with_crop_only_metadata(self):
        photos = self.event_photos(4)
        response, _queue = self.create(source_type="event", event_id=photos[0].pk)
        collage = Collage.objects.get(pk=response.data["id"])
        result = process_collage(collage.pk, collage.task_id)
        self.assertEqual(result.status, Collage.Status.READY, result.failure_reason)
        self.assertTrue(result.expires_at > timezone.now())
        self.assertEqual([entry["image_id"] for entry in result.manifest["photos"]], [p.pk for p in photos])
        for entry in result.manifest["photos"]:
            self.assertEqual(set(entry), {"image_id", "crop", "faces_in_frame"})
        download = self.client.get(reverse("collage-download", kwargs={"pk": collage.pk}))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["Content-Type"], "image/jpeg")
        body = b"".join(download.streaming_content)
        decoded = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape[:2], (CANVAS_HEIGHT, CANVAS_WIDTH))
        detail = self.client.get(reverse("collage-detail", kwargs={"pk": collage.pk}))
        self.assertTrue(detail.data["download_url"].endswith(f"/api/collages/{collage.pk}/download/"))

    def test_more_than_six_photos_are_sampled_across_the_event(self):
        photos = self.event_photos(10)
        response, _queue = self.create(source_type="event", event_id=photos[0].pk)
        self.assertEqual(response.data["photo_count"], 6)
        self.assertEqual(response.data["image_ids"][0], photos[0].pk)
        self.assertEqual(response.data["image_ids"][-1], photos[-1].pk)

    def test_too_few_processed_photos_is_a_clear_400(self):
        photos = self.event_photos(2)
        self.photo(self.owner, 2, processed=False)
        response, queue = self.create(source_type="event", event_id=photos[0].pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("At least 3", str(response.data))
        queue.assert_not_called()
        self.assertFalse(Collage.objects.exists())

    def test_unknown_or_someone_elses_event_is_404(self):
        theirs = self.event_photos(4, owner=self.other)
        for event_id in (theirs[0].pk, 999999):
            response, _queue = self.create(source_type="event", event_id=event_id)
            self.assertEqual(response.status_code, 404, event_id)

    def test_request_validation(self):
        for payload in (
            {"source_type": "event"},
            {"source_type": "person"},
            {"source_type": "bogus", "event_id": 1},
            {"source_type": "event", "event_id": 1, "gap_hours": 0},
        ):
            response, _queue = self.create(**payload)
            self.assertEqual(response.status_code, 400, payload)

    def test_only_one_collage_runs_at_a_time_per_owner(self):
        photos = self.event_photos(4)
        first, _queue = self.create(source_type="event", event_id=photos[0].pk)
        second, queue = self.create(source_type="event", event_id=photos[0].pk)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["id"], second.data["id"])
        queue.assert_not_called()
        self.assertEqual(Collage.objects.count(), 1)

    def test_a_queue_outage_marks_the_collage_failed(self):
        photos = self.event_photos(3)
        with patch("collages.tasks.generate_collage_task.apply_async", side_effect=ConnectionError("redis down")):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("collage-list"), {"source_type": "event", "event_id": photos[0].pk}, format="json",
                )
        collage = Collage.objects.get(pk=response.data["id"])
        self.assertEqual(collage.status, Collage.Status.FAILED)
        self.assertNotIn("redis", collage.failure_reason)

    def test_photos_that_vanish_before_the_job_runs_fail_it_cleanly(self):
        photos = self.event_photos(3)
        response, _queue = self.create(source_type="event", event_id=photos[0].pk)
        photos[1].delete()
        collage = Collage.objects.get(pk=response.data["id"])
        result = process_collage(collage.pk, collage.task_id)
        self.assertEqual(result.status, Collage.Status.FAILED)
        self.assertIn("Too few", result.failure_reason)
        self.assertFalse(result.collage_file)

    def test_a_superseded_task_does_not_run(self):
        photos = self.event_photos(3)
        response, _queue = self.create(source_type="event", event_id=photos[0].pk)
        collage = Collage.objects.get(pk=response.data["id"])
        result = process_collage(collage.pk, "some-other-task")
        self.assertEqual(result.status, Collage.Status.QUEUED)

    def test_download_before_ready_is_409_and_after_expiry_410(self):
        photos = self.event_photos(3)
        response, _queue = self.create(source_type="event", event_id=photos[0].pk)
        url = reverse("collage-download", kwargs={"pk": response.data["id"]})
        self.assertEqual(self.client.get(url).status_code, 409)
        collage = Collage.objects.get(pk=response.data["id"])
        process_collage(collage.pk, collage.task_id)
        Collage.objects.filter(pk=collage.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(url).status_code, 410)
        collage.refresh_from_db()
        self.assertEqual(collage.status, Collage.Status.EXPIRED)
        self.assertFalse(collage.collage_file)

    def test_expiry_sweep_removes_files(self):
        photos = self.event_photos(3)
        response, _queue = self.create(source_type="event", event_id=photos[0].pk)
        collage = Collage.objects.get(pk=response.data["id"])
        process_collage(collage.pk, collage.task_id)
        collage.refresh_from_db()
        name = collage.collage_file.name
        self.assertTrue(collage.collage_file.storage.exists(name))
        Collage.objects.filter(pk=collage.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(expire_due_collages(), 1)
        self.assertFalse(collage.collage_file.storage.exists(name))

    def test_other_users_cannot_see_download_or_delete_a_collage(self):
        photos = self.event_photos(3)
        response, _queue = self.create(source_type="event", event_id=photos[0].pk)
        pk = response.data["id"]
        intruder = APIClient()
        intruder.force_authenticate(self.other)
        self.assertEqual(intruder.get(reverse("collage-list")).data["count"], 0)
        for method, name in (("get", "collage-detail"), ("get", "collage-download"), ("delete", "collage-detail")):
            self.assertEqual(getattr(intruder, method)(reverse(name, kwargs={"pk": pk})).status_code, 404, name)

    def test_deleting_a_collage_removes_its_file(self):
        photos = self.event_photos(3)
        response, _queue = self.create(source_type="event", event_id=photos[0].pk)
        collage = Collage.objects.get(pk=response.data["id"])
        process_collage(collage.pk, collage.task_id)
        collage.refresh_from_db()
        storage, name = collage.collage_file.storage, collage.collage_file.name
        delete = self.client.delete(reverse("collage-detail", kwargs={"pk": collage.pk}))
        self.assertEqual(delete.status_code, 204)
        self.assertFalse(storage.exists(name))

    def test_faces_stay_inside_their_tile(self):
        # Face on the far right of a 4:3 photo; the collage tile is wider than tall.
        faces = [{"standardized_box": {"x": 250, "y": 90, "width": 50, "height": 60}}]
        photos = [self.photo(self.owner, hours, faces=faces) for hours in range(3)]
        response, _queue = self.create(source_type="event", event_id=photos[0].pk)
        collage = Collage.objects.get(pk=response.data["id"])
        result = process_collage(collage.pk, collage.task_id)
        self.assertEqual(result.status, Collage.Status.READY, result.failure_reason)
        self.assertTrue(all(entry["faces_in_frame"] for entry in result.manifest["photos"]))


class PersonCollageTests(CollageTestCase):
    def setUp(self):
        super().setUp()
        self.images = [self.photo(self.owner, hours, faces=[]) for hours in range(4)]
        self.group = self.make_group(self.owner, self.images, name="Sara")

    def test_a_person_collage_uses_existing_group_members_and_records_only_the_name(self):
        response, _queue = self.create(source_type="person", group_id=self.group.pk)
        self.assertEqual(response.status_code, 202, response.data)
        collage = Collage.objects.get(pk=response.data["id"])
        result = process_collage(collage.pk, collage.task_id)
        self.assertEqual(result.status, Collage.Status.READY, result.failure_reason)
        self.assertEqual(result.manifest["subject"], "Sara")
        flattened = str(result.manifest).lower()
        for forbidden in ("embedding", "similarity", "centroid", "distance", "score"):
            self.assertNotIn(forbidden, flattened)

    @override_settings(BIOMETRIC_CONSENT_REQUIRED=True)
    def test_without_consent_a_person_collage_is_refused(self):
        withdraw_face_consent(self.owner)
        response, queue = self.create(source_type="person", group_id=self.group.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("consent", str(response.data).lower())
        queue.assert_not_called()

    def test_consent_withdrawn_before_the_job_runs_fails_the_job(self):
        response, _queue = self.create(source_type="person", group_id=self.group.pk)
        collage = Collage.objects.get(pk=response.data["id"])
        withdraw_face_consent(self.owner)
        with override_settings(BIOMETRIC_CONSENT_REQUIRED=True):
            result = process_collage(collage.pk, collage.task_id)
        self.assertEqual(result.status, Collage.Status.FAILED)
        self.assertIn("consent", result.failure_reason.lower())

    def test_someone_elses_or_inactive_group_is_404(self):
        theirs = self.make_group(self.other, [self.photo(self.other, hours, faces=[]) for hours in range(3)])
        response, _queue = self.create(source_type="person", group_id=theirs.pk)
        self.assertEqual(response.status_code, 404)
        IdentityGroup.objects.filter(pk=self.group.pk).update(state="archived")
        response, _queue = self.create(source_type="person", group_id=self.group.pk)
        self.assertEqual(response.status_code, 404)

    def test_removed_memberships_are_not_used(self):
        IdentityMembership.objects.filter(identity_group=self.group).update(state="removed")
        response, _queue = self.create(source_type="person", group_id=self.group.pk)
        self.assertEqual(response.status_code, 400)
