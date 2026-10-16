"""Administrator image monitoring (B06)."""
import io
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import User
from activity.models import ActivityEvent
from organizations.services import create_organization
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

MEDIA = tempfile.mkdtemp(prefix="facecloud-admin-images-")


def jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (320, 240), (90, 140, 200)).save(buffer, "JPEG")
    return buffer.getvalue()


@override_settings(MEDIA_ROOT=MEDIA, THUMBNAIL_PREGENERATE=False)
class AdminImageMonitoringTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.admin = self.make_user("admin", role="admin")
        self.alice = self.make_user("alice")
        self.bob = self.make_user("bob")
        self.organization = create_organization(name="Org", owner=self.bob)
        self.alice_image = self.make_image(self.alice, "holiday.jpg")
        self.bob_image = self.make_image(self.bob, "team.jpg", organization=self.organization)
        self.rejected = self.make_image(self.bob, "drawing.svg", status="rejected")
        ImageQualityDecision.objects.create(
            owner=self.alice, image=self.alice_image, status="completed", decision="needs_review",
            preprocessing_result=ImagePreprocessingResult.objects.create(
                owner=self.alice, image=self.alice_image, status="completed"),
            overall_score=41.5, sharpness_score=30.0, brightness_score=55.0,
        )

    def make_user(self, name, role="user"):
        return User.objects.create_user(username=name, email=f"{name}@example.com", password="ForestRiver!4821",
                                        is_email_verified=True, role=role)

    def make_image(self, owner, name, status="uploaded", organization=None):
        batch = UploadBatch.objects.create(owner=owner, upload_type="single", organization=organization)
        image = ImageUpload.objects.create(owner=owner, batch=batch, original_name=name, status=status,
                                           file_extension=".jpg", mime_type="image/jpeg", width=320, height=240)
        if status == "uploaded":
            image.original_file.save(name, ContentFile(jpeg_bytes()))
        return image

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_only_administrators_may_use_it(self):
        for user in (self.alice, self.make_user("mod", role="moderator")):
            client = self.client_for(user)
            for path in ("/api/system/images/", f"/api/system/images/{self.alice_image.pk}/",
                         f"/api/system/images/{self.alice_image.pk}/preview/"):
                with self.subTest(user=user.username, path=path):
                    self.assertEqual(client.get(path).status_code, 403)
        self.assertEqual(APIClient().get("/api/system/images/").status_code, 401)

    def test_the_list_covers_every_user_and_is_always_paginated(self):
        body = self.client_for(self.admin).get("/api/system/images/").json()
        self.assertEqual(set(body), {"count", "next", "previous", "results"})
        self.assertEqual(body["count"], 3)
        by_id = {row["id"]: row for row in body["results"]}
        self.assertEqual(by_id[self.alice_image.pk]["owner"]["email"], "alice@example.com")
        self.assertEqual(by_id[self.alice_image.pk]["quality"]["sharpness_score"], 30.0)
        self.assertEqual(by_id[self.bob_image.pk]["organization_id"], self.organization.pk)
        self.assertEqual(self.client_for(self.admin).get("/api/system/images/?page_size=500").json()["count"], 3)

    def test_filters(self):
        client = self.client_for(self.admin)
        ids = lambda query: {row["id"] for row in client.get(f"/api/system/images/?{query}").json()["results"]}  # noqa: E731
        self.assertEqual(ids(f"owner={self.alice.pk}"), {self.alice_image.pk})
        self.assertEqual(ids("status=rejected"), {self.rejected.pk})
        self.assertEqual(ids(f"organization={self.organization.pk}"), {self.bob_image.pk})
        self.assertEqual(ids("quality_decision=needs_review"), {self.alice_image.pk})
        self.assertEqual(ids("q=holiday"), {self.alice_image.pk})
        self.assertEqual(ids("q=bob@example"), {self.bob_image.pk, self.rejected.pk})
        self.assertEqual(client.get("/api/system/images/?owner=alice").status_code, 400)
        self.assertEqual(client.get("/api/system/images/?created_after=yesterday").status_code, 400)

    def test_listing_is_not_audited_but_opening_an_image_is(self):
        client = self.client_for(self.admin)
        client.get("/api/system/images/")
        self.assertFalse(ActivityEvent.objects.filter(user=self.admin).exists())

        detail = client.get(f"/api/system/images/{self.alice_image.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["preprocessing"]["status"], "completed")
        event = ActivityEvent.objects.get(user=self.admin)
        self.assertEqual((event.category, event.event, event.object_id, event.details["owner_id"]),
                         ("admin", "admin_image_viewed", str(self.alice_image.pk), self.alice.pk))
        audit = client.get("/api/system/audit/").json()
        self.assertIn("admin_image_viewed", str(audit))

    def test_the_preview_is_a_thumbnail_not_the_original_and_is_audited(self):
        response = self.client_for(self.admin).get(f"/api/system/images/{self.alice_image.pk}/preview/")
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "image/jpeg"))
        self.assertEqual(response["Cache-Control"], "private, no-store")
        preview = Image.open(io.BytesIO(b"".join(response.streaming_content)))
        self.assertLessEqual(max(preview.size), 320)
        self.assertTrue(ActivityEvent.objects.filter(user=self.admin, event="admin_image_previewed").exists())

    def test_a_rejected_upload_has_no_preview(self):
        self.assertEqual(self.client_for(self.admin).get(f"/api/system/images/{self.rejected.pk}/preview/").status_code, 404)

    def test_personal_endpoints_stay_owner_scoped(self):
        bob = self.client_for(self.bob)
        self.assertEqual(bob.get(f"/api/images/{self.alice_image.pk}/").status_code, 404)
        self.assertNotIn(self.alice_image.pk, [row["id"] for row in bob.get("/api/images/my/").json()["results"]])


class AdminUserDetailTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="root", email="root@example.com", password="ForestRiver!4821",
                                              is_email_verified=True, role="admin")
        self.user = User.objects.create_user(username="member", email="member@example.com",
                                             password="ForestRiver!4821", is_email_verified=True, job_title="Editor")
        UploadBatch.objects.create(owner=self.user, upload_type="single")

    def test_an_administrator_sees_the_account_detail(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        body = client.get(f"/api/user/admin/users/{self.user.pk}/").json()
        self.assertEqual((body["email"], body["job_title"]), ("member@example.com", "Editor"))
        self.assertNotIn("two_factor_enabled", body)
        self.assertEqual(body["usage"], {"upload_batches": 1, "images": 0, "enhancements": 0})
        self.assertIn("organizations", body)

    def test_a_non_administrator_does_not(self):
        client = APIClient()
        client.force_authenticate(self.user)
        self.assertEqual(client.get(f"/api/user/admin/users/{self.admin.pk}/").status_code, 403)

    def test_patch_is_unchanged(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.patch(f"/api/user/admin/users/{self.user.pk}/", {"is_suspended": True,
                                                                            "suspension_reason": "test"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("usage", response.json())
