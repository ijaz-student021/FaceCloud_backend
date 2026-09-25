from django.test import TestCase, override_settings


@override_settings(CORS_ALLOW_ALL_ORIGINS=False, CORS_ALLOWED_ORIGINS=["https://app.example.com"])
class CorsExposedHeadersTests(TestCase):
    def test_browser_scripts_may_read_download_and_throttle_headers(self):
        response = self.client.get(
            "/api/schema/", HTTP_ORIGIN="https://app.example.com",
        )
        exposed = response["Access-Control-Expose-Headers"].lower()
        self.assertIn("content-disposition", exposed)
        self.assertIn("retry-after", exposed)

    def test_unlisted_origins_get_no_cors_headers(self):
        response = self.client.get("/api/schema/", HTTP_ORIGIN="https://evil.example.com")
        self.assertNotIn("Access-Control-Allow-Origin", response)
