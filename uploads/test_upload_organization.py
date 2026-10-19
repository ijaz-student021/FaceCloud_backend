"""Filing an upload under an organization: the way a team's moderators come to see a member's photographs."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from organizations.models import OrganizationMembership
from organizations.services import create_organization, create_personal_organization

from .models import UploadBatch
from .tests import make_image, make_zip

User = get_user_model()
PASSWORD = "ForestRiver!4821"


class UploadOrganizationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="team-owner", email="team-owner@example.com", password=PASSWORD,
                                              is_email_verified=True, role="admin")
        self.moderator = User.objects.create_user(username="team-mod", email="team-mod@example.com", password=PASSWORD,
                                                  is_email_verified=True, role="moderator")
        self.member = User.objects.create_user(username="team-member", email="team-member@example.com", password=PASSWORD,
                                               is_email_verified=True)
        self.outsider = User.objects.create_user(username="outsider", email="outsider@example.com", password=PASSWORD,
                                                 is_email_verified=True)
        self.team = create_organization(name="Acme Team", owner=self.owner)
        self.other_team = create_organization(name="Other Team", owner=self.owner)
        for user in (self.moderator, self.member, self.outsider):
            create_personal_organization(user)
        OrganizationMembership.objects.create(organization=self.team, user=self.moderator, role="moderator", status="active")
        self.membership = OrganizationMembership.objects.create(organization=self.team, user=self.member, role="member", status="active")
        self.client = APIClient()
        self.client.force_authenticate(self.member)

    def single(self, **extra):
        return self.client.post(reverse("image-upload-single"), {"file": make_image(), **extra}, format="multipart")

    def test_without_a_choice_the_photo_stays_in_the_persons_own_workspace(self):
        self.assertEqual(self.single().status_code, 201)
        self.assertTrue(UploadBatch.objects.get().organization.is_personal)

    def test_a_member_can_file_an_upload_under_their_team_and_its_moderator_then_sees_it(self):
        response = self.single(organization_id=self.team.pk)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(UploadBatch.objects.get().organization_id, self.team.pk)
        self.client.force_authenticate(self.moderator)
        listed = self.client.get(f"/api/organizations/{self.team.pk}/moderator/batches/").json()
        self.assertEqual([row["owner_username"] for row in listed], ["team-member"])

    def test_the_teams_moderator_does_not_see_uploads_that_were_not_filed_there(self):
        self.single()                                                              # personal
        self.client.force_authenticate(self.moderator)
        self.assertEqual(self.client.get(f"/api/organizations/{self.team.pk}/moderator/batches/").json(), [])

    def test_multiple_and_zip_uploads_take_the_same_choice(self):
        multiple = self.client.post(reverse("image-upload-multiple"), {
            "files": [make_image("a.jpg"), make_image("b.jpg")], "organization_id": self.team.pk}, format="multipart")
        self.assertEqual(multiple.status_code, 201, multiple.content)
        archive = self.client.post(reverse("image-upload-zip"), {
            "file": make_zip([("a.jpg", make_image("a.jpg").read())]), "organization_id": self.team.pk}, format="multipart")
        self.assertEqual(archive.status_code, 201, archive.content)
        self.assertEqual(set(UploadBatch.objects.values_list("organization_id", flat=True)), {self.team.pk})

    def test_someone_who_is_not_a_member_cannot_file_under_that_organization_and_nothing_is_stored(self):
        self.client.force_authenticate(self.outsider)
        response = self.single(organization_id=self.team.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("organization_id", response.json())
        self.assertFalse(UploadBatch.objects.exists())

    def test_a_suspended_or_removed_membership_does_not_count(self):
        for state in ("suspended", "removed"):
            OrganizationMembership.objects.filter(pk=self.membership.pk).update(status=state)
            self.assertEqual(self.single(organization_id=self.team.pk).status_code, 400, state)
        self.assertFalse(UploadBatch.objects.exists())

    def test_another_organization_the_person_is_not_in_is_refused_even_though_they_are_in_a_team(self):
        self.assertEqual(self.single(organization_id=self.other_team.pk).status_code, 400)

    def test_a_malformed_value_is_a_field_error(self):
        for bad in ("abc", "1.5"):
            self.assertEqual(self.single(organization_id=bad).status_code, 400, bad)

    def test_a_blank_value_means_no_choice(self):
        self.assertEqual(self.single(organization_id="").status_code, 201)
        self.assertTrue(UploadBatch.objects.get().organization.is_personal)

    def test_the_owner_still_sees_their_own_upload_whichever_organization_it_is_filed_under(self):
        self.single(organization_id=self.team.pk)
        self.assertEqual(len(self.client.get(reverse("upload-batch-list")).json()), 1)
