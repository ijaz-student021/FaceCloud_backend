"""Albums: made by hand, owned by one person, never deleting a photograph."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from uploads.models import ImageUpload, UploadBatch

from .models import Album, AlbumImage

User = get_user_model()


class AlbumBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="album-owner", email="album-owner@example.com", password="Password!123", is_email_verified=True)
        self.stranger = User.objects.create_user(username="album-other", email="album-other@example.com", password="Password!123", is_email_verified=True)
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple")
        self.photos = [ImageUpload.objects.create(owner=self.owner, batch=batch, original_name=f"a-{i}.jpg", status="uploaded") for i in range(6)]
        self.refused = ImageUpload.objects.create(owner=self.owner, batch=batch, original_name="refused.jpg", status="rejected")
        self.theirs = ImageUpload.objects.create(owner=self.stranger, batch=UploadBatch.objects.create(owner=self.stranger, upload_type="single"), original_name="theirs.jpg", status="uploaded")
        self.client = self.api(self.owner)

    @staticmethod
    def api(user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        return client

    def make(self, title="Eid 2026", image_ids=(), client=None, **extra):
        response = (client or self.client).post(reverse("albums:album-list"), {"title": title, "image_ids": list(image_ids), **extra}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def ids(self, album, **params):
        body = self.client.get(reverse("albums:album-images", kwargs={"pk": album["id"]}), params).json()
        return [row["id"] for row in body["results"]]


class CreateAndListTests(AlbumBase):
    def test_an_album_is_made_with_a_title_and_optionally_photographs(self):
        body = self.make("  Eid   2026 ", [self.photos[0].pk, self.photos[1].pk], description="Day one")
        self.assertEqual((body["title"], body["description"], body["photo_count"]), ("Eid 2026", "Day one", 2))
        self.assertEqual(body["added_ids"], [self.photos[0].pk, self.photos[1].pk])
        self.assertEqual(body["cover_image"], self.photos[0].pk)                                         # the first added, until one is chosen
        self.assertTrue(body["cover_thumbnail_url"].endswith(f"/api/images/{self.photos[0].pk}/thumbnail/"))
        empty = self.make("Empty")
        self.assertEqual((empty["photo_count"], empty["cover_image"], empty["cover_thumbnail_url"]), (0, None, None))

    def test_a_bad_title_is_refused(self):
        url = reverse("albums:album-list")
        for body in ({}, {"title": ""}, {"title": "   "}, {"title": "x" * 121}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(url, body, format="json").status_code, 400)
        self.assertFalse(Album.objects.exists())

    def test_photographs_that_are_not_yours_are_reported_not_added(self):
        body = self.make("Mixed", [self.photos[0].pk, self.theirs.pk, self.refused.pk, 999999])
        self.assertEqual((body["photo_count"], body["added_ids"], body["not_found_ids"]), (1, [self.photos[0].pk], [self.theirs.pk, self.refused.pk, 999999]))

    @override_settings(ALBUM_MAX_PER_USER=2)
    def test_the_number_of_albums_is_limited_with_a_clear_message(self):
        self.make("One")
        self.make("Two")
        response = self.client.post(reverse("albums:album-list"), {"title": "Three"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("most albums allowed (2)", str(response.json()))
        self.make("Mine too", client=self.api(self.stranger))                                          # the limit is per person

    def test_list_most_recently_changed_first_filters_and_owner_scope(self):
        first = self.make("Beach trip", [self.photos[0].pk])
        second = self.make("Birthday")
        self.make("Secret", client=self.api(self.stranger))
        listing = self.client.get(reverse("albums:album-list")).json()
        self.assertEqual([row["title"] for row in listing["results"]], ["Birthday", "Beach trip"])
        self.client.post(reverse("albums:album-images", kwargs={"pk": first["id"]}), {"image_ids": [self.photos[1].pk]}, format="json")
        self.assertEqual([row["title"] for row in self.client.get(reverse("albums:album-list")).json()["results"]], ["Beach trip", "Birthday"])      # adding moved it up
        self.assertEqual([row["id"] for row in self.client.get(reverse("albums:album-list"), {"q": "birth"}).json()["results"]], [second["id"]])
        self.assertEqual([row["id"] for row in self.client.get(reverse("albums:album-list"), {"image": self.photos[1].pk}).json()["results"]], [first["id"]])
        self.assertEqual(self.client.get(reverse("albums:album-list"), {"image": "x"}).status_code, 400)

    def test_the_list_is_paginated(self):
        for index in range(5):
            self.make(f"Album {index}")
        body = self.client.get(reverse("albums:album-list"), {"page_size": 2}).json()
        self.assertEqual((body["count"], len(body["results"])), (5, 2))
        self.assertIsNotNone(body["next"])


class DetailTests(AlbumBase):
    def test_get_rename_describe_and_delete(self):
        album = self.make("Old title", [self.photos[0].pk])
        url = reverse("albums:album-detail", kwargs={"pk": album["id"]})
        self.assertEqual(self.client.get(url).json()["photo_count"], 1)
        renamed = self.client.patch(url, {"title": "New title", "description": "About it"}, format="json")
        self.assertEqual((renamed.status_code, renamed.json()["title"], renamed.json()["description"]), (200, "New title", "About it"))
        only_description = self.client.patch(url, {"description": ""}, format="json").json()
        self.assertEqual((only_description["title"], only_description["description"]), ("New title", ""))
        self.assertEqual(self.client.patch(url, {"title": "  "}, format="json").status_code, 400)
        self.assertEqual(self.client.put(url, {"title": "x"}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertTrue(ImageUpload.objects.filter(pk=self.photos[0].pk).exists())                        # the photograph was not deleted
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_nothing_of_another_persons_album_is_reachable(self):
        theirs = self.make("Private", client=self.api(self.stranger))
        for name, method, kwargs in (("albums:album-detail", "get", {}), ("albums:album-detail", "patch", {"data": {"title": "x"}}), ("albums:album-detail", "delete", {}),
                                    ("albums:album-images", "get", {}), ("albums:album-images", "post", {"data": {"image_ids": [1]}}),
                                    ("albums:album-images-remove", "post", {"data": {"image_ids": [1]}}), ("albums:album-cover", "put", {"data": {"image_id": 1}}), ("albums:album-cover", "delete", {})):
            with self.subTest(endpoint=name, method=method):
                response = getattr(self.client, method)(reverse(name, kwargs={"pk": theirs["id"]}), format="json", **kwargs)
                self.assertEqual(response.status_code, 404)
        self.assertEqual(APIClient().get(reverse("albums:album-list")).status_code, 401)


class PhotographTests(AlbumBase):
    def test_adding_is_idempotent_and_reports_each_kind_of_id(self):
        album = self.make("Trip", [self.photos[0].pk])
        url = reverse("albums:album-images", kwargs={"pk": album["id"]})
        body = self.client.post(url, {"image_ids": [self.photos[0].pk, self.photos[1].pk, self.theirs.pk, self.photos[1].pk]}, format="json").json()
        self.assertEqual((body["added_ids"], body["already_in_album_ids"], body["not_found_ids"], body["photo_count"]), ([self.photos[1].pk], [self.photos[0].pk], [self.theirs.pk], 2))
        again = self.client.post(url, {"image_ids": [self.photos[1].pk]}, format="json").json()
        self.assertEqual((again["added_ids"], again["photo_count"]), ([], 2))
        self.assertEqual(AlbumImage.objects.filter(album_id=album["id"]).count(), 2)

    def test_the_request_is_validated(self):
        album = self.make("Trip")
        url = reverse("albums:album-images", kwargs={"pk": album["id"]})
        for body in ({}, {"image_ids": []}, {"image_ids": ["x"]}, {"image_ids": list(range(1, 202))}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(url, body, format="json").status_code, 400)

    @override_settings(ALBUM_MAX_IMAGES=3)
    def test_an_album_holds_a_limited_number_of_photographs(self):
        album = self.make("Full", [p.pk for p in self.photos[:2]])
        url = reverse("albums:album-images", kwargs={"pk": album["id"]})
        response = self.client.post(url, {"image_ids": [p.pk for p in self.photos[2:5]]}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("room for 1 more", str(response.json()))
        self.assertEqual(AlbumImage.objects.filter(album_id=album["id"]).count(), 2)                      # nothing was half-added
        self.assertEqual(self.client.post(url, {"image_ids": [self.photos[2].pk]}, format="json").status_code, 200)

    def test_the_photographs_are_listed_newest_added_first_as_gallery_rows_and_paginated(self):
        album = self.make("Trip", [self.photos[0].pk])
        for photo in self.photos[1:4]:
            self.client.post(reverse("albums:album-images", kwargs={"pk": album["id"]}), {"image_ids": [photo.pk]}, format="json")
        self.assertEqual(self.ids(album), [self.photos[3].pk, self.photos[2].pk, self.photos[1].pk, self.photos[0].pk])
        body = self.client.get(reverse("albums:album-images", kwargs={"pk": album["id"]}), {"page_size": 2}).json()
        self.assertEqual((body["count"], len(body["results"])), (4, 2))
        row = body["results"][0]
        for key in ("id", "original_name", "display_status", "thumbnail_url", "is_favorite", "quality_decision"):
            self.assertIn(key, row)

    def test_remove_reports_and_never_deletes_the_photograph(self):
        album = self.make("Trip", [p.pk for p in self.photos[:3]])
        url = reverse("albums:album-images-remove", kwargs={"pk": album["id"]})
        body = self.client.post(url, {"image_ids": [self.photos[1].pk, self.photos[5].pk]}, format="json").json()
        self.assertEqual((body["removed_ids"], body["not_in_album_ids"], body["photo_count"]), ([self.photos[1].pk], [self.photos[5].pk], 2))
        self.assertTrue(ImageUpload.objects.filter(pk=self.photos[1].pk).exists())
        self.assertEqual(set(self.ids(album)), {self.photos[0].pk, self.photos[2].pk})

    def test_the_cover_is_the_first_added_until_one_is_chosen_and_goes_back_when_that_photograph_leaves(self):
        album = self.make("Trip", [self.photos[0].pk, self.photos[1].pk, self.photos[2].pk])
        cover = reverse("albums:album-cover", kwargs={"pk": album["id"]})
        self.assertEqual(self.client.put(cover, {"image_id": self.photos[2].pk}, format="json").json()["cover_image"], self.photos[2].pk)
        self.assertEqual(self.client.put(cover, {"image_id": self.photos[5].pk}, format="json").status_code, 400)              # not in the album
        self.assertEqual(self.client.put(cover, {}, format="json").status_code, 400)
        self.client.post(reverse("albums:album-images-remove", kwargs={"pk": album["id"]}), {"image_ids": [self.photos[2].pk]}, format="json")
        back = self.client.get(reverse("albums:album-detail", kwargs={"pk": album["id"]})).json()
        self.assertEqual(back["cover_image"], self.photos[0].pk)                                          # automatic again
        self.client.put(cover, {"image_id": self.photos[1].pk}, format="json")
        self.assertEqual(self.client.delete(cover).json()["cover_image"], self.photos[0].pk)

    def test_deleting_a_photograph_takes_it_out_of_its_albums_and_clears_a_cover_that_was_it(self):
        album = self.make("Trip", [self.photos[0].pk, self.photos[1].pk])
        self.client.put(reverse("albums:album-cover", kwargs={"pk": album["id"]}), {"image_id": self.photos[1].pk}, format="json")
        self.assertEqual(self.client.delete(reverse("image-upload-detail", kwargs={"pk": self.photos[1].pk})).status_code, 204)
        body = self.client.get(reverse("albums:album-detail", kwargs={"pk": album["id"]})).json()
        self.assertEqual((body["photo_count"], body["cover_image"]), (1, self.photos[0].pk))
        self.assertEqual(Album.objects.get(pk=album["id"]).cover_image_id, None)

    def test_the_same_photograph_can_be_in_several_albums(self):
        one, two = self.make("One", [self.photos[0].pk]), self.make("Two", [self.photos[0].pk])
        self.assertEqual({row["id"] for row in self.client.get(reverse("albums:album-list"), {"image": self.photos[0].pk}).json()["results"]}, {one["id"], two["id"]})


class GalleryAndSearchTests(AlbumBase):
    def test_the_gallery_can_be_filtered_by_album(self):
        album = self.make("Trip", [self.photos[0].pk, self.photos[1].pk])
        listing = lambda **params: {row["id"] for row in self.client.get(reverse("my-image-list"), params).json()["results"]}   # noqa: E731
        self.assertEqual(listing(album=album["id"]), {self.photos[0].pk, self.photos[1].pk})
        self.assertEqual(self.client.get(reverse("my-image-list"), {"album": "x"}).status_code, 400)
        theirs = self.make("Theirs", client=self.api(self.stranger))
        self.assertEqual(listing(album=theirs["id"]), set())                                              # another person's album shows nothing

    def test_search_finds_albums_by_title(self):
        album = self.make("Beach trip", [self.photos[0].pk])
        self.make("Private", client=self.api(self.stranger))
        body = self.client.get(reverse("search"), {"q": "beach"}).json()
        (found,) = body["albums"]
        self.assertEqual((found["id"], found["title"], found["photo_count"]), (album["id"], "Beach trip", 1))
        self.assertEqual(self.client.get(reverse("search"), {"q": "private"}).json()["albums"], [])
