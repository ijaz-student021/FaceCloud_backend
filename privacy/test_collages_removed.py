"""Photo collages (Feature 49) are gone, and nothing around them broke.

What this pins: the app, its URLs, its API-schema paths, its job kind, its setting, its beat task and its rate limit no longer exist; the migration that drops the
table works on a database that has it, on one that does not, and twice; and erasing facial data, the neighbouring privacy feature, still works.
"""
from django.apps import apps
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import NoReverseMatch, Resolver404, resolve, reverse
from drf_spectacular.generators import SchemaGenerator
from rest_framework import status
from rest_framework.test import APIClient

from identities.models import IdentityFace
from operations.jobs import JOB_KINDS

from .tests import make_group, make_user, seed_identity_data

TABLE = "collages_collage"
BEFORE = [("privacy", "0004_organizationexport")]
AFTER = [("privacy", "0005_drop_collages")]


class NoCollagesLeftTests(SimpleTestCase):
    def test_the_app_is_not_installed_and_cannot_be_imported(self):
        self.assertFalse(apps.is_installed("collages"))
        with self.assertRaises(LookupError):
            apps.get_app_config("collages")
        with self.assertRaises(ImportError):
            __import__("collages")

    def test_no_url_remains(self):
        for path in ("/api/collages/", "/api/collages/1/", "/api/collages/1/download/"):
            with self.subTest(path=path), self.assertRaises(Resolver404):
                resolve(path)
        for name in ("collage-list", "collage-detail", "collage-download"):
            with self.subTest(name=name), self.assertRaises(NoReverseMatch):
                reverse(name, args=[1]) if name != "collage-list" else reverse(name)

    def test_the_api_schema_has_no_collages_paths_tag_or_schema(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        self.assertEqual([path for path in schema["paths"] if "collage" in path.lower()], [])
        self.assertEqual([name for name in schema.get("components", {}).get("schemas", {}) if "collage" in name.lower()], [])
        operation_ids = [op["operationId"] for item in schema["paths"].values() for op in item.values() if isinstance(op, dict) and "operationId" in op]
        self.assertEqual([name for name in operation_ids if "collage" in name], [])
        self.assertEqual([tag for operation in (op for item in schema["paths"].values() for op in item.values() if isinstance(op, dict))
                          for tag in operation.get("tags", []) if "collage" in tag], [])

    def test_the_failed_jobs_list_has_no_collage_kind(self):
        self.assertNotIn("collage", {kind.key for kind in JOB_KINDS})

    def test_the_setting_the_beat_task_and_the_rate_limit_are_gone(self):
        self.assertFalse(hasattr(settings, "COLLAGE_TTL_HOURS"))
        self.assertEqual([name for name, entry in settings.CELERY_BEAT_SCHEDULE.items() if "collage" in name or "collage" in entry["task"]], [])
        self.assertNotIn("collage", settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])
        self.assertNotIn("collages", settings.INSTALLED_APPS)


class DropCollagesMigrationTests(TransactionTestCase):
    """The migration runs on an old database (the table exists), on a new one (it never did) and a second time."""

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor

    def tables(self):
        return set(connection.introspection.table_names())

    def make_old_table(self):
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE TABLE "{TABLE}" (id integer PRIMARY KEY, owner_id integer NOT NULL, status varchar(12) NOT NULL)')
            cursor.execute(f"INSERT INTO \"{TABLE}\" (id, owner_id, status) VALUES (1, 1, 'ready')")

    def tearDown(self):
        with connection.cursor() as cursor:
            cursor.execute(f'DROP TABLE IF EXISTS "{TABLE}"')
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())     # leave the test database fully migrated for the next test
        super().tearDown()

    def test_an_old_database_loses_the_table_and_keeps_everything_else(self):
        self.migrate(BEFORE)
        self.make_old_table()
        self.assertIn(TABLE, self.tables())
        before = self.tables() - {TABLE}

        self.migrate(AFTER)

        self.assertNotIn(TABLE, self.tables())
        self.assertEqual(self.tables(), before)

    def test_a_new_database_that_never_had_the_table_migrates_cleanly(self):
        self.migrate(BEFORE)
        self.assertNotIn(TABLE, self.tables())
        self.migrate(AFTER)
        self.assertNotIn(TABLE, self.tables())

    def test_the_drop_can_be_run_again_and_going_backwards_does_nothing_harmful(self):
        self.migrate(AFTER)
        migration = MigrationExecutor(connection).loader.get_migration(*AFTER[0])
        with connection.schema_editor() as editor:
            for operation in migration.operations:                    # the same SQL a second time: no error
                operation.database_forwards("privacy", editor, None, None)
        self.migrate(BEFORE)                                          # backwards: a no-op
        self.make_old_table()                                         # ... and forwards again drops it
        self.migrate(AFTER)
        self.assertNotIn(TABLE, self.tables())


class ErasingFacialDataStillWorksTests(TestCase):
    def setUp(self):
        self.user = make_user("erasure-owner")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_facial_data_is_inspected_erased_and_verified_through_the_api(self):
        _image, _analysis, faces = seed_identity_data(self.user, "after-collages")
        make_group(self.user, "Someone", faces)
        url = reverse("privacy:facial-data")
        self.assertEqual(self.client.get(url).data["stored"]["faces"], 2)

        response = self.client.delete(url, {"confirm": True}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["verified"])
        self.assertEqual(IdentityFace.objects.filter(owner=self.user).count(), 0)
        self.assertEqual(self.client.get(url).data["stored"]["faces"], 0)
