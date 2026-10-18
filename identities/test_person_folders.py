"""Person Folders: cover images, matching confidence and the paginated member photos."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership
from .storage import protect_embedding

User = get_user_model()
State = IdentityMembership.State


class FolderFixtures:
    """Users, groups and member photos built straight in the database."""

    def make_user(self, name):
        return User.objects.create_user(
            username=name, email=f"{name}@example.com", password="ForestRiver!4821", is_email_verified=True,
        )

    def make_group(self, owner, name="Person 001", state=IdentityGroup.State.ACTIVE):
        return IdentityGroup.objects.create(
            owner=owner, display_name=name, state=state, member_count=0,
            protected_centroid=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3,
            provider="test", policy_version="test-v1",
        )

    def add_photo(self, group, name="member.jpg", score=0.9, state=State.CONFIRMED, stored=True, size=1234):
        owner = group.owner
        batch = UploadBatch.objects.create(owner=owner, upload_type="single")
        image = ImageUpload.objects.create(
            owner=owner, batch=batch, original_name=name, status="uploaded", file_size=size,
            width=640, height=480, original_file=f"users/{owner.pk}/originals/{name}" if stored else "",
        )
        preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
        validation = FaceValidationResult.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing, decision="accepted", face_count=1,
        )
        analysis = IdentityAnalysis.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing, face_validation=validation,
            status="completed", provider="test", policy_version="test-v1",
        )
        face = IdentityFace.objects.create(
            owner=owner, image=image, analysis=analysis, face_index=0,
            protected_embedding=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3, provider="test",
        )
        membership = IdentityMembership.objects.create(
            owner=owner, identity_face=face, identity_group=group, state=state, similarity_score=score,
        )
        IdentityGroup.objects.filter(pk=group.pk).update(member_count=group.memberships.exclude(
            state__in=[State.REJECTED, State.REMOVED]).count())
        return image, membership


class GroupSummaryFieldTests(FolderFixtures, TestCase):
    def setUp(self):
        self.owner = self.make_user("folder-owner")
        self.other = self.make_user("folder-other")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def listed(self):
        return self.client.get(reverse("identities:group-list")).json()

    def test_cover_images_are_at_most_four_with_thumbnail_urls(self):
        group = self.make_group(self.owner)
        images = [self.add_photo(group, f"p{index}.jpg")[0] for index in range(6)]
        body = self.listed()
        covers = body[0]["cover_images"]
        self.assertEqual(len(covers), 4)
        self.assertEqual(set(covers[0]), {"id", "thumbnail_url"})
        for cover in covers:
            self.assertTrue(cover["thumbnail_url"].endswith(f"/api/images/{cover['id']}/thumbnail/"))
            self.assertIn(cover["id"], [image.pk for image in images])
        self.assertEqual(body[0]["preview_image"], covers[0]["id"])

    def test_a_photo_with_two_faces_of_the_person_is_one_cover(self):
        group = self.make_group(self.owner)
        image, _ = self.add_photo(group)
        second_face = IdentityFace.objects.create(
            owner=self.owner, image=image, analysis=image.identity_analysis, face_index=1,
            protected_embedding=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3, provider="test",
        )
        IdentityMembership.objects.create(
            owner=self.owner, identity_face=second_face, identity_group=group, state=State.CONFIRMED, similarity_score=0.8,
        )
        self.assertEqual(len(self.listed()[0]["cover_images"]), 1)

    def test_rejected_and_removed_members_do_not_appear_or_count_towards_confidence(self):
        group = self.make_group(self.owner)
        self.add_photo(group, "kept.jpg", score=0.9)
        self.add_photo(group, "removed.jpg", score=0.1, state=State.REMOVED)
        self.add_photo(group, "rejected.jpg", score=0.1, state=State.REJECTED)
        item = self.listed()[0]
        self.assertEqual(len(item["cover_images"]), 1)
        self.assertEqual(item["matching_confidence"], 90.0)

    def test_matching_confidence_is_the_average_member_score_as_a_percentage(self):
        group = self.make_group(self.owner)
        self.add_photo(group, "a.jpg", score=0.9)
        self.add_photo(group, "b.jpg", score=0.8)
        self.add_photo(group, "unscored.jpg", score=None)
        self.assertEqual(self.listed()[0]["matching_confidence"], 85.0)

    def test_matching_confidence_is_null_not_invented_when_there_is_no_score(self):
        empty = self.make_group(self.owner, "Empty")
        unscored = self.make_group(self.owner, "Unscored")
        self.add_photo(unscored, score=None)
        by_name = {item["display_name"]: item for item in self.listed()}
        self.assertIsNone(by_name["Empty"]["matching_confidence"])
        self.assertEqual(by_name["Empty"]["cover_images"], [])
        self.assertIsNone(by_name["Unscored"]["matching_confidence"])
        self.assertEqual(empty.state, IdentityGroup.State.ACTIVE)

    def test_the_detail_response_carries_the_same_fields_and_keeps_memberships(self):
        group = self.make_group(self.owner)
        self.add_photo(group, score=0.75)
        body = self.client.get(reverse("identities:group-detail", args=[group.pk])).json()
        self.assertEqual(body["matching_confidence"], 75.0)
        self.assertEqual(len(body["cover_images"]), 1)
        self.assertEqual(len(body["memberships"]), 1)
        for key in ("id", "display_name", "state", "member_count", "preview_image", "created_at", "updated_at"):
            self.assertIn(key, body)

    def test_the_list_costs_the_same_number_of_queries_for_one_group_or_many(self):
        group = self.make_group(self.owner)
        self.add_photo(group)
        with self.assertNumQueries(2):
            self.listed()
        for index in range(4):
            extra = self.make_group(self.owner, f"Person {index + 2}")
            self.add_photo(extra, f"x{index}.jpg")
        with self.assertNumQueries(2):
            self.listed()

    def test_another_users_groups_are_not_listed_and_their_detail_is_404(self):
        theirs = self.make_group(self.other, "Theirs")
        self.add_photo(theirs)
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.client.get(reverse("identities:group-detail", args=[theirs.pk])).status_code, 404)


class GroupImagesTests(FolderFixtures, TestCase):
    def setUp(self):
        self.owner = self.make_user("images-owner")
        self.other = self.make_user("images-other")
        self.group = self.make_group(self.owner)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.url = reverse("identities:group-images", args=[self.group.pk])

    def test_requires_authentication(self):
        self.assertEqual(APIClient().get(self.url).status_code, 401)

    def test_each_photo_has_the_documented_fields(self):
        image, _ = self.add_photo(self.group, "face.jpg", score=0.93, size=4321)
        body = self.client.get(self.url).json()
        self.assertEqual(body["count"], 1)
        item = body["results"][0]
        self.assertEqual(
            set(item),
            {"id", "original_name", "thumbnail_url", "file_size", "width", "height", "display_status", "matching_confidence",
             "membership_id", "membership_state"},
        )
        self.assertEqual(item["id"], image.pk)
        self.assertEqual(item["original_name"], "face.jpg")
        self.assertTrue(item["thumbnail_url"].endswith(f"/api/images/{image.pk}/thumbnail/"))
        self.assertEqual((item["file_size"], item["width"], item["height"]), (4321, 640, 480))
        self.assertEqual(item["matching_confidence"], 93.0)

    def test_each_photo_carries_the_membership_that_move_remove_and_cover_actions_take(self):
        image, membership = self.add_photo(self.group, state=State.NEEDS_REVIEW)
        item = self.client.get(self.url).json()["results"][0]
        self.assertEqual((item["id"], item["membership_id"], item["membership_state"]), (image.pk, membership.pk, "needs_review"))

    def test_with_two_faces_of_one_photo_in_the_folder_the_better_match_is_named(self):
        image, first = self.add_photo(self.group, score=0.7)
        second_face = IdentityFace.objects.create(
            owner=self.owner, image=image, analysis=first.identity_face.analysis, face_index=1,
            protected_embedding=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3, provider="test",
        )
        better = IdentityMembership.objects.create(
            owner=self.owner, identity_face=second_face, identity_group=self.group, state=State.CONFIRMED, similarity_score=0.95,
        )
        body = self.client.get(self.url).json()
        self.assertEqual(body["count"], 1)                                  # one row per photo
        self.assertEqual((body["results"][0]["membership_id"], body["results"][0]["matching_confidence"]), (better.pk, 95.0))

    def test_the_id_works_with_the_actions_that_need_it(self):
        _, membership = self.add_photo(self.group)
        other_group = self.make_group(self.owner, "Person 002")
        item = self.client.get(self.url).json()["results"][0]
        moved = self.client.patch(
            reverse("identities:membership-move", args=[item["membership_id"]]), {"target_group_id": other_group.pk}, format="json",
        )
        self.assertEqual(moved.status_code, 200, moved.content)
        self.assertEqual(IdentityMembership.objects.get(pk=membership.pk).identity_group_id, other_group.pk)

    def test_display_status_is_the_value_the_images_list_reports(self):
        image, _ = self.add_photo(self.group)
        mine = self.client.get(reverse("my-image-list")).json()
        mine_status = {row["id"]: row["display_status"] for row in mine["results"]}
        theirs = self.client.get(self.url).json()["results"][0]["display_status"]
        self.assertEqual(theirs, mine_status[image.pk])

    def test_pagination(self):
        for index in range(5):
            self.add_photo(self.group, f"p{index}.jpg")
        first = self.client.get(self.url, {"page_size": 2}).json()
        self.assertEqual((first["count"], len(first["results"])), (5, 2))
        self.assertIsNotNone(first["next"])
        last = self.client.get(self.url, {"page_size": 2, "page": 3}).json()
        self.assertEqual(len(last["results"]), 1)
        self.assertIsNone(last["next"])
        listed = [row["id"] for page in (1, 2, 3) for row in self.client.get(self.url, {"page_size": 2, "page": page}).json()["results"]]
        self.assertEqual(len(set(listed)), 5)

    def test_an_empty_folder_is_an_empty_page(self):
        body = self.client.get(self.url).json()
        self.assertEqual((body["count"], body["results"]), (0, []))

    def test_removed_and_rejected_members_are_not_listed(self):
        self.add_photo(self.group, "kept.jpg")
        self.add_photo(self.group, "removed.jpg", state=State.REMOVED)
        self.add_photo(self.group, "rejected.jpg", state=State.REJECTED)
        names = [row["original_name"] for row in self.client.get(self.url).json()["results"]]
        self.assertEqual(names, ["kept.jpg"])

    def test_a_photo_with_two_faces_is_listed_once_with_its_best_score(self):
        image, _ = self.add_photo(self.group, score=0.6)
        face = IdentityFace.objects.create(
            owner=self.owner, image=image, analysis=image.identity_analysis, face_index=1,
            protected_embedding=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3, provider="test",
        )
        IdentityMembership.objects.create(
            owner=self.owner, identity_face=face, identity_group=self.group, state=State.CONFIRMED, similarity_score=0.95,
        )
        rows = self.client.get(self.url).json()["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["matching_confidence"], 95.0)

    def test_another_users_folder_is_404(self):
        theirs = self.make_group(self.other, "Theirs")
        self.add_photo(theirs)
        response = self.client.get(reverse("identities:group-images", args=[theirs.pk]))
        self.assertEqual(response.status_code, 404)

    def test_the_folders_photos_never_leak_into_another_folder(self):
        second = self.make_group(self.owner, "Second")
        self.add_photo(self.group, "in-first.jpg")
        self.add_photo(second, "in-second.jpg")
        names = [row["original_name"] for row in self.client.get(self.url).json()["results"]]
        self.assertEqual(names, ["in-first.jpg"])
