"""Places: where a photograph was taken. GPS from real EXIF, an offline gazetteer, owner-only coordinates, the places list and filter, removal, the backfill."""
import hashlib
import io
import tempfile
from datetime import timedelta
from fractions import Fraction
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from places import gazetteer
from places.checks import place_name_checks
from places.models import Place
from places.queries import places_for
from places.service import attach_place, remove_location
from uploads.capture import gps_position, parse_gps
from uploads.models import ImageUpload, UploadBatch

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix="facecloud-places-")

# (geonameid, name, latitude, longitude, country, admin1, population[, feature code])
CITIES = [
    (1172451, "Lahore", 31.558, 74.35071, "PK", "04", 6310888),
    (1176615, "Islamabad", 33.72148, 73.04329, "PK", "08", 601600),
    (1174872, "Karachi", 24.8608, 67.0104, "PK", "05", 11624219),
    (2988507, "Paris", 48.85341, 2.3488, "FR", "11", 2138551),
    (5128581, "New York City", 40.71427, -74.00597, "US", "NY", 8175133),
    (2988000, "Passy", 48.8584, 2.2945, "FR", "11", 0, "PPLX"),                 # a neighbourhood of Paris
    (1190001, "Kot Tiny", 31.80, 74.35, "PK", "04", 3000),                      # a village about 27 km from Lahore
    (1190002, "Near Tiny", 31.65, 74.35, "PK", "04", 3000),                     # a village about 10 km from Lahore
    (1190003, "Old Mill", 34.5, 72.5, "PK", "08", 5000, "PPLQ"),                # abandoned
]


def city_line(geoname_id, name, lat, lon, country, admin1, population, feature_code="PPL"):
    columns = [str(geoname_id), name, name, "", f"{lat}", f"{lon}", "P", feature_code, country, "", admin1, "", "", "", str(population), "", "", "UTC", "2026-01-01"]
    return "\t".join(columns)


def install_gazetteer(approved=True, with_names=True):
    """A small GeoNames-format folder and the settings that make it usable; returns the settings dict."""
    folder = Path(tempfile.mkdtemp(prefix="facecloud-gaz-"))
    cities = folder / "cities1000.txt"
    cities.write_text("\n".join(city_line(*row) for row in CITIES) + "\n", encoding="utf-8")
    if with_names:
        (folder / "countryInfo.txt").write_text("# comment\nPK\tPAK\t586\tPK\tPakistan\tIslamabad\n" "FR\tFRA\t250\tFR\tFrance\tParis\nUS\tUSA\t840\tUS\tUnited States\tWashington\n", encoding="utf-8")
        (folder / "admin1CodesASCII.txt").write_text("PK.04\tPunjab\tPunjab\t1168700\nPK.08\tIslamabad\tIslamabad\t1176615\nFR.11\tIle-de-France\tIle-de-France\t3012874\nUS.NY\tNew York\tNew York\t5128638\n", encoding="utf-8")
    digest = hashlib.sha256(cities.read_bytes()).hexdigest()
    record = folder / "record.md"
    record.write_text(f"Owner approval: {'approved' if approved else 'pending'}\n\ncities1000.txt {digest}\n", encoding="utf-8")
    return {"LOCATION_ENABLED": True, "LOCATION_GAZETTEER_DIR": str(folder), "LOCATION_GAZETTEER_SHA256": digest, "LOCATION_LICENSE_RECORD_PATH": str(record)}


def to_dms(value):
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = round((value - degrees - minutes / 60) * 3600, 4)
    return (float(degrees), float(minutes), seconds)


def jpeg(lat=None, lon=None, fmt="JPEG", size=(64, 48)):
    """A small picture whose EXIF says it was taken at (lat, lon)."""
    exif = Image.Exif()
    if lat is not None:
        gps = exif.get_ifd(0x8825)
        gps[1], gps[2] = "N" if lat >= 0 else "S", to_dms(lat)
        gps[3], gps[4] = "E" if lon >= 0 else "W", to_dms(lon)
        exif[0x8825] = gps
    buffer = io.BytesIO()
    Image.new("RGB", size, (90, 120, 150)).save(buffer, format=fmt, exif=exif)
    return buffer.getvalue()


class ParseTests(SimpleTestCase):
    def test_degrees_minutes_seconds_in_both_hemispheres(self):
        self.assertEqual(parse_gps({1: "N", 2: (31, 33, 28.8), 3: "E", 4: (74, 21, 2.56)}), (31.5580, 74.3507))
        self.assertEqual(parse_gps({1: "S", 2: (33, 52, 0), 3: "W", 4: (151, 12, 36)}), (-33.8667, -151.21))

    def test_the_numbers_may_be_fractions_and_the_hemisphere_bytes_or_lower_case(self):
        gps = {1: b"N\x00", 2: (Fraction(31), Fraction(33), Fraction(144, 5)), 3: "e", 4: (Fraction(74), Fraction(21), Fraction(64, 25))}
        self.assertEqual(parse_gps(gps), (31.558, 74.3507))

    def test_it_is_rounded_to_about_eleven_metres(self):
        latitude, longitude = parse_gps({1: "N", 2: (31, 0, 0.123456789), 3: "E", 4: (74, 0, 0.987654321)})
        self.assertEqual((latitude, longitude), (round(31 + 0.123456789 / 3600, 4), round(74 + 0.987654321 / 3600, 4)))

    def test_what_is_not_a_position_is_none(self):
        bad = [
            {},                                                                 # no GPS block
            {1: "N", 2: (31, 33, 28.8)},                                        # no longitude
            {1: "X", 2: (31, 33, 28.8), 3: "E", 4: (74, 21, 2.5)},              # no hemisphere
            {2: (31, 33, 28.8), 3: "E", 4: (74, 21, 2.5)},                      # latitude hemisphere missing
            {1: "N", 2: (91, 0, 0), 3: "E", 4: (74, 21, 2.5)},                  # latitude out of range
            {1: "N", 2: (31, 0, 0), 3: "E", 4: (181, 0, 0)},                    # longitude out of range
            {1: "N", 2: (0, 0, 0), 3: "E", 4: (0, 0, 0)},                       # what cameras write without a fix
            {1: "N", 2: (31, -5, 0), 3: "E", 4: (74, 0, 0)},                    # negative minutes
            {1: "N", 2: (float("nan"), 0, 0), 3: "E", 4: (74, 0, 0)},
            {1: "N", 2: (31, 33), 3: "E", 4: (74, 21, 2.5)},                    # two numbers
            {1: "N", 2: "31 33 28", 3: "E", 4: (74, 21, 2.5)},
            {1: "N", 2: (1, 0, 0), 3: "E", 4: (1, 0, float("inf"))},
        ]
        for gps in bad:
            with self.subTest(gps=gps):
                self.assertIsNone(parse_gps(gps))

    def test_a_real_jpeg_and_png_carry_it_and_a_plain_one_does_not(self):
        for fmt in ("JPEG", "PNG"):
            with self.subTest(fmt=fmt):
                self.assertEqual(gps_position(Image.open(io.BytesIO(jpeg(31.5497, 74.3436, fmt=fmt)))), (31.5497, 74.3436))
        self.assertEqual(gps_position(Image.open(io.BytesIO(jpeg(-33.8568, -70.25)))), (-33.8568, -70.25))
        self.assertIsNone(gps_position(Image.open(io.BytesIO(jpeg()))))
        self.assertIsNone(gps_position(object()))                                                      # never raises


class GazetteerTests(TestCase):
    def test_the_nearest_place_wins_and_carries_country_and_region_names(self):
        with override_settings(**install_gazetteer()):
            hit = gazetteer.nearest(31.5882, 74.3108)                                                  # the Badshahi Mosque
            self.assertEqual((hit["name"], hit["country"], hit["region"], hit["country_code"]), ("Lahore", "Pakistan", "Punjab", "PK"))
            self.assertLess(hit["distance_km"], 10)
            self.assertEqual(gazetteer.nearest(48.8584, 2.2945)["name"], "Paris")
            self.assertEqual(gazetteer.nearest(40.758, -73.9855)["region"], "New York")

    def test_farther_than_the_limit_there_is_no_place(self):
        with override_settings(**install_gazetteer()):
            self.assertIsNone(gazetteer.nearest(0.5, -30.0))                                           # mid-Atlantic
            self.assertIsNone(gazetteer.nearest(33.0, 70.0))                                           # more than 50 km from anything listed
        with override_settings(**install_gazetteer(), LOCATION_MAX_DISTANCE_KM=500):
            self.assertEqual(gazetteer.nearest(33.0, 70.0)["name"], "Islamabad")

    def test_a_part_of_a_town_and_an_abandoned_place_are_never_chosen(self):
        with override_settings(**install_gazetteer()):
            self.assertEqual(gazetteer.nearest(48.8584, 2.2945)["name"], "Paris")                       # the Eiffel Tower's own neighbourhood is skipped
            self.assertIsNone(gazetteer.nearest(34.5, 72.5))                                           # the abandoned mill is not a place to name a photograph after

    def test_a_village_next_to_a_big_city_is_named_after_the_city_but_a_far_one_keeps_its_name(self):
        with override_settings(**install_gazetteer()):
            self.assertEqual(gazetteer.nearest(31.65, 74.35)["name"], "Lahore")                         # 10 km from Lahore
            self.assertEqual(gazetteer.nearest(31.80, 74.35)["name"], "Kot Tiny")                      # 27 km away: its own place
            self.assertEqual(gazetteer.nearest(31.558, 74.35)["name"], "Lahore")

    def test_the_names_files_are_optional(self):
        with override_settings(**install_gazetteer(with_names=False)):
            hit = gazetteer.nearest(31.58, 74.35)
            self.assertEqual((hit["name"], hit["country"], hit["region"]), ("Lahore", "PK", ""))

    def test_place_names_are_off_unless_everything_is_in_order(self):
        good = install_gazetteer()
        with override_settings(**good):
            self.assertEqual(gazetteer.status(), (True, ""))
        cases = {
            "off": ({**good, "LOCATION_ENABLED": False}, "switched off"),
            "no file": ({**good, "LOCATION_GAZETTEER_DIR": tempfile.mkdtemp()}, "not installed"),
            "no checksum": ({**good, "LOCATION_GAZETTEER_SHA256": ""}, "SHA256 is not set"),
            "wrong checksum": ({**good, "LOCATION_GAZETTEER_SHA256": "0" * 64}, "not the one"),
            "pending": (install_gazetteer(approved=False), "not approved"),
        }
        for label, (overrides, expected) in cases.items():
            with self.subTest(label), override_settings(**overrides):
                usable, reason = gazetteer.status()
                self.assertFalse(usable)
                self.assertIn(expected, reason)
                self.assertIsNone(gazetteer.nearest(31.58, 74.35))
        unlisted = install_gazetteer()
        Path(unlisted["LOCATION_LICENSE_RECORD_PATH"]).write_text("Owner approval: approved\n(no checksum)\n")
        with override_settings(**unlisted):
            self.assertIn("does not list", gazetteer.status()[1])

    def test_a_missing_file_is_only_a_warning_when_it_is_there_but_unusable(self):
        with override_settings(LOCATION_ENABLED=True, LOCATION_GAZETTEER_DIR=tempfile.mkdtemp()):
            self.assertEqual(place_name_checks(None), [])                                              # not installed: silence
        with override_settings(**install_gazetteer(approved=False)):
            warnings = place_name_checks(None)
            self.assertEqual([warning.id for warning in warnings], ["places.W001"])
        with override_settings(**install_gazetteer()):
            self.assertEqual(place_name_checks(None), [])


@override_settings(MEDIA_ROOT=MEDIA)
class PlacesBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="pl-owner", email="pl-owner@example.com", password="Password!123", is_email_verified=True)
        self.other = User.objects.create_user(username="pl-other", email="pl-other@example.com", password="Password!123", is_email_verified=True)
        self.client = self.api(self.owner)
        self.batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple")
        self.count = 0
        self.gazetteer = override_settings(**install_gazetteer())
        self.gazetteer.enable()
        self.addCleanup(self.gazetteer.disable)

    @staticmethod
    def api(user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        return client

    def photo(self, lat=None, lon=None, *, owner=None, place=True, status="uploaded", trashed=False):
        self.count += 1
        owner = owner or self.owner
        image = ImageUpload.objects.create(
            owner=owner, batch=self.batch if owner == self.owner else UploadBatch.objects.create(owner=owner, upload_type="single"),
            original_name=f"p-{self.count}.jpg", status=status, gps_lat=lat, gps_lon=lon, location_checked_at=timezone.now(),
        )
        if trashed:
            ImageUpload.all_objects.filter(pk=image.pk).update(trashed_at=timezone.now())
        if lat is not None and place:
            attach_place(image)
        return ImageUpload.all_objects.get(pk=image.pk)

    def upload(self, data, client=None):
        with self.captureOnCommitCallbacks(execute=False):
            response = (client or self.client).post("/api/images/upload/single/", {"file": SimpleUploadedFile("shot.jpg", data, content_type="image/jpeg")}, format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["images"][0]


class UploadTests(PlacesBase):
    def test_an_upload_records_the_position_and_the_named_place_but_the_row_shows_no_coordinates(self):
        row = self.upload(jpeg(31.5882, 74.3108, size=(480, 640)))
        stored = ImageUpload.objects.get(pk=row["id"])
        self.assertEqual((stored.gps_lat, stored.gps_lon), (31.5882, 74.3108))
        self.assertEqual(stored.place.name, "Lahore")
        self.assertIsNotNone(stored.location_checked_at)
        listing = self.client.get(reverse("my-image-list")).json()["results"][0]
        self.assertEqual((listing["has_location"], listing["place"]["name"], listing["place"]["country_code"]), (True, "Lahore", "PK"))
        self.assertNotIn("31.58", self.client.get(reverse("my-image-list")).content.decode())
        self.assertNotIn("gps_lat", listing)

    def test_a_photograph_without_a_position_has_none(self):
        row = self.upload(jpeg())
        listing = self.client.get(reverse("my-image-list")).json()["results"][0]
        self.assertEqual((listing["has_location"], listing["place"]), (False, None))
        self.assertIsNotNone(ImageUpload.objects.get(pk=row["id"]).location_checked_at)             # looked at, found nothing

    def test_with_the_switch_off_nothing_is_read(self):
        with override_settings(LOCATION_ENABLED=False):
            row = self.upload(jpeg(31.5882, 74.3108))
        stored = ImageUpload.objects.get(pk=row["id"])
        self.assertEqual((stored.gps_lat, stored.place_id, stored.location_checked_at), (None, None, None))

    def test_without_an_approved_place_file_the_position_is_kept_and_no_place_is_named(self):
        with override_settings(LOCATION_GAZETTEER_DIR=tempfile.mkdtemp()):
            row = self.upload(jpeg(31.5882, 74.3108))
            stored = ImageUpload.objects.get(pk=row["id"])
            self.assertEqual((stored.gps_lat, stored.place_id), (31.5882, None))
            self.assertFalse(self.client.get(reverse("places:place-list")).json()["names_available"])

    def test_a_position_far_from_any_place_is_kept_without_one(self):
        row = self.upload(jpeg(0.5, -30.0))
        stored = ImageUpload.objects.get(pk=row["id"])
        self.assertEqual((stored.gps_lat, stored.place_id), (0.5, None))


class PlacesApiTests(PlacesBase):
    def test_the_list_counts_only_my_stored_photographs_that_are_not_in_the_trash(self):
        for _ in range(3):
            self.photo(31.58, 74.35)
        self.photo(33.69, 73.05)
        self.photo(31.58, 74.35, trashed=True)
        self.photo(31.58, 74.35, status="rejected")
        self.photo(31.58, 74.35, owner=self.other)
        body = self.client.get(reverse("places:place-list")).json()
        self.assertEqual([(row["name"], row["photo_count"]) for row in body["places"]], [("Lahore", 3), ("Islamabad", 1)])
        self.assertEqual(body["attribution"], gazetteer.ATTRIBUTION)
        self.assertTrue(body["names_available"])
        lahore = body["places"][0]
        self.assertEqual((lahore["region"], lahore["country"], lahore["country_code"]), ("Punjab", "Pakistan", "PK"))
        self.assertEqual((lahore["latitude"], lahore["longitude"]), (31.558, 74.35071))               # the city's centre, not a photograph's position
        self.assertRegex(lahore["cover_thumbnail_url"], r"/api/images/\d+/thumbnail/$")

    def test_a_place_with_only_trashed_photographs_disappears_and_returns_on_restore(self):
        image = self.photo(48.8584, 2.2945)
        self.assertEqual([row["name"] for row in self.client.get(reverse("places:place-list")).json()["places"]], ["Paris"])
        ImageUpload.all_objects.filter(pk=image.pk).update(trashed_at=timezone.now())
        self.assertEqual(self.client.get(reverse("places:place-list")).json()["places"], [])
        self.assertEqual(self.client.get(reverse("places:place-detail", args=[image.place_id])).status_code, 404)
        ImageUpload.all_objects.filter(pk=image.pk).update(trashed_at=None)
        self.assertEqual(self.client.get(reverse("places:place-detail", args=[image.place_id])).json()["photo_count"], 1)

    def test_another_persons_place_is_not_mine(self):
        theirs = self.photo(48.8584, 2.2945, owner=self.other)
        self.assertEqual(self.client.get(reverse("places:place-list")).json()["places"], [])
        self.assertEqual(self.client.get(reverse("places:place-detail", args=[theirs.place_id])).status_code, 404)
        stranger = self.api(self.other)
        self.assertEqual(len(stranger.get(reverse("places:place-list")).json()["places"]), 1)

    def test_the_cover_is_a_favourite_first_else_the_newest(self):
        older = self.photo(31.58, 74.35)
        newer = self.photo(31.58, 74.35)
        row = lambda: self.client.get(reverse("places:place-detail", args=[older.place_id])).json()
        self.assertTrue(row()["cover_thumbnail_url"].endswith(f"/{newer.pk}/thumbnail/"))
        ImageUpload.all_objects.filter(pk=older.pk).update(is_favorite=True)
        self.assertTrue(row()["cover_thumbnail_url"].endswith(f"/{older.pk}/thumbnail/"))

    def test_filters_and_validation(self):
        self.photo(31.58, 74.35)
        self.photo(48.8584, 2.2945)
        names = lambda **query: [row["name"] for row in self.client.get(reverse("places:place-list"), query).json()["places"]]   # noqa: E731
        self.assertEqual(names(q="lah"), ["Lahore"])
        self.assertEqual(names(country="fr"), ["Paris"])
        self.assertEqual(len(names(limit=1)), 1)
        for query in ({"country": "PAK"}, {"country": "1"}, {"limit": "0"}, {"limit": "x"}):
            self.assertEqual(self.client.get(reverse("places:place-list"), query).status_code, 400, query)
        self.assertEqual(APIClient().get(reverse("places:place-list")).status_code, 401)

    def test_the_gallery_filters_by_place_and_country(self):
        lahore, other_lahore, paris = self.photo(31.58, 74.35), self.photo(31.59, 74.34), self.photo(48.8584, 2.2945)
        gallery = lambda **query: sorted(row["id"] for row in self.client.get(reverse("my-image-list"), query).json()["results"])   # noqa: E731
        self.assertEqual(gallery(place=lahore.place_id), sorted([lahore.pk, other_lahore.pk]))
        self.assertEqual(gallery(country="FR"), [paris.pk])
        self.assertEqual(gallery(country="fr", place=paris.place_id), [paris.pk])
        self.assertEqual(gallery(place=999999), [])
        for query in ({"place": "x"}, {"country": "FRA"}):
            self.assertEqual(self.client.get(reverse("my-image-list"), query).status_code, 400)
        ImageUpload.all_objects.filter(pk=paris.pk).update(trashed_at=timezone.now())
        self.assertEqual(gallery(country="FR"), [])

    def test_the_top_bar_search_finds_places(self):
        self.photo(31.58, 74.35)
        body = self.client.get(reverse("search"), {"q": "lah"}).json()
        self.assertEqual([row["name"] for row in body["places"]], ["Lahore"])
        self.assertEqual(self.client.get(reverse("search"), {"q": "zzzz"}).json()["places"], [])

    def test_a_photograph_in_two_places_counts_once_for_each_person(self):
        self.photo(31.58, 74.35, owner=self.other)
        self.photo(31.58, 74.35)
        self.assertEqual([row.photo_count for row in places_for(self.owner)], [1])
        self.assertEqual(Place.objects.count(), 1)                                                     # one shared place row; the photographs are what is private


class LocationEndpointTests(PlacesBase):
    def test_the_owner_sees_the_coordinates_and_the_place(self):
        image = self.photo(31.5882, 74.3108)
        body = self.client.get(reverse("image-location", args=[image.pk])).json()
        self.assertEqual((body["latitude"], body["longitude"], body["place"]["name"]), (31.5882, 74.3108, "Lahore"))

    def test_a_photograph_with_no_location_or_someone_elses_is_a_404(self):
        plain, theirs = self.photo(), self.photo(31.58, 74.35, owner=self.other)
        for image in (plain, theirs):
            self.assertEqual(self.client.get(reverse("image-location", args=[image.pk])).status_code, 404)
        self.assertEqual(self.client.delete(reverse("image-location", args=[theirs.pk])).status_code, 404)
        self.assertEqual(ImageUpload.objects.get(pk=theirs.pk).gps_lat, 31.58)
        self.assertEqual(APIClient().get(reverse("image-location", args=[plain.pk])).status_code, 401)

    def test_coordinates_are_not_available_while_the_photograph_is_in_the_trash(self):
        image = self.photo(31.58, 74.35, trashed=True)
        self.assertEqual(self.client.get(reverse("image-location", args=[image.pk])).status_code, 404)

    def test_removing_a_location_takes_it_off_and_it_stays_off(self):
        image = self.photo(31.5882, 74.3108)
        self.assertEqual(self.client.delete(reverse("image-location", args=[image.pk])).status_code, 204)
        stored = ImageUpload.objects.get(pk=image.pk)
        self.assertEqual((stored.gps_lat, stored.gps_lon, stored.place_id), (None, None, None))
        self.assertEqual(self.client.get(reverse("image-location", args=[image.pk])).status_code, 404)
        self.assertEqual(self.client.delete(reverse("image-location", args=[image.pk])).status_code, 204)        # idempotent
        self.assertEqual(self.client.get(reverse("places:place-list")).json()["places"], [])
        row = self.client.get(reverse("my-image-list")).json()["results"][0]
        self.assertEqual((row["has_location"], row["place"]), (False, None))

    def test_the_bulk_removal_says_what_it_did(self):
        has, plain, theirs = self.photo(31.58, 74.35), self.photo(), self.photo(31.58, 74.35, owner=self.other)
        body = self.client.post(reverse("image-location-remove"), {"image_ids": [has.pk, plain.pk, theirs.pk, 999999, has.pk]}, format="json").json()
        self.assertEqual(body, {"removed_ids": [has.pk], "without_location_ids": [plain.pk], "not_found_ids": [theirs.pk, 999999]})
        self.assertEqual(ImageUpload.objects.get(pk=theirs.pk).gps_lat, 31.58)
        for payload in ({}, {"image_ids": []}, {"image_ids": ["a"]}, {"image_ids": list(range(1, 202))}):
            self.assertEqual(self.client.post(reverse("image-location-remove"), payload, format="json").status_code, 400, payload)

    def test_the_service_marks_the_photograph_as_looked_at(self):
        image = self.photo(31.58, 74.35)
        ImageUpload.all_objects.filter(pk=image.pk).update(location_checked_at=None)
        self.assertEqual(remove_location([image]), 1)
        self.assertIsNotNone(ImageUpload.objects.get(pk=image.pk).location_checked_at)


class NobodyElseSeesCoordinatesTests(PlacesBase):
    def test_coordinates_are_only_in_the_location_endpoint_and_the_owners_own_export(self):
        image = self.photo(31.5497, 74.3436)
        export = self.client.get("/api/privacy/export/").json()
        row = next(row for row in export["images"] if row["id"] == image.pk)
        self.assertEqual((row["gps_lat"], row["gps_lon"]), (31.5497, 74.3436))
        for name in ("my-image-list", "places:place-list", "search"):
            query = {"q": "lah"} if name == "search" else {}
            self.assertNotIn("31.5497", self.client.get(reverse(name), query).content.decode(), name)
        self.assertNotIn("31.5497", self.client.get(reverse("image-upload-detail", args=[image.pk])).content.decode())
        self.assertIn("31.5497", self.client.get(reverse("image-location", args=[image.pk])).content.decode())

    def test_thumbnails_and_standardized_copies_carry_no_gps(self):
        from thumbnails.services import render_thumbnail
        from uploads.services import preprocess_image

        image = self.photo(31.5497, 74.3436)
        image.original_file.save("gps.jpg", ContentFile(jpeg(31.5497, 74.3436, size=(320, 240))))
        original = ImageUpload.all_objects.get(pk=image.pk)
        self.assertIsNotNone(gps_position(Image.open(original.original_file)))                           # the original keeps what the camera wrote
        payload, _w, _h = render_thumbnail(original.original_file, 256)
        self.assertEqual(dict(Image.open(io.BytesIO(payload)).getexif().get_ifd(0x8825)), {})
        result, _created = preprocess_image(original)
        self.assertEqual(dict(Image.open(result.processed_file).getexif().get_ifd(0x8825)), {})


@override_settings(MEDIA_ROOT=MEDIA)
class BackfillTests(PlacesBase):
    def stored(self, data, **fields):
        self.count += 1
        image = ImageUpload.objects.create(owner=self.owner, batch=self.batch, original_name=f"b-{self.count}.jpg", status="uploaded", **fields)
        image.original_file.save(image.original_name, ContentFile(data))
        return ImageUpload.objects.get(pk=image.pk)

    def run_command(self, *args):
        out = io.StringIO()
        call_command("backfill_location", *args, stdout=out)
        return out.getvalue()

    def test_it_reads_old_photographs_then_places_them_and_a_second_run_changes_nothing(self):
        with_gps, plain = self.stored(jpeg(31.5882, 74.3108)), self.stored(jpeg())
        text = self.run_command()
        self.assertIn("read a position for 1", text)
        self.assertIn("placed 1", text)
        stored = ImageUpload.objects.get(pk=with_gps.pk)
        self.assertEqual((stored.gps_lat, stored.gps_lon, stored.place.name), (31.5882, 74.3108, "Lahore"))
        self.assertEqual((ImageUpload.objects.get(pk=plain.pk).gps_lat, ImageUpload.objects.get(pk=plain.pk).location_checked_at is not None), (None, True))
        again = self.run_command()
        self.assertIn("looked at 0 photograph(s)", again.split("Pass 2")[0])
        self.assertIn("looked at 0 photograph(s) with a position", again)

    def test_a_removed_location_is_never_read_again(self):
        image = self.stored(jpeg(31.5882, 74.3108))
        remove_location([image])
        self.run_command()
        self.assertEqual((ImageUpload.objects.get(pk=image.pk).gps_lat, ImageUpload.objects.get(pk=image.pk).place_id), (None, None))

    def test_a_dry_run_and_a_limit(self):
        for _ in range(3):
            self.stored(jpeg(31.5882, 74.3108))
        text = self.run_command("--dry-run")
        self.assertIn("would read a position for 3", text)
        self.assertEqual(ImageUpload.objects.filter(gps_lat__isnull=False).count(), 0)
        self.run_command("--limit", "2")
        self.assertEqual(ImageUpload.objects.filter(gps_lat__isnull=False).count(), 2)

    def test_without_an_approved_place_file_it_reads_positions_and_skips_naming(self):
        image = self.stored(jpeg(31.5882, 74.3108))
        with override_settings(LOCATION_GAZETTEER_DIR=tempfile.mkdtemp()):
            text = self.run_command()
        self.assertIn("Pass 2 skipped", text)
        self.assertEqual((ImageUpload.objects.get(pk=image.pk).gps_lat, ImageUpload.objects.get(pk=image.pk).place_id), (31.5882, None))
        self.run_command()                                                                                # the file arrives later: the next run names it
        self.assertEqual(ImageUpload.objects.get(pk=image.pk).place.name, "Lahore")

    def test_it_does_nothing_with_the_switch_off(self):
        image = self.stored(jpeg(31.5882, 74.3108))
        with override_settings(LOCATION_ENABLED=False):
            self.assertIn("nothing to do", self.run_command())
        self.assertIsNone(ImageUpload.objects.get(pk=image.pk).gps_lat)

    def test_a_trashed_photograph_is_read_too_so_it_is_right_when_restored(self):
        image = self.stored(jpeg(31.5882, 74.3108))
        ImageUpload.all_objects.filter(pk=image.pk).update(trashed_at=timezone.now() - timedelta(days=1))
        self.run_command()
        self.assertEqual(ImageUpload.all_objects.get(pk=image.pk).place.name, "Lahore")
