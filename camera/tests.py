"""Live camera sessions: metadata, explicit snapshots, 30-day retention, no video."""
import io
import shutil
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import User
from uploads.models import ImageUpload

from .models import CameraSession, CameraSnapshot
from .services import purge_old_sessions

MEDIA = tempfile.mkdtemp(prefix="facecloud-camera-")


def snapshot_file(name="snap.jpg"):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (90, 120, 150)).save(buffer, "JPEG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/jpeg")


@override_settings(MEDIA_ROOT=MEDIA, DUPLICATE_AUTO_ANALYZE=False, IDENTITY_AUTO_ANALYZE=False)
class CameraSessionTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.user = User.objects.create_user(username="cam", email="cam@example.com", password="ForestRiver!4821",
                                             is_email_verified=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def start(self, **body):
        response = self.client.post("/api/camera/sessions/", {"device_label": "Laptop webcam", **body}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_a_session_records_metadata_only(self):
        session = self.start(settings={"resolution": "1280x720", "overlay": True})
        self.assertEqual((session["snapshot_count"], session["ended_at"]), (0, None))
        self.assertFalse(ImageUpload.objects.exists())  # starting stores no image

    def test_a_snapshot_is_an_ordinary_upload_linked_to_the_session(self):
        session = self.start()
        response = self.client.post(f"/api/camera/sessions/{session['id']}/snapshots/", {"file": snapshot_file()},
                                    format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        image = ImageUpload.objects.get(pk=body["snapshot"]["image_id"])
        self.assertEqual((image.owner, image.status, image.batch.upload_type), (self.user, "uploaded", "single"))
        detail = self.client.get(f"/api/camera/sessions/{session['id']}/").json()
        self.assertEqual([row["image_id"] for row in detail["snapshots"]], [image.pk])

    def test_video_is_not_accepted_as_a_snapshot(self):
        session = self.start()
        video = SimpleUploadedFile("clip.mp4", b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4")
        response = self.client.post(f"/api/camera/sessions/{session['id']}/snapshots/", {"file": video},
                                    format="multipart")
        self.assertEqual(response.json()["snapshot"]["image_status"], "rejected")
        self.assertFalse(ImageUpload.objects.filter(status="uploaded").exists())

    def test_an_ended_session_takes_no_snapshots(self):
        session = self.start()
        self.assertIsNotNone(self.client.post(f"/api/camera/sessions/{session['id']}/end/").json()["ended_at"])
        response = self.client.post(f"/api/camera/sessions/{session['id']}/snapshots/", {"file": snapshot_file()},
                                    format="multipart")
        self.assertEqual(response.status_code, 409)

    def test_sessions_are_private(self):
        session = self.start()
        other = APIClient()
        other.force_authenticate(User.objects.create_user(username="cam2", email="cam2@example.com",
                                                          password="ForestRiver!4821", is_email_verified=True))
        self.assertEqual(other.get(f"/api/camera/sessions/{session['id']}/").status_code, 404)
        self.assertEqual(other.get("/api/camera/sessions/").json(), [])
        self.assertEqual(APIClient().get("/api/camera/sessions/").status_code, 401)

    def test_metadata_is_deleted_after_thirty_days_but_saved_images_stay(self):
        session = self.start()
        self.client.post(f"/api/camera/sessions/{session['id']}/snapshots/", {"file": snapshot_file()},
                         format="multipart")
        CameraSession.objects.update(started_at=timezone.now() - timedelta(days=31))
        self.assertEqual(self.client.get("/api/camera/sessions/").json(), [])  # purged on read
        self.assertFalse(CameraSnapshot.objects.exists())
        self.assertEqual(ImageUpload.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(purge_old_sessions(), 0)

    def test_settings_are_bounded(self):
        response = self.client.post("/api/camera/sessions/", {"settings": {"k" * 50: 1}}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_the_privacy_export_lists_sessions(self):
        session = self.start()
        export = self.client.get("/api/privacy/export/").json()
        self.assertEqual(export["camera_sessions"][0]["id"], session["id"])
