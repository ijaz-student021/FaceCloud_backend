"""Files an operating system adds to an archive (macOS's __MACOSX folder and ._ copies, .DS_Store, Thumbs.db) are not photographs: they are left out of the batch."""
import zipfile

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from uploads.tests import image_bytes, make_zip
from uploads.validators import is_archive_junk, validate_zip_archive


def names(entries):
    return [member.filename for member in validate_zip_archive(make_zip(entries))]


class JunkNamesTests(SimpleTestCase):
    def test_the_system_files_are_recognised(self):
        for name in ("__MACOSX/photos/._a.jpg", "photos/._a.jpg", "._a.jpg", ".DS_Store", "photos/.DS_Store", "Thumbs.db", "photos/THUMBS.DB", "desktop.ini", "a/__MACOSX/b.jpg"):
            self.assertTrue(is_archive_junk(name), name)

    def test_a_photograph_is_not_junk(self):
        for name in ("a.jpg", "photos/IMG_1.JPG", "photos/dot.in.name.png", "my._photo.jpg", "macosx/a.jpg", "photos/_a.jpg"):
            self.assertFalse(is_archive_junk(name), name)


class ArchiveMembersTests(SimpleTestCase):
    def test_only_the_photographs_are_returned(self):
        photo = image_bytes()
        self.assertEqual(
            names([("a.jpg", photo), ("__MACOSX/._a.jpg", b"x" * 40), ("._b.jpg", b"x" * 40), (".DS_Store", b"x" * 40), ("folder/b.jpg", photo)]),
            ["a.jpg", "folder/b.jpg"],
        )

    def test_an_archive_of_only_system_files_has_no_photos(self):
        with self.assertRaises(ValidationError) as caught:
            names([("__MACOSX/._a.jpg", b"x" * 40), (".DS_Store", b"x" * 40)])
        self.assertIn("no photos", str(caught.exception.detail))

    def test_an_archive_with_no_files_at_all_is_still_empty(self):
        content = make_zip([])
        with zipfile.ZipFile(content) as archive:
            archive.testzip()
        with self.assertRaises(ValidationError):
            validate_zip_archive(content)

    def test_a_system_file_still_goes_through_the_safety_checks(self):
        with self.assertRaises(ValidationError):
            names([("a.jpg", image_bytes()), ("__MACOSX/../evil.jpg", b"x" * 40)])
