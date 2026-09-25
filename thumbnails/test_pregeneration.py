from io import StringIO
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from uploads.models import ImageUpload

from .models import ImageThumbnail
from .tasks import generate_thumbnail_task, pregenerate_thumbnail
from .tests import ThumbnailTestCase, encode, noisy_rgb

DISPATCH = "thumbnails.tasks.generate_thumbnail_task.delay"


class PregenerationSignalTests(ThumbnailTestCase):
    def store(self, **extra):
        """Create an image exactly as the upload code does: file attached, then saved."""
        batch = self.photo(b"", store=False).batch
        image = ImageUpload(owner=self.owner, batch=batch, original_name="new.png", status="uploaded", **extra)
        image.original_file.save("new.png", ContentFile(encode(noisy_rgb(400, 300))), save=False)
        image.save()
        return image

    def test_a_new_stored_photo_queues_its_thumbnail_after_commit(self):
        with patch(DISPATCH) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                image = self.store()
        dispatch.assert_called_once_with(image.pk)

    def test_nothing_is_queued_before_the_transaction_commits(self):
        with patch(DISPATCH) as dispatch:
            self.store()
        dispatch.assert_not_called()

    def test_rejected_or_fileless_photos_and_later_saves_do_not_queue(self):
        with patch(DISPATCH) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                rejected = ImageUpload.objects.create(
                    owner=self.owner, batch=self.photo(b"", store=False).batch, original_name="r.png", status="rejected",
                )
                ImageUpload.objects.create(
                    owner=self.owner, batch=rejected.batch, original_name="f.png", status="uploaded",
                )
                existing = self.photo(encode(noisy_rgb(50, 50)))
                dispatch.reset_mock()
                existing.original_name = "renamed.png"
                existing.save()
        dispatch.assert_not_called()

    @override_settings(THUMBNAIL_PREGENERATE=False)
    def test_it_can_be_switched_off(self):
        with patch(DISPATCH) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                self.store()
        dispatch.assert_not_called()

    def test_a_queue_outage_never_breaks_the_upload(self):
        with patch(DISPATCH, side_effect=ConnectionError("redis down")):
            with self.captureOnCommitCallbacks(execute=True):
                image = self.store()
        self.assertTrue(ImageUpload.objects.filter(pk=image.pk).exists())


class PregenerationTaskTests(ThumbnailTestCase):
    def test_the_task_creates_the_default_thumbnail_once(self):
        image = self.photo(encode(noisy_rgb(1000, 800)))
        first = generate_thumbnail_task.apply(args=[image.pk]).get()
        second = generate_thumbnail_task.apply(args=[image.pk]).get()
        self.assertEqual(first, {"image_id": image.pk, "generated": True})
        self.assertEqual(second, {"image_id": image.pk, "generated": True})
        thumbnail = ImageThumbnail.objects.get(image=image)
        self.assertEqual(thumbnail.size, 256)
        self.assertEqual(ImageThumbnail.objects.filter(image=image).count(), 1)

    def test_the_first_gallery_request_then_finds_it_cached(self):
        image = self.photo(encode(noisy_rgb(1000, 800)))
        generate_thumbnail_task.apply(args=[image.pk])
        with patch("thumbnails.services.render_thumbnail") as render:
            response, _body = self.fetch(image)
        self.assertEqual(response.status_code, 200)
        render.assert_not_called()

    def test_missing_rejected_unreadable_and_unlisted_sizes_are_quietly_skipped(self):
        rejected = self.photo(encode(noisy_rgb(50, 50)), status="rejected")
        broken = self.photo(b"not an image")
        good = self.photo(encode(noisy_rgb(50, 50)))
        self.assertIsNone(pregenerate_thumbnail(999999))
        self.assertIsNone(pregenerate_thumbnail(rejected.pk))
        self.assertIsNone(pregenerate_thumbnail(broken.pk))
        self.assertIsNone(pregenerate_thumbnail(good.pk, size=300))
        self.assertFalse(ImageThumbnail.objects.exists())


class BackfillCommandTests(ThumbnailTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("generate_thumbnails", *args, stdout=out)
        return out.getvalue()

    def test_it_fills_in_only_the_missing_thumbnails(self):
        done = self.photo(encode(noisy_rgb(300, 300)))
        pregenerate_thumbnail(done.pk)
        missing = [self.photo(encode(noisy_rgb(300, 200))) for _ in range(2)]
        self.photo(encode(noisy_rgb(50, 50)), status="rejected")
        self.photo(b"", store=False)

        self.assertIn("2 photo(s) need", self.run_command("--dry-run"))
        self.assertEqual(ImageThumbnail.objects.count(), 1)

        output = self.run_command()
        self.assertIn("Generated 2", output)
        self.assertEqual(ImageThumbnail.objects.count(), 3)
        self.assertEqual({t.image_id for t in ImageThumbnail.objects.all()}, {done.pk, *[m.pk for m in missing]})
        self.assertIn("0 photo(s) need", self.run_command("--dry-run"))

    def test_limit_and_size_options_and_an_invalid_size(self):
        for _ in range(3):
            self.photo(encode(noisy_rgb(2000, 1500)))
        self.assertIn("Generated 2", self.run_command("--limit", "2"))
        self.assertIn("Generated 3", self.run_command("--size", "1024"))
        self.assertEqual(sorted(ImageThumbnail.objects.values_list("size", flat=True)), [256, 256, 1024, 1024, 1024])
        with self.assertRaises(CommandError):
            self.run_command("--size", "300")

    def test_an_unreadable_photo_is_counted_not_fatal(self):
        self.photo(b"garbage")
        self.photo(encode(noisy_rgb(100, 100)))
        self.assertIn("Generated 1", self.run_command())
        self.assertIn("1 photo(s) could not be read", self.run_command())
