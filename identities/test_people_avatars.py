"""People you can tap: avatars cut from the best face, the chips list, people in search, and the person filter of the gallery."""
import io
import tempfile

import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .avatars import MARGIN, face_score, square_around
from .models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix="facecloud-people-")
GRAY = (128, 128, 128)
RED, BLUE = (210, 30, 30), (30, 30, 210)


def picture(box, colour, size=(400, 500)):
    """A grey picture with a ``colour`` rectangle exactly where the face box is, as JPEG bytes."""
    image = Image.new("RGB", size, GRAY)
    x, y, w, h = box
    image.paste(colour, (x, y, x + w, y + h))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@override_settings(MEDIA_ROOT=MEDIA)
class PeopleBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="people-owner", email="people-owner@example.com", password="Password!123", is_email_verified=True)
        self.stranger = User.objects.create_user(username="people-other", email="people-other@example.com", password="Password!123", is_email_verified=True)
        self.batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple")
        self.client = self.api(self.owner)
        self.count = 0

    @staticmethod
    def api(user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        return client

    def group(self, name="Person 001", owner=None, state="active"):
        return IdentityGroup.objects.create(
            owner=owner or self.owner, display_name=name, state=state, member_count=0, embedding_dimension=3, provider="t", policy_version="v1", protected_centroid=b"x",
        )

    def face(self, group, *, box=(150, 100, 100, 120), colour=RED, confidence=0.9, brightness=120.0, state="confirmed", batch=None, standardized=True, image=None):
        """One photograph with one face of ``group``; the standardized picture has ``colour`` where the box is."""
        self.count += 1
        owner = group.owner
        if image is None:
            image = ImageUpload.objects.create(owner=owner, batch=batch or UploadBatch.objects.create(owner=owner, upload_type="single"), original_name=f"p-{self.count}.jpg", status="uploaded")
            image.original_file.save(f"p-{self.count}.jpg", ContentFile(b"original"))
            preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed", output_format="png")
            if standardized:
                preprocessing.processed_file.save(f"p-{self.count}.png", ContentFile(picture(box, colour)))
            FaceValidationResult.objects.create(owner=owner, image=image, preprocessing_result=preprocessing, decision="accepted", face_count=1, faces=[])
        analysis, _ = IdentityAnalysis.objects.get_or_create(
            image=image, defaults={"owner": owner, "preprocessing_result": image.preprocessing_result, "face_validation": image.face_validation, "status": "completed", "provider": "t", "policy_version": "v1"},
        )
        identity_face = IdentityFace.objects.create(
            owner=owner, image=image, analysis=analysis, face_index=analysis.faces.count(), bounding_box={"x": box[0], "y": box[1], "width": box[2], "height": box[3]},
            quality={"confidence": confidence, "brightness": brightness}, protected_embedding=b"\x00", embedding_dimension=3, provider="t",
        )
        IdentityMembership.objects.create(owner=owner, identity_face=identity_face, identity_group=group, state=state, decision_method="test")
        IdentityGroup.objects.filter(pk=group.pk).update(member_count=group.memberships.filter(state__in=["auto_matched", "new_group", "confirmed"]).count())
        return image

    def avatar(self, group, **params):
        return self.client.get(reverse("identities:group-avatar", kwargs={"pk": group.pk}), params)

    @staticmethod
    def pixels(response):
        return np.asarray(Image.open(io.BytesIO(response.content)).convert("RGB"), dtype=np.int16)


class AvatarTests(PeopleBase):
    def test_the_avatar_is_the_face_with_a_margin_not_the_whole_photograph(self):
        group = self.group()
        self.face(group, box=(150, 100, 100, 120), colour=RED)
        response = self.avatar(group)
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "image/jpeg"))
        self.assertEqual(response["Cache-Control"], "private, max-age=3600")
        pixels = self.pixels(response)
        self.assertEqual(pixels.shape, (96, 96, 3))
        centre, corner = pixels[48, 48], pixels[2, 2]
        self.assertTrue(centre[0] > 150 and centre[1] < 90 and centre[2] < 90, centre)           # the face (red) is in the middle
        self.assertTrue(all(abs(int(channel) - 128) < 25 for channel in corner), corner)        # the margin shows the grey around it
        red = ((pixels[..., 0] > 150) & (pixels[..., 1] < 90)).mean()
        expected = (100 * 120) / (120 * (1 + 2 * MARGIN)) ** 2                                  # the face's share of the square
        self.assertAlmostEqual(red, expected, delta=0.04)

    def test_the_sizes(self):
        group = self.group()
        self.face(group)
        for size in (96, 192, 384):
            with self.subTest(size=size):
                self.assertEqual(self.pixels(self.avatar(group, size=size)).shape[:2], (size, size))
        for bad in ("100", "0", "abc", "-96"):
            with self.subTest(bad=bad):
                self.assertEqual(self.avatar(group, size=bad).status_code, 400)

    def test_the_best_face_is_chosen_bright_enough_first_then_confidence_then_size(self):
        group = self.group()
        self.face(group, colour=RED, confidence=0.80)
        self.face(group, colour=BLUE, confidence=0.97)
        self.assertGreater(self.pixels(self.avatar(group))[48, 48][2], 150)                    # the 0.97 face (blue)
        dark_but_sure = self.group(name="Person 002")
        self.face(dark_but_sure, colour=RED, confidence=0.99, brightness=20.0)
        self.face(dark_but_sure, colour=BLUE, confidence=0.70, brightness=130.0)
        self.assertGreater(self.pixels(self.avatar(dark_but_sure))[48, 48][2], 150)             # a bright face beats a dark one even if the dark one is surer

    def test_only_faces_that_are_placed_in_the_folder_count(self):
        group = self.group()
        self.face(group, colour=RED, confidence=0.9, state="confirmed")
        self.face(group, colour=BLUE, confidence=0.99, state="needs_review")                     # only suggested: not part of the folder
        self.face(group, colour=BLUE, confidence=0.99, state="removed")
        self.assertGreater(self.pixels(self.avatar(group))[48, 48][0], 150)

    def test_not_found_cases_and_ownership(self):
        empty = self.group(name="Person 009")
        self.assertEqual(self.avatar(empty).status_code, 404)
        no_picture = self.group(name="Person 010")
        self.face(no_picture, standardized=False)
        self.assertEqual(self.avatar(no_picture).status_code, 404)
        theirs = self.group(name="Theirs", owner=self.stranger)
        self.face(theirs)
        self.assertEqual(self.avatar(theirs).status_code, 404)
        self.assertEqual(APIClient().get(reverse("identities:group-avatar", kwargs={"pk": empty.pk})).status_code, 401)

    def test_the_folder_says_where_its_avatar_is_or_that_it_has_none(self):
        group, empty = self.group(), self.group(name="Person 002")
        self.face(group)
        body = {row["id"]: row for row in self.client.get(reverse("identities:group-list")).json()}
        self.assertTrue(body[group.pk]["avatar_url"].endswith(f"/api/identities/groups/{group.pk}/avatar/"))
        self.assertIsNone(body[empty.pk]["avatar_url"])
        detail = self.client.get(reverse("identities:group-detail", kwargs={"pk": group.pk})).json()
        self.assertTrue(detail["avatar_url"])


class SquareTests(SimpleTestCase):
    def test_the_square_has_the_margin_and_stays_inside_the_picture(self):
        left, top, side = square_around((150, 100, 100, 120), 400, 500)
        self.assertEqual(side, round(120 * (1 + 2 * MARGIN)))
        self.assertEqual((left, top), (round(200 - side / 2), round(160 - side / 2)))
        self.assertEqual(square_around((0, 0, 100, 100), 400, 500)[:2], (0, 0))                 # pushed back inside at the corner
        self.assertEqual(square_around((350, 450, 50, 50), 400, 500)[:2], (400 - square_around((350, 450, 50, 50), 400, 500)[2], 500 - square_around((350, 450, 50, 50), 400, 500)[2]))

    def test_a_box_bigger_than_the_picture_gives_the_largest_square_that_fits(self):
        self.assertEqual(square_around((0, 0, 400, 500), 400, 500), (0, 50, 400))                  # centred on the box, as large as the picture allows

    def test_the_score_order(self):
        class Face:
            def __init__(self, **quality):
                self.quality = quality
                self.bounding_box = {"x": 0, "y": 0, "width": 50, "height": 50}

        self.assertGreater(face_score(Face(confidence=0.7, brightness=100)), face_score(Face(confidence=0.99, brightness=10)))
        self.assertGreater(face_score(Face(confidence=0.9)), face_score(Face(confidence=0.8)))


class PeopleListTests(PeopleBase):
    def people(self, **params):
        response = self.client.get(reverse("identities:people"), params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_most_photographs_first_with_the_fields_a_chip_needs(self):
        few, many, named = self.group("Person 001"), self.group("Person 002"), self.group("Ayesha")
        self.face(few)
        for _ in range(3):
            self.face(many)
        for _ in range(2):
            self.face(named)
        rows = self.people()
        self.assertEqual([row["display_name"] for row in rows], ["Person 002", "Ayesha", "Person 001"])
        self.assertEqual([row["photo_count"] for row in rows], [3, 2, 1])
        self.assertEqual([row["is_named"] for row in rows], [False, True, False])
        self.assertEqual(set(rows[0]), {"id", "display_name", "is_named", "photo_count", "avatar_url"})
        self.assertTrue(rows[0]["avatar_url"].endswith(f"/api/identities/groups/{many.pk}/avatar/"))

    def test_the_photo_count_is_distinct_photographs_of_placed_faces_only(self):
        group = self.group("Bilal")
        image = self.face(group)
        self.face(group, image=image, box=(10, 10, 40, 40))                                      # a second face of the same person in the same photograph
        self.face(group, state="needs_review")
        self.face(group, state="removed")
        self.assertEqual(self.people()[0]["photo_count"], 1 + 0)                                 # one photograph (the review and the removed ones do not count)

    def test_folders_without_photographs_and_inactive_ones_are_not_people(self):
        self.group("Empty")
        merged = self.group("Merged", state="merged")
        self.face(merged)
        archived = self.group("Archived", state="archived")
        self.face(archived)
        theirs = self.group("Theirs", owner=self.stranger)
        self.face(theirs)
        self.assertEqual(self.people(), [])

    def test_name_filter_limit_and_unnamed(self):
        for name in ("Ayesha", "Ayaan", "Bilal", "Person 003"):
            self.face(self.group(name))
        self.assertEqual({row["display_name"] for row in self.people(q="ay")}, {"Ayesha", "Ayaan"})
        self.assertEqual(len(self.people(limit=2)), 2)
        self.assertEqual({row["display_name"] for row in self.people(include_unnamed="false")}, {"Ayesha", "Ayaan", "Bilal"})
        for params in ({"limit": "0"}, {"limit": "x"}, {"include_unnamed": "maybe"}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(reverse("identities:people"), params).status_code, 400)
        self.assertEqual(len(self.people(limit=1000)), 4)                                         # capped, not refused

    def test_it_needs_a_signed_in_person(self):
        self.assertEqual(APIClient().get(reverse("identities:people")).status_code, 401)


class SearchAndFilterTests(PeopleBase):
    def test_search_returns_people_next_to_the_old_groups(self):
        group = self.group("Ayesha")
        self.face(group)
        self.face(group)
        body = self.client.get(reverse("search"), {"q": "ayes"}).json()
        self.assertEqual([row["id"] for row in body["groups"]], [group.pk])                       # unchanged
        (person,) = body["people"]
        self.assertEqual((person["id"], person["display_name"], person["photo_count"], person["is_named"]), (group.pk, "Ayesha", 2, True))
        self.assertTrue(person["avatar_url"])

    def test_search_does_not_list_an_empty_folder_as_a_person(self):
        self.group("Ayesha")
        body = self.client.get(reverse("search"), {"q": "ayes"}).json()
        self.assertEqual((len(body["groups"]), body["people"]), (1, []))

    def test_the_person_filter_returns_that_persons_photographs_and_several_means_all_of_them(self):
        ayesha, bilal = self.group("Ayesha"), self.group("Bilal")
        both = self.face(ayesha)
        self.face(bilal, image=both, box=(10, 10, 60, 60))
        only_ayesha = self.face(ayesha)
        only_bilal = self.face(bilal)

        def ids(**params):
            return {row["id"] for row in self.client.get(reverse("my-image-list"), params).json()["results"]}

        self.assertEqual(ids(person=ayesha.pk), {both.pk, only_ayesha.pk})
        self.assertEqual(ids(person=bilal.pk), {both.pk, only_bilal.pk})
        response = self.client.get(f"{reverse('my-image-list')}?person={ayesha.pk}&person={bilal.pk}")
        self.assertEqual({row["id"] for row in response.json()["results"]}, {both.pk})

    def test_the_person_filter_only_sees_placed_faces_and_never_another_owners_folder(self):
        group = self.group("Ayesha")
        suggested = self.face(group, state="needs_review")
        placed = self.face(group)
        theirs = self.group("Theirs", owner=self.stranger)
        self.face(theirs)
        ids = {row["id"] for row in self.client.get(reverse("my-image-list"), {"person": group.pk}).json()["results"]}
        self.assertEqual(ids, {placed.pk})
        self.assertNotIn(suggested.pk, ids)
        self.assertEqual(self.client.get(reverse("my-image-list"), {"person": theirs.pk}).json()["results"], [])
        self.assertEqual(self.client.get(reverse("my-image-list"), {"person": "abc"}).status_code, 400)
