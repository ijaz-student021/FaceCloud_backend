from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from uploads.models import UploadBatch

from django.contrib.auth import get_user_model

User = get_user_model()


class DatabaseInfoTests(TestCase):
    def run_command(self):
        out = StringIO()
        call_command("database_info", stdout=out)
        return out.getvalue()

    def test_it_names_the_engine_and_the_counts_and_says_migrations_are_applied(self):
        user = User.objects.create_user(username="db-user", email="db-user@example.com", password="x")
        UploadBatch.objects.create(owner=user, upload_type="single")
        text = self.run_command()
        self.assertIn("Engine:   sqlite3", text)
        self.assertIn("Migrations: all applied", text)
        self.assertIn("1 users, 0 photos, 1 upload batches", text)

    def test_it_changes_nothing(self):
        before = User.objects.count()
        self.run_command()
        self.assertEqual(User.objects.count(), before)
