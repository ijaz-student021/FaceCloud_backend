"""The trash: delete with a way back. Moving, restoring, purging, and what each does to folders, packages, albums, files and the orphan-file sweep."""
import os
import tempfile
from datetime import timedelta
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from albums.models import Album, AlbumImage
from downloads.models import DownloadPackage
from enhancement.models import AutoEnhancement, EnhancementLevel, ImageEnhancementResult
from identities.models import IdentityChange, IdentityGroup, IdentityMembership
from identities.test_person_folders import FolderFixtures
from organizations.models import OrganizationMembership
from organizations.services import create_organization, create_personal_organization
from privacy.cleanup import orphan_media_files, referenced_media_names
from quality_control.models import ImageQualityDecision

from .models import ImagePreprocessingResult, ImageUpload, TrashEntry, UploadBatch
from .tasks import purge_trash_task
from .trash import blocked_reason, days_left, move_to_trash, purge_expired, restore

State = IdentityMembership.State
MEDIA = tempfile.mkdtemp(prefix="facecloud-trash-")


@override_settings(MEDIA_ROOT=MEDIA)
class TrashBase(FolderFixtures, TestCase):
    def setUp(self):
        self.owner = self.make_user("trash-owner")
        self.other = self.make_user("trash-other")
        self.group = self.make_group(self.owner, "Person 001")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.url = reverse("image-trash")

    def photo(self, name="t.jpg", group=None, verdict="accepted", **kwargs):
        """A settled photograph: checked, accepted by default, in a folder."""
        image, membership = self.add_photo(group or self.group, name, **kwargs)
        if verdict:
            ImageQualityDecision.objects.create(
                owner=image.owner, image=image, preprocessing_result=image.preprocessing_result, status="completed", decision=verdict, overall_score=70,
            )
        return image

    def move(self, *images, client=None):
        return (client or self.client).post(self.url, {"image_ids": [image.pk for image in images]}, format="json")

    def trashed(self, image):
        return ImageUpload.all_objects.get(pk=image.pk).trashed_at is not None


class MoveAndRestoreTests(TrashBase):
    def test_a_moved_photograph_leaves_every_listing_and_the_trash_lists_it_with_the_days_left(self):
        keep, gone = self.photo("keep.jpg"), self.photo("gone.jpg")
        response = self.move(gone)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"trashed_ids": [gone.pk], "skipped": [], "not_found_ids": [], "retention_days": 30})
        listed = [row["id"] for row in self.client.get(reverse("my-image-list")).json()["results"]]
        self.assertEqual(listed, [keep.pk])
        self.assertFalse(ImageUpload.objects.filter(pk=gone.pk).exists())
        self.assertTrue(ImageUpload.all_objects.filter(pk=gone.pk).exists())
        rows = self.client.get(self.url).json()["results"]
        self.assertEqual([row["id"] for row in rows], [gone.pk])
        row = rows[0]
        self.assertEqual((row["original_name"], row["days_left"]), ("gone.jpg", 30))
        self.assertIsNotNone(row["trashed_at"])
        self.assertIsNotNone(row["purge_at"])
        self.assertTrue(row["thumbnail_url"].endswith(f"/api/images/trash/{gone.pk}/thumbnail/"))

    def test_the_photograph_cannot_be_opened_by_its_ordinary_addresses_while_in_the_trash(self):
        image = self.photo()
        self.move(image)
        for name in ("image-upload-detail", "image-upload-download", "image-thumbnail", "image-preprocessing-detail"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, args=[image.pk])).status_code, 404)
        self.assertEqual(self.client.put(reverse("image-favorite", args=[image.pk])).status_code, 404)

    def test_restoring_brings_it_back_with_its_favourite_flag_and_album_place(self):
        image = self.photo()
        ImageUpload.objects.filter(pk=image.pk).update(is_favorite=True)
        album = Album.objects.create(owner=self.owner, title="Trip")
        AlbumImage.objects.create(album=album, image=image)
        self.move(image)
        response = self.client.post(reverse("image-trash-restore"), {"image_ids": [image.pk]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["restored_ids"], [image.pk])
        again = ImageUpload.objects.get(pk=image.pk)
        self.assertEqual((again.trashed_at, again.is_favorite), (None, True))
        self.assertTrue(AlbumImage.objects.filter(album=album, image=image).exists())
        self.assertFalse(TrashEntry.objects.filter(image=image).exists())
        self.assertEqual(self.client.get(self.url).json()["results"], [])

    def test_only_your_own_stored_photographs_that_are_not_already_in_the_trash(self):
        mine, rejected = self.photo(), self.photo("rej.jpg")
        ImageUpload.objects.filter(pk=rejected.pk).update(status="rejected")
        theirs = self.photo("theirs.jpg", group=self.make_group(self.other, "Theirs"))
        self.move(mine)
        response = self.move(mine, rejected, theirs).json()
        self.assertEqual(response["trashed_ids"], [])
        self.assertEqual(response["not_found_ids"], [mine.pk, rejected.pk, theirs.pk])
        self.assertFalse(self.trashed(theirs))
        restoring = self.client.post(reverse("image-trash-restore"), {"image_ids": [rejected.pk, theirs.pk, 999999]}, format="json").json()
        self.assertEqual((restoring["restored_ids"], restoring["not_found_ids"]), ([], [rejected.pk, theirs.pk, 999999]))

    def test_the_request_is_validated_and_needs_a_sign_in(self):
        for body in ({}, {"image_ids": []}, {"image_ids": ["a"]}, {"image_ids": [0]}, {"image_ids": list(range(1, 202))}):
            self.assertEqual(self.client.post(self.url, body, format="json").status_code, 400, body)
            self.assertEqual(self.client.post(reverse("image-trash-restore"), body, format="json").status_code, 400, body)
        self.assertEqual(APIClient().post(self.url, {"image_ids": [1]}, format="json").status_code, 401)
        self.assertEqual(APIClient().get(self.url).status_code, 401)

    def test_the_trash_list_is_yours_newest_first_and_searchable(self):
        first, second = self.photo("alpha.jpg"), self.photo("beta.jpg")
        stranger = self.photo("alpha-theirs.jpg", group=self.make_group(self.other, "Theirs"))
        self.move(first)
        self.move(second)
        ImageUpload.all_objects.filter(pk=stranger.pk).update(trashed_at=timezone.now())
        self.assertEqual([row["id"] for row in self.client.get(self.url).json()["results"]], [second.pk, first.pk])
        self.assertEqual([row["id"] for row in self.client.get(self.url, {"search": "alp"}).json()["results"]], [first.pk])

    def test_days_left_counts_down_and_never_goes_below_zero(self):
        image = self.photo()
        self.move(image)
        image = ImageUpload.all_objects.get(pk=image.pk)
        now = image.trashed_at
        self.assertEqual(days_left(image, now), 30)
        self.assertEqual(days_left(image, now + timedelta(days=10, hours=1)), 20)
        self.assertEqual(days_left(image, now + timedelta(days=45)), 0)
        self.assertIsNone(days_left(self.photo("not-trashed.jpg")))


class SkippedTests(TrashBase):
    def test_a_photograph_still_being_checked_is_skipped_and_can_be_moved_once_it_is_settled(self):
        image = self.photo(verdict=None)                                                   # the checks have not produced a verdict yet
        body = self.move(image).json()
        self.assertEqual((body["trashed_ids"], body["skipped"]), ([], [{"id": image.pk, "reason": "still_processing"}]))
        self.assertFalse(self.trashed(image))
        ImageQualityDecision.objects.create(
            owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, status="processing", decision="", overall_score=None,
        )
        self.assertEqual(self.move(image).json()["skipped"][0]["reason"], "still_processing")
        ImageQualityDecision.objects.filter(image=image).update(status="completed", decision="accepted", overall_score=70)
        self.assertEqual(self.move(image).json()["trashed_ids"], [image.pk])

    def test_a_photograph_whose_enhancement_is_waiting_or_running_is_skipped(self):
        image = self.photo()
        record = AutoEnhancement.objects.create(image=image, owner=self.owner, batch=image.batch, state=AutoEnhancement.State.WAITING)
        self.assertEqual(blocked_reason(ImageUpload.objects.get(pk=image.pk)), "still_processing")
        record.state = AutoEnhancement.State.SKIPPED
        record.save()
        self.assertIsNone(blocked_reason(ImageUpload.objects.get(pk=image.pk)))
        result = ImageEnhancementResult.objects.create(
            owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, status="processing", requested_level=EnhancementLevel.AUTO,
        )
        self.assertEqual(blocked_reason(ImageUpload.objects.get(pk=image.pk)), "still_processing")
        result.status = "completed"
        result.save()
        self.assertIsNone(blocked_reason(ImageUpload.objects.get(pk=image.pk)))

    def test_a_photograph_waiting_for_a_moderator_is_skipped_but_a_personal_one_is_not(self):
        team = create_organization(name="Trash Co", owner=self.make_user("trash-boss"))
        OrganizationMembership.objects.create(organization=team, user=self.owner, role="member", status="active")
        personal = create_personal_organization(self.owner)
        reviewed, kept = self.photo("review.jpg"), self.photo("personal.jpg")
        UploadBatch.objects.filter(pk=reviewed.batch_id).update(organization=team)
        UploadBatch.objects.filter(pk=kept.batch_id).update(organization=personal)
        ImageQualityDecision.objects.filter(image__in=[reviewed, kept]).update(decision="needs_review", overall_score=60)
        body = self.move(reviewed, kept).json()
        self.assertEqual(body["trashed_ids"], [kept.pk])
        self.assertEqual(body["skipped"], [{"id": reviewed.pk, "reason": "in_review"}])

    def test_the_photographs_that_can_be_moved_are_moved_even_when_another_is_skipped(self):
        busy, free = self.photo("busy.jpg", verdict=None), self.photo("free.jpg")
        body = self.move(busy, free).json()
        self.assertEqual((body["trashed_ids"], [item["id"] for item in body["skipped"]]), ([free.pk], [busy.pk]))


class FolderTests(TrashBase):
    def test_moving_takes_the_photograph_out_of_its_folder_and_restoring_puts_it_back(self):
        first, second = self.photo("a.jpg"), self.photo("b.jpg")
        self.group.refresh_from_db()
        self.assertEqual(self.group.member_count, 2)
        self.move(first)
        self.group.refresh_from_db()
        self.assertEqual(self.group.member_count, 1)
        self.assertEqual(IdentityMembership.objects.get(identity_face__image=first).state, State.REMOVED)
        self.assertEqual(IdentityMembership.objects.get(identity_face__image=second).state, State.CONFIRMED)
        entry = TrashEntry.objects.get(image=first)
        self.assertEqual(list(entry.memberships.values()), ["confirmed"])
        self.client.post(reverse("image-trash-restore"), {"image_ids": [first.pk]}, format="json")
        self.group.refresh_from_db()
        self.assertEqual(self.group.member_count, 2)
        self.assertEqual(IdentityMembership.objects.get(identity_face__image=first).state, State.CONFIRMED)

    def test_a_folder_left_empty_is_archived_and_comes_back_when_the_photograph_does(self):
        only = self.photo()
        self.move(only)
        self.group.refresh_from_db()
        self.assertEqual((self.group.state, self.group.member_count), (IdentityGroup.State.ARCHIVED, 0))
        self.assertEqual(TrashEntry.objects.get(image=only).archived_groups, [self.group.pk])
        body = self.client.post(reverse("image-trash-restore"), {"image_ids": [only.pk]}, format="json").json()
        self.assertEqual(body["folders_restored"], 1)
        self.group.refresh_from_db()
        self.assertEqual((self.group.state, self.group.member_count), (IdentityGroup.State.ACTIVE, 1))

    def test_photographs_come_back_to_their_folder_in_whatever_order_they_are_restored(self):
        first, second = self.photo("one.jpg"), self.photo("two.jpg")
        self.move(first)
        self.move(second)                                                                  # the last one out archives the folder
        self.group.refresh_from_db()
        self.assertEqual(self.group.state, IdentityGroup.State.ARCHIVED)
        self.client.post(reverse("image-trash-restore"), {"image_ids": [first.pk]}, format="json")      # not the one that archived it
        self.group.refresh_from_db()
        self.assertEqual((self.group.state, self.group.member_count), (IdentityGroup.State.ACTIVE, 1))
        self.assertEqual(IdentityMembership.objects.get(identity_face__image=first).state, State.CONFIRMED)
        self.client.post(reverse("image-trash-restore"), {"image_ids": [second.pk]}, format="json")
        self.group.refresh_from_db()
        self.assertEqual(self.group.member_count, 2)

    def test_a_folder_that_the_regrouping_archived_is_not_revived_by_a_restore(self):
        image = self.photo()
        other = self.make_group(self.owner, "Merged into")
        self.move(image)
        self.group.refresh_from_db()
        self.assertEqual(self.group.state, IdentityGroup.State.ARCHIVED)
        IdentityChange.objects.create(                                                     # a later automatic archive for another reason is the last word
            owner=self.owner, actor=None, group=self.group, action=IdentityChange.Action.ARCHIVED, before={}, after={}, reason="Merged into another folder.",
        )
        IdentityGroup.objects.filter(pk=self.group.pk).update(state=IdentityGroup.State.MERGED)
        self.client.post(reverse("image-trash-restore"), {"image_ids": [image.pk]}, format="json")
        self.assertEqual(IdentityMembership.objects.get(identity_face__image=image).state, State.REMOVED)
        self.assertEqual(other.member_count, 0)

    def test_a_folder_the_owner_named_is_not_archived(self):
        named = self.make_group(self.owner, "Ayesha")
        image = self.photo("named.jpg", group=named)
        self.move(image)
        named.refresh_from_db()
        self.assertEqual((named.state, named.member_count), (IdentityGroup.State.ACTIVE, 0))
        self.client.post(reverse("image-trash-restore"), {"image_ids": [image.pk]}, format="json")
        named.refresh_from_db()
        self.assertEqual(named.member_count, 1)

    def test_a_face_that_was_placed_somewhere_else_meanwhile_stays_where_it_is(self):
        image = self.photo()
        self.move(image)
        elsewhere = self.make_group(self.owner, "Elsewhere")
        face = IdentityMembership.objects.get(identity_face__image=image).identity_face
        IdentityMembership.objects.create(owner=self.owner, identity_face=face, identity_group=elsewhere, state=State.CONFIRMED)
        self.client.post(reverse("image-trash-restore"), {"image_ids": [image.pk]}, format="json")
        self.assertFalse(self.trashed(image))
        self.assertEqual(IdentityMembership.objects.get(identity_face=face, identity_group=self.group).state, State.REMOVED)
        self.assertEqual(IdentityMembership.objects.filter(identity_face=face, state=State.CONFIRMED).count(), 1)

    def test_a_folder_that_was_deleted_or_archived_by_someone_else_is_not_revived(self):
        archived_by_owner, deleted = self.make_group(self.owner, "Gone"), self.make_group(self.owner, "Deleted")
        kept_out, vanished = self.photo("k.jpg", group=archived_by_owner), self.photo("v.jpg", group=deleted)
        self.photo("stays.jpg", group=archived_by_owner)
        self.move(kept_out, vanished)
        IdentityGroup.objects.filter(pk=archived_by_owner.pk).update(state=IdentityGroup.State.ARCHIVED)
        deleted.delete()
        response = self.client.post(reverse("image-trash-restore"), {"image_ids": [kept_out.pk, vanished.pk]}, format="json")
        self.assertEqual((response.status_code, response.json()["folders_restored"]), (200, 0))
        self.assertFalse(self.trashed(kept_out) or self.trashed(vanished))
        self.assertEqual(IdentityMembership.objects.get(identity_face__image=kept_out).state, State.REMOVED)

    def test_a_face_waiting_for_a_decision_with_no_folder_can_be_trashed_and_restored(self):
        image = self.photo()
        membership = IdentityMembership.objects.get(identity_face__image=image)
        IdentityMembership.objects.filter(pk=membership.pk).update(state=State.NEEDS_REVIEW, identity_group=None)
        self.assertEqual(self.move(image).status_code, 200)
        self.assertTrue(self.trashed(image))
        self.client.post(reverse("image-trash-restore"), {"image_ids": [image.pk]}, format="json")
        self.assertFalse(self.trashed(image))
        self.assertEqual(IdentityMembership.objects.get(pk=membership.pk).state, State.NEEDS_REVIEW)


class PackageTests(TrashBase):
    def test_a_ready_package_that_holds_a_copy_is_expired_when_the_photograph_is_moved(self):
        image = self.photo()
        package = DownloadPackage.objects.create(
            owner=self.owner, batch=image.batch, package_type="accepted_results", status="ready", file_size=5, expires_at=timezone.now() + timedelta(days=1),
        )
        package.package_file.save("p.zip", ContentFile(b"zipzip"))
        untouched = DownloadPackage.objects.create(
            owner=self.owner, batch=UploadBatch.objects.create(owner=self.owner, upload_type="single"), package_type="accepted_results", status="ready",
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.move(image)
        package.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual((package.status, package.package_file.name), ("expired", ""))
        self.assertEqual(untouched.status, "ready")


class PurgeTests(TrashBase):
    def test_the_daily_task_deletes_only_photographs_older_than_the_retention_files_included(self):
        old, young, kept = self.photo("old.jpg"), self.photo("young.jpg"), self.photo("kept.jpg")
        for image in (old, young):
            image.original_file.save(image.original_name, ContentFile(b"bytes-of-" + image.original_name.encode()))
        old_name, young_name = ImageUpload.all_objects.get(pk=old.pk).original_file.name, ImageUpload.all_objects.get(pk=young.pk).original_file.name
        self.move(old, young)
        ImageUpload.all_objects.filter(pk=old.pk).update(trashed_at=timezone.now() - timedelta(days=31))
        ImageUpload.all_objects.filter(pk=young.pk).update(trashed_at=timezone.now() - timedelta(days=29))
        self.assertEqual(purge_trash_task(), {"purged": 1})
        self.assertFalse(ImageUpload.all_objects.filter(pk=old.pk).exists())
        self.assertFalse(TrashEntry.objects.filter(image_id=old.pk).exists())
        self.assertFalse(default_storage.exists(old_name))
        self.assertTrue(ImageUpload.all_objects.filter(pk=young.pk).exists())
        self.assertTrue(default_storage.exists(young_name))
        self.assertTrue(ImageUpload.objects.filter(pk=kept.pk).exists())
        self.assertEqual(purge_trash_task(), {"purged": 0})                               # running it again changes nothing

    def test_the_retention_is_a_setting(self):
        image = self.photo()
        self.move(image)
        ImageUpload.all_objects.filter(pk=image.pk).update(trashed_at=timezone.now() - timedelta(days=8))
        self.assertEqual(purge_expired(), 0)
        with override_settings(TRASH_RETENTION_DAYS=7):
            self.assertEqual(purge_expired(), 1)

    def test_purging_recounts_nothing_twice_and_a_limit_is_respected(self):
        images = [self.photo(f"p{i}.jpg") for i in range(5)]
        self.move(*images)
        ImageUpload.all_objects.filter(pk__in=[image.pk for image in images]).update(trashed_at=timezone.now() - timedelta(days=40))
        self.assertEqual(purge_expired(limit=2), 2)
        self.assertEqual(ImageUpload.all_objects.in_trash().count(), 3)
        self.assertEqual(purge_expired(), 3)

    def test_deleting_one_for_good_and_emptying_the_trash(self):
        a, b, c = self.photo("a.jpg"), self.photo("b.jpg"), self.photo("c.jpg")
        self.move(a, b)
        self.assertEqual(self.client.delete(reverse("image-trash-item", args=[c.pk])).status_code, 404)          # not in the trash
        self.assertEqual(self.client.delete(reverse("image-trash-item", args=[a.pk])).status_code, 204)
        self.assertFalse(ImageUpload.all_objects.filter(pk=a.pk).exists())
        empty = reverse("image-trash-empty")
        for body in ({}, {"confirm": False}, {"confirm": "no"}):
            self.assertEqual(self.client.post(empty, body, format="json").status_code, 400, body)
        self.assertTrue(ImageUpload.all_objects.filter(pk=b.pk).exists())
        body = self.client.post(empty, {"confirm": True}, format="json").json()
        self.assertEqual(body, {"deleted": 1, "remaining": 0})
        self.assertTrue(ImageUpload.objects.filter(pk=c.pk).exists())                                            # what was not in the trash is untouched

    def test_emptying_works_in_pieces_when_the_trash_is_large(self):
        images = [self.photo(f"e{i}.jpg") for i in range(5)]
        self.move(*images)
        with mock.patch("uploads.trash_views.EMPTY_LIMIT", 2):
            first = self.client.post(reverse("image-trash-empty"), {"confirm": True}, format="json").json()
            second = self.client.post(reverse("image-trash-empty"), {"confirm": True}, format="json").json()
        self.assertEqual((first, second), ({"deleted": 2, "remaining": 3}, {"deleted": 2, "remaining": 1}))

    def test_one_persons_trash_is_not_another_persons(self):
        theirs = self.photo("t.jpg", group=self.make_group(self.other, "Theirs"))
        ImageUpload.all_objects.filter(pk=theirs.pk).update(trashed_at=timezone.now())
        self.assertEqual(self.client.delete(reverse("image-trash-item", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("image-trash-empty"), {"confirm": True}, format="json").json()["deleted"], 0)
        self.assertTrue(ImageUpload.all_objects.filter(pk=theirs.pk).exists())

    def test_the_permanent_deletes_still_reach_a_photograph_in_the_trash(self):
        image, batch_image = self.photo("p.jpg"), self.photo("q.jpg")
        self.move(image, batch_image)
        response = self.client.post(reverse("image-bulk-delete"), {"image_ids": [image.pk]}, format="json").json()
        self.assertEqual(response["deleted_ids"], [image.pk])
        self.assertFalse(ImageUpload.all_objects.filter(pk=image.pk).exists())
        self.assertEqual(self.client.delete(reverse("upload-batch-detail", args=[batch_image.batch_id])).status_code, 204)
        self.assertFalse(ImageUpload.all_objects.filter(pk=batch_image.pk).exists())

    def test_deleting_the_account_takes_the_trash_with_it(self):
        image = self.photo()
        self.move(image)
        self.owner.delete()
        self.assertFalse(ImageUpload.all_objects.filter(pk=image.pk).exists())


@override_settings(MEDIA_ROOT=MEDIA, ORPHAN_MEDIA_GRACE_HOURS=24, ORPHAN_MEDIA_CLEANUP_ENABLED=False)
class OrphanSweepTests(TrashBase):
    def test_the_sweep_never_reports_the_files_of_a_photograph_in_the_trash(self):
        image = self.photo("sweep.jpg")
        image.original_file.save("sweep.jpg", ContentFile(b"original-in-the-trash"))
        name = ImageUpload.all_objects.get(pk=image.pk).original_file.name
        os.utime(default_storage.path(name), (1, 1))                                       # long past the grace period: only a reference protects it
        self.assertEqual(orphan_media_files(), [])
        self.move(image)
        self.assertIn(name, referenced_media_names())
        self.assertEqual(orphan_media_files(), [])
        self.assertTrue(default_storage.exists(name))
        self.assertTrue(restore([ImageUpload.all_objects.get(pk=image.pk)])["restored"])
        self.assertEqual(orphan_media_files(), [])


class ServiceTests(TrashBase):
    def test_moving_twice_in_a_row_changes_nothing_the_second_time(self):
        image = self.photo()
        first = move_to_trash([ImageUpload.objects.get(pk=image.pk)])
        trashed_at = ImageUpload.all_objects.get(pk=image.pk).trashed_at
        second = move_to_trash([ImageUpload.all_objects.get(pk=image.pk)])
        self.assertEqual((len(first["trashed"]), len(second["trashed"])), (1, 0))
        self.assertEqual(ImageUpload.all_objects.get(pk=image.pk).trashed_at, trashed_at)

    def test_restoring_something_that_is_not_in_the_trash_does_nothing(self):
        image = self.photo()
        self.assertEqual(restore([image]), {"restored": [], "folders_restored": 0})

    def test_a_trashed_photograph_is_not_part_of_the_batch_it_came_from(self):
        image, other = self.photo("a.jpg"), self.photo("b.jpg")
        self.move(image)
        self.assertEqual(list(image.batch.images.all()), [])
        self.assertEqual(list(other.batch.images.all()), [other])


class CountsAndFiltersTests(TrashBase):
    """The numbers and filters a screen draws from: they drop when a photograph is trashed and come back when it is restored."""

    def setUp(self):
        super().setUp()
        self.batch_photos = [self.photo(f"c{i}.jpg") for i in range(3)]
        self.album = Album.objects.create(owner=self.owner, title="Trip")
        for image in self.batch_photos:
            AlbumImage.objects.create(album=self.album, image=image)

    def album_row(self):
        return self.client.get(reverse("albums:album-detail", args=[self.album.pk])).json()

    def restore_all(self, *images):
        self.client.post(reverse("image-trash-restore"), {"image_ids": [image.pk for image in images]}, format="json")

    def test_an_album_counts_and_chooses_its_cover_among_the_photographs_that_are_not_trashed(self):
        first, second, third = self.batch_photos
        Album.objects.filter(pk=self.album.pk).update(cover_image=first)
        self.assertEqual((self.album_row()["photo_count"], self.album_row()["cover_image"]), (3, first.pk))
        self.move(first)
        row = self.album_row()
        self.assertEqual((row["photo_count"], row["cover_image"]), (2, second.pk))                 # the chosen cover is out of sight: the first one in view takes over
        images = self.client.get(reverse("albums:album-images", args=[self.album.pk])).json()["results"]
        self.assertEqual(sorted(item["id"] for item in images), sorted([second.pk, third.pk]))
        self.move(second, third)
        row = self.album_row()
        self.assertEqual((row["photo_count"], row["cover_image"], row["cover_thumbnail_url"]), (0, None, None))
        self.restore_all(first, second, third)
        row = self.album_row()
        self.assertEqual((row["photo_count"], row["cover_image"]), (3, first.pk))                  # its place and its cover mark were never lost

    def test_an_album_cannot_take_a_photograph_that_is_in_the_trash(self):
        gone = self.photo("gone.jpg")
        self.move(gone)
        body = self.client.post(reverse("albums:album-images", args=[self.album.pk]), {"image_ids": [gone.pk]}, format="json").json()
        self.assertEqual((body["added_ids"], body["not_found_ids"]), ([], [gone.pk]))

    def test_the_people_and_folder_counts_drop_and_an_emptied_unnamed_person_disappears(self):
        people = lambda: {row["id"]: row["photo_count"] for row in self.client.get(reverse("identities:people")).json()}
        self.assertEqual(people()[self.group.pk], 3)
        self.move(self.batch_photos[0])
        self.assertEqual(people()[self.group.pk], 2)
        self.move(*self.batch_photos[1:])
        self.assertNotIn(self.group.pk, people())                                                   # nobody to show: the chip is gone, the (unnamed) folder archived
        self.restore_all(*self.batch_photos)
        self.assertEqual(people()[self.group.pk], 3)

    def test_the_gallery_filters_never_return_a_trashed_photograph(self):
        first = self.batch_photos[0]
        ImageUpload.objects.filter(pk=first.pk).update(is_favorite=True)
        self.move(first)
        gallery = reverse("my-image-list")
        for query in ({}, {"search": "c0"}, {"favorite": "true"}, {"album": self.album.pk}, {"identity_group": self.group.pk}, {"batch": first.batch_id}, {"status": "accepted"}):
            with self.subTest(query=query):
                ids = [row["id"] for row in self.client.get(gallery, query).json()["results"]]
                self.assertNotIn(first.pk, ids)
        self.assertEqual(self.client.get(gallery, {"search": "c0"}).json()["count"], 0)

    def test_the_dashboard_counts_the_trash_apart_and_still_counts_its_storage(self):
        dashboard = lambda: self.client.get("/api/user/dashboard/").json()
        before = dashboard()["summary"]
        ImageUpload.objects.filter(pk=self.batch_photos[0].pk).update(file_size=1000)
        before = dashboard()["summary"]
        self.move(self.batch_photos[0])
        after = dashboard()
        summary = after["summary"]
        self.assertEqual(summary["total_images"], before["total_images"] - 1)
        self.assertEqual(summary["accepted_images"], before["accepted_images"] - 1)
        self.assertEqual((summary["trash_images"], summary["trash_bytes"]), (1, 1000))
        self.assertEqual(summary["storage_bytes"], before["storage_bytes"])                         # a trashed photograph is still stored, so it still counts
        self.assertNotIn(self.batch_photos[0].pk, [row["id"] for row in after["recent_uploads"]])
        self.assertNotIn(self.batch_photos[0].pk, [row.get("image_id") for row in after["recent_activity"]])
        self.restore_all(self.batch_photos[0])
        self.assertEqual(dashboard()["summary"]["trash_images"], 0)

    def test_the_batch_progress_leaves_a_trashed_photograph_out_of_its_counts(self):
        batch = self.batch_photos[0].batch
        progress = lambda: self.client.get(reverse("upload-batch-progress", args=[batch.pk])).json()
        self.assertEqual(progress()["counts"]["accepted"] + progress()["counts"]["completed"] + progress()["counts"]["needs_review"] + progress()["counts"]["queued"] + progress()["counts"]["processing"], 1)
        self.move(self.batch_photos[0])
        body = progress()
        self.assertEqual(sum(body["counts"].values()), 0)
        self.assertEqual([row.get("image_id", row.get("id")) for row in body.get("images", [])], [])


class HistoryAndExportTests(TrashBase):
    def test_the_data_export_still_holds_a_photograph_in_the_trash_and_says_so(self):
        image, kept = self.photo("exported.jpg"), self.photo("kept.jpg")
        self.move(image)
        body = self.client.get("/api/privacy/export/").json()
        rows = {row["id"]: row for row in body["images"]} if isinstance(body.get("images"), list) else {row["id"]: row for row in body["data"]["images"]}
        self.assertIsNotNone(rows[image.pk]["trashed_at"])
        self.assertIsNone(rows[kept.pk]["trashed_at"])

    def test_the_audit_trail_of_an_organization_keeps_naming_a_photograph_that_was_trashed(self):
        from types import SimpleNamespace

        from organizations.audit_reading import labels_for

        image = self.photo("audited.jpg")
        self.move(image)
        labels = labels_for([SimpleNamespace(target_type="ImageUpload", target_id=image.pk)])
        self.assertEqual(labels.get(("ImageUpload", image.pk)), "audited.jpg")

    def test_the_platform_analytics_still_count_what_was_uploaded(self):
        from operations.analytics import _storage_growth_by_month

        image = self.photo("counted.jpg")
        ImageUpload.objects.filter(pk=image.pk).update(file_size=4096)
        before = sum(row["added_bytes"] for row in _storage_growth_by_month(self.owner.pk))
        self.move(image)
        self.assertEqual(sum(row["added_bytes"] for row in _storage_growth_by_month(self.owner.pk)), before)


class ThumbnailTests(TrashBase):
    def test_the_trash_shows_a_thumbnail_of_what_it_holds_and_only_of_that(self):
        from .test_trash_audit import jpeg_bytes

        image, visible = self.photo("thumb.jpg"), self.photo("visible.jpg")
        for photo in (image, visible):
            photo.original_file.save(photo.original_name, ContentFile(jpeg_bytes()))
        trash_address = lambda photo: reverse("image-trash-thumbnail", args=[photo.pk])
        self.assertEqual(self.client.get(trash_address(image)).status_code, 404)                 # not in the trash: nothing to show here
        self.move(image)
        response = self.client.get(trash_address(image))
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "image/jpeg"))
        self.assertEqual(self.client.get(reverse("image-thumbnail", args=[image.pk])).status_code, 404)    # the ordinary address stays closed
        self.assertEqual(self.client.get(trash_address(visible)).status_code, 404)
        stranger = APIClient()
        stranger.force_authenticate(self.other)
        self.assertEqual(stranger.get(trash_address(image)).status_code, 404)
        self.assertEqual(APIClient().get(trash_address(image)).status_code, 401)


class RunningWorkTests(TrashBase):
    def test_a_batch_preprocessing_run_skips_a_photograph_trashed_since_it_began_instead_of_crashing(self):
        from .tasks import preprocess_batch_task

        first = self.photo("first.jpg")
        second = ImageUpload.objects.create(owner=self.owner, batch=first.batch, original_name="second.jpg", status="uploaded")
        ImagePreprocessingResult.objects.filter(image=first).update(status="pending")
        ImagePreprocessingResult.objects.create(owner=self.owner, image=second, status="pending")

        def preprocess_then_the_other_is_trashed(image, requested_format="auto"):
            ImageUpload.all_objects.filter(pk=second.pk).update(trashed_at=timezone.now())            # the person moves it to the trash while the run is going
            ImagePreprocessingResult.objects.filter(image=image).update(status="completed")

        with mock.patch("uploads.tasks.preprocess_image", side_effect=preprocess_then_the_other_is_trashed) as preprocess:
            outcome = preprocess_batch_task.apply(args=[first.batch_id]).get()
        self.assertEqual(preprocess.call_count, 1)                                                    # the trashed one was not prepared
        self.assertEqual((outcome["status"], outcome["processed_files"], outcome["failed_files"]), ("completed", 1, 0))

    def test_a_waiting_enhancement_of_a_trashed_photograph_is_not_started_by_the_release_task(self):
        image = self.photo("wait.jpg")
        record = AutoEnhancement.objects.create(image=image, owner=self.owner, batch=image.batch, state=AutoEnhancement.State.SKIPPED)
        self.move(image)
        record.refresh_from_db()
        self.assertEqual(record.state, AutoEnhancement.State.SKIPPED)
