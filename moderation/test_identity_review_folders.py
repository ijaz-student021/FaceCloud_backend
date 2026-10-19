"""The Identity Review screen: the organization's person folders, suggested merges and merging two of one member's folders."""
import numpy as np
from django.test import TestCase
from django.urls import reverse

from identities.models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership
from identities.storage import protect_embedding
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization, create_personal_organization
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .tests import ModerationFixtureMixin

State = IdentityMembership.State


def unit(angle_degrees):
    radians = np.radians(angle_degrees)
    return [float(np.cos(radians)), float(np.sin(radians)), 0.0]


class FolderScreenTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("idrev-owner")
        self.organization = create_organization(name="Idrev Co", owner=self.owner)
        self.moderator = self.make_user("idrev-mod")
        OrganizationMembership.objects.create(organization=self.organization, user=self.moderator,
                                              role=OrganizationMembership.Role.MODERATOR)
        self.sara = self.make_user("idrev-sara")
        self.omar = self.make_user("idrev-omar")
        for user in (self.sara, self.omar):
            OrganizationMembership.objects.create(organization=self.organization, user=user)
        self.client = self.authenticated_client(self.moderator)

    def folder(self, owner, name, angle, photos=1, organization=None, state=IdentityGroup.State.ACTIVE, score=0.9):
        group = IdentityGroup.objects.create(
            owner=owner, display_name=name, state=state, protected_centroid=protect_embedding(unit(angle)), embedding_dimension=3,
            provider="test", model_checksum="m1", policy_version="v1", member_count=photos,
        )
        batch = UploadBatch.objects.create(owner=owner, organization=organization or self.organization, upload_type="multiple")
        for index in range(photos):
            image = ImageUpload.objects.create(owner=owner, batch=batch, original_name=f"{name}-{index}.jpg", status="uploaded", file_size=1)
            pre = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
            validation = FaceValidationResult.objects.create(owner=owner, image=image, preprocessing_result=pre, decision="accepted", face_count=1)
            analysis = IdentityAnalysis.objects.create(owner=owner, image=image, preprocessing_result=pre, face_validation=validation,
                                                       status="completed", provider="test", policy_version="v1")
            face = IdentityFace.objects.create(owner=owner, image=image, analysis=analysis, face_index=0, protected_embedding=protect_embedding(unit(angle)),
                                               embedding_dimension=3, provider="test")
            IdentityMembership.objects.create(owner=owner, identity_face=face, identity_group=group, state=State.CONFIRMED, similarity_score=score)
        return group

    def url(self, name):
        return reverse(f"moderation:{name}", kwargs={"organization_id": self.organization.pk})

    def test_the_list_shows_folders_with_owner_counts_confidence_and_preview_covers(self):
        one = self.folder(self.sara, "Person 001", 0, photos=3, score=0.9)
        body = self.client.get(self.url("identity-group-list")).json()
        self.assertEqual([row["id"] for row in body], [one.pk])
        row = body[0]
        self.assertEqual((row["display_name"], row["owner"]["username"], row["member_count"], row["photos_in_organization"], row["matching_confidence"]),
                         ("Person 001", "idrev-sara", 3, 3, 90.0))
        self.assertEqual(len(row["cover_images"]), 3)
        self.assertTrue(row["cover_images"][0]["preview_url"].endswith(f"/moderator/images/{row['cover_images'][0]['id']}/preview/"))

    def test_only_folders_with_photos_submitted_to_this_organization_appear(self):
        shared = self.folder(self.sara, "Shared", 0)
        personal = create_personal_organization(self.omar)
        self.folder(self.omar, "Private", 10, organization=personal)
        other = create_organization(name="Elsewhere", owner=self.make_user("idrev-elsewhere-owner"))
        self.folder(self.omar, "Elsewhere folder", 20, organization=other)
        self.folder(self.sara, "Merged away", 30, state=IdentityGroup.State.MERGED)
        self.assertEqual([row["id"] for row in self.client.get(self.url("identity-group-list")).json()], [shared.pk])

    def test_a_folder_counts_only_this_organizations_photos_in_photos_in_organization(self):
        group = self.folder(self.sara, "Mixed", 0, photos=2)
        personal = create_personal_organization(self.sara)
        extra = self.folder(self.sara, "ignored", 5, photos=1, organization=personal)
        IdentityMembership.objects.filter(identity_group=extra).update(identity_group=group)
        row = self.client.get(self.url("identity-group-list")).json()[0]
        self.assertEqual(row["photos_in_organization"], 2)
        self.assertEqual(len(row["cover_images"]), 2)               # the private photo is not previewed to the moderator

    def test_search_and_owner_filters(self):
        self.folder(self.sara, "Alice", 0)
        self.folder(self.omar, "Bob", 40)
        self.assertEqual([r["display_name"] for r in self.client.get(self.url("identity-group-list"), {"q": "ali"}).json()], ["Alice"])
        self.assertEqual([r["display_name"] for r in self.client.get(self.url("identity-group-list"), {"q": "idrev-omar"}).json()], ["Bob"])
        self.assertEqual([r["display_name"] for r in self.client.get(self.url("identity-group-list"), {"owner": self.omar.pk}).json()], ["Bob"])
        self.assertEqual(self.client.get(self.url("identity-group-list"), {"owner": "x"}).status_code, 400)

    def test_suggestions_pair_one_members_alike_folders_most_alike_first(self):
        a = self.folder(self.sara, "A", 0)
        b = self.folder(self.sara, "B", 5)              # cos 5 deg = 99.6 %
        c = self.folder(self.sara, "C", 30)             # cos 30 deg = 86.6 % from A
        self.folder(self.sara, "D", 90)                 # unrelated
        self.folder(self.omar, "E", 1)                  # alike, but another member's: never paired with Sara's
        suggestions = self.client.get(self.url("identity-group-suggestions")).json()
        pairs = [(s["source"]["id"], s["target"]["id"]) for s in suggestions]
        self.assertEqual(pairs[0], (b.pk, a.pk))                     # same size: the newer folder is merged into the older
        self.assertEqual(suggestions[0]["similarity"], 99.6)
        self.assertIn((c.pk, a.pk), pairs)
        self.assertTrue(all(s["source"]["owner"]["id"] == s["target"]["owner"]["id"] for s in suggestions))
        self.assertEqual([s["similarity"] for s in suggestions], sorted((s["similarity"] for s in suggestions), reverse=True))

    def test_the_bigger_folder_is_the_one_to_keep(self):
        small = self.folder(self.sara, "Small", 0, photos=1)
        big = self.folder(self.sara, "Big", 4, photos=3)
        suggestion = self.client.get(self.url("identity-group-suggestions")).json()[0]
        self.assertEqual((suggestion["source"]["id"], suggestion["target"]["id"]), (small.pk, big.pk))

    def test_the_threshold_is_a_setting(self):
        self.folder(self.sara, "A", 0)
        self.folder(self.sara, "B", 30)
        with self.settings(MODERATOR_MERGE_SUGGESTION_MIN_SIMILARITY=0.95):
            self.assertEqual(self.client.get(self.url("identity-group-suggestions")).json(), [])

    def test_merging_moves_the_photos_audits_it_and_keeps_the_target(self):
        source = self.folder(self.sara, "Source", 0, photos=2)
        target = self.folder(self.sara, "Target", 3, photos=1)
        response = self.client.post(self.url("identity-group-merge"), {"source_group_id": source.pk, "target_group_id": target.pk, "reason": "same person"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        source.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual((source.state, target.member_count), ("merged", 3))
        event = OrganizationAuditEvent.objects.get(action="identity_groups_merged_by_moderator")
        self.assertEqual((event.actor_id, event.target_id, event.reason), (self.moderator.pk, target.pk, "same person"))
        self.assertEqual([r["id"] for r in self.client.get(self.url("identity-group-list")).json()], [target.pk])

    def test_two_different_members_folders_cannot_be_merged(self):
        a = self.folder(self.sara, "Sara's", 0)
        b = self.folder(self.omar, "Omar's", 1)
        response = self.client.post(self.url("identity-group-merge"), {"source_group_id": a.pk, "target_group_id": b.pk}, format="json")
        self.assertEqual(response.status_code, 400)
        a.refresh_from_db()
        self.assertEqual(a.state, "active")

    def test_a_folder_outside_this_organization_cannot_be_merged(self):
        inside = self.folder(self.sara, "Inside", 0)
        personal = create_personal_organization(self.sara)
        outside = self.folder(self.sara, "Outside", 1, organization=personal)
        self.assertEqual(self.client.post(self.url("identity-group-merge"), {"source_group_id": inside.pk, "target_group_id": outside.pk}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.url("identity-group-merge"), {"source_group_id": inside.pk, "target_group_id": inside.pk}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.url("identity-group-merge"), {"source_group_id": inside.pk}, format="json").status_code, 400)

    def test_a_plain_member_is_refused_everywhere_and_an_outsider_learns_nothing(self):
        member_client = self.authenticated_client(self.sara)
        outsider = self.authenticated_client(self.make_user("idrev-outsider"))
        for name in ("identity-group-list", "identity-group-suggestions"):
            self.assertEqual(member_client.get(self.url(name)).status_code, 403)
            self.assertEqual(outsider.get(self.url(name)).status_code, 404)
        self.assertEqual(member_client.post(self.url("identity-group-merge"), {"source_group_id": 1, "target_group_id": 2}, format="json").status_code, 403)

    def test_a_list_of_folders_costs_a_fixed_number_of_queries(self):
        for index in range(5):
            self.folder(self.sara, f"F{index}", index * 20, photos=2)
        with self.assertNumQueries(4):                           # user, membership, the folders, their members
            self.client.get(self.url("identity-group-list"))
