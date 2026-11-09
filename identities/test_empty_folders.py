"""A person folder with no photograph left is deleted, the automatic names close up, and a folder can be deleted on purpose."""
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from uploads.models import ImageUpload

from .management import delete_empty_group, move_membership, remove_membership
from .models import IdentityChange, IdentityGroup, IdentityMembership
from .services import prune_empty_folders, renumber_person_folders
from .test_person_folders import FolderFixtures

State = IdentityMembership.State


class EmptyFolderBase(FolderFixtures, TestCase):
    def setUp(self):
        self.owner = self.make_user("pruner")
        self.other = self.make_user("neighbour")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def names(self, owner=None):
        return list(IdentityGroup.objects.filter(owner=owner or self.owner, state=IdentityGroup.State.ACTIVE).order_by("display_name").values_list("display_name", flat=True))

    def four_folders(self):
        """Person 001..004, one photograph each: ``{number: (folder, image)}``."""
        made = {}
        for number in range(1, 5):
            group = self.make_group(self.owner, f"Person {number:03d}")
            made[number] = (group, self.add_photo(group, f"p{number}.jpg")[0])
        return made

    def delete_photo(self, image):
        return self.client.delete(reverse("image-upload-detail", args=[image.pk]))


class DeletingPhotographsTests(EmptyFolderBase):
    def test_the_last_photograph_takes_its_folder_with_it(self):
        group = self.make_group(self.owner)
        image, _ = self.add_photo(group)
        self.assertEqual(self.delete_photo(image).status_code, 204)
        self.assertFalse(IdentityGroup.objects.filter(pk=group.pk).exists())

    def test_a_folder_that_still_has_photographs_stays_and_is_recounted(self):
        group = self.make_group(self.owner)
        first, _ = self.add_photo(group, "a.jpg")
        self.add_photo(group, "b.jpg")
        self.delete_photo(first)
        group.refresh_from_db()
        self.assertEqual(group.member_count, 1)

    def test_the_reported_case_person_004_becomes_person_002_and_ids_do_not_change(self):
        made = self.four_folders()
        self.delete_photo(made[2][1])
        self.delete_photo(made[3][1])
        self.assertEqual(self.names(), ["Person 001", "Person 002"])
        self.assertEqual(IdentityGroup.objects.get(pk=made[4][0].pk).display_name, "Person 002")      # the same folder, the same id, a new number
        self.assertFalse(IdentityGroup.objects.filter(pk__in=[made[2][0].pk, made[3][0].pk]).exists())
        listed = self.client.get(reverse("identities:group-list")).json()
        self.assertEqual(sorted((row["display_name"], row["member_count"]) for row in listed), [("Person 001", 1), ("Person 002", 1)])

    def test_a_new_folder_after_the_cleanup_takes_the_next_number(self):
        made = self.four_folders()
        self.delete_photo(made[2][1])
        from .services import _next_group_name

        self.assertEqual(_next_group_name(self.owner), "Person 004")          # 001, 002 (was 003), 003 (was 004)

    def test_bulk_delete_reports_the_deleted_and_renumbered_folders(self):
        made = self.four_folders()
        response = self.client.post(reverse("image-bulk-delete"), {"image_ids": [made[1][1].pk, made[2][1].pk]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["folders_updated"], body["folders_deleted"], body["folders_renumbered"]), (2, 2, 2))
        self.assertEqual(self.names(), ["Person 001", "Person 002"])

    def test_deleting_an_upload_batch_removes_its_emptied_folders_too(self):
        made = self.four_folders()
        self.client.delete(reverse("upload-batch-detail", args=[made[2][1].batch_id]))
        self.assertEqual(self.names(), ["Person 001", "Person 002", "Person 003"])

    def test_another_owners_folders_are_never_touched(self):
        theirs = [self.make_group(self.other, f"Person {number:03d}") for number in (1, 2, 4)]
        for group in theirs:
            self.add_photo(group)
        made = self.four_folders()
        self.delete_photo(made[1][1])
        self.assertEqual(self.names(self.other), ["Person 001", "Person 002", "Person 004"])

    def test_a_folder_the_owner_named_is_deleted_when_it_has_no_photograph_left_and_the_others_keep_their_names(self):
        named = self.make_group(self.owner, "Mother")
        image, _ = self.add_photo(named)
        keep = self.make_group(self.owner, "Person 005")
        self.add_photo(keep, "k.jpg")
        self.delete_photo(image)
        self.assertEqual(self.names(), ["Person 001"])           # the unnamed folder closes up to 001

    def test_a_suggestion_that_pointed_at_the_emptied_folder_stays_a_suggestion_with_no_folder(self):
        group = self.make_group(self.owner)
        image, _ = self.add_photo(group)
        _other_image, suggestion = self.add_photo(group, "maybe.jpg", state=State.NEEDS_REVIEW)
        self.delete_photo(image)
        suggestion.refresh_from_db()
        self.assertEqual((suggestion.state, suggestion.identity_group_id), (State.NEEDS_REVIEW, None))
        self.assertTrue(ImageUpload.objects.filter(pk=suggestion.identity_face.image_id).exists())          # its photograph is untouched

    def test_the_deletion_is_written_to_the_history_with_the_folders_name(self):
        group = self.make_group(self.owner, "Person 003")
        image, _ = self.add_photo(group)
        self.delete_photo(image)
        change = IdentityChange.objects.get(owner=self.owner, action=IdentityChange.Action.DELETED)
        self.assertEqual((change.before["display_name"], change.before["group_id"], change.group_id), ("Person 003", group.pk, None))


class WhatIsKeptTests(EmptyFolderBase):
    def test_a_folder_a_photograph_in_the_trash_can_return_to_is_kept(self):
        group = self.make_group(self.owner)
        trashed, _ = self.add_photo(group, "trashed.jpg", state=State.REMOVED)
        ImageUpload.all_objects.filter(pk=trashed.pk).update(trashed_at=timezone.now())
        live, _ = self.add_photo(group, "live.jpg")
        self.delete_photo(live)
        self.assertTrue(IdentityGroup.objects.filter(pk=group.pk).exists())
        group.refresh_from_db()
        self.assertTrue(group.memberships.filter(state=State.REMOVED).exists())

    def test_purging_the_last_trashed_photograph_deletes_the_archived_folder(self):
        group = self.make_group(self.owner, state=IdentityGroup.State.ARCHIVED)
        trashed, _ = self.add_photo(group, "trashed.jpg", state=State.REMOVED)
        ImageUpload.all_objects.filter(pk=trashed.pk).update(trashed_at=timezone.now())
        self.assertEqual(self.client.delete(reverse("image-trash-item", args=[trashed.pk])).status_code, 204)
        self.assertFalse(IdentityGroup.objects.filter(pk=group.pk).exists())

    def test_a_merged_folder_is_never_deleted(self):
        merged = self.make_group(self.owner, "Person 002", state=IdentityGroup.State.MERGED)
        self.assertEqual(prune_empty_folders([merged])["deleted"], [])
        self.assertTrue(IdentityGroup.objects.filter(pk=merged.pk).exists())


class RenumberingTests(EmptyFolderBase):
    def test_it_closes_the_gaps_in_the_order_of_the_numbers(self):
        for number in (1, 4, 7):
            self.make_group(self.owner, f"Person {number:03d}")
        self.assertEqual(renumber_person_folders(self.owner), 2)
        self.assertEqual(self.names(), ["Person 001", "Person 002", "Person 003"])
        self.assertEqual(renumber_person_folders(self.owner), 0)          # nothing left to do

    def test_the_order_is_kept(self):
        first = self.make_group(self.owner, "Person 002")
        second = self.make_group(self.owner, "Person 009")
        renumber_person_folders(self.owner)
        self.assertEqual((IdentityGroup.objects.get(pk=first.pk).display_name, IdentityGroup.objects.get(pk=second.pk).display_name), ("Person 001", "Person 002"))

    def test_a_folder_the_owner_renamed_keeps_its_name_even_if_it_looks_automatic(self):
        mine = self.make_group(self.owner, "Person 002")
        IdentityChange.objects.create(owner=self.owner, actor=self.owner, group=mine, action=IdentityChange.Action.RENAMED, before={"display_name": "Ann"}, after={"display_name": "Person 002"})
        later = self.make_group(self.owner, "Person 005")
        renumber_person_folders(self.owner)
        self.assertEqual(IdentityGroup.objects.get(pk=mine.pk).display_name, "Person 002")
        self.assertEqual(IdentityGroup.objects.get(pk=later.pk).display_name, "Person 001")
        third = self.make_group(self.owner, "Person 009")
        renumber_person_folders(self.owner)
        self.assertEqual(IdentityGroup.objects.get(pk=third.pk).display_name, "Person 003")          # 002 is taken, so it is skipped

    def test_an_archived_or_merged_folder_keeps_its_number_so_no_two_folders_share_a_name(self):
        archived = self.make_group(self.owner, "Person 001", state=IdentityGroup.State.ARCHIVED)
        merged = self.make_group(self.owner, "Person 002", state=IdentityGroup.State.MERGED)
        active = self.make_group(self.owner, "Person 006")
        renumber_person_folders(self.owner)
        self.assertEqual(IdentityGroup.objects.get(pk=active.pk).display_name, "Person 003")
        self.assertEqual((IdentityGroup.objects.get(pk=archived.pk).display_name, IdentityGroup.objects.get(pk=merged.pk).display_name), ("Person 001", "Person 002"))

    def test_a_name_that_is_not_automatic_is_not_touched(self):
        self.make_group(self.owner, "Family")
        self.make_group(self.owner, "Person 004")
        renumber_person_folders(self.owner)
        self.assertEqual(self.names(), ["Family", "Person 001"])

    def test_it_does_not_change_the_order_new_faces_are_matched_in(self):
        group = self.make_group(self.owner, "Person 004")
        before = IdentityGroup.objects.get(pk=group.pk).updated_at
        renumber_person_folders(self.owner)
        self.assertEqual(IdentityGroup.objects.get(pk=group.pk).updated_at, before)


class ManagementTests(EmptyFolderBase):
    def test_removing_the_last_photograph_from_a_folder_deletes_it_and_the_record_says_so(self):
        made = self.four_folders()
        membership = IdentityMembership.objects.get(identity_face__image=made[2][1])
        removed = remove_membership(membership, self.owner, "wrong person")
        self.assertFalse(IdentityGroup.objects.filter(pk=made[2][0].pk).exists())
        self.assertEqual((removed.state, removed.identity_group_id), (State.REJECTED, None))
        self.assertEqual(self.names(), ["Person 001", "Person 002", "Person 003"])

    def test_moving_the_last_photograph_out_of_a_folder_deletes_the_folder(self):
        made = self.four_folders()
        membership = IdentityMembership.objects.get(identity_face__image=made[1][1])
        move_membership(membership, made[3][0], self.owner)
        self.assertFalse(IdentityGroup.objects.filter(pk=made[1][0].pk).exists())
        self.assertEqual(IdentityGroup.objects.get(pk=made[3][0].pk).member_count, 2)
        self.assertEqual(self.names(), ["Person 001", "Person 002", "Person 003"])

    def test_removing_one_of_two_photographs_keeps_the_folder(self):
        group = self.make_group(self.owner)
        self.add_photo(group, "a.jpg")
        _, membership = self.add_photo(group, "b.jpg")
        removed = remove_membership(membership, self.owner)
        self.assertTrue(IdentityGroup.objects.filter(pk=group.pk).exists())
        self.assertEqual((removed.state, removed.identity_group_id), (State.REMOVED, group.pk))


class DeleteAFolderTests(EmptyFolderBase):
    def url(self, group):
        return reverse("identities:group-delete", args=[group.pk])

    def test_a_folder_with_photographs_is_refused_unless_dissolve_is_true(self):
        group = self.make_group(self.owner)
        self.add_photo(group)
        self.assertEqual(self.client.delete(self.url(group), {}, format="json").status_code, 400)
        self.assertEqual(self.client.delete(self.url(group), {"dissolve": False}, format="json").status_code, 400)
        self.assertTrue(IdentityGroup.objects.filter(pk=group.pk).exists())

    def test_dissolving_deletes_the_folder_keeps_every_photograph_and_closes_the_numbers(self):
        made = self.four_folders()
        response = self.client.delete(self.url(made[2][0]), {"dissolve": True, "reason": "not needed"}, format="json")
        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(IdentityGroup.objects.filter(pk=made[2][0].pk).exists())
        self.assertTrue(ImageUpload.objects.filter(pk=made[2][1].pk).exists())          # the photograph is still in the library
        membership = IdentityMembership.objects.get(identity_face__image=made[2][1])
        self.assertEqual((membership.state, membership.identity_group_id), (State.REJECTED, None))
        self.assertEqual(self.names(), ["Person 001", "Person 002", "Person 003"])
        change = IdentityChange.objects.get(owner=self.owner, action=IdentityChange.Action.DELETED)
        self.assertEqual((change.before["display_name"], change.before["photographs_released"], change.reason), ("Person 002", 1, "not needed"))

    def test_an_empty_folder_needs_no_flag(self):
        group = self.make_group(self.owner)
        self.assertEqual(self.client.delete(self.url(group), {}, format="json").status_code, 204)
        self.assertFalse(IdentityGroup.objects.filter(pk=group.pk).exists())

    def test_it_cannot_delete_someone_elses_folder(self):
        theirs = self.make_group(self.other)
        self.assertEqual(self.client.delete(self.url(theirs), {"dissolve": True}, format="json").status_code, 404)
        self.assertTrue(IdentityGroup.objects.filter(pk=theirs.pk).exists())

    def test_the_service_refuses_a_folder_with_photographs_without_dissolve(self):
        group = self.make_group(self.owner)
        self.add_photo(group)
        with self.assertRaisesMessage(Exception, "Remove or move"):
            delete_empty_group(group, self.owner)


class PruneCommandTests(EmptyFolderBase):
    def run_command(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("prune_empty_person_folders", *args, stdout=out)
        return out.getvalue()

    def stale_data(self):
        """The state the reported account was in: Person 001, two empty folders, then Person 004 and 005 with photographs."""
        first = self.make_group(self.owner, "Person 001")
        self.add_photo(first, "a.jpg")
        empty = [self.make_group(self.owner, "Person 002"), self.make_group(self.owner, "Person 003")]
        later = [self.make_group(self.owner, "Person 004"), self.make_group(self.owner, "Person 005")]
        for group in later:
            self.add_photo(group, f"{group.pk}.jpg")
        return empty, later

    def test_a_dry_run_lists_the_folders_and_the_new_names_and_changes_nothing(self):
        empty, later = self.stale_data()
        before = list(IdentityGroup.objects.order_by("pk").values_list("pk", "display_name"))
        text = self.run_command()
        self.assertIn("Person folders with no photograph: 2", text)
        self.assertIn(f"would rename folder {later[0].pk}: \"Person 004\" -> \"Person 002\"", text)
        self.assertIn(f"would rename folder {later[1].pk}: \"Person 005\" -> \"Person 003\"", text)
        self.assertEqual(list(IdentityGroup.objects.order_by("pk").values_list("pk", "display_name")), before)
        self.assertFalse(IdentityChange.objects.filter(action=IdentityChange.Action.DELETED).exists())

    def test_apply_deletes_the_empty_folders_and_renumbers(self):
        empty, later = self.stale_data()
        text = self.run_command("--apply")
        self.assertIn("Deleted 2 folder(s); renumbered 2", text)
        self.assertFalse(IdentityGroup.objects.filter(pk__in=[group.pk for group in empty]).exists())
        self.assertEqual(self.names(), ["Person 001", "Person 002", "Person 003"])
        self.assertEqual(IdentityGroup.objects.get(pk=later[0].pk).display_name, "Person 002")

    def test_it_is_safe_to_run_twice_and_says_when_there_is_nothing_to_do(self):
        self.stale_data()
        self.run_command("--apply")
        self.assertIn("No person folder is empty.", self.run_command("--apply"))

    def test_owner_limits_it_to_one_account(self):
        self.stale_data()
        empty_theirs = self.make_group(self.other, "Person 001")
        self.run_command("--apply", "--owner", str(self.owner.pk))
        self.assertTrue(IdentityGroup.objects.filter(pk=empty_theirs.pk).exists())
        self.assertEqual(len(self.names()), 3)

    def test_a_folder_with_a_photograph_in_the_trash_is_left_alone(self):
        group = self.make_group(self.owner)
        trashed, _ = self.add_photo(group, "t.jpg", state=State.REMOVED)
        ImageUpload.all_objects.filter(pk=trashed.pk).update(trashed_at=timezone.now())
        self.assertIn("No person folder is empty.", self.run_command("--apply"))
        self.assertTrue(IdentityGroup.objects.filter(pk=group.pk).exists())
