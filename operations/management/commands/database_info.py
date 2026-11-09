"""Say which database this process would use and what is in it (read-only)."""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = "Print the database in use (engine, file, size), a few row counts and whether migrations are unapplied. Changes nothing."

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        engine = database["ENGINE"].rsplit(".", 1)[-1]
        name = str(database["NAME"])
        self.stdout.write(f"Engine:   {engine}")
        self.stdout.write(f"Database: {name}")
        if engine == "sqlite3" and name != ":memory:":
            path = Path(name)
            size = f"{path.stat().st_size / 1024 / 1024:.1f} MB" if path.exists() else "does not exist yet"
            self.stdout.write(f"File:     {path.resolve()} ({size})")
        else:
            self.stdout.write(f"Host:     {database.get('HOST') or 'local'}:{database.get('PORT') or 'default'}")
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        pending = [f"{migration.app_label}.{migration.name}" for migration, _backwards in plan]
        if pending:
            self.stdout.write(self.style.WARNING(f"Migrations: {len(pending)} not applied yet (python manage.py migrate --check). First: {pending[0]}"))
        else:
            self.stdout.write(self.style.SUCCESS("Migrations: all applied"))
        if pending:
            self.stdout.write("Row counts skipped until the migrations are applied.")
            return
        from django.contrib.auth import get_user_model
        from uploads.models import ImageUpload, UploadBatch

        self.stdout.write(
            f"Rows:     {get_user_model().objects.count()} users, {ImageUpload.objects.count()} photos, {UploadBatch.objects.count()} upload batches"
        )
