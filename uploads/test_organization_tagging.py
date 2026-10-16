"""T-008 Phase B: new batches are tagged with the owner's organization.

Owner-scoped access is unchanged (see UploadBatch.organization's docstring) --
this only proves the field a future org-scoped moderator view will query is
actually populated, not that any existing endpoint behaves differently.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from organizations.services import create_organization, create_personal_organization

from .services import create_image_batch
from .tests import make_image

User = get_user_model()


class BatchOrganizationTaggingTests(TestCase):
    def make_user(self, name):
        return User.objects.create_user(
            username=name, email=f"{name}@example.com", password="ForestRiver!4821",
        )

    def test_a_new_batch_is_tagged_with_the_owners_personal_organization(self):
        owner = self.make_user("batch-owner")
        personal_org = create_personal_organization(owner)

        batch = create_image_batch(owner, "single", [make_image()])

        self.assertEqual(batch.organization, personal_org)

    def test_a_new_batch_prefers_the_personal_organization_over_a_team_one(self):
        owner = self.make_user("multi-org-owner")
        create_organization(name="Some Team", owner=owner)
        personal_org = create_personal_organization(owner)

        batch = create_image_batch(owner, "single", [make_image()])

        self.assertEqual(batch.organization, personal_org)

    def test_a_batch_owner_with_no_organization_gets_none_not_an_error(self):
        owner = self.make_user("no-org-owner")

        batch = create_image_batch(owner, "single", [make_image()])

        self.assertIsNone(batch.organization)
