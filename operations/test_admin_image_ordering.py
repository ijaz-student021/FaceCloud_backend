"""``ordering`` on the administrator's photo list (``GET /api/system/images/?ordering=``)."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient

from accounts.models import User
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .admin_images import ORDERING_VALUES

# Five photographs with names that differ at the first letter (and in case), a tie in size, and two kinds of "no sharpness score": no quality decision at all, and one with no score.
#                key   name           size  owner  days ago  sharpness
PHOTOS = [("a", "alpha.jpg",   500, "zed", 5, 10.0),
          ("b", "Bravo.jpg",   100, "amy", 4, 90.0),
          ("c", "charlie.jpg", 300, "bob", 3, "no decision"),
          ("d", "delta.jpg",   300, "cat", 2, 50.0),
          ("e", "echo.jpg",    200, "dan", 1, None)]
#                       value -> the photographs in the order it must list them. Ties go to the newest id, whichever way the field sorts (c and d are both 300, d is newer;
#                       c and e both have no score).
EXPECTED = [
    ("created_at", "a b c d e"), ("-created_at", "e d c b a"),
    ("original_name", "a b c d e"), ("-original_name", "e d c b a"),                       # A to Z without regard to case: "alpha" before "Bravo"
    ("file_size", "b e d c a"), ("-file_size", "a d c e b"),
    ("owner__username", "b c d e a"), ("-owner__username", "a e d c b"),
    ("quality_decision__sharpness_score", "a d b e c"), ("-quality_decision__sharpness_score", "b d a e c"),     # no score: last, in both directions
]


class OrderingTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="root", email="root@example.com", password="ForestRiver!4821", is_email_verified=True, role="admin")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        now = timezone.now()
        self.photo = {}
        for key, name, size, owner_name, days_ago, sharpness in PHOTOS:
            owner = User.objects.create_user(username=owner_name, email=f"{owner_name}@example.com", password="ForestRiver!4821", is_email_verified=True)
            batch = UploadBatch.objects.create(owner=owner, upload_type="single")
            image = ImageUpload.objects.create(owner=owner, batch=batch, original_name=name, status="uploaded", file_size=size)
            ImageUpload.objects.filter(pk=image.pk).update(created_at=now - timedelta(days=days_ago))
            if sharpness != "no decision":
                ImageQualityDecision.objects.create(
                    owner=owner, image=image, status="completed", decision="accepted", sharpness_score=sharpness,
                    preprocessing_result=ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed"),
                )
            self.photo[key] = image.pk

    def listed(self, query=""):
        response = self.client.get(f"/api/system/images/?{query}")
        self.assertEqual(response.status_code, 200, response.content)
        return [row["id"] for row in response.json()["results"]]

    def expected(self, keys):
        return [self.photo[key] for key in keys.split()]

    def test_each_allowed_value_orders_the_photos_as_expected(self):
        self.assertEqual(len(EXPECTED), len(ORDERING_VALUES))
        for value, keys in EXPECTED:
            with self.subTest(ordering=value):
                self.assertEqual(self.listed(f"ordering={value}"), self.expected(keys))

    def test_without_the_parameter_nothing_changes_it_is_newest_first(self):
        newest_first = self.expected("e d c b a")
        self.assertEqual(self.listed(), newest_first)
        self.assertEqual(self.listed("ordering="), newest_first)                           # an empty value is no value, like the filters
        self.assertEqual(self.listed("ordering=-created_at"), newest_first)

    def test_an_unknown_value_is_a_400_with_a_sentence_never_a_500(self):
        for value in ("pk", "-pk", "id", "owner", "owner_id", "name", "created_at,file_size", "--created_at", "+file_size", "%20file_size", "file_size%20", "FILE_SIZE",
                      "quality_decision__overall_score", "owner__email", "owner__password", "batch", "-", "created_at;drop", "x" * 5000, "%C3%A9"):
            with self.subTest(ordering=value[:40]):
                response = self.client.get(f"/api/system/images/?ordering={value}")
                self.assertEqual(response.status_code, 400)
                (sentence,) = response.json()["ordering"]
                self.assertIn("created_at", sentence)
                self.assertIn("leading -", sentence)

    def test_it_combines_with_the_filters_and_with_paging_without_repeating_or_skipping_a_photo(self):
        pages = []
        url = "/api/system/images/?ordering=-file_size&page_size=2"
        while url:
            body = self.client.get(url).json()
            pages.append([row["id"] for row in body["results"]])
            url = body["next"]
        self.assertEqual([len(page) for page in pages], [2, 2, 1])
        self.assertEqual([photo for page in pages for photo in page], self.expected("a d c e b"))
        self.assertEqual(self.listed("ordering=file_size&q=a.jpg"), self.expected("d a"))              # "alpha.jpg" and "delta.jpg", smallest first
        amy = User.objects.get(username="amy").pk
        self.assertEqual(self.listed(f"ordering=-original_name&owner={amy}"), self.expected("b"))

    def test_the_default_listing_is_still_for_administrators_only(self):
        self.assertEqual(APIClient().get("/api/system/images/?ordering=file_size").status_code, 401)

    def test_the_parameter_is_in_the_documentation_next_to_the_filters(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        parameters = {parameter["name"]: parameter for parameter in schema["paths"]["/api/system/images/"]["get"]["parameters"]}
        self.assertTrue({"status", "owner", "organization", "quality_decision", "q", "created_after", "created_before", "ordering"} <= set(parameters))
        self.assertEqual(sorted(parameters["ordering"]["schema"]["enum"]), sorted(ORDERING_VALUES))      # the schema lists the values sorted
        self.assertIn("leading -", parameters["ordering"]["description"])
