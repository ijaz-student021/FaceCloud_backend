"""The Swagger/OpenAPI documentation is complete: every operation is described, and a new endpoint cannot ship undocumented."""
from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator

from config.api_docs import CATALOGUE, EXAMPLES, TAGS

METHODS = ("get", "post", "put", "patch", "delete")


class ApiDocumentationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)
        cls.operations = [(path, method, operation) for path, item in cls.schema["paths"].items()
                          for method, operation in item.items() if method in METHODS]

    def test_every_operation_has_a_catalogue_entry_so_a_new_endpoint_must_be_documented(self):
        missing = sorted(op["operationId"] for _p, _m, op in self.operations if op["operationId"] not in CATALOGUE)
        self.assertEqual(missing, [], "Add these to config/api_docs.py CATALOGUE (summary, description).")

    def test_the_catalogue_has_no_stale_entries(self):
        known = {op["operationId"] for _p, _m, op in self.operations}
        self.assertEqual(sorted(set(CATALOGUE) - known), [])
        self.assertEqual(sorted(set(EXAMPLES) - known), [])

    def test_every_operation_has_a_summary_a_description_and_an_access_note(self):
        for path, method, operation in self.operations:
            with self.subTest(operation=operation["operationId"]):
                self.assertTrue(operation.get("summary", "").strip(), f"{method} {path}")
                self.assertGreater(len(operation["summary"]), 5)
                self.assertTrue(operation.get("description", "").strip(), f"{method} {path}")
                self.assertIn("**Access:**", operation["description"])

    def test_no_developer_notes_leak_into_the_public_documentation(self):
        for _path, _method, operation in self.operations:
            for marker in ("request.active_membership", "self.organization", "``"):
                self.assertNotIn(marker, operation["description"], operation["operationId"])

    def test_every_tag_used_is_described(self):
        described = {tag["name"] for tag in TAGS if tag["description"].strip()}
        used = {tag for _p, _m, op in self.operations for tag in op.get("tags", [])}
        self.assertEqual(sorted(used - described), [])

    def test_the_key_flow_has_examples_and_the_info_block_explains_it(self):
        by_id = {op["operationId"]: op for _p, _m, op in self.operations}
        for operation_id in ("auth_login_create", "enhancements_images_create", "enhancements_results_retrieve"):
            operation = by_id[operation_id]
            bodies = [c for r in operation["responses"].values() for c in r.get("content", {}).values()]
            bodies += list(operation.get("requestBody", {}).get("content", {}).values())
            self.assertTrue(any("examples" in body for body in bodies), operation_id)
        self.assertIn("Quick start", self.schema["info"]["description"])
        self.assertIn("jwtAuth", self.schema["components"]["securitySchemes"])
