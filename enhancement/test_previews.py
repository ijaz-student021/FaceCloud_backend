"""Editor previews: bounded, authenticated, revision-specific, atomic, disposable.

Classical processing only (no model, no network); the queue is not involved.
"""
import io
import os
import shutil
import tempfile
import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

import cv2
import numpy as np
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import User
from uploads.models import UploadBatch

from .editor_source import source_version
from .models import EditPreview, EditPreviewSource, ImageEditVersion, ImageEnhancementResult
from .previews import cleanup_expired
from .services import process_enhancement_result
from .tasks import cleanup_edit_previews, process_gallery_edit
from .test_edit_lifecycle import NO_MODELS, EditLifecycleFixture
from .test_pipeline_regressions import ResultFixture

BODY = {"mode": "manual", "adjustments": {"brightness": 20, "contrast": 15, "saturation": 20, "sharpness": 30}}


class PreviewFixture(EditLifecycleFixture):
    def setUp(self):
        # Own storage and no real queue, whatever DEBUG or an earlier test left behind.
        self.media = tempfile.mkdtemp(prefix="previews-test-")
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media, CELERY_TASK_ALWAYS_EAGER=False)
        override.enable()
        self.addCleanup(override.disable)
        queued = patch("enhancement.views.process_gallery_edit.apply_async")
        self.queue = queued.start()
        self.addCleanup(queued.stop)
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.result = process_enhancement_result(self.make_result("big.jpg", size=(900, 1400)))
        self.url = f"/api/enhancements/results/{self.result.pk}/previews/"

    def preview(self, revision, body=None, **extra):
        return self.client.post(self.url, {"revision": revision, **(body or BODY), **extra}, format="json")

    def image_of(self, preview_id):
        response = self.client.get(f"/api/enhancements/previews/{preview_id}/image/")
        self.assertEqual(response.status_code, 200)
        return b"".join(response.streaming_content), response

    def other_client(self):
        other = APIClient()
        other.force_authenticate(User.objects.create_user(
            username="intruder", email="intruder@example.com", password="ForestRiver!4821", is_email_verified=True))
        return other

    def previews_folder_files(self):
        folder = f"users/{self.user.pk}/previews"
        try:
            return set(default_storage.listdir(folder)[1])
        except FileNotFoundError:
            return set()


class PreviewBasicsTests(PreviewFixture):
    def test_a_preview_is_bounded_authenticated_and_described(self):
        response = self.preview(1)
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(max(body["width"], body["height"]), settings.ENHANCEMENT_PREVIEW_DEFAULT_EDGE)
        self.assertEqual((body["revision"], body["is_latest"], body["latest_revision"]), (1, True, 1))
        self.assertEqual(body["source_version"], source_version(self.result))
        self.assertEqual(body["source"]["full_width"], 1400)
        self.assertEqual(body["render"]["renderer_version"], "local_tone_renderer_v2")
        self.assertEqual(body["render"]["outcome"], "rendered")
        data, response = self.image_of(body["id"])
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(data)).size, (body["width"], body["height"]))

    def test_the_dashboard_sliders_drive_a_preview_including_noise_reduction(self):
        """The five dashboard sliders (strength, brightness, contrast, skin detail, noise reduction), as the
        frontend maps them (docs/enhancement/dashboard-sliders.md), at their default positions."""
        dashboard = {"strength": 75, "brightness": 50, "contrast": 60, "detail": 45, "noise_reduction": 40}
        adjustments = {
            "intensity": dashboard["strength"],
            "brightness": (dashboard["brightness"] - 50) * 2,
            "contrast": (dashboard["contrast"] - 50) * 2,
            "sharpness": (dashboard["detail"] - 50) * 2,
            "denoise": dashboard["noise_reduction"],
        }
        response = self.preview(1, {"mode": "manual", "adjustments": adjustments})
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["render"]["outcome"], "rendered")
        self.assertEqual(body["applied_adjustments"]["denoise"], 40.0)
        self.assertEqual(body["applied_adjustments"]["contrast"], 20.0)
        self.assertEqual(body["applied_adjustments"]["sharpness"], -10.0)

        too_far = self.preview(2, {"mode": "manual", "adjustments": {"denoise": 101}})
        self.assertEqual(too_far.status_code, 400)

    def test_the_requested_size_is_honoured_and_bounded(self):
        self.assertEqual(max(self.preview(1, size=400).json()["width"], 0), 400)
        for size in (64, settings.ENHANCEMENT_PREVIEW_MAX_EDGE + 1):
            response = self.preview(2, size=size)
            self.assertEqual(response.status_code, 400)
            self.assertIn("size", response.json())

    def test_the_source_copy_is_bounded_lossless_and_made_once(self):
        self.preview(1)
        self.preview(2, {"mode": "manual", "adjustments": {"contrast": 5}})
        source = EditPreviewSource.objects.get()
        self.assertLessEqual(max(source.width, source.height), settings.ENHANCEMENT_PREVIEW_MAX_EDGE)
        self.assertTrue(source.file.name.endswith(".png"))

    def test_invalid_settings_and_revisions_are_field_errors(self):
        for body, field in (
            ({"revision": 0}, "revision"), ({"revision": "x"}, "revision"), ({}, "revision"),
            ({"revision": 1, "mode": "manual", "adjustments": {"brightness": 500}}, "adjustments"),
            ({"revision": 1, "mode": "manual", "adjustments": {"warmth": True}}, "adjustments"),
        ):
            with self.subTest(body=body):
                response = self.client.post(self.url, body, format="json")
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn(field, response.json())

    def test_a_neutral_preview_is_the_source_unchanged(self):
        body = self.preview(1, {"mode": "manual", "adjustments": {}}).json()
        self.assertEqual(body["render"]["outcome"], "no_change")
        data, _ = self.image_of(body["id"])
        shown = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        with self.result.enhanced_file.open("rb") as handle:
            full = cv2.imdecode(np.frombuffer(handle.read(), np.uint8), cv2.IMREAD_COLOR)
        expected = cv2.resize(full, (shown.shape[1], shown.shape[0]), interpolation=cv2.INTER_AREA)
        self.assertLess(float(np.abs(shown.astype(int) - expected.astype(int)).mean()), 2.0)  # JPEG at q88 only

    def test_only_the_owner_can_use_any_of_it(self):
        made = self.preview(1).json()
        stranger = self.other_client()
        for method, path in (("get", self.url), ("post", self.url), ("get", f"/api/enhancements/previews/{made['id']}/"),
                             ("get", f"/api/enhancements/previews/{made['id']}/image/"),
                             ("post", f"/api/enhancements/previews/{made['id']}/save/"),
                             ("delete", f"/api/enhancements/previews/{made['id']}/")):
            with self.subTest(path=path, method=method):
                kwargs = {"data": {"revision": 9, **BODY}, "format": "json"} if method == "post" and path == self.url else {}
                self.assertEqual(getattr(stranger, method)(path, **kwargs).status_code, 404)
        self.assertEqual(APIClient().get(self.url).status_code, 401)
        self.assertEqual(APIClient().get(f"/api/enhancements/previews/{made['id']}/image/").status_code, 401)

    def test_a_result_that_is_not_complete_has_no_previews(self):
        pending = self.make_result("pending.jpg")
        self.assertEqual(self.client.post(f"/api/enhancements/results/{pending.pk}/previews/",
                                          {"revision": 1, **BODY}, format="json").status_code, 404)


class RevisionTests(PreviewFixture):
    def test_a_late_answer_to_an_older_revision_never_overwrites_a_newer_preview(self):
        newest = self.preview(5).json()
        newest_bytes, _ = self.image_of(newest["id"])
        late = self.preview(3, {"mode": "manual", "adjustments": {"brightness": -30}})
        self.assertEqual(late.status_code, 201)
        self.assertEqual((late.json()["is_latest"], late.json()["latest_revision"]), (False, 5))
        self.assertNotEqual(late.json()["id"], newest["id"])
        self.assertEqual(self.image_of(newest["id"])[0], newest_bytes)  # untouched
        listing = self.client.get(self.url).json()
        self.assertEqual(listing["latest_revision"], 5)
        self.assertEqual([row["revision"] for row in listing["previews"]], [5, 3])
        self.assertEqual(self.client.get(f"/api/enhancements/previews/{newest['id']}/").json()["is_latest"], True)

    def test_repeating_a_revision_with_the_same_settings_returns_it_without_rendering_again(self):
        first = self.preview(1)
        with patch("enhancement.services.apply_gallery_adjustments") as render:
            again = self.preview(1)
        render.assert_not_called()
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.json()["id"], again.json()["id"])
        self.assertEqual(EditPreview.objects.count(), 1)

    def test_the_same_revision_with_different_settings_is_a_conflict(self):
        self.preview(1)
        response = self.preview(1, {"mode": "manual", "adjustments": {"brightness": -50}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "revision_conflict")
        self.assertEqual(EditPreview.objects.count(), 1)

    def test_a_request_for_another_source_version_is_refused_with_the_current_one(self):
        response = self.preview(1, source_version="0123456789abcdef")
        self.assertEqual(response.status_code, 409)
        self.assertEqual((response.json()["code"], response.json()["source_version"]),
                         ("stale_source", source_version(self.result)))
        self.assertFalse(EditPreview.objects.exists())
        self.assertEqual(self.preview(2, source_version=source_version(self.result)).status_code, 201)

    def test_only_the_newest_previews_are_kept_and_the_files_of_older_ones_are_removed(self):
        with override_settings(ENHANCEMENT_PREVIEW_KEEP_PER_RESULT=3):
            ids = [self.preview(n, {"mode": "manual", "adjustments": {"brightness": n}}).json()["id"] for n in range(1, 7)]
            kept = list(EditPreview.objects.order_by("revision").values_list("revision", flat=True))
            files_before = self.previews_folder_files()
        self.assertEqual(kept, [4, 5, 6])
        # 3 preview images + 1 cached source: nothing left behind for the 3 trimmed previews
        self.assertEqual(len(files_before), 4)
        self.assertEqual(self.client.get(f"/api/enhancements/previews/{ids[0]}/").status_code, 404)

    def test_a_failed_save_leaves_no_file_behind(self):
        self.preview(1)
        before = self.previews_folder_files()
        with patch.object(EditPreview, "save", side_effect=RuntimeError("database failed")):
            with self.assertRaises(RuntimeError):
                self.client.raise_request_exception = True
                self.preview(2, {"mode": "manual", "adjustments": {"brightness": 7}})
        self.assertEqual(self.previews_folder_files(), before)
        self.assertEqual(EditPreview.objects.count(), 1)

    def test_deleting_a_preview_removes_its_file(self):
        made = self.preview(1).json()
        name = EditPreview.objects.get(pk=made["id"]).file.name
        self.assertEqual(self.client.delete(f"/api/enhancements/previews/{made['id']}/").status_code, 204)
        self.assertFalse(default_storage.exists(name))


class SaveTests(PreviewFixture):
    def test_a_saved_edit_looks_like_its_preview_at_full_resolution(self):
        """The preview is a faithful small version of the full-resolution save, for every control."""
        made = self.preview(1).json()
        saved = self.client.post(f"/api/enhancements/previews/{made['id']}/save/")
        self.assertEqual(saved.status_code, 202, saved.content)
        self.assertEqual((saved.json()["from_preview"], saved.json()["source_version"]),
                         (made["id"], made["source_version"]))
        self.assertEqual(process_gallery_edit.run(saved.json()["id"])["status"], "completed")
        edit = ImageEditVersion.objects.get(pk=saved.json()["id"])
        self.assertEqual((edit.output_width, edit.output_height), (self.result.output_width, self.result.output_height))
        self.assertEqual(edit.applied_adjustments, made["applied_adjustments"])
        with edit.edited_file.open("rb") as handle:
            full = cv2.imdecode(np.frombuffer(handle.read(), np.uint8), cv2.IMREAD_COLOR)
        data, _ = self.image_of(made["id"])
        shown = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        shrunk = cv2.resize(full, (shown.shape[1], shown.shape[0]), interpolation=cv2.INTER_AREA)
        difference = float(np.abs(shrunk.astype(int) - shown.astype(int)).mean())
        self.assertLess(difference, 2.5, f"preview and full-resolution save differ by {difference:.2f} levels")

    def test_saving_a_preview_whose_image_has_changed_is_refused(self):
        made = self.preview(1).json()
        ImageEnhancementResult.objects.filter(pk=self.result.pk).update(output_file_size=self.result.output_file_size + 1)
        response = self.client.post(f"/api/enhancements/previews/{made['id']}/save/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "stale_source")
        self.assertFalse(ImageEditVersion.objects.exists())

    def test_a_direct_save_may_name_its_source_version(self):
        url = f"/api/enhancements/results/{self.result.pk}/edits/"
        stale = self.client.post(url, {"mode": "manual", "adjustments": {"contrast": 5}, "source_version": "bad"}, format="json")
        self.assertEqual(stale.status_code, 409)
        good = self.client.post(url, {"mode": "manual", "adjustments": {"contrast": 5},
                                      "source_version": source_version(self.result)}, format="json")
        self.assertEqual(good.status_code, 202)

    def test_only_one_save_may_be_in_progress_per_image(self):
        made = self.preview(1).json()
        first = self.client.post(f"/api/enhancements/previews/{made['id']}/save/")
        again = self.client.post(f"/api/enhancements/previews/{made['id']}/save/")
        self.assertEqual((first.status_code, again.status_code), (202, 400))

    def test_the_saved_download_is_protected(self):
        made = self.preview(1).json()
        edit_id = self.client.post(f"/api/enhancements/previews/{made['id']}/save/").json()["id"]
        process_gallery_edit.run(edit_id)
        self.assertEqual(self.client.get(f"/api/enhancements/edits/{edit_id}/download/").status_code, 200)
        self.assertEqual(self.other_client().get(f"/api/enhancements/edits/{edit_id}/download/").status_code, 404)
        self.assertEqual(APIClient().get(f"/api/enhancements/edits/{edit_id}/download/").status_code, 401)

    def test_a_saved_version_survives_the_cleanup_of_its_preview(self):
        made = self.preview(1).json()
        edit_id = self.client.post(f"/api/enhancements/previews/{made['id']}/save/").json()["id"]
        process_gallery_edit.run(edit_id)
        EditPreview.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        cleanup_edit_previews()
        edit = ImageEditVersion.objects.get(pk=edit_id)
        self.assertIsNone(edit.from_preview)
        self.assertTrue(edit.edited_file and default_storage.exists(edit.edited_file.name))


class TransparencyTests(PreviewFixture):
    def test_a_png_preview_keeps_the_alpha_channel(self):
        result, alpha = self.make_png_result()
        url = f"/api/enhancements/results/{result.pk}/previews/"
        made = self.client.post(url, {"revision": 1, **BODY, "size": 256}, format="json")
        self.assertEqual(made.status_code, 201, made.content)
        self.assertEqual(made.json()["output_format"], "png")
        data, response = self.image_of(made.json()["id"])
        self.assertEqual(response["Content-Type"], "image/png")
        decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        self.assertEqual(decoded.shape[2], 4)
        expected = cv2.resize(alpha, (decoded.shape[1], decoded.shape[0]), interpolation=cv2.INTER_AREA)
        np.testing.assert_array_equal(decoded[:, :, 3], expected)

    def test_the_editor_states_its_transparency_policy(self):
        body = self.client.get("/api/enhancements/editor/configuration/").json()
        self.assertIn("alpha preserved", body["transparency"]["png"])
        self.assertIn("composited", body["transparency"]["jpeg"])
        self.assertEqual(body["preview"]["max_edge"], settings.ENHANCEMENT_PREVIEW_MAX_EDGE)
        self.assertEqual(body["preview_strategy"], "server_preview_revisions_then_full_resolution_save")


class RetentionTests(PreviewFixture):
    def test_expired_previews_and_unused_sources_are_deleted_but_nothing_else(self):
        made = self.preview(1).json()
        name = EditPreview.objects.get(pk=made["id"]).file.name
        original = self.result.image.original_file.name
        enhanced = self.result.enhanced_file.name
        edit = process_gallery_edit  # a saved version, to prove it is left alone
        saved = self.client.post(f"/api/enhancements/previews/{made['id']}/save/").json()["id"]
        edit.run(saved)
        EditPreview.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        EditPreviewSource.objects.update(created_at=timezone.now() - timedelta(hours=settings.ENHANCEMENT_PREVIEW_RETENTION_HOURS + 1))

        counts = cleanup_expired()

        self.assertEqual(counts, {"previews": 1, "sources": 1})
        self.assertFalse(default_storage.exists(name))
        self.assertEqual(self.previews_folder_files(), set())
        self.assertTrue(default_storage.exists(original) and default_storage.exists(enhanced))
        self.assertTrue(ImageEditVersion.objects.get(pk=saved).edited_file)

    def test_a_preview_that_has_not_expired_is_kept(self):
        self.preview(1)
        self.assertEqual(cleanup_expired(), {"previews": 0, "sources": 0})

    def test_the_cleanup_runs_on_its_own_schedule(self):
        entry = settings.CELERY_BEAT_SCHEDULE["cleanup-edit-previews"]
        self.assertEqual(entry["task"], "enhancement.cleanup_previews")

    def test_preview_files_are_registered_with_the_orphan_scanner(self):
        from privacy.cleanup import MEDIA_SUBDIRECTORIES

        self.assertIn("previews", MEDIA_SUBDIRECTORIES)


@unittest.skipUnless(connection.vendor == "postgresql", "row-lock ordering needs PostgreSQL (run with DB_ENGINE=postgresql)")
@override_settings(**NO_MODELS)
class ConcurrentPreviewTests(TransactionTestCase):
    """Real concurrent requests on PostgreSQL: the lock on the result serialises writers of one image's previews."""

    def setUp(self):
        self.media = tempfile.mkdtemp(prefix="previews-concurrent-")
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user(
            username="concurrent", email="concurrent@example.com", password="ForestRiver!4821", is_email_verified=True)
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="multiple")
        self.result = process_enhancement_result(ResultFixture.make_result(self, "big.jpg", size=(600, 900)))

    def fire(self, bodies):
        results, errors = [], []
        barrier = threading.Barrier(len(bodies))

        def work(body):
            try:
                client = APIClient()
                client.force_authenticate(self.user)
                barrier.wait(timeout=30)
                response = client.post(f"/api/enhancements/results/{self.result.pk}/previews/", body, format="json")
                results.append((body, response.status_code, response.json()))
            except Exception as error:  # reported by the assertions below
                errors.append(repr(error))
            finally:
                connection.close()

        threads = [threading.Thread(target=work, args=(body,)) for body in bodies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(errors, [])
        return results

    def test_the_same_revision_sent_many_times_at_once_makes_one_preview(self):
        body = {"revision": 1, **BODY, "size": 256}
        results = self.fire([body] * 6)
        self.assertTrue(all(code in (200, 201) for _, code, _ in results), results)
        self.assertEqual(EditPreview.objects.count(), 1)
        self.assertEqual(len({payload["id"] for _, _, payload in results}), 1)
        self.assertEqual(len(os.listdir(os.path.join(self.media, f"users/{self.user.pk}/previews"))), 2)  # preview + source

    def test_one_revision_with_conflicting_settings_has_exactly_one_winner(self):
        bodies = [{"revision": 1, "mode": "manual", "adjustments": {"brightness": n * 10}, "size": 256} for n in range(1, 6)]
        results = self.fire(bodies)
        codes = sorted(code for _, code, _ in results)
        self.assertEqual(codes, [201, 409, 409, 409, 409], results)
        self.assertEqual(EditPreview.objects.count(), 1)

    def test_many_revisions_at_once_are_all_kept_in_order_and_exactly_one_is_latest(self):
        bodies = [{"revision": n, "mode": "manual", "adjustments": {"contrast": n}, "size": 256} for n in range(1, 9)]
        results = self.fire(bodies)
        self.assertTrue(all(code == 201 for _, code, _ in results), results)
        self.assertEqual(list(EditPreview.objects.order_by("revision").values_list("revision", flat=True)), list(range(1, 9)))
        client = APIClient()
        client.force_authenticate(self.user)
        listing = client.get(f"/api/enhancements/results/{self.result.pk}/previews/").json()
        self.assertEqual(listing["latest_revision"], 8)
        self.assertEqual(sum(row["is_latest"] for row in listing["previews"]), 1)
        self.assertEqual(len(os.listdir(os.path.join(self.media, f"users/{self.user.pk}/previews"))), 9)  # 8 + source
