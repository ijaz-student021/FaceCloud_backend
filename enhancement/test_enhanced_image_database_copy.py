"""The enhanced image as a database row (ENHANCED_IMAGE_DATABASE_COPY_ENABLED) and its preview on the Django admin result page (ADMIN_IMAGE_PREVIEW_ENABLED).

The copy must be exactly the stored file, must never exist while stored images are encrypted, and must never put an enhancement at risk. The admin page
must show the picture only to staff with the view permission, only while its switch is on, and say where the picture came from.
"""
import hashlib
import io
import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from . import test_pipeline_regressions as regressions
from .database_copy import content_type_of, copy_after_completion, read_stored, store_copy, verify_copy
from .models import EnhancedImageBlob, EnhancementLevel, ImageEnhancementResult
from .services import process_enhancement_result

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix="facecloud-database-copy-")
KEY = "an-encryption-key-of-more-than-32-characters"
ON = dict(ENHANCED_IMAGE_DATABASE_COPY_ENABLED=True)
ENCRYPTED = dict(IMAGE_ENCRYPTION_ENABLED=True, IMAGE_ENCRYPTION_KEY=KEY)
# The project's test runner switches image encryption on for every test (config/test_runner.py): a test about plain files says so.
PLAIN = dict(MEDIA_ROOT=MEDIA, THUMBNAIL_PREGENERATE=False, IMAGE_ENCRYPTION_ENABLED=False)


def jpeg(size=(64, 48), colour=(90, 140, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "JPEG")
    return buffer.getvalue()


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Media(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_user(username="owner", email="owner@example.com", password="ForestRiver!4821", is_email_verified=True)

    def make_result(self, name="photo.jpg", data=None, status="completed", with_file=True, recorded_size=True):
        data = data or jpeg()
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple")
        original = ImageUpload.objects.create(
            owner=self.owner, batch=batch, original_name=name, file_extension=".jpg", mime_type="image/jpeg",
            file_size=len(data), width=64, height=48, status="uploaded",
        )
        original.original_file.save(name, ContentFile(jpeg(colour=(10, 20, 30))))
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=original, status="completed", output_format="jpeg", output_name=name,
            output_width=64, output_height=48, processed_file_size=len(data),
        )
        result = ImageEnhancementResult.objects.create(
            owner=self.owner, image=original, preprocessing_result=preprocessing, status=status, requested_level=EnhancementLevel.LOW,
        )
        if with_file:
            result.enhanced_file.save("enhanced.jpg", ContentFile(data), save=False)
            result.output_name, result.output_format, result.output_width, result.output_height = "enhanced.jpg", "jpeg", 64, 48
            result.output_file_size = len(data) if recorded_size else 0
            result.save()
        return result


@override_settings(MEDIA_ROOT=MEDIA, THUMBNAIL_PREGENERATE=False)
class DefaultsTests(SimpleTestCase):
    def test_both_switches_are_off_unless_asked_for(self):
        self.assertIs(settings.ENHANCED_IMAGE_DATABASE_COPY_ENABLED, False)
        self.assertIs(settings.ADMIN_IMAGE_PREVIEW_ENABLED, False)

    def test_the_content_type_is_an_image_type_or_nothing_the_browser_will_draw(self):
        self.assertEqual(content_type_of("users/1/enhanced/a.jpg"), "image/jpeg")
        self.assertEqual(content_type_of("users/1/enhanced/a.png"), "image/png")
        self.assertEqual(content_type_of("users/1/enhanced/a.html"), "application/octet-stream")
        self.assertEqual(content_type_of("users/1/enhanced/a"), "application/octet-stream")


@override_settings(**PLAIN)
class StoreCopyTests(Media):
    def test_the_copy_is_exactly_the_stored_file(self):
        data = jpeg(colour=(200, 30, 60))
        result = self.make_result(data=data)
        outcome, copy = store_copy(result)
        self.assertEqual(outcome, "stored")
        row = EnhancedImageBlob.objects.get(result=result)
        self.assertEqual(bytes(row.data), data)
        self.assertEqual(bytes(row.data), read_stored(result.enhanced_file))
        self.assertEqual((row.byte_size, row.sha256, row.content_type), (len(data), sha(data), "image/jpeg"))
        self.assertEqual(row.source_name, result.enhanced_file.name)
        self.assertEqual(copy.pk, row.pk)

    def test_a_second_run_changes_nothing(self):
        result = self.make_result()
        store_copy(result)
        outcome, _copy = store_copy(result)
        self.assertEqual(outcome, "unchanged")
        self.assertEqual(EnhancedImageBlob.objects.filter(result=result).count(), 1)

    def test_a_different_stored_file_replaces_the_copy_instead_of_adding_one(self):
        result = self.make_result()
        store_copy(result)
        newer = jpeg(colour=(1, 2, 3))
        result.enhanced_file.save("again.jpg", ContentFile(newer), save=False)
        result.output_file_size = len(newer)
        result.save()
        outcome, _copy = store_copy(result)
        self.assertEqual(outcome, "stored")
        row = EnhancedImageBlob.objects.get(result=result)
        self.assertEqual(bytes(row.data), newer)

    def test_a_result_without_a_file_has_nothing_to_copy(self):
        result = self.make_result(status="failed", with_file=False)
        self.assertEqual(store_copy(result), ("no_file", None))
        self.assertFalse(EnhancedImageBlob.objects.exists())

    def test_a_file_that_is_not_the_recorded_size_is_not_copied(self):
        result = self.make_result()
        ImageEnhancementResult.objects.filter(pk=result.pk).update(output_file_size=result.output_file_size + 1)
        result.refresh_from_db()
        self.assertEqual(store_copy(result), ("size_differs", None))
        self.assertFalse(EnhancedImageBlob.objects.exists())

    def test_an_unrecorded_size_does_not_stop_the_copy(self):
        result = self.make_result(recorded_size=False)
        self.assertEqual(store_copy(result)[0], "stored")

    def test_a_file_that_is_gone_is_reported_not_raised(self):
        result = self.make_result()
        result.enhanced_file.storage.delete(result.enhanced_file.name)
        self.assertEqual(store_copy(result), ("file_missing", None))

    def test_a_file_that_vanishes_between_the_checks_and_the_read_is_reported(self):
        result = self.make_result()
        with patch("enhancement.database_copy.read_stored", side_effect=FileNotFoundError):
            self.assertEqual(store_copy(result), ("file_missing", None))
        self.assertFalse(EnhancedImageBlob.objects.exists())

    def test_a_dry_run_reads_and_writes_nothing(self):
        result = self.make_result()
        with patch("enhancement.database_copy.read_stored", side_effect=AssertionError("must not read")):
            self.assertEqual(store_copy(result, write=False), ("would_store", None))
        self.assertFalse(EnhancedImageBlob.objects.exists())

    def test_deleting_the_result_deletes_its_copy(self):
        result = self.make_result()
        store_copy(result)
        result.delete()
        self.assertFalse(EnhancedImageBlob.objects.exists())


@override_settings(MEDIA_ROOT=MEDIA, THUMBNAIL_PREGENERATE=False)
class EncryptionRuleTests(Media):
    def test_nothing_is_copied_while_stored_images_are_encrypted(self):
        with override_settings(**ENCRYPTED):
            result = self.make_result()
            self.assertEqual(store_copy(result), ("encrypted", None))
        self.assertFalse(EnhancedImageBlob.objects.exists())

    def test_nothing_is_copied_from_a_file_stored_encrypted_even_after_the_setting_is_switched_off(self):
        with override_settings(**ENCRYPTED):
            result = self.make_result()
        with override_settings(IMAGE_ENCRYPTION_ENABLED=False):
            self.assertTrue(result.enhanced_file.storage.is_encrypted(result.enhanced_file.name))
            self.assertEqual(store_copy(result), ("encrypted", None))
        self.assertFalse(EnhancedImageBlob.objects.exists())

    @override_settings(IMAGE_ENCRYPTION_ENABLED=False)
    def test_a_plain_file_written_before_encryption_was_switched_on_is_not_copied_once_it_is_on(self):
        result = self.make_result()
        with override_settings(**ENCRYPTED):
            self.assertFalse(result.enhanced_file.storage.is_encrypted(result.enhanced_file.name))
            self.assertEqual(store_copy(result), ("encrypted", None))
        self.assertFalse(EnhancedImageBlob.objects.exists())

    @override_settings(IMAGE_ENCRYPTION_ENABLED=False)
    def test_a_plain_file_is_still_copied_when_encryption_is_off(self):
        result = self.make_result()
        self.assertFalse(result.enhanced_file.storage.is_encrypted(result.enhanced_file.name))
        self.assertEqual(store_copy(result)[0], "stored")

    @override_settings(**ON)
    def test_the_finished_enhancement_hook_logs_that_it_did_not_copy_and_does_not_raise(self):
        with override_settings(**ENCRYPTED):
            result = self.make_result()
            with self.assertLogs("enhancement.database_copy", "INFO") as logged:
                copy_after_completion(result)
        self.assertIn("encrypted", "\n".join(logged.output))
        self.assertFalse(EnhancedImageBlob.objects.exists())


@override_settings(**PLAIN)
class HookTests(Media):
    def test_with_the_switch_off_nothing_is_copied(self):
        result = self.make_result()
        copy_after_completion(result)
        self.assertFalse(EnhancedImageBlob.objects.exists())

    @override_settings(**ON)
    def test_with_the_switch_on_the_copy_is_made(self):
        result = self.make_result()
        copy_after_completion(result)
        self.assertEqual(EnhancedImageBlob.objects.get(result=result).sha256, sha(read_stored(result.enhanced_file)))

    @override_settings(**ON)
    def test_a_failing_copy_never_raises_and_is_logged_with_the_result_id(self):
        result = self.make_result()
        with patch("enhancement.database_copy.store_copy", side_effect=RuntimeError("disk on fire")):
            with self.assertLogs("enhancement.database_copy", "ERROR") as logged:
                copy_after_completion(result)
        self.assertIn(f"result {result.pk}", "\n".join(logged.output))

    @override_settings(**ON)
    def test_a_database_error_while_copying_leaves_the_callers_transaction_usable(self):
        result = self.make_result()
        EnhancedImageBlob.objects.create(result=result, content_type="image/jpeg", byte_size=1, sha256="0" * 64, source_name="other.jpg", data=b"x")

        def duplicate(**kwargs):
            return EnhancedImageBlob.objects.create(  # the database refuses a second row for the result
                result=result, content_type="image/jpeg", byte_size=1, sha256="1" * 64, source_name="again.jpg", data=b"y",
            )

        with patch.object(EnhancedImageBlob.objects, "update_or_create", side_effect=duplicate):
            with self.assertLogs("enhancement.database_copy", "ERROR"):
                copy_after_completion(result)
        self.assertEqual(ImageEnhancementResult.objects.filter(pk=result.pk).count(), 1)  # a poisoned transaction would raise here on PostgreSQL

    @override_settings(**ON)
    def test_the_size_mismatch_is_a_warning_not_an_error(self):
        result = self.make_result()
        ImageEnhancementResult.objects.filter(pk=result.pk).update(output_file_size=5)
        result.refresh_from_db()
        with self.assertLogs("enhancement.database_copy", "WARNING"):
            copy_after_completion(result)
        self.assertFalse(EnhancedImageBlob.objects.exists())


@override_settings(**PLAIN, **regressions.NO_MODELS)
class PublishedEnhancementTests(regressions.ResultFixture):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    @override_settings(**ON)
    def test_a_real_enhancement_leaves_a_copy_of_the_file_it_published(self):
        result = process_enhancement_result(self.make_result())
        self.assertEqual(result.status, "completed")
        row = EnhancedImageBlob.objects.get(result=result)
        stored = read_stored(result.enhanced_file)
        self.assertEqual(bytes(row.data), stored)
        self.assertEqual((row.byte_size, row.sha256), (result.output_file_size, sha(stored)))
        self.assertEqual(verify_copy(result), "ok")

    def test_a_real_enhancement_leaves_no_copy_by_default(self):
        result = process_enhancement_result(self.make_result())
        self.assertEqual(result.status, "completed")
        self.assertFalse(EnhancedImageBlob.objects.exists())

    @override_settings(**ON)
    def test_an_enhancement_still_completes_when_its_copy_fails(self):
        with patch("enhancement.database_copy.store_copy", side_effect=RuntimeError("disk on fire")):
            with self.assertLogs("enhancement.database_copy", "ERROR"):
                result = process_enhancement_result(self.make_result())
        result.refresh_from_db()
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.enhanced_file)


@override_settings(**PLAIN)
class VerifyTests(Media):
    def copied(self, **kwargs):
        result = self.make_result(**kwargs)
        store_copy(result)
        return result

    def test_a_perfect_copy_is_ok(self):
        self.assertEqual(verify_copy(self.copied()), "ok")

    def test_no_copy_is_missing(self):
        self.assertEqual(verify_copy(self.make_result()), "missing")

    def test_a_changed_copy_is_caught_by_its_own_checksum(self):
        result = self.copied()
        EnhancedImageBlob.objects.filter(result=result).update(data=jpeg(colour=(0, 0, 0)))
        self.assertEqual(verify_copy(result), "bytes_differ")

    def test_a_copy_damaged_to_the_same_length_is_a_bad_copy_even_when_the_file_is_gone_too(self):
        result = self.copied()
        damaged = bytearray(read_stored(result.enhanced_file))
        damaged[20] ^= 0xFF
        EnhancedImageBlob.objects.filter(result=result).update(data=bytes(damaged))
        result.enhanced_file.storage.delete(result.enhanced_file.name)
        self.assertEqual(verify_copy(result), "bytes_differ")

    def test_a_consistent_but_different_copy_is_caught_against_the_file(self):
        result = self.copied()
        other = jpeg(colour=(0, 0, 0))
        EnhancedImageBlob.objects.filter(result=result).update(data=other, byte_size=len(other), sha256=sha(other))
        self.assertEqual(verify_copy(result), "bytes_differ")

    def test_a_missing_stored_file_is_reported(self):
        result = self.copied()
        result.enhanced_file.storage.delete(result.enhanced_file.name)
        self.assertEqual(verify_copy(result), "file_missing")

    def test_bytes_that_are_not_an_image_are_reported(self):
        junk = b"this is not a picture" * 20
        result = self.copied(data=junk, recorded_size=False)
        self.assertEqual(verify_copy(result), "not_an_image")

    def test_an_image_of_another_size_than_recorded_is_reported(self):
        result = self.copied(data=jpeg(size=(80, 60)))
        self.assertEqual(verify_copy(result), "size_differs")

    def test_a_copy_returned_as_a_memoryview_is_judged_by_its_bytes(self):
        result = self.copied()
        data = read_stored(result.enhanced_file)
        row = SimpleNamespace(data=memoryview(data), byte_size=len(data), sha256=sha(data))
        with patch("enhancement.database_copy.EnhancedImageBlob.objects") as manager:
            manager.filter.return_value.first.return_value = row
            self.assertEqual(verify_copy(result), "ok")


@override_settings(**PLAIN)
class CommandTests(Media):
    def run_command(self, *args):
        out = io.StringIO()
        call_command("copy_enhanced_images_to_database", *args, stdout=out)
        return out.getvalue()

    def test_every_completed_result_with_a_file_is_copied_and_the_rest_are_left_alone(self):
        first, second = self.make_result("a.jpg"), self.make_result("b.jpg", data=jpeg(colour=(5, 5, 5)))
        self.make_result("c.jpg", status="failed", with_file=False)
        report = self.run_command()
        self.assertEqual(EnhancedImageBlob.objects.count(), 2)
        self.assertIn("Completed results with an enhanced file: 2", report)
        self.assertIn("copied into the database", report)
        self.assertEqual(verify_copy(first), "ok")
        self.assertEqual(verify_copy(second), "ok")

    def test_only_completed_results_are_copied_even_when_another_one_has_a_file(self):
        self.make_result("done.jpg")
        self.make_result("failed.jpg", status="failed")
        report = self.run_command()
        self.assertIn("Completed results with an enhanced file: 1", report)
        self.assertEqual(EnhancedImageBlob.objects.count(), 1)

    def test_running_it_again_copies_nothing_new(self):
        self.make_result()
        self.run_command()
        report = self.run_command()
        self.assertIn("already copied", report)
        self.assertEqual(EnhancedImageBlob.objects.count(), 1)

    def test_a_dry_run_writes_nothing_and_says_what_it_would_do(self):
        self.make_result()
        report = self.run_command("--dry-run")
        self.assertIn("would be copied", report)
        self.assertFalse(EnhancedImageBlob.objects.exists())

    def test_verify_passes_when_every_copy_is_perfect(self):
        self.make_result()
        self.run_command()
        report = self.run_command("--verify")
        self.assertIn("identical to the stored file and a valid image of the recorded size", report)

    def test_verify_fails_and_names_the_result_when_a_copy_is_wrong(self):
        good, bad = self.make_result("a.jpg"), self.make_result("b.jpg", data=jpeg(colour=(9, 9, 9)))
        self.run_command()
        EnhancedImageBlob.objects.filter(result=bad).update(data=b"corrupted")
        with self.assertRaisesMessage(CommandError, "1 result(s) have no perfect copy"):
            self.run_command("--verify")
        self.assertEqual(verify_copy(good), "ok")

    def test_verify_fails_for_a_result_that_has_no_copy(self):
        self.make_result()
        with self.assertRaises(CommandError):
            self.run_command("--verify")

    def test_verify_writes_nothing(self):
        self.make_result()
        with self.assertRaises(CommandError):
            self.run_command("--verify")
        self.assertFalse(EnhancedImageBlob.objects.exists())

    def test_verify_and_dry_run_cannot_be_combined(self):
        with self.assertRaises(CommandError):
            self.run_command("--verify", "--dry-run")

    def test_nothing_is_copied_while_images_are_encrypted_and_the_report_says_so(self):
        with override_settings(**ENCRYPTED):
            self.make_result()
            report = self.run_command()
        self.assertIn("encrypted", report)
        self.assertFalse(EnhancedImageBlob.objects.exists())

    def test_a_missing_file_is_a_listed_problem_not_a_crash(self):
        result = self.make_result()
        result.enhanced_file.storage.delete(result.enhanced_file.name)
        report = self.run_command()
        self.assertIn(f"result {result.pk}: not copied: the stored file is missing", report)


@override_settings(**PLAIN, ADMIN_IMAGE_PREVIEW_ENABLED=True)
class AdminPreviewTests(Media):
    def setUp(self):
        super().setUp()
        self.admin_user = User.objects.create_superuser(username="root", email="root@example.com", password="ForestRiver!4821")
        User.objects.filter(pk=self.admin_user.pk).update(is_email_verified=True)
        self.client.force_login(self.admin_user)
        self.data = jpeg(colour=(200, 30, 60))
        self.result = self.make_result(data=self.data)

    def change_url(self):
        return reverse("admin:enhancement_imageenhancementresult_change", args=[self.result.pk])

    def image_url(self, which="enhanced", pk=None):
        return reverse("admin:enhancement_imageenhancementresult_image", args=[pk or self.result.pk, which])

    def test_the_list_of_results_still_opens(self):
        page = self.client.get(reverse("admin:enhancement_imageenhancementresult_changelist"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, self.change_url())

    def test_the_page_shows_both_images_and_says_where_the_enhanced_one_comes_from(self):
        page = self.client.get(self.change_url())
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'<img src="{self.image_url("enhanced")}"')
        self.assertContains(page, f'<img src="{self.image_url("original")}"')
        self.assertContains(page, "Shown from the stored file.")
        self.assertContains(page, "No row for this result in enhancement_enhancedimageblob.")

    def test_with_a_copy_in_the_database_the_page_says_so_and_the_picture_comes_from_the_row(self):
        store_copy(self.result)
        page = self.client.get(self.change_url())
        self.assertContains(page, "Shown from the copy in the database.")
        self.assertContains(page, "Yes: ")
        self.assertContains(page, sha(self.data)[:16])
        served = self.client.get(self.image_url("enhanced"))
        self.assertEqual(served["X-Image-Source"], "database")
        self.assertEqual(served.content, self.data)

    def test_the_picture_really_comes_from_the_row_when_there_is_one(self):
        store_copy(self.result)
        other = jpeg(colour=(0, 255, 0))
        EnhancedImageBlob.objects.filter(result=self.result).update(data=other)
        self.assertEqual(self.client.get(self.image_url("enhanced")).content, other)

    def test_without_a_copy_the_picture_comes_from_the_stored_file(self):
        served = self.client.get(self.image_url("enhanced"))
        self.assertEqual((served.status_code, served["X-Image-Source"], served["Content-Type"]), (200, "file", "image/jpeg"))
        self.assertEqual(served.content, self.data)

    def test_the_original_is_the_uploaded_file(self):
        served = self.client.get(self.image_url("original"))
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.content, read_stored(self.result.image.original_file))
        self.assertNotEqual(served.content, self.data)

    def test_the_response_is_not_cached_and_not_sniffed(self):
        served = self.client.get(self.image_url("enhanced"))
        self.assertIn("private", served["Cache-Control"])
        self.assertIn("no-store", served["Cache-Control"])
        self.assertEqual(served["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", served["Content-Security-Policy"])

    @override_settings(SECURE_CONTENT_TYPE_NOSNIFF=False)
    def test_nosniff_comes_from_the_view_itself_not_only_from_the_security_middleware(self):
        self.assertEqual(self.client.get(self.image_url("enhanced"))["X-Content-Type-Options"], "nosniff")

    def test_the_columns_of_an_unsaved_result_are_empty_not_errors(self):
        model_admin = admin.site._registry[ImageEnhancementResult]
        unsaved = ImageEnhancementResult()
        self.assertEqual(model_admin.database_copy_status(unsaved), "-")
        self.assertEqual(model_admin.enhanced_image(unsaved), "-")
        self.assertEqual(model_admin.original_image(unsaved), "-")

    def test_the_stored_path_is_text_not_a_link_to_a_folder_that_is_not_served(self):
        page = self.client.get(self.change_url())
        self.assertContains(page, self.result.enhanced_file.name)
        self.assertNotContains(page, "/media/")
        self.assertNotContains(page, 'type="file"')  # not an upload widget either: the stored file cannot be replaced from this page

    def test_an_unknown_image_kind_or_result_is_not_found(self):
        self.assertEqual(self.client.get(self.image_url("secret")).status_code, 404)
        self.assertEqual(self.client.get(self.image_url("enhanced", pk=self.result.pk + 999)).status_code, 404)

    def test_a_result_without_a_file_has_no_enhanced_image(self):
        failed = self.make_result("f.jpg", status="failed", with_file=False)
        page = self.client.get(reverse("admin:enhancement_imageenhancementresult_change", args=[failed.pk]))
        self.assertContains(page, "No enhanced image (status: Failed).")
        self.assertEqual(self.client.get(self.image_url("enhanced", pk=failed.pk)).status_code, 404)

    def test_a_stored_file_that_is_gone_is_not_found_not_an_error(self):
        self.result.enhanced_file.storage.delete(self.result.enhanced_file.name)
        self.assertEqual(self.client.get(self.image_url("enhanced")).status_code, 404)

    def test_each_view_is_logged_without_the_file_name(self):
        with self.assertLogs("enhancement.admin", "INFO") as logged:
            self.client.get(self.image_url("enhanced"))
        line = "\n".join(logged.output)
        self.assertIn(f"Administrator {self.admin_user.pk} opened the enhanced image of enhancement result {self.result.pk}", line)
        self.assertNotIn(self.result.enhanced_file.name, line)

    def test_the_add_page_does_not_break_on_an_unsaved_result(self):
        self.assertEqual(self.client.get(reverse("admin:enhancement_imageenhancementresult_add")).status_code, 200)

    def test_a_visitor_who_is_not_signed_in_is_sent_to_the_login_page(self):
        self.client.logout()
        served = self.client.get(self.image_url("enhanced"))
        self.assertEqual(served.status_code, 302)
        self.assertIn("/admin/login/", served["Location"])

    def test_staff_without_the_view_permission_are_refused(self):
        staff = User.objects.create_user(username="staff", email="staff@example.com", password="ForestRiver!4821", is_staff=True, is_email_verified=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.image_url("enhanced")).status_code, 403)

    def test_staff_with_the_view_permission_may_look(self):
        staff = User.objects.create_user(username="staff", email="staff@example.com", password="ForestRiver!4821", is_staff=True, is_email_verified=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_imageenhancementresult"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.image_url("enhanced")).status_code, 200)

    @override_settings(ADMIN_IMAGE_PREVIEW_ENABLED=False)
    def test_with_the_switch_off_the_page_shows_no_picture_and_the_address_is_not_found(self):
        page = self.client.get(self.change_url())
        self.assertContains(page, "Previews are switched off (ADMIN_IMAGE_PREVIEW_ENABLED).")
        self.assertNotContains(page, "<img src=")
        self.assertEqual(self.client.get(self.image_url("enhanced")).status_code, 404)

    @override_settings(ADMIN_IMAGE_PREVIEW_ENABLED=False)
    def test_with_the_switch_off_the_permission_check_still_comes_first(self):
        self.client.logout()
        staff = User.objects.create_user(username="staff", email="staff@example.com", password="ForestRiver!4821", is_staff=True, is_email_verified=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.image_url("enhanced")).status_code, 403)


class IntegrityTests(SimpleTestCase):
    def test_one_row_per_result_is_a_database_rule(self):
        field = EnhancedImageBlob._meta.get_field("result")
        self.assertTrue(field.unique)
