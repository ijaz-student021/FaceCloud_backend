import hashlib
import io
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image, ImageChops
from rest_framework.test import APIClient

from activity.models import ActivityEvent
from enhancement.models import ImageEditVersion, ImageEnhancementResult
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import ShareLink, ShareLinkAccess
from .services import cleanup_expired_links, hash_token

User = get_user_model()


def png_bytes(colour, size=(640, 480)):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


class ShareFixtureMixin:
    def make_user(self, name, **extra):
        return User.objects.create_user(
            username=name, email=f"{name}@example.com", password="ForestRiver!4821",
            is_email_verified=True, **extra,
        )

    def make_result(self, owner, colour=(90, 120, 150), status="completed"):
        batch = UploadBatch.objects.create(owner=owner, upload_type="single")
        image = ImageUpload.objects.create(
            owner=owner, batch=batch, original_name="a.png", status="uploaded",
        )
        pre = ImagePreprocessingResult.objects.create(
            owner=owner, image=image, status="completed", output_format="png",
        )
        payload = png_bytes(colour)
        result = ImageEnhancementResult.objects.create(
            owner=owner, image=image, preprocessing_result=pre, status=status,
            output_name="a_enhanced.png", output_format="png", output_file_size=len(payload),
            output_width=640, output_height=480,
        )
        if status == "completed":
            result.enhanced_file.save("a.png", ContentFile(payload), save=True)
        return result, payload

    def make_edit(self, result, colour=(200, 50, 50), status="completed"):
        payload = png_bytes(colour)
        edit = ImageEditVersion.objects.create(
            owner=result.owner, enhancement_result=result, mode="manual", status=status,
            output_name="a_edit.png", output_format="png", output_file_size=len(payload),
            output_width=640, output_height=480,
        )
        edit.edited_file.save("e.png", ContentFile(payload), save=True)
        return edit, payload

    def body(self, response):
        if response.streaming:
            return b"".join(response.streaming_content)
        return response.content


class ShareLinkOwnerApiTests(ShareFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("share-owner")
        self.other = self.make_user("share-other")
        self.result, self.payload = self.make_result(self.owner)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.list_url = reverse("sharing:link-list-create")

    def create(self, **extra):
        return self.client.post(self.list_url, {"result": self.result.pk, **extra}, format="json")

    def test_creating_a_link_returns_the_token_once_and_stores_only_its_hash(self):
        response = self.create(label="For Sara")

        self.assertEqual(response.status_code, 201)
        token = response.data["token"]
        self.assertGreaterEqual(len(token), 40)
        self.assertIn(token, response.data["public_url"])
        link = ShareLink.objects.get()
        self.assertEqual(link.token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, str(link.__dict__))
        self.assertEqual(response.data["state"], "active")
        self.assertEqual(response.data["label"], "For Sara")

    def test_the_token_is_never_shown_again(self):
        token = self.create().data["token"]
        link = ShareLink.objects.get()

        listing = self.client.get(self.list_url)
        detail = self.client.get(reverse("sharing:link-detail", kwargs={"pk": link.pk}))

        self.assertNotIn(token, str(listing.data))
        self.assertNotIn(token, str(detail.data))
        self.assertNotIn("token", listing.data[0])

    def test_default_lifetime_is_24_hours(self):
        self.create()
        remaining = ShareLink.objects.get().expires_at - timezone.now()
        self.assertAlmostEqual(remaining.total_seconds(), 24 * 3600, delta=60)

    @override_settings(SHARE_LINK_MAX_TTL_HOURS=48)
    def test_lifetime_can_be_chosen_up_to_the_maximum(self):
        self.assertEqual(self.create(ttl_hours=2).status_code, 201)
        too_long = self.create(ttl_hours=49)
        self.assertEqual(too_long.status_code, 400)
        self.assertIn("ttl_hours", too_long.data)
        self.assertEqual(self.create(ttl_hours=0).status_code, 400)

    def test_only_your_own_completed_results_can_be_shared(self):
        stranger, _ = self.make_result(self.other)
        unfinished, _ = self.make_result(self.owner, status="failed")
        for pk in (stranger.pk, unfinished.pk, 999999):
            with self.subTest(result=pk):
                response = self.client.post(self.list_url, {"result": pk}, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn("result", response.data)
        self.assertEqual(ShareLink.objects.count(), 0)

    def test_an_edit_must_belong_to_the_result(self):
        edit, _ = self.make_edit(self.result)
        foreign_result, _ = self.make_result(self.owner)
        foreign_edit, _ = self.make_edit(foreign_result)

        self.assertEqual(self.create(edit=edit.pk).status_code, 201)
        response = self.create(edit=foreign_edit.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("edit", response.data)

    def test_watermark_options_are_validated_and_saved(self):
        bad = self.create(watermark="custom")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("watermark_text", bad.data)

        good = self.create(watermark="username", watermark_position="tile")
        self.assertEqual(good.status_code, 201)
        self.assertEqual(good.data["watermark"]["mode"], "username")
        self.assertEqual(good.data["watermark"]["position"], "tile")

    def test_login_is_required_and_lists_are_private(self):
        self.create()
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.list_url).status_code, 401)
        self.assertEqual(self.client.post(self.list_url, {"result": self.result.pk}, format="json").status_code, 401)

        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.list_url).data, [])
        link = ShareLink.objects.get()
        self.assertEqual(self.client.get(reverse("sharing:link-detail", kwargs={"pk": link.pk})).status_code, 404)
        self.assertEqual(self.client.post(reverse("sharing:link-revoke", kwargs={"pk": link.pk})).status_code, 404)

    @override_settings(SHARE_LINK_MAX_ACTIVE_PER_USER=2)
    def test_active_links_are_capped_and_revoking_frees_a_slot(self):
        first = self.create().data["id"]
        self.create()
        blocked = self.create()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("2 active share links", blocked.data["detail"])

        self.client.post(reverse("sharing:link-revoke", kwargs={"pk": first}))
        self.assertEqual(self.create().status_code, 201)

    def test_revoking_is_idempotent_and_recorded(self):
        link_id = self.create().data["id"]
        url = reverse("sharing:link-revoke", kwargs={"pk": link_id})

        first = self.client.post(url)
        stamp = ShareLink.objects.get().revoked_at
        second = self.client.post(url)

        self.assertEqual(first.data["state"], "revoked")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(ShareLink.objects.get().revoked_at, stamp)
        events = list(ActivityEvent.objects.filter(user=self.owner, category="share").values_list("event", flat=True))
        self.assertEqual(sorted(events), ["share_link_created", "share_link_revoked"])

    def test_activity_events_never_contain_the_token(self):
        token = self.create().data["token"]
        for event in ActivityEvent.objects.all():
            self.assertNotIn(token, str(event.details))
            self.assertNotIn(token, event.object_id)


class PublicShareTests(ShareFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("public-owner")
        self.result, self.payload = self.make_result(self.owner)
        owner_client = APIClient()
        owner_client.force_authenticate(self.owner)
        created = owner_client.post(
            reverse("sharing:link-list-create"), {"result": self.result.pk}, format="json",
        )
        self.token = created.data["token"]
        self.link = ShareLink.objects.get()
        self.url = reverse("sharing:public-view", kwargs={"token": self.token})
        self.anonymous = APIClient()

    def test_anyone_with_the_token_sees_the_image_without_logging_in(self):
        response = self.anonymous.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.body(response), self.payload)
        self.assertEqual(response["Content-Type"], "image/png")

    def test_response_is_inline_uncacheable_and_locked_down(self):
        response = self.anonymous.get(self.url)

        self.assertTrue(response["Content-Disposition"].startswith("inline"))
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertIn("noindex", response["X-Robots-Tag"])

    def test_a_stale_login_header_does_not_break_a_public_link(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer expired-or-garbage")
        self.assertEqual(client.get(self.url).status_code, 200)

    def test_unknown_tokens_are_404(self):
        response = self.anonymous.get(reverse("sharing:public-view", kwargs={"token": "nope" * 10}))
        self.assertEqual(response.status_code, 404)

    def test_expired_links_are_410(self):
        ShareLink.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.anonymous.get(self.url).status_code, 410)

    def test_revoked_links_stop_working_immediately(self):
        self.assertEqual(self.anonymous.get(self.url).status_code, 200)
        ShareLink.objects.update(revoked_at=timezone.now())
        self.assertEqual(self.anonymous.get(self.url).status_code, 410)

    def test_links_die_with_a_suspended_or_deactivated_owner(self):
        User.objects.filter(pk=self.owner.pk).update(is_suspended=True)
        self.assertEqual(self.anonymous.get(self.url).status_code, 410)
        User.objects.filter(pk=self.owner.pk).update(is_suspended=False, is_active=False)
        self.assertEqual(self.anonymous.get(self.url).status_code, 410)

    def test_links_die_with_the_result_or_the_account(self):
        self.result.delete()
        self.assertEqual(ShareLink.objects.count(), 0)
        self.assertEqual(self.anonymous.get(self.url).status_code, 404)

    def test_each_view_is_counted_without_storing_who_viewed(self):
        self.anonymous.get(self.url, HTTP_USER_AGENT="Mozilla/5.0 " + "x" * 400)
        self.anonymous.get(self.url)

        self.link.refresh_from_db()
        self.assertEqual(self.link.access_count, 2)
        self.assertIsNotNone(self.link.last_accessed_at)
        newest = ShareLinkAccess.objects.order_by("accessed_at").first()
        self.assertLessEqual(len(newest.user_agent), 200)
        stored_fields = {field.name for field in ShareLinkAccess._meta.fields}
        self.assertEqual(stored_fields, {"id", "link", "accessed_at", "user_agent"})

    def test_failed_lookups_are_not_counted(self):
        ShareLink.objects.update(revoked_at=timezone.now())
        self.anonymous.get(self.url)
        self.assertEqual(ShareLinkAccess.objects.count(), 0)

    def test_owner_sees_recent_accesses_in_the_detail(self):
        self.anonymous.get(self.url)
        owner_client = APIClient()
        owner_client.force_authenticate(self.owner)

        detail = owner_client.get(reverse("sharing:link-detail", kwargs={"pk": self.link.pk}))

        self.assertEqual(detail.data["access_count"], 1)
        self.assertEqual(len(detail.data["recent_accesses"]), 1)

    def test_info_endpoint_describes_the_image_without_counting_a_view(self):
        response = self.anonymous.get(reverse("sharing:public-info", kwargs={"token": self.token}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["width"], 640)
        self.assertEqual(response.data["content_type"], "image/png")
        self.assertFalse(response.data["watermarked"])
        self.assertNotIn("owner", response.data)
        self.link.refresh_from_db()
        self.assertEqual(self.link.access_count, 0)

    def test_info_endpoint_enforces_expiry_too(self):
        ShareLink.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.anonymous.get(reverse("sharing:public-info", kwargs={"token": self.token}))
        self.assertEqual(response.status_code, 410)

    def test_a_link_to_an_edit_shows_the_edit_not_the_original_result(self):
        edit, edit_payload = self.make_edit(self.result)
        ShareLink.objects.update(edit_version=edit)

        self.assertEqual(self.body(self.anonymous.get(self.url)), edit_payload)

    def test_watermarked_links_serve_a_marked_copy_and_leave_the_file_alone(self):
        ShareLink.objects.update(watermark={
            "mode": "facecloud", "position": "bottom_right", "opacity": 0.8, "text": "",
        })

        response = self.anonymous.get(self.url)

        stamped = Image.open(io.BytesIO(self.body(response))).convert("RGB")
        original = Image.open(io.BytesIO(self.payload)).convert("RGB")
        self.assertIsNotNone(ImageChops.difference(original, stamped).getbbox())
        self.result.refresh_from_db()
        self.result.enhanced_file.open("rb")
        try:
            self.assertEqual(self.result.enhanced_file.read(), self.payload)
        finally:
            self.result.enhanced_file.close()
        info = self.anonymous.get(reverse("sharing:public-info", kwargs={"token": self.token}))
        self.assertTrue(info.data["watermarked"])


class ShareCleanupTests(ShareFixtureMixin, TestCase):
    @override_settings(SHARE_LINK_RETENTION_DAYS=30)
    def test_only_links_that_ended_long_ago_are_deleted(self):
        owner = self.make_user("cleanup-owner")
        result, _ = self.make_result(owner)
        now = timezone.now()

        def link(name, **fields):
            return ShareLink.objects.create(
                owner=owner, enhancement_result=result, token_hash=hash_token(name),
                expires_at=fields.pop("expires_at", now + timedelta(days=1)), **fields,
            )

        active = link("active")
        recently_expired = link("recent", expires_at=now - timedelta(days=5))
        long_expired = link("old", expires_at=now - timedelta(days=45))
        long_revoked = link("revoked", revoked_at=now - timedelta(days=40))
        ShareLinkAccess.objects.create(link=long_expired)

        summary = cleanup_expired_links()

        self.assertEqual(summary, {"deleted": 2})
        self.assertEqual(
            set(ShareLink.objects.values_list("pk", flat=True)),
            {active.pk, recently_expired.pk},
        )
        self.assertEqual(ShareLinkAccess.objects.count(), 0)
        self.assertNotIn(long_revoked.pk, ShareLink.objects.values_list("pk", flat=True))
