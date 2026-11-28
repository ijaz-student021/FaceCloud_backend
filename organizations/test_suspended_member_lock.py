"""A member whose membership is suspended (or removed) can reach nothing that is filed under that organization, through any owner-scoped endpoint.

What stays: the person's account and login, their personal workspace, every other organization they belong to, and the organization's own records for its moderators,
its owner and the administrator. Nothing is deleted; reactivating the membership brings the content back. See organizations/access.py.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from django.core.files.uploadedfile import SimpleUploadedFile

from downloads.test_download_approval import ApprovalFixture, png
from activity.models import ActivityEvent
from downloads.models import DownloadEvent
from identities.models import IdentityGroup
from identities.test_person_folders import FolderFixtures
from organizations.access import locked_org_ids_for, owner_is_locked_for
from organizations.models import Organization, OrganizationMembership
from organizations.services import create_organization, create_personal_organization, update_member
from uploads.models import ImageUpload, UploadBatch

User = get_user_model()
Role = OrganizationMembership.Role
State = OrganizationMembership.Status
GONE = (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND, status.HTTP_410_GONE)


def rows(response):
    data = response.json()
    if isinstance(data, dict):
        for key in ("results", "items", "images", "photos", "groups", "people"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return [data]
    return data


def ids_rows(response):
    data = response.json() if hasattr(response, 'json') else response.data
    return data['results'] if isinstance(data, dict) and 'results' in data else data


def ids_of(response, *keys):
    found = set()

    def walk(value):
        if isinstance(value, dict):
            for key in keys or ("id",):
                if isinstance(value.get(key), int):
                    found.add(value[key])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(response.json())
    return found


class LockFixture(ApprovalFixture, FolderFixtures):
    """An organization with a moderator, a member and another member; the member also has a personal workspace and a second organization."""

    def setUp(self):
        super().setUp()
        self.personal = create_personal_organization(self.member)
        self.other_owner = self.make_user("lk-owner2")
        self.second = create_organization(name="Second Co", owner=self.other_owner)
        OrganizationMembership.objects.create(organization=self.second, user=self.member, role=Role.MEMBER)
        # the member's content: filed under the organization, under the second organization, and personal
        self.org_photo = self.make_photo(self.member, "org.png")
        self.org_photo_2 = self.make_photo(self.member, "org2.png", edited=True)
        self.second_photo = self.make_photo(self.member, "second.png", organization=self.second)
        self.personal_photo = self.make_photo(self.member, "mine.png", organization=self.personal)
        self.colleague_photo = self.make_photo(self.other_member, "colleague.png")
        # person folders: one for the organization, one personal
        self.org_group = self.make_group(self.member, "Org Person")
        self.org_group_image, _ = self.add_photo(self.org_group, "g-org.jpg")
        UploadBatch.objects.filter(pk=self.org_group_image.batch_id).update(organization=self.organization)
        IdentityGroup.objects.filter(pk=self.org_group.pk).update(organization=self.organization)
        self.my_group = self.make_group(self.member, "My Person")
        self.my_group_image, _ = self.add_photo(self.my_group, "g-mine.jpg")

    def membership(self, user=None, organization=None):
        return OrganizationMembership.objects.get(user=user or self.member, organization=organization or self.organization)

    def suspend(self, status_value=State.SUSPENDED):
        return update_member(membership=self.membership(), actor_membership=self.membership(self.owner), status_value=status_value, reason="test")

    def reactivate(self):
        return update_member(membership=self.membership(), actor_membership=self.membership(self.owner), status_value=State.ACTIVE, reason="test")

    def get(self, name, *args, client=None, **query):
        return (client or self.member_client).get(reverse(name, args=args), query)

    def post(self, name, *args, body=None, client=None):
        return (client or self.member_client).post(reverse(name, args=args), body or {}, format="json")


class LockedMemberCannotReachOrganizationContentTests(LockFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.org_pks = {self.org_photo.pk, self.org_photo_2.pk, self.org_group_image.pk}
        self.kept_pks = {self.second_photo.pk, self.personal_photo.pk, self.my_group_image.pk}

    def test_a_personal_folder_that_holds_an_organization_photo_counts_only_what_the_member_can_still_reach(self):
        mixed, _ = self.add_photo(self.my_group, "mixed.jpg")
        UploadBatch.objects.filter(pk=mixed.batch_id).update(organization=self.organization)
        self.assertEqual(self.get("identities:group-detail", self.my_group.pk).data["member_count"], 2)
        self.suspend()
        detail = self.get("identities:group-detail", self.my_group.pk).data
        listed = next(row for row in ids_rows(self.get("identities:group-list")) if row["id"] == self.my_group.pk)
        self.assertEqual((detail["member_count"], detail["confirmed_count"], listed["member_count"]), (1, 1, 1))
        self.reactivate()
        self.assertEqual(self.get("identities:group-detail", self.my_group.pk).data["member_count"], 2)

    # ---- what the member could do before, so that "denied" means something
    def test_before_the_suspension_everything_is_reachable(self):
        self.assertEqual(self.get("image-upload-detail", self.org_photo.pk).status_code, 200)
        self.assertEqual(self.get("enhancement:result-detail", self.org_photo.result.pk).status_code, 200)
        self.assertEqual(self.get("image-thumbnail", self.org_photo.pk, size=256).status_code, 200)
        self.assertEqual(self.get("identities:group-detail", self.org_group.pk).status_code, 200)
        self.assertTrue(self.org_pks <= ids_of(self.get("my-image-list")))

    # ---- single objects: every route answers "not found" (or refused), never the content
    def denied_calls(self):
        photo, other, result, edit = self.org_photo, self.org_photo_2, self.org_photo.result, self.org_photo_2.edit
        calls = [
            ("image detail", "get", "image-upload-detail", [photo.pk]),
            ("image original download", "get", "image-upload-download", [photo.pk]),
            ("image preprocessing", "get", "image-preprocessing-detail", [photo.pk]),
            ("image preprocessing download", "get", "image-preprocessing-download", [photo.pk]),
            ("thumbnail", "get", "image-thumbnail", [photo.pk]),
            ("batch detail", "get", "upload-batch-detail", [photo.batch_id]),
            ("batch progress", "get", "upload-batch-progress", [photo.batch_id]),
            ("favorite", "put", "image-favorite", [photo.pk]),
            ("enhance an org photo", "post", "enhancement:image-enhance", [photo.pk]),
            ("enhance an org batch", "post", "enhancement:batch-enhance", [photo.batch_id]),
            ("compare", "post", "enhancement:image-compare", [photo.pk]),
            ("result detail", "get", "enhancement:result-detail", [result.pk]),
            ("result download", "get", "enhancement:result-download", [result.pk]),
            ("result comparison", "get", "enhancement:result-comparison", [result.pk]),
            ("result watermark preview", "get", "enhancement:result-watermark-preview", [result.pk]),
            ("result previews", "get", "enhancement:preview-list-create", [result.pk]),
            ("result edits", "get", "enhancement:edit-list-create", [other.result.pk]),
            ("edit detail", "get", "enhancement:edit-detail", [edit.pk]),
            ("edit download", "get", "enhancement:edit-download", [edit.pk]),
            ("quality of an org photo", "get", "quality:image-quality", [photo.pk]),
            ("face validation of an org photo", "get", "validation:image-validation", [photo.pk]),
            ("duplicates of an org photo", "get", "duplicates:image-analysis", [photo.pk]),
            ("identity analysis of an org photo", "get", "identities:image-analysis", [self.org_group_image.pk]),
            ("report for an org photo", "post", "reports:image-create", [photo.pk]),
            ("report for an org batch", "post", "reports:batch-create", [photo.batch_id]),
            ("package for an org batch", "post", "downloads:package-create", [photo.batch_id]),
            ("person folder detail", "get", "identities:group-detail", [self.org_group.pk]),
            ("person folder photos", "get", "identities:group-images", [self.org_group.pk]),
            ("person folder avatar", "get", "identities:group-avatar", [self.org_group.pk]),
            ("person folder rename", "patch", "identities:group-rename", [self.org_group.pk]),
            ("person folder profile", "patch", "identities:group-profile", [self.org_group.pk]),
            ("person folder export", "post", "identities:group-export-list-create", [self.org_group.pk]),
            ("person folder delete", "delete", "identities:group-delete", [self.org_group.pk]),
            ("organization detail", "get", "organizations:organization-detail", {"organization_id": self.organization.pk}),
            ("organization members", "get", "organizations:organization-member-list", {"organization_id": self.organization.pk}),
            ("organization moderator images", "get", "moderation:image-list", {"organization_id": self.organization.pk}),
        ]
        return calls

    def call(self, verb, name, args, body=None):
        url = reverse(name, kwargs=args) if isinstance(args, dict) else reverse(name, args=args)
        client = self.member_client
        if verb == "get":
            return client.get(url)
        return getattr(client, verb)(url, body or {}, format="json")

    def test_every_single_object_route_is_closed_after_the_suspension(self):
        self.suspend()
        for label, verb, name, args in self.denied_calls():
            with self.subTest(label):
                response = self.call(verb, name, args, {"name": "x", "display_name": "x"})
                self.assertIn(response.status_code, GONE, f"{label}: {response.status_code} {getattr(response, 'content', b'')[:200]}")

    def test_the_same_routes_are_closed_for_a_removed_member(self):
        self.suspend(State.REMOVED)
        for label, verb, name, args in self.denied_calls():
            with self.subTest(label):
                self.assertIn(self.call(verb, name, args, {"name": "x", "display_name": "x"}).status_code, GONE, label)

    def test_a_token_issued_before_the_suspension_changes_nothing(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken

        token = RefreshToken.for_user(self.member).access_token
        token["token_version"] = self.member.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(client.get(reverse("image-upload-detail", args=[self.org_photo.pk])).status_code, 200)
        self.suspend()
        self.assertEqual(client.get(reverse("image-upload-detail", args=[self.org_photo.pk])).status_code, 404)        # the same token, the live membership decides
        self.assertEqual(client.get(reverse("image-upload-detail", args=[self.personal_photo.pk])).status_code, 200)

    # ---- lists, counts, search: the organization's content is simply absent
    def list_calls(self):
        return [
            ("my images", "my-image-list", {}, "image"), ("images", "image-upload-list", {}, "image"), ("batches", "upload-batch-list", {}, "batch"),
            ("trash", "image-trash", {}, "image"), ("enhancement results", "enhancement:result-list", {}, "result"), ("enhancement jobs", "enhancement:job-list", {}, "text"),
            ("quality results", "quality:result-list", {}, "text"), ("duplicate analyses", "duplicates:analysis-list", {}, "text"), ("duplicate matches", "duplicates:match-list", {}, "text"),
            ("person folders", "identities:group-list", {}, "group"), ("people", "identities:people", {}, "text"), ("folder history", "identities:history-list", {}, "text"),
            ("review queue", "identities:review-list", {}, "text"), ("reports", "reports:list", {}, "text"), ("downloads", "downloads:history", {}, "text"),
            ("packages", "downloads:package-list", {}, "text"), ("download requests", "downloads:request-list-create", {}, "text"), ("share links", "sharing:link-list-create", {}, "text"),
            ("search", "search", {"q": "org"}, "text"), ("memories", "memories:memory-list", {}, "text"), ("events", "event-list", {}, "text"),
        ]

    NAMES = ("org.png", "org2.png", "trashed.png", "g-org.jpg", "Org Person")

    def test_no_list_mentions_the_organizations_photographs_afterwards(self):
        trashed = self.make_photo(self.member, "trashed.png")
        ImageUpload.all_objects.filter(pk=trashed.pk).update(trashed_at=timezone.now())
        forbidden = {
            "image": {self.org_photo.pk, self.org_photo_2.pk, trashed.pk, self.org_group_image.pk},
            "batch": {self.org_photo.batch_id, self.org_photo_2.batch_id, trashed.batch_id, self.org_group_image.batch_id},
            "result": {self.org_photo.result.pk, self.org_photo_2.result.pk, trashed.result.pk},
            "group": {self.org_group.pk},
        }
        before = {label: self.get(name, **query) for label, name, query, _ in self.list_calls()}
        self.assertTrue(forbidden["image"] - {trashed.pk} <= {row["id"] for row in rows(before["my images"])}, "the fixture must show the photographs before")
        self.assertIn(trashed.pk, {row["id"] for row in rows(before["trash"])})
        self.suspend()
        for label, name, query, kind in self.list_calls():
            with self.subTest(label):
                response = self.get(name, **query)
                self.assertIn(response.status_code, (200, 403, 404), label)
                if response.status_code != 200:
                    continue
                if kind == "text":
                    text = response.content.decode()
                    for needle in self.NAMES:
                        self.assertNotIn(needle, text, f"{label} mentions {needle}")
                else:
                    self.assertFalse({row.get("id") for row in rows(response)} & forbidden[kind], f"{label} lists the organization's {kind}s")
        trash = self.get("image-trash")
        self.assertNotIn(trashed.pk, {row["id"] for row in rows(trash)})
        self.reactivate()
        self.assertIn(trashed.pk, {row["id"] for row in rows(self.get("image-trash"))})

    def test_what_is_not_the_organizations_is_untouched(self):
        self.suspend()
        for pk in self.kept_pks:
            self.assertEqual(self.get("image-upload-detail", pk).status_code, 200, pk)
        self.assertEqual(self.get("image-thumbnail", self.personal_photo.pk, size=256).status_code, 200)
        self.assertEqual(self.get("enhancement:result-detail", self.personal_photo.result.pk).status_code, 200)
        self.assertEqual(self.get("enhancement:result-detail", self.second_photo.result.pk).status_code, 200)
        self.assertEqual(self.get("identities:group-detail", self.my_group.pk).status_code, 200)
        self.assertEqual(self.get("identities:group-images", self.my_group.pk).status_code, 200)
        self.assertTrue(self.kept_pks <= ids_of(self.get("my-image-list")))
        self.assertEqual(self.get("user-dashboard").status_code, 200)

    def test_the_dashboard_counts_leave_the_organizations_photographs_out(self):
        before = self.get("user-dashboard").json()
        self.suspend()
        after = self.get("user-dashboard").json()
        self.assertNotEqual(before, after)
        flat = repr(after)
        self.assertNotIn("org.png", flat)
        self.assertNotIn("org2.png", flat)

    def test_an_album_cannot_be_used_to_reach_them(self):
        album = self.member_client.post(reverse("albums:album-list"), {"title": "Mixed"}, format="json").json()
        added = self.member_client.post(reverse("albums:album-images", args=[album["id"]]), {"image_ids": [self.org_photo.pk, self.personal_photo.pk]}, format="json")
        self.assertEqual(added.status_code, 200)
        self.assertEqual(sorted(added.json()["added_ids"]), sorted([self.org_photo.pk, self.personal_photo.pk]))
        self.suspend()
        listed = self.get("albums:album-images", album["id"])
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn(self.org_photo.pk, ids_of(listed))
        self.assertIn(self.personal_photo.pk, ids_of(listed))
        again = self.member_client.post(reverse("albums:album-images", args=[album["id"]]), {"image_ids": [self.org_photo_2.pk]}, format="json")
        self.assertEqual(again.json().get("added_ids"), [])
        self.assertIn(self.org_photo_2.pk, again.json().get("not_found_ids", []))

    @staticmethod
    def created_id(response, key):
        body = response.json()
        return body["id"] if "id" in body else body[key]["id"]

    def test_things_made_before_the_suspension_are_closed_too_reports_exports_links_trash_and_bulk_actions(self):
        made = {}
        report = self.member_client.post(reverse("reports:image-create", args=[self.org_photo.pk]), {}, format="json")
        self.assertIn(report.status_code, (200, 201, 202), report.content)
        made["report"] = self.created_id(report, "report")
        export = self.member_client.post(reverse("identities:group-export-list-create", args=[self.org_group.pk]), {}, format="json")
        self.assertIn(export.status_code, (200, 201, 202), export.content)
        made["export"] = self.created_id(export, "export")
        link = self.member_client.post(reverse("sharing:link-list-create"), {"result": self.org_photo_2.result.pk}, format="json")
        self.assertEqual(link.status_code, 201, link.content)
        made["link"] = link.json()["id"]
        trashed = self.make_photo(self.member, "bin.png")
        ImageUpload.all_objects.filter(pk=trashed.pk).update(trashed_at=timezone.now())
        self.suspend()
        closed = [
            ("report detail", "get", "reports:detail", [made["report"]]), ("report download", "get", "reports:download", [made["report"]]),
            ("report regenerate", "post", "reports:regenerate", [made["report"]]),
            ("export detail", "get", "identities:group-export-detail", [made["export"]]), ("export download", "get", "identities:group-export-download", [made["export"]]),
            ("export share", "post", "identities:group-export-share", [made["export"]]), ("export regenerate", "post", "identities:group-export-regenerate", [made["export"]]),
            ("link detail", "get", "sharing:link-detail", [made["link"]]), ("link revoke", "post", "sharing:link-revoke", [made["link"]]),
            ("trashed photo", "delete", "image-trash-item", [trashed.pk]), ("trashed thumbnail", "get", "image-trash-thumbnail", [trashed.pk]),
        ]
        for label, verb, name, args in closed:
            with self.subTest(label):
                self.assertIn(self.call(verb, name, args).status_code, GONE, label)
        for label, name, body in (
            ("bulk delete", "image-bulk-delete", {"image_ids": [self.org_photo.pk, self.org_photo_2.pk]}),
            ("bulk favorite", "image-bulk-favorite", {"image_ids": [self.org_photo.pk], "favorite": True}),
            ("restore from trash", "image-trash-restore", {"image_ids": [trashed.pk]}),
        ):
            with self.subTest(label):
                response = self.member_client.post(reverse(name), body, format="json")
                self.assertIn(response.status_code, GONE + (status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST), label)
        self.assertTrue(ImageUpload.objects.filter(pk=self.org_photo.pk, is_favorite=False).exists(), "a locked member's bulk favourite changed nothing")
        self.assertTrue(ImageUpload.objects.filter(pk__in=[self.org_photo.pk, self.org_photo_2.pk]).count() == 2, "a locked member's bulk delete removed nothing")
        self.assertEqual(ImageUpload.all_objects.get(pk=trashed.pk).trashed_at is not None, True, "a locked member's restore changed nothing")
        self.reactivate()
        self.assertEqual(self.get("reports:detail", made["report"]).status_code, 200)

    def test_an_upload_into_the_suspended_organization_is_refused_and_a_personal_upload_works(self):
        self.suspend()
        refused = self.member_client.post(
            reverse("image-upload-single"), {"file": SimpleUploadedFile("late.png", png(size=(64, 64)), content_type="image/png"), "organization_id": self.organization.pk}, format="multipart")
        self.assertIn(refused.status_code, (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND), refused.content)
        self.assertFalse(UploadBatch.objects.filter(owner=self.member, organization=self.organization).exclude(pk__in=[self.org_photo.batch_id, self.org_photo_2.batch_id, self.org_group_image.batch_id]).exists())

    def test_the_download_history_forgets_what_was_taken_from_the_organization(self):
        for photo, name in ((self.org_photo, "org-copy.png"), (self.personal_photo, "mine-copy.png")):
            DownloadEvent.objects.create(owner=self.member, resource_type="enhanced_image", resource_id=photo.result.pk, batch_id=photo.batch_id, image_id=photo.pk, filename=name)
        text = self.get("downloads:history").content.decode()
        self.assertIn("org-copy.png", text)
        self.suspend()
        text = self.get("downloads:history").content.decode()
        self.assertNotIn("org-copy.png", text)
        self.assertIn("mine-copy.png", text)
        self.reactivate()
        self.assertIn("org-copy.png", self.get("downloads:history").content.decode())

    def test_the_activity_timeline_forgets_what_was_done_with_the_organizations_content(self):
        for kind, pk, name in (("ImageUpload", self.org_photo.pk, "org-upload.png"), ("ImageUpload", self.personal_photo.pk, "mine-upload.png"),
                               ("UploadBatch", self.org_photo.batch_id, "org-batch"), ("IdentityGroup", self.org_group.pk, "org-folder")):
            ActivityEvent.objects.create(user=self.member, category="upload", event="x", object_type=kind, object_id=str(pk), details={"name": name})
        text = self.get("user-activity").content.decode()
        self.assertIn("org-upload.png", text)
        self.suspend()
        text = self.get("user-activity").content.decode()
        for name in ("org-upload.png", "org-batch", "org-folder"):
            self.assertNotIn(name, text)
        self.assertIn("mine-upload.png", text)

    def test_a_locked_member_can_still_create_and_regenerate_personal_work_that_takes_row_locks(self):
        """Reports, folder exports and enhancements lock their rows (select_for_update); the lock must not turn that into a database error on PostgreSQL."""
        self.suspend()
        report = self.member_client.post(reverse("reports:image-create", args=[self.personal_photo.pk]), {}, format="json")
        self.assertIn(report.status_code, (200, 201, 202), report.content)
        report_id = self.created_id(report, "report")
        self.assertEqual(self.member_client.post(reverse("reports:regenerate", args=[report_id]), {}, format="json").status_code in (200, 202), True)
        export = self.member_client.post(reverse("identities:group-export-list-create", args=[self.my_group.pk]), {}, format="json")
        self.assertIn(export.status_code, (200, 201, 202), export.content)
        self.assertIn(self.member_client.post(reverse("enhancement:image-enhance", args=[self.personal_photo.pk]), {}, format="json").status_code, (200, 201, 202, 400, 409))

    def test_the_data_export_leaves_the_organizations_photographs_out(self):
        before = self.get("privacy:export").content.decode()
        self.assertIn("org.png", before)
        self.suspend()
        after = self.get("privacy:export").content.decode()
        self.assertNotIn("org.png", after)
        self.assertNotIn("org2.png", after)
        self.assertIn("mine.png", after)

    # ---- share links: a link made before the suspension goes dormant
    def test_a_public_link_to_an_organization_photograph_goes_dormant_and_wakes_up_with_the_membership(self):
        created = self.member_client.post(reverse("sharing:link-list-create"), {"result": self.org_photo.result.pk}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        token = created.json().get("token") or created.json().get("url", "").rstrip("/").rsplit("/", 1)[-1]
        mine = self.member_client.post(reverse("sharing:link-list-create"), {"result": self.personal_photo.result.pk}, format="json").json()
        mine_token = mine.get("token") or mine.get("url", "").rstrip("/").rsplit("/", 1)[-1]
        from rest_framework.test import APIClient

        anonymous = APIClient()
        self.assertEqual(anonymous.get(reverse("sharing:public-view", args=[token])).status_code, 200)
        self.suspend()
        self.assertEqual(anonymous.get(reverse("sharing:public-view", args=[token])).status_code, 410)
        self.assertEqual(anonymous.get(reverse("sharing:public-info", args=[token])).status_code in (404, 410, 200), True)
        self.assertEqual(anonymous.get(reverse("sharing:public-view", args=[mine_token])).status_code, 200)               # a personal link keeps working
        self.assertNotIn(created.json()["id"], ids_of(self.get("sharing:link-list-create")))
        self.reactivate()
        self.assertEqual(anonymous.get(reverse("sharing:public-view", args=[token])).status_code, 200)

    # ---- what the organization keeps seeing
    def test_the_moderator_and_the_owner_still_see_the_organizations_records(self):
        self.suspend()
        for client in (self.moderator_client, self.owner_client):
            listed = client.get(reverse("moderation:image-list", kwargs={"organization_id": self.organization.pk}))
            self.assertEqual(listed.status_code, 200)
            self.assertTrue({self.org_photo.pk, self.org_photo_2.pk} <= ids_of(listed), "the organization's records stay with the organization")
        self.assertTrue(ImageUpload.objects.filter(pk=self.org_photo.pk).exists())               # outside a request nothing is hidden
        self.assertEqual(OrganizationMembership.objects.filter(organization=self.organization).count(), 4)

    def test_other_members_and_the_second_organization_are_unaffected(self):
        self.suspend()
        other_client = self.authenticated_client(self.other_member)
        self.assertEqual(self.get("image-upload-detail", self.colleague_photo.pk, client=other_client).status_code, 200)
        self.assertEqual(self.get("enhancement:result-detail", self.colleague_photo.result.pk, client=other_client).status_code, 200)
        self.assertEqual(self.membership(organization=self.second).status, State.ACTIVE)
        self.assertEqual(locked_org_ids_for(self.member), {self.organization.pk})
        self.assertEqual(locked_org_ids_for(self.other_member), set())

    def test_the_person_can_still_sign_in_and_use_their_own_workspace(self):
        self.suspend()
        self.assertEqual(self.get("my-image-list").status_code, 200)
        self.assertEqual(self.get("user-dashboard").status_code, 200)
        self.assertEqual(self.get("organizations:organization-list").status_code, 200)

    def test_reactivating_the_membership_brings_everything_back(self):
        self.suspend()
        self.assertEqual(self.get("image-upload-detail", self.org_photo.pk).status_code, 404)
        self.reactivate()
        self.assertEqual(self.get("image-upload-detail", self.org_photo.pk).status_code, 200)
        self.assertEqual(self.get("enhancement:result-download", self.org_photo.result.pk).status_code, 200)
        self.assertEqual(self.get("identities:group-detail", self.org_group.pk).status_code, 200)
        self.assertTrue(self.org_pks <= ids_of(self.get("my-image-list")))

    def test_the_helpers_agree(self):
        self.assertFalse(owner_is_locked_for(self.member, self.organization.pk))
        self.assertFalse(owner_is_locked_for(self.member, None))
        self.suspend()
        self.assertTrue(owner_is_locked_for(self.member, self.organization.pk))
        self.assertFalse(owner_is_locked_for(self.member, self.second.pk))
        self.assertFalse(owner_is_locked_for(self.member, self.personal.pk))
        self.assertIsInstance(Organization.objects.get(pk=self.organization.pk), Organization)


class LockedMemberAndDownloadApprovalTests(LockFixture, TestCase):
    """Download requests and grants of a locked member: still stored, no longer reachable by them; the moderator still sees the request."""

    def setUp(self):
        super().setUp()
        self.require_approval()

    def test_requests_grants_and_files_are_closed_to_the_locked_member_only(self):
        request, images = self.make_request(images=[self.org_photo])
        self.decide(request, "approve_all")
        item = request.items.get()
        before = self.get("downloads:request-detail", request.pk)
        self.assertEqual(before.status_code, 200)
        self.suspend()
        for label, name, args in (
            ("request detail", "downloads:request-detail", [request.pk]), ("request zip", "downloads:request-zip", [request.pk]),
            ("item file", "downloads:request-item-file", [request.pk, item.pk]), ("cancel", "downloads:request-cancel", [request.pk]),
        ):
            with self.subTest(label):
                response = self.member_client.post(reverse(name, args=args)) if label == "cancel" else self.get(name, *args)
                self.assertIn(response.status_code, GONE, label)
        self.assertEqual(self.get("enhancement:result-download", self.org_photo.result.pk).status_code, 404)
        self.assertNotIn(request.pk, ids_of(self.get("downloads:request-list-create")))
        again = self.submit([self.org_photo])
        self.assertIn(again.status_code, GONE + (status.HTTP_400_BAD_REQUEST, status.HTTP_409_CONFLICT), again.content)
        # the moderator still sees the request
        seen = self.moderator_client.get(reverse("org-download-request-detail", kwargs={"organization_id": self.organization.pk, "request_id": request.pk}))
        self.assertEqual(seen.status_code, 200)
        self.reactivate()
        self.assertEqual(self.get("downloads:request-detail", request.pk).status_code, 200)
