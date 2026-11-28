from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import OrganizationMembership
from .services import create_organization, create_personal_organization, get_default_organization

User = get_user_model()


class DefaultOrganizationTests(TestCase):
    def make_user(self, name):
        return User.objects.create_user(
            username=name, email=f"{name}@example.com", password="ForestRiver!4821",
        )

    def test_a_user_with_no_organization_at_all_has_no_default(self):
        user = self.make_user("no-org")

        self.assertIsNone(get_default_organization(user))

    def test_the_personal_organization_is_preferred_even_when_it_was_not_created_first(self):
        user = self.make_user("multi-org")
        team_org = create_organization(name="Team Co", owner=user)
        personal_org = create_personal_organization(user)

        self.assertNotEqual(team_org.pk, personal_org.pk)
        self.assertEqual(get_default_organization(user), personal_org)

    def test_never_falls_back_to_an_organization_the_person_merely_belongs_to(self):
        """An organization is never inferred from memberships (it used to be: the oldest active one). Without a personal workspace the default is no organization at all."""
        user = self.make_user("no-personal-org")
        create_organization(name="Only Org", owner=user)

        self.assertIsNone(get_default_organization(user))

    def test_a_suspended_membership_is_not_used_as_the_default(self):
        user = self.make_user("suspended-member")
        organization = create_organization(name="Suspended Membership Co", owner=self.make_user("someone-else"))
        OrganizationMembership.objects.create(
            organization=organization, user=user, role=OrganizationMembership.Role.MEMBER,
            status=OrganizationMembership.Status.SUSPENDED,
        )

        self.assertIsNone(get_default_organization(user))
