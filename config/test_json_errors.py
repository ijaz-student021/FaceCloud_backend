"""An unknown address or an uncaught exception under /api/ answers JSON; everywhere else Django's own pages are unchanged (BE-002)."""
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import include, path
from rest_framework.test import APIClient

from config import urls as project_urls

User = get_user_model()


def boom(request):
    raise RuntimeError("deliberate failure for the test")


def fine(request):
    return HttpResponse("ok")


urlpatterns = [path("api/boom/", boom), path("boom/", boom), path("api/fine/", fine), path("", include(project_urls))]
# This test urlconf wears the project's own handlers, whatever they are (so the test fails, rather than breaks, if the project has none).
if hasattr(project_urls, "handler404"):
    handler404 = project_urls.handler404
if hasattr(project_urls, "handler500"):
    handler500 = project_urls.handler500


@override_settings(DEBUG=False)
class NotFoundTests(TestCase):
    def test_an_address_that_matches_no_route_under_api_is_json(self):
        response = self.client.get("/api/images/abc/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"detail": "Not found."})

    def test_other_unknown_api_addresses_are_json_too(self):
        for address in ("/api/", "/api/nothing/here/", "/api/images/1/2/3/4/"):
            with self.subTest(address=address):
                response = self.client.get(address)
                self.assertEqual((response.status_code, response["Content-Type"]), (404, "application/json"))

    def test_an_address_outside_api_keeps_djangos_page(self):
        for address in ("/no-such-page/", "/apiary/", "/static/missing.css"):                     # "/apiary/" starts with "api" but is not under /api/
            with self.subTest(address=address):
                response = self.client.get(address)
                self.assertEqual(response.status_code, 404)
                self.assertTrue(response["Content-Type"].startswith("text/html"), response["Content-Type"])
                self.assertIn(b"Not Found", response.content)

    def test_a_404_the_api_itself_raises_is_unchanged(self):
        user = User.objects.create_user(username="err-user", email="err-user@example.com", password="Password!123", is_email_verified=True)
        client = APIClient()
        client.force_authenticate(user)
        response = client.get("/api/images/999999/")
        self.assertEqual((response.status_code, response["Content-Type"]), (404, "application/json"))
        self.assertIn("detail", response.json())

    def test_the_real_api_still_answers_normally(self):
        self.assertEqual(self.client.get("/api/schema/").status_code, 200)
        self.assertEqual(self.client.get("/api/images/my/").status_code, 401)                     # the usual JSON 401, not a 404


@override_settings(DEBUG=True)
class DebugModeTests(TestCase):
    def test_a_development_server_answers_the_same_json_under_api(self):
        response = self.client.get("/api/images/abc/")
        self.assertEqual((response.status_code, response["Content-Type"], response.json()), (404, "application/json", {"detail": "Not found."}))

    def test_and_keeps_the_debug_page_elsewhere(self):
        response = self.client.get("/no-such-page/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response["Content-Type"].startswith("text/html"))
        self.assertIn(b"Page not found", response.content)                                        # Django's technical 404 page


@override_settings(DEBUG=False, ROOT_URLCONF="config.test_json_errors", ALLOWED_HOSTS=["testserver"])
class ServerErrorTests(SimpleTestCase):
    def setUp(self):
        self.client = Client(raise_request_exception=False)

    def test_a_view_that_raises_under_api_answers_json_500(self):
        response = self.client.get("/api/boom/")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"detail": "Server error."})
        self.assertNotIn(b"deliberate", response.content)                                          # nothing about the failure is told to the caller

    def test_a_view_that_raises_elsewhere_keeps_djangos_page(self):
        response = self.client.get("/boom/")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response["Content-Type"].startswith("text/html"))
        self.assertIn(b"Server Error", response.content)

    def test_a_working_view_is_untouched(self):
        self.assertEqual(self.client.get("/api/fine/").content, b"ok")
