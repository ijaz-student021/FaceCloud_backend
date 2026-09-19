import os
import tempfile
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import models
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from uploads.models import ImageUpload, UploadBatch

from .checks import privacy_configuration_check
from .cleanup import (
    MEDIA_SUBDIRECTORIES, cleanup_orphan_media, cleanup_stale_temp_archives,
    cleanup_temporary_files, orphan_media_files, referenced_media_names, stale_temp_archives,
)
from .models import PrivacyRequest
from .services import audit_report
from .tasks import cleanup_temporary_files_task
from .tests import make_export, make_group, make_user, seed_identity_data

HOUR = 3600


def age_file(path, hours):
    stamp = time.time() - hours * HOUR
    os.utime(path, (stamp, stamp))


class StaleTempArchiveTests(SimpleTestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)

    def tearDown(self):
        self._directory.cleanup()

    def _make(self, name, hours_old, content=b"zip-bytes"):
        path = self.directory / name
        path.write_bytes(content)
        age_file(path, hours_old)
        return path

    def test_only_old_build_archives_are_removed(self):
        old_package = self._make("facecloud-package-abc123.zip", 10)
        old_export = self._make("facecloud-identity-export-xyz789.zip", 7)
        recent = self._make("facecloud-package-still-building.zip", 1)
        unrelated = self._make("holiday-photos.zip", 100)
        other_prefix = self._make("facecloud-notes.txt", 100)
        wrong_suffix = self._make("facecloud-package-abc.tmp", 100)

        summary = cleanup_stale_temp_archives(directory=self.directory)

        self.assertEqual(summary["found"], 2)
        self.assertEqual(summary["deleted"], 2)
        self.assertEqual(summary["failed"], 0)
        self.assertFalse(old_package.exists())
        self.assertFalse(old_export.exists())
        for survivor in (recent, unrelated, other_prefix, wrong_suffix):
            self.assertTrue(survivor.exists(), survivor.name)

    def test_the_age_limit_is_honoured_on_both_sides(self):
        just_under = self._make("facecloud-package-a.zip", 5.9)
        just_over = self._make("facecloud-package-b.zip", 6.1)
        found = {path.name for path, _size in stale_temp_archives(self.directory)}
        self.assertEqual(found, {just_over.name})
        self.assertTrue(just_under.exists())

    def test_a_dry_run_reports_size_and_deletes_nothing(self):
        path = self._make("facecloud-package-big.zip", 50, content=b"x" * 1234)
        summary = cleanup_stale_temp_archives(dry_run=True, directory=self.directory)
        self.assertEqual((summary["found"], summary["bytes_found"], summary["deleted"]), (1, 1234, 0))
        self.assertTrue(path.exists())

    def test_directories_are_never_deleted(self):
        folder = self.directory / "facecloud-package-folder.zip"
        folder.mkdir()
        age_file(folder, 100)
        self.assertEqual(stale_temp_archives(self.directory), [])
        self.assertTrue(folder.exists())

    def test_symlinks_are_never_followed(self):
        target = self._make("real-target.bin", 100)
        link = self.directory / "facecloud-package-link.zip"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are not permitted in this environment.")
        self.assertEqual(stale_temp_archives(self.directory), [])
        self.assertTrue(target.exists())

    def test_one_undeletable_file_does_not_stop_the_rest(self):
        stuck = self._make("facecloud-package-stuck.zip", 20)
        fine = self._make("facecloud-package-fine.zip", 20)
        real_unlink = Path.unlink

        def flaky_unlink(path, *args, **kwargs):
            if path.name == stuck.name:
                raise PermissionError("in use")
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", flaky_unlink):
            summary = cleanup_stale_temp_archives(directory=self.directory)
        self.assertEqual((summary["deleted"], summary["failed"]), (1, 1))
        self.assertTrue(stuck.exists())
        self.assertFalse(fine.exists())

    def test_a_missing_directory_is_not_an_error(self):
        self.assertEqual(stale_temp_archives(self.directory / "does-not-exist"), [])

    def test_the_system_temp_directory_is_the_default(self):
        old = self._make("facecloud-identity-export-default.zip", 12)
        with patch("privacy.cleanup.tempfile.gettempdir", return_value=str(self.directory)):
            summary = cleanup_stale_temp_archives()
        self.assertEqual(summary["deleted"], 1)
        self.assertFalse(old.exists())


class OrphanMediaTests(TestCase):
    def setUp(self):
        self._media = tempfile.TemporaryDirectory()
        self._override = override_settings(
            MEDIA_ROOT=self._media.name, IDENTITY_EMBEDDING_KEY="cleanup-test-key",
            ORPHAN_MEDIA_GRACE_HOURS=24, ORPHAN_MEDIA_CLEANUP_ENABLED=False,
        )
        self._override.enable()
        self.user = make_user("cleaner")
        batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        self.image = ImageUpload.objects.create(owner=self.user, batch=batch, original_name="kept.jpg", status="uploaded")
        self.image.original_file.save("kept.jpg", ContentFile(b"referenced-photo"))
        _image, _analysis, faces = seed_identity_data(self.user, "cleanup")
        self.export = make_export(self.user, make_group(self.user, "Person", faces))
        # Everything the database knows about is old on disk, so only references protect it.
        for name in (self.image.original_file.name, self.export.export_file.name):
            age_file(default_storage.path(name), 500)

    def tearDown(self):
        self._override.disable()
        self._media.cleanup()

    def _orphan(self, relative, hours_old=100):
        name = default_storage.save(relative, ContentFile(b"orphaned-face-photo"))
        age_file(default_storage.path(name), hours_old)
        return name

    def test_referenced_files_are_never_reported_however_old(self):
        self.assertEqual(orphan_media_files(), [])
        references = referenced_media_names()
        self.assertIn(self.image.original_file.name, references)
        self.assertIn(self.export.export_file.name, references)

    def test_only_old_unreferenced_files_in_known_folders_are_orphans(self):
        old = self._orphan(f"users/{self.user.pk}/enhanced/never-committed.jpg")
        recent = self._orphan(f"users/{self.user.pk}/edited/just-written.jpg", hours_old=1)
        elsewhere = self._orphan(f"users/{self.user.pk}/some-future-feature/data.bin")
        top_level = self._orphan("avatars/someone.png")
        too_deep = self._orphan(f"users/{self.user.pk}/enhanced/nested/deeper.jpg")

        found = {name for name, _size in orphan_media_files()}

        self.assertEqual(found, {old})
        for untouched in (recent, elsewhere, top_level, too_deep):
            self.assertTrue(default_storage.exists(untouched), untouched)

    def test_reporting_never_deletes_and_deleting_only_removes_orphans(self):
        orphan = self._orphan(f"users/{self.user.pk}/reports/lost.pdf")

        report = cleanup_orphan_media(delete=False)
        self.assertEqual((report["found"], report["deleted"], report["delete_enabled"]), (1, 0, False))
        self.assertEqual(report["names"], [orphan])
        self.assertTrue(default_storage.exists(orphan))

        result = cleanup_orphan_media(delete=True)
        self.assertEqual((result["found"], result["deleted"], result["failed"]), (1, 1, 0))
        self.assertFalse(default_storage.exists(orphan))
        self.assertTrue(default_storage.exists(self.image.original_file.name))
        self.assertTrue(default_storage.exists(self.export.export_file.name))
        self.assertTrue(ImageUpload.objects.filter(pk=self.image.pk).exists())

    def test_files_left_behind_by_a_deleted_account_are_found(self):
        orphan = self._orphan(f"users/{self.user.pk}/enhanced/left-behind.jpg")
        self.user.delete()
        self.assertEqual({name for name, _ in orphan_media_files()}, {orphan})
        cleanup_orphan_media(delete=True)
        self.assertFalse(default_storage.exists(orphan))

    def test_orphans_are_only_deleted_when_the_setting_or_argument_enables_it(self):
        orphan = self._orphan(f"users/{self.user.pk}/enhanced/lost-a.jpg")
        cleanup_temporary_files()
        self.assertTrue(default_storage.exists(orphan))
        self.assertFalse(PrivacyRequest.objects.filter(kind="temp_cleanup").exists())

        with override_settings(ORPHAN_MEDIA_CLEANUP_ENABLED=True):
            summary = cleanup_temporary_files()
        self.assertEqual(summary["orphaned_media"]["deleted"], 1)
        self.assertFalse(default_storage.exists(orphan))

    def test_a_dry_run_overrides_even_an_explicit_delete_request(self):
        orphan = self._orphan(f"users/{self.user.pk}/enhanced/lost-b.jpg")
        summary = cleanup_temporary_files(dry_run=True, delete_orphans=True)
        self.assertEqual(summary["orphaned_media"]["found"], 1)
        self.assertEqual(summary["orphaned_media"]["deleted"], 0)
        self.assertTrue(default_storage.exists(orphan))
        self.assertFalse(PrivacyRequest.objects.exists())

    def test_deletions_are_audited_without_file_names(self):
        self._orphan(f"users/{self.user.pk}/enhanced/lost-c.jpg")
        cleanup_temporary_files(delete_orphans=True)
        record = PrivacyRequest.objects.get(kind="temp_cleanup")
        self.assertEqual((record.status, record.verified, record.trigger), ("completed", True, "operator"))
        self.assertIsNone(record.user)
        self.assertEqual(record.summary["orphaned_media"]["deleted"], 1)
        self.assertNotIn("names", record.summary["orphaned_media"])
        self.assertNotIn("lost-c", str(record.summary))

    def test_a_run_that_finds_nothing_writes_no_audit_record(self):
        cleanup_temporary_files(delete_orphans=True)
        self.assertFalse(PrivacyRequest.objects.exists())

    def test_the_celery_task_runs_both_cleanups(self):
        self._orphan(f"users/{self.user.pk}/enhanced/lost-d.jpg")
        summary = cleanup_temporary_files_task()
        self.assertEqual(summary["orphaned_media"]["found"], 1)
        self.assertNotIn("names", summary["orphaned_media"])

    def test_the_command_reports_and_only_deletes_when_asked(self):
        orphan = self._orphan(f"users/{self.user.pk}/enhanced/lost-e.jpg")
        out = StringIO()
        call_command("cleanup_temporary_files", "--dry-run", stdout=out)
        self.assertIn("1 found", out.getvalue())
        self.assertIn("report only", out.getvalue())
        self.assertIn(orphan, out.getvalue())
        self.assertTrue(default_storage.exists(orphan))

        call_command("cleanup_temporary_files", "--delete-orphans", stdout=StringIO())
        self.assertFalse(default_storage.exists(orphan))

    def test_the_audit_report_counts_stale_temp_archives(self):
        with patch("privacy.services.stale_temp_archives", return_value=[(Path("a.zip"), 1), (Path("b.zip"), 2)]):
            report = audit_report()
        self.assertEqual(report["temporary_files"]["stale_temp_archives"], 2)
        self.assertEqual(report["temporary_files"]["max_age_hours"], 6)
        self.assertFalse(report["temporary_files"]["orphan_media_deletion_enabled"])


def sample_instance(model):
    """An unsaved instance whose owner is 1, for computing its upload path."""
    try:
        return model(owner_id=1)
    except TypeError:
        # Models that reach their owner through a parent (comparison outputs).
        from enhancement.models import ModelComparison

        return model(comparison=ModelComparison(owner_id=1))


class MediaLayoutCoverageTests(SimpleTestCase):
    """Guards the orphan scanner against a new upload path it does not know about."""

    def test_every_file_field_in_the_project_uploads_to_a_scanned_folder(self):
        checked = 0
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if not isinstance(field, models.FileField):
                    continue
                checked += 1
                name = field.generate_filename(sample_instance(model), "example.jpg").replace("\\", "/")
                parts = name.split("/")
                with self.subTest(field=f"{model.__name__}.{field.name}"):
                    self.assertEqual(parts[0], "users", name)
                    self.assertEqual(len(parts), 4, name)
                    self.assertIn(
                        parts[2], MEDIA_SUBDIRECTORIES,
                        f"{model.__name__}.{field.name} writes to '{parts[2]}', which the orphan "
                        "scanner does not know; add it to MEDIA_SUBDIRECTORIES.",
                    )
        self.assertGreaterEqual(checked, 7)

    def test_every_scanned_folder_is_actually_used(self):
        used = set()
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if isinstance(field, models.FileField):
                    used.add(field.generate_filename(sample_instance(model), "x.jpg").replace("\\", "/").split("/")[2])
        self.assertEqual(used, set(MEDIA_SUBDIRECTORIES))


class CleanupConfigurationChecks(SimpleTestCase):
    @override_settings(TEMP_FILE_MAX_AGE_HOURS=0)
    def test_the_age_limits_must_be_positive(self):
        self.assertIn("privacy.E008", [issue.id for issue in privacy_configuration_check(None)])
        with override_settings(TEMP_FILE_MAX_AGE_HOURS=6, ORPHAN_MEDIA_GRACE_HOURS=0):
            self.assertIn("privacy.E008", [issue.id for issue in privacy_configuration_check(None)])

    @override_settings(TEMP_FILE_MAX_AGE_HOURS=1, CELERY_TASK_TIME_LIMIT=3600)
    def test_the_temp_age_limit_must_outlast_the_longest_task(self):
        self.assertIn("privacy.E009", [issue.id for issue in privacy_configuration_check(None)])

    @override_settings(TEMP_FILE_MAX_AGE_HOURS=6, ORPHAN_MEDIA_GRACE_HOURS=24, CELERY_TASK_TIME_LIMIT=3600)
    def test_the_defaults_are_consistent(self):
        self.assertEqual(privacy_configuration_check(None), [])
        self.assertLess(timezone.now().timestamp(), timezone.now().timestamp() + 1)
