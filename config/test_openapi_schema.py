import tempfile
from pathlib import Path

import yaml
from django.core.management import call_command
from django.test import SimpleTestCase


class OpenAPISchemaTests(SimpleTestCase):
    def test_schema_generates_with_no_warnings(self):
        """A new untyped field or enum name clash fails here instead of silently
        degrading the contract the frontend generates its types from."""
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "schema.yml"
            call_command("spectacular", "--file", str(target), "--validate", "--fail-on-warn")
            text = target.read_text(encoding="utf-8")
        self.assertIn("ErrorDetail", text)
        self.assertIn("/api/system/queue/", text)
        schemas = yaml.safe_load(text)["components"]["schemas"]
        # the enhancement request takes a preset, and the result says which engine produced each stage
        self.assertEqual(schemas["PresetEnum"]["enum"], ["auto", "standard", "pro"])
        self.assertIn("preset", schemas["EnhancementRequest"]["properties"])
        self.assertNotIn("preset", schemas["EnhancementRequest"].get("required", []))
        self.assertTrue(schemas["ImageEnhancementResult"]["properties"]["processing_summary"]["nullable"])
        self.assertEqual(set(schemas["ProcessingSummary"]["properties"]), {"ai_applied", "classical_fallback", "stages"})
        self.assertEqual(set(schemas["ProcessingStage"]["properties"]), {"stage", "engine", "kind", "fallback", "replaced"})
