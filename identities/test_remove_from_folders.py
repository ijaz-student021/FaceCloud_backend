"""Taking a photograph out of its person folders (a rejected photograph, a trashed one) must work for every kind of membership the photograph can have."""
from django.test import TestCase

from .models import IdentityChange, IdentityGroup, IdentityMembership
from .services import remove_photo_from_folders
from .test_person_folders import FolderFixtures

State = IdentityMembership.State


class RemovePhotoFromFoldersTests(FolderFixtures, TestCase):
    def setUp(self):
        self.owner = self.make_user("remover")
        self.group = self.make_group(self.owner, "Person 001")

    def test_a_suggestion_with_no_folder_is_taken_out_instead_of_breaking_the_database_rule(self):
        image, membership = self.add_photo(self.group)
        IdentityMembership.objects.filter(pk=membership.pk).update(state=State.NEEDS_REVIEW, identity_group=None)
        outcome = remove_photo_from_folders(image)
        self.assertEqual(outcome["memberships"], 1)
        self.assertEqual(IdentityMembership.objects.get(pk=membership.pk).state, State.REJECTED)       # inactive, and legal without a folder

    def test_a_confirmed_membership_becomes_removed_and_the_folder_is_recounted(self):
        image, membership = self.add_photo(self.group)
        other, _ = self.add_photo(self.group, "other.jpg")
        remove_photo_from_folders(image)
        self.assertEqual(IdentityMembership.objects.get(pk=membership.pk).state, State.REMOVED)
        self.group.refresh_from_db()
        self.assertEqual(self.group.member_count, 1)
        self.assertTrue(IdentityChange.objects.filter(membership=membership, action=IdentityChange.Action.REMOVED).exists())
        self.assertEqual(self.group.state, IdentityGroup.State.ACTIVE)
