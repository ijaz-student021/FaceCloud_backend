"""Download approval: an organization member downloads an image only after a moderator approved it (``downloads/approval.py``).

Every route that delivers a file is tried with approval OFF (nothing changes: this is the safe default for every existing organization, and personal workspaces
are never subject to it) and ON (403 without a grant, working with one, refused again when the grant expires, is revoked or the image stops being accepted).
"""
import io
import zipfile
from datetime import timedelta
from unittest import mock

from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework import status

from downloads.approval import DownloadNotAuthorized, authorize_download
from downloads.models import DownloadGrant, DownloadRequest, DownloadRequestItem
from enhancement.models import ImageEditVersion, ImageEnhancementResult
from identities.models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityGroupExport, IdentityMembership
from validation.models import FaceValidationResult
from moderation.tests import ModerationFixtureMixin
from notifications.models import UserNotification
from organizations.models import Organization, OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization, create_personal_organization
from quality_control.models import ImageQualityDecision
from sharing.models import ShareLink
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

Role = OrganizationMembership.Role
Item = DownloadRequestItem


def png(color=(120, 80, 40), size=(40, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class ApprovalFixture(ModerationFixtureMixin):
    def setUp(self):
        self.owner = self.make_user("ap-owner")
        self.organization = create_organization(name="Event Planners", owner=self.owner)
        self.moderator = self.add_member("ap-moderator", Role.MODERATOR)
        self.member = self.add_member("ap-member", Role.MEMBER)
        self.other_member = self.add_member("ap-other", Role.MEMBER)
        self.member_client = self.authenticated_client(self.member)
        self.moderator_client = self.authenticated_client(self.moderator)
        self.owner_client = self.authenticated_client(self.owner)

    # ---- builders
    def add_member(self, name, role, organization=None, **membership_extra):
        user = self.make_user(name)
        OrganizationMembership.objects.create(organization=organization or self.organization, user=user, role=role, **membership_extra)
        return user

    def require_approval(self, organization=None, hours=None):
        organization = organization or self.organization
        organization.download_approval_required = True
        organization.download_grant_hours = hours
        organization.save()

    def make_photo(self, owner, name="p.png", organization=None, decision="accepted", enhanced=True, edited=False, size=(40, 30)):
        """An accepted photograph with a real original and (by default) a finished enhancement: what a member would list in their gallery."""
        organization = organization or self.organization
        batch = UploadBatch.objects.create(owner=owner, upload_type="single", organization=organization)
        image = ImageUpload.objects.create(owner=owner, batch=batch, original_name=name, file_extension=".png", mime_type="image/png", status="uploaded", file_size=len(png()))
        image.original_file.save(name, ContentFile(png((10, 20, 30), size)), save=True)
        pre = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed", output_format="png", output_name=f"std_{name}")
        pre.processed_file.save(f"std_{name}", ContentFile(png((11, 21, 31))), save=True)
        ImageQualityDecision.objects.create(owner=owner, image=image, preprocessing_result=pre, status="completed", decision=decision)
        image.result = image.edit = None
        if enhanced:
            payload = png((200, 100, 50), size)
            image.result = ImageEnhancementResult.objects.create(
                owner=owner, image=image, preprocessing_result=pre, status="completed", output_name=f"enh_{name}", output_format="png", output_file_size=len(payload),
                output_width=40, output_height=30,
            )
            image.result.enhanced_file.save(f"enh_{name}", ContentFile(payload), save=True)
            if edited:
                edit_payload = png((1, 2, 3))
                image.edit = ImageEditVersion.objects.create(
                    owner=owner, enhancement_result=image.result, mode="manual", status="completed", output_name=f"edit_{name}", output_format="png",
                    output_file_size=len(edit_payload), output_width=40, output_height=30,
                )
                image.edit.edited_file.save(f"edit_{name}", ContentFile(edit_payload), save=True)
        return image

    def photos(self, count, owner=None, **extra):
        return [self.make_photo(owner or self.member, name=f"p{index}.png", **extra) for index in range(count)]

    # ---- calls
    def submit(self, images, client=None, organization=None, **extra):
        body = {"organization_id": (organization or self.organization).pk, "items": [{"image_id": image.pk} for image in images], **extra}
        return (client or self.member_client).post(reverse("downloads:request-list-create"), body, format="json")

    def make_request(self, images=None, **extra):
        images = images or self.photos(3)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.submit(images, **extra)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        return DownloadRequest.objects.get(pk=response.data["request"]["id"]), images

    def decide(self, download_request, action, client=None, organization=None, **body):
        url = reverse("org-download-request-decision", kwargs={"organization_id": (organization or self.organization).pk, "request_id": download_request.pk})
        with self.captureOnCommitCallbacks(execute=True):
            return (client or self.moderator_client).post(url, {"action": action, **body}, format="json")

    def item_for(self, download_request, image):
        return download_request.items.get(image=image)

    def reasons(self, code="policy_restriction", text=""):
        return {"code": code, "text": text}

    def body(self, response):
        return b"".join(response.streaming_content) if response.streaming else response.content

    def enhanced_download(self, result, client=None):
        return (client or self.member_client).get(reverse("enhancement:result-download", kwargs={"pk": result.pk}))

    def original_download(self, image, client=None):
        return (client or self.member_client).get(reverse("image-upload-download", kwargs={"pk": image.pk}))

    def audit_actions(self, action):
        return OrganizationAuditEvent.objects.filter(organization=self.organization, action=action)


class ApprovalOffTests(ApprovalFixture, TestCase):
    """The default: nothing about downloading changes."""

    def test_an_organization_does_not_require_approval_by_default(self):
        self.assertFalse(Organization.objects.get(pk=self.organization.pk).download_approval_required)

    def test_downloads_work_directly_when_approval_is_off(self):
        image = self.make_photo(self.member)
        self.assertEqual(self.original_download(image).status_code, status.HTTP_200_OK)
        self.assertEqual(self.enhanced_download(image.result).status_code, status.HTTP_200_OK)
        self.assertFalse(self.audit_actions(OrganizationAuditEvent.Action.DOWNLOAD_BLOCKED).exists())

    def test_a_request_is_refused_when_approval_is_off(self):
        response = self.submit(self.photos(1))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "approval_not_required")

    def test_a_personal_workspace_is_never_subject_to_approval(self):
        personal = create_personal_organization(self.member)
        Organization.objects.filter(pk=personal.pk).update(download_approval_required=True)       # even a flag set by hand is ignored there
        image = self.make_photo(self.member, organization=personal)
        self.assertEqual(self.original_download(image).status_code, status.HTTP_200_OK)
        self.assertEqual(self.enhanced_download(image.result).status_code, status.HTTP_200_OK)

    def test_batch_package_and_share_link_work_when_approval_is_off(self):
        image = self.make_photo(self.member)
        response = self.member_client.post(reverse("sharing:link-list-create"), {"result": image.result.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)


class AdministratorControlTests(ApprovalFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.admin_client = self.authenticated_client(self.make_user("ap-admin", role="admin"))

    def url(self):
        return reverse("organizations:organization-admin-detail", kwargs={"organization_id": self.organization.pk})

    def test_the_administrator_switches_approval_on_and_sets_the_hours(self):
        response = self.admin_client.patch(self.url(), {"download_approval_required": True, "download_grant_hours": 6}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertTrue(response.data["download_approval_required"])
        self.assertEqual(response.data["download_grant_hours"], 6)
        organization = Organization.objects.get(pk=self.organization.pk)
        self.assertTrue(organization.download_approval_required)
        event = self.audit_actions(OrganizationAuditEvent.Action.ORGANIZATION_UPDATED).get()
        self.assertEqual(event.new_value["download_approval_required"], True)

    def test_the_owner_cannot_switch_it_on(self):
        response = self.owner_client.patch(reverse("organizations:organization-detail", kwargs={"organization_id": self.organization.pk}), {"download_approval_required": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Organization.objects.get(pk=self.organization.pk).download_approval_required)

    def test_a_personal_workspace_cannot_turn_it_on(self):
        personal = create_personal_organization(self.member)
        response = self.admin_client.patch(reverse("organizations:organization-admin-detail", kwargs={"organization_id": personal.pk}), {"download_approval_required": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_members_can_read_the_setting(self):
        self.require_approval()
        response = self.member_client.get(reverse("organizations:organization-detail", kwargs={"organization_id": self.organization.pk}))
        self.assertTrue(response.data["download_approval_required"])


class DirectDownloadsAreBlockedTests(ApprovalFixture, TestCase):
    """With approval ON, an approved-quality image still cannot be downloaded without a grant, on any route."""

    def setUp(self):
        super().setUp()
        self.require_approval()
        self.image = self.make_photo(self.member, edited=True)

    def assert_blocked(self, response, reason="no_grant"):
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, getattr(response, "content", b"")[:200])
        self.assertEqual(response.data["code"], "download_approval_required")
        self.assertEqual(response.data["reason"], reason)

    def test_original_enhanced_standardized_and_edited_files_are_blocked(self):
        self.assert_blocked(self.original_download(self.image))
        self.assert_blocked(self.enhanced_download(self.image.result))
        self.assert_blocked(self.member_client.get(reverse("image-preprocessing-download", kwargs={"pk": self.image.pk})))
        self.assert_blocked(self.member_client.get(reverse("enhancement:edit-download", kwargs={"pk": self.image.edit.pk})))
        self.assertEqual(self.audit_actions(OrganizationAuditEvent.Action.DOWNLOAD_BLOCKED).count(), 4)

    def test_a_watermarked_enhanced_download_is_blocked_too(self):
        self.assert_blocked(self.member_client.get(reverse("enhancement:result-download", kwargs={"pk": self.image.result.pk}) + "?watermark=username"))

    def test_a_share_link_cannot_be_made_for_such_an_image(self):
        response = self.member_client.post(reverse("sharing:link-list-create"), {"result": self.image.result.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["reason"], "bulk_not_allowed")
        self.assertFalse(ShareLink.objects.exists())

    def test_a_share_link_made_before_approval_was_switched_on_is_no_longer_served(self):
        Organization.objects.filter(pk=self.organization.pk).update(download_approval_required=False)
        created = self.member_client.post(reverse("sharing:link-list-create"), {"result": self.image.result.pk}, format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.content)
        token = created.data["token"] if "token" in created.data else created.data["url"].rstrip("/").split("/")[-1]
        anonymous = self.client
        self.assertEqual(anonymous.get(reverse("sharing:public-view", kwargs={"token": token})).status_code, status.HTTP_200_OK)
        self.require_approval()
        self.assertEqual(anonymous.get(reverse("sharing:public-view", kwargs={"token": token})).status_code, status.HTTP_410_GONE)

    def test_a_batch_package_is_refused(self):
        response = self.member_client.post(reverse("downloads:package-create", kwargs={"batch_id": self.image.batch_id}), {"package_type": "accepted_results"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["code"], "download_approval_required")

    def test_nothing_changes_for_a_photograph_in_another_organization_without_approval(self):
        other = create_organization(name="No Approval Co", owner=self.owner)
        self.add_member("ap-member-of-other", Role.MEMBER, organization=other)
        stranger = Organization.objects.get(pk=other.pk)
        image = self.make_photo(self.member, organization=stranger)
        self.assertEqual(self.original_download(image).status_code, status.HTTP_200_OK)


class EligibilityAndSubmissionTests(ApprovalFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.require_approval()

    def check(self, images, client=None):
        body = {"organization_id": self.organization.pk, "items": [{"image_id": image.pk} for image in images]}
        return (client or self.member_client).post(reverse("downloads:request-check"), body, format="json")

    def test_eligibility_explains_why_an_image_cannot_be_requested(self):
        good = self.make_photo(self.member, "good.png")
        in_review = self.make_photo(self.member, "review.png", decision="needs_review")
        unprocessed = self.make_photo(self.member, "raw.png", enhanced=False)
        someone_elses = self.make_photo(self.other_member, "theirs.png")
        response = self.check([good, in_review, unprocessed, someone_elses])
        rows = {row["image_id"]: row for row in response.data["items"]}
        self.assertTrue(response.data["approval_required"])
        self.assertTrue(rows[good.pk]["eligible"])
        self.assertEqual(rows[in_review.pk]["reason"], "not_approved")
        self.assertEqual(rows[unprocessed.pk]["reason"], "not_processed")
        self.assertEqual(rows[someone_elses.pk]["reason"], "not_found")                      # another member's image is not even confirmed to exist
        self.assertEqual(rows[good.pk]["access_state"], "not_requested")

    def test_a_multi_image_request_keeps_the_exact_selection(self):
        images = self.photos(12)
        request, _ = self.make_request(images)
        self.assertEqual(request.status, DownloadRequest.Status.PENDING)
        self.assertEqual(sorted(request.items.values_list("image_id", flat=True)), sorted(image.pk for image in images))
        self.assertTrue(all(item.artifact_type == "enhanced" and item.artifact_id == image.result.pk for item, image in zip(request.items.order_by("image_id"), sorted(images, key=lambda i: i.pk))))
        self.assertEqual(self.audit_actions(OrganizationAuditEvent.Action.DOWNLOAD_REQUESTED).get().new_value["images"], 12)

    def test_one_ineligible_image_refuses_the_whole_request_and_saves_nothing(self):
        good = self.make_photo(self.member, "good.png")
        bad = self.make_photo(self.member, "bad.png", decision="needs_review")
        response = self.submit([good, bad])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "items_not_eligible")
        self.assertEqual([(row["image_id"], row["reason"]) for row in response.data["items"]], [(bad.pk, "not_approved")])
        self.assertFalse(DownloadRequest.objects.exists())

    def test_the_same_submission_sent_twice_makes_one_request(self):
        images = self.photos(2)
        first = self.submit(images, client_token="abc-123")
        again = self.submit(images, client_token="abc-123")
        self.assertEqual((first.status_code, again.status_code), (status.HTTP_201_CREATED, status.HTTP_200_OK))
        self.assertFalse(again.data["created"])
        self.assertEqual(first.data["request"]["id"], again.data["request"]["id"])
        self.assertEqual(DownloadRequest.objects.count(), 1)

    def test_an_image_in_an_undecided_request_cannot_be_requested_again(self):
        images = self.photos(1)
        self.make_request(images)
        response = self.submit(images)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["items"][0]["reason"], "already_requested")

    def test_the_organization_comes_from_the_membership_not_from_the_caller(self):
        outsider = self.make_user("ap-outsider")
        response = self.submit(self.photos(1), client=self.authenticated_client(outsider))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(DownloadRequest.objects.exists())

    def test_the_requester_and_the_moderators_are_told_and_email_is_queued(self):
        self.make_request(self.photos(2))
        submitted = UserNotification.objects.get(recipient=self.member, event_type="download_request_submitted")
        self.assertEqual(submitted.title, "Download Request Submitted")
        self.assertEqual(submitted.message, "Your download request has been sent to your organization's moderator for review.")
        for reviewer in (self.moderator, self.owner):
            received = UserNotification.objects.get(recipient=reviewer, event_type="download_request_received")
            self.assertIn("2 images", received.message)
            self.assertIn(self.organization.name, received.message)
            self.assertIn(received.email_status, ("queued", "sent"))                  # the e-mail carries a link to the authenticated page, never a photograph
        self.assertFalse(UserNotification.objects.filter(recipient=self.other_member).exists())


class ModeratorDecisionTests(ApprovalFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.require_approval()

    def test_approve_all_creates_one_time_limited_grant_per_image(self):
        request, images = self.make_request()
        response = self.decide(request, "approve_all")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        request.refresh_from_db()
        self.assertEqual(request.status, DownloadRequest.Status.APPROVED)
        self.assertEqual(request.decided_by, self.moderator)
        grants = list(DownloadGrant.objects.filter(item__request=request))
        self.assertEqual(len(grants), 3)
        for grant in grants:
            self.assertEqual(grant.grantee, self.member)
            self.assertLess(abs((grant.expires_at - (timezone.now() + timedelta(hours=24))).total_seconds()), 60)       # the default is 24 hours
        title = UserNotification.objects.get(recipient=self.member, event_type="download_request_decided")
        self.assertEqual(title.title, "Download Request Approved")
        self.assertEqual(self.audit_actions(OrganizationAuditEvent.Action.DOWNLOAD_DECIDED).count(), 1)

    def test_the_organization_may_set_its_own_number_of_hours(self):
        self.require_approval(hours=2)
        request, _ = self.make_request()
        self.decide(request, "approve_all")
        grant = DownloadGrant.objects.filter(item__request=request).first()
        self.assertLess(abs((grant.expires_at - (timezone.now() + timedelta(hours=2))).total_seconds()), 60)

    def test_approve_selected_denies_the_rest_and_the_status_is_partially_approved(self):
        request, images = self.make_request(self.photos(12))
        keep = [self.item_for(request, image).pk for image in images[:10]]
        response = self.decide(request, "approve_selected", item_ids=keep, reason=self.reasons("client_restriction"),
                               item_reasons={str(self.item_for(request, images[10]).pk): self.reasons("privacy_or_consent")})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        request.refresh_from_db()
        self.assertEqual(request.status, DownloadRequest.Status.PARTIALLY_APPROVED)
        self.assertEqual(request.items.filter(status="approved").count(), 10)
        denied = {item.image_id: item for item in request.items.filter(status="denied")}
        self.assertEqual(denied[images[10].pk].denial_reason_code, "privacy_or_consent")
        self.assertEqual(denied[images[11].pk].denial_reason_code, "client_restriction")
        self.assertEqual(DownloadGrant.objects.filter(item__request=request).count(), 10)       # grants only for approved items
        self.assertEqual(response.data["counts"], {"total": 12, "pending": 0, "approved": 10, "denied": 2})
        note = UserNotification.objects.get(recipient=self.member, event_type="download_request_decided")
        self.assertEqual(note.title, "Download Request Partially Approved")
        self.assertIn("10 of 12", note.message)

    def test_deny_selected_leaves_the_rest_in_review_and_the_request_is_not_final(self):
        request, images = self.make_request()
        first = self.item_for(request, images[0]).pk
        response = self.decide(request, "deny_selected", item_ids=[first], reason=self.reasons("incorrect_selection"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        request.refresh_from_db()
        self.assertEqual(request.status, DownloadRequest.Status.IN_REVIEW)
        self.assertIsNone(request.decided_at)
        self.assertFalse(UserNotification.objects.filter(recipient=self.member, event_type="download_request_decided").exists())     # told once, when everything is decided
        self.decide(request, "approve_all")
        request.refresh_from_db()
        self.assertEqual(request.status, DownloadRequest.Status.PARTIALLY_APPROVED)

    def test_deny_all_denies_every_image(self):
        request, _ = self.make_request()
        response = self.decide(request, "deny_all", reason=self.reasons("other", "Not for distribution"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        request.refresh_from_db()
        self.assertEqual(request.status, DownloadRequest.Status.DENIED)
        self.assertFalse(DownloadGrant.objects.exists())
        self.assertEqual(set(request.items.values_list("denial_reason_text", flat=True)), {"Not for distribution"})
        self.assertEqual(UserNotification.objects.get(recipient=self.member, event_type="download_request_decided").title, "Download Request Denied")

    def test_a_denial_needs_a_reason_and_other_needs_an_explanation(self):
        request, _ = self.make_request()
        self.assertEqual(self.decide(request, "deny_all").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.decide(request, "deny_all", reason=self.reasons("other")).status_code, status.HTTP_400_BAD_REQUEST)
        request.refresh_from_db()
        self.assertEqual(request.status, DownloadRequest.Status.PENDING)

    def test_an_image_that_stopped_being_accepted_is_denied_instead_of_approved(self):
        request, images = self.make_request(self.photos(2))
        ImageQualityDecision.objects.filter(image=images[0]).update(decision="needs_review")
        self.decide(request, "approve_all")
        stale = self.item_for(request, images[0])
        self.assertEqual((stale.status, stale.denial_reason_code), ("denied", "no_longer_eligible"))
        self.assertFalse(DownloadGrant.objects.filter(image=images[0]).exists())
        request.refresh_from_db()
        self.assertEqual(request.status, DownloadRequest.Status.PARTIALLY_APPROVED)

    def test_two_moderators_cannot_decide_the_same_image_twice(self):
        request, images = self.make_request()
        second = self.authenticated_client(self.add_member("ap-moderator-2", Role.MODERATOR))
        ids = [self.item_for(request, images[0]).pk]
        self.assertEqual(self.decide(request, "approve_selected", item_ids=ids, reason=self.reasons()).status_code, status.HTTP_200_OK)     # finalizes: the others were denied
        late = self.decide(request, "deny_selected", client=second, item_ids=ids, reason=self.reasons())
        self.assertEqual(late.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(late.data["code"], "request_closed")
        self.assertEqual(DownloadGrant.objects.count(), 1)

    def test_deciding_an_image_that_is_already_decided_conflicts(self):
        request, images = self.make_request()
        first, second = (self.item_for(request, image).pk for image in images[:2])
        self.decide(request, "deny_selected", item_ids=[first], reason=self.reasons())
        again = self.decide(request, "approve_selected", item_ids=[first, second], reason=self.reasons())
        self.assertEqual((again.status_code, again.data["code"]), (status.HTTP_409_CONFLICT, "item_already_decided"))

    def test_a_stale_version_is_refused(self):
        request, _ = self.make_request()
        response = self.decide(request, "approve_all", expected_version=request.version + 5)
        self.assertEqual((response.status_code, response.data["code"]), (status.HTTP_409_CONFLICT, "request_changed"))
        self.assertFalse(DownloadGrant.objects.exists())

    def test_a_moderator_cannot_decide_their_own_request(self):
        image = self.make_photo(self.moderator)
        request, _ = self.make_request([image], client=self.moderator_client)
        response = self.decide(request, "approve_all", client=self.moderator_client)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["code"], "cannot_decide_own_request")
        self.assertEqual(self.decide(request, "approve_all", client=self.owner_client).status_code, status.HTTP_200_OK)       # the owner can

    def test_a_plain_member_cannot_decide(self):
        request, _ = self.make_request()
        response = self.decide(request, "approve_all", client=self.authenticated_client(self.other_member))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_requester_can_cancel_only_before_anyone_decided(self):
        request, images = self.make_request()
        response = self.member_client.post(reverse("downloads:request-cancel", kwargs={"pk": request.pk}))
        self.assertEqual((response.status_code, response.data["status"]), (status.HTTP_200_OK, "cancelled"))
        self.assertEqual(self.decide(request, "approve_all").status_code, status.HTTP_409_CONFLICT)
        other, _ = self.make_request(self.photos(1))
        self.decide(other, "approve_all")
        self.assertEqual(self.member_client.post(reverse("downloads:request-cancel", kwargs={"pk": other.pk})).status_code, status.HTTP_409_CONFLICT)

    def test_a_failing_notification_never_undoes_the_decision(self):
        request, _ = self.make_request()
        with mock.patch("notifications.tasks.send_notification_email_task.delay", side_effect=RuntimeError("queue is down")):
            response = self.decide(request, "approve_all")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        request.refresh_from_db()
        self.assertEqual(request.status, DownloadRequest.Status.APPROVED)
        self.assertEqual(DownloadGrant.objects.count(), 3)
        note = UserNotification.objects.get(recipient=self.member, event_type="download_request_decided")
        self.assertEqual(note.email_status, "failed")                       # the failure is recorded on the notification; the approval stands
        self.assertTrue(note.email_failure_reason)

    def test_the_decision_notification_is_not_repeated(self):
        request, _ = self.make_request()
        self.decide(request, "approve_all")
        from downloads.approval import _notify_decided

        _notify_decided(request.pk)
        self.assertEqual(UserNotification.objects.filter(recipient=self.member, event_type="download_request_decided").count(), 1)


class ApprovedDownloadTests(ApprovalFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.require_approval()
        self.request, self.images = self.make_request(self.photos(3))
        self.approved, self.denied, self.undecided = self.images
        ids = [self.item_for(self.request, self.approved).pk, self.item_for(self.request, self.undecided).pk]
        self.decide(self.request, "approve_selected", item_ids=ids, reason=self.reasons("policy_restriction"))
        self.request.refresh_from_db()

    def item_url(self, image):
        return reverse("downloads:request-item-file", kwargs={"pk": self.request.pk, "item_id": self.item_for(self.request, image).pk})

    def grant_of(self, image):
        return DownloadGrant.objects.get(image=image)

    def test_an_approved_image_downloads_by_the_request_and_by_the_ordinary_route(self):
        self.assertEqual(self.member_client.get(self.item_url(self.approved)).status_code, status.HTTP_200_OK)
        response = self.enhanced_download(self.approved.result)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.grant_of(self.approved).use_count, 2)
        self.assertTrue(self.audit_actions(OrganizationAuditEvent.Action.DOWNLOAD_COMPLETED).exists())

    def test_the_grant_covers_only_the_enhanced_file_it_names(self):
        for blocked in (self.original_download(self.approved),
                        self.member_client.get(reverse("image-preprocessing-download", kwargs={"pk": self.approved.pk}))):
            self.assertEqual(blocked.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(blocked.data["reason"], "no_grant")

    def test_a_denied_image_cannot_be_downloaded_by_any_route(self):
        self.assertEqual(self.member_client.get(self.item_url(self.denied)).status_code, status.HTTP_404_NOT_FOUND)         # not an approved item
        response = self.enhanced_download(self.denied.result)
        self.assertEqual((response.status_code, response.data["reason"]), (status.HTTP_403_FORBIDDEN, "no_grant"))
        self.assertEqual(self.original_download(self.denied).status_code, status.HTTP_403_FORBIDDEN)

    def test_another_member_cannot_use_someone_elses_grant(self):
        stranger = self.authenticated_client(self.other_member)
        self.assertEqual(self.enhanced_download(self.approved.result, client=stranger).status_code, status.HTTP_404_NOT_FOUND)    # their own routes only find their own results
        with self.assertRaises(DownloadNotAuthorized):
            authorize_download(self.other_member, self.approved, "enhanced", self.approved.result.pk)

    def test_the_zip_holds_only_approved_images(self):
        response = self.member_client.get(reverse("downloads:request-zip", kwargs={"pk": self.request.pk}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = zipfile.ZipFile(io.BytesIO(self.body(response))).namelist()
        self.assertEqual(sorted(names), sorted([f"images/enh_{self.approved.original_name}", f"images/enh_{self.undecided.original_name}"]))
        self.assertTrue(all("p1" not in name for name in names))                           # the denied image (p1) is not in it
        self.assertEqual(self.grant_of(self.approved).use_count, 1)

    def test_the_zip_leaves_out_an_image_whose_grant_expired_and_refuses_when_nothing_is_left(self):
        DownloadGrant.objects.filter(image=self.undecided).update(expires_at=timezone.now() - timedelta(minutes=1))
        names = zipfile.ZipFile(io.BytesIO(self.body(self.member_client.get(reverse("downloads:request-zip", kwargs={"pk": self.request.pk}))))).namelist()
        self.assertEqual(names, [f"images/enh_{self.approved.original_name}"])
        DownloadGrant.objects.all().update(expires_at=timezone.now() - timedelta(minutes=1))
        response = self.member_client.get(reverse("downloads:request-zip", kwargs={"pk": self.request.pk}))
        self.assertEqual((response.status_code, response.data["reason"]), (status.HTTP_403_FORBIDDEN, "expired"))

    def test_an_expired_grant_stops_working(self):
        DownloadGrant.objects.filter(image=self.approved).update(expires_at=timezone.now() - timedelta(seconds=1))
        for response in (self.member_client.get(self.item_url(self.approved)), self.enhanced_download(self.approved.result)):
            self.assertEqual((response.status_code, response.data["reason"]), (status.HTTP_403_FORBIDDEN, "expired"))

    def test_a_revoked_grant_stops_working_at_once(self):
        grant = self.grant_of(self.approved)
        url = reverse("org-download-grant-revoke", kwargs={"organization_id": self.organization.pk, "grant_id": grant.pk})
        self.assertEqual(self.moderator_client.post(url, {"reason": "client asked"}, format="json").status_code, status.HTTP_200_OK)
        response = self.enhanced_download(self.approved.result)
        self.assertEqual((response.status_code, response.data["reason"]), (status.HTTP_403_FORBIDDEN, "revoked"))
        self.assertEqual(self.audit_actions(OrganizationAuditEvent.Action.DOWNLOAD_GRANT_REVOKED).count(), 1)
        self.assertEqual(self.moderator_client.post(url, {}, format="json").status_code, status.HTTP_200_OK)          # revoking twice changes nothing
        self.assertEqual(self.audit_actions(OrganizationAuditEvent.Action.DOWNLOAD_GRANT_REVOKED).count(), 1)

    def test_rejecting_the_image_afterwards_withdraws_the_permission(self):
        ImageQualityDecision.objects.filter(image=self.approved).update(decision="needs_review")
        response = self.enhanced_download(self.approved.result)
        self.assertEqual((response.status_code, response.data["reason"]), (status.HTTP_403_FORBIDDEN, "not_approved"))

    def test_losing_membership_or_suspension_withdraws_the_permission(self):
        OrganizationMembership.objects.filter(user=self.member, organization=self.organization).update(status="removed")
        # a removed member no longer has an organization to download in; the grant must not work either
        with self.assertRaises(DownloadNotAuthorized):
            authorize_download(self.member, self.approved, "enhanced", self.approved.result.pk)
        OrganizationMembership.objects.filter(user=self.member, organization=self.organization).update(status="active")
        Organization.objects.filter(pk=self.organization.pk).update(status="suspended")
        response = self.enhanced_download(self.approved.result)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("downloads.approval.settings.DOWNLOAD_GRANT_MAX_USES", 1)
    def test_a_grant_may_be_limited_to_a_number_of_uses(self):
        request, images = self.make_request(self.photos(1))
        self.decide(request, "approve_all")
        result = images[0].result
        self.assertEqual(self.enhanced_download(result).status_code, status.HTTP_200_OK)
        response = self.enhanced_download(result)
        self.assertEqual((response.status_code, response.data["reason"]), (status.HTTP_403_FORBIDDEN, "used_up"))

    def test_the_access_state_of_each_image_is_reported(self):
        body = {"organization_id": self.organization.pk, "items": [{"image_id": image.pk} for image in self.images]}
        states = {row["image_id"]: row["access_state"] for row in self.member_client.post(reverse("downloads:request-check"), body, format="json").data["items"]}
        self.assertEqual(states, {self.approved.pk: "granted", self.denied.pk: "denied", self.undecided.pk: "granted"})
        DownloadGrant.objects.filter(image=self.approved).update(expires_at=timezone.now() - timedelta(seconds=1))
        DownloadGrant.objects.filter(image=self.undecided).update(revoked_at=timezone.now())
        states = {row["image_id"]: row["access_state"] for row in self.member_client.post(reverse("downloads:request-check"), body, format="json").data["items"]}
        self.assertEqual((states[self.approved.pk], states[self.undecided.pk]), ("expired", "revoked"))


class TenantAndScopeTests(ApprovalFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.require_approval()

    def test_an_outsider_and_another_organizations_moderator_see_nothing(self):
        request, _ = self.make_request()
        other_owner = self.make_user("ap-other-owner")
        other = create_organization(name="Rival Co", owner=other_owner)
        rival_moderator = self.authenticated_client(self.add_member("ap-rival-mod", Role.MODERATOR, organization=other))
        url = reverse("org-download-request-detail", kwargs={"organization_id": self.organization.pk, "request_id": request.pk})
        self.assertEqual(rival_moderator.get(url).status_code, status.HTTP_404_NOT_FOUND)                  # not a member here: the organization is not even confirmed
        via_own = reverse("org-download-request-detail", kwargs={"organization_id": other.pk, "request_id": request.pk})
        self.assertEqual(rival_moderator.get(via_own).status_code, status.HTTP_404_NOT_FOUND)            # this organization's id with another organization's request
        decision = self.decide(request, "approve_all", client=rival_moderator, organization=other)
        self.assertEqual(decision.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(DownloadGrant.objects.exists())

    def test_another_member_cannot_read_or_cancel_a_request(self):
        request, _ = self.make_request()
        other = self.authenticated_client(self.other_member)
        self.assertEqual(other.get(reverse("downloads:request-detail", kwargs={"pk": request.pk})).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(other.post(reverse("downloads:request-cancel", kwargs={"pk": request.pk})).status_code, status.HTTP_404_NOT_FOUND)

    def test_a_moderator_who_only_sees_their_own_members_does_not_see_other_members_requests(self):
        Organization.objects.filter(pk=self.organization.pk).update(moderator_data_scope="own_members")
        second = self.add_member("ap-moderator-2", Role.MODERATOR)
        OrganizationMembership.objects.filter(user=self.member).update(invited_by=second)          # this member belongs to the second moderator
        request, _ = self.make_request()
        listing = self.moderator_client.get(reverse("org-download-request-list", kwargs={"organization_id": self.organization.pk}))
        self.assertEqual(len(listing.data), 0)
        self.assertEqual(self.decide(request, "approve_all").status_code, status.HTTP_404_NOT_FOUND)
        mine = self.authenticated_client(second).get(reverse("org-download-request-list", kwargs={"organization_id": self.organization.pk}))
        self.assertEqual([row["id"] for row in mine.data], [request.pk])
        self.assertEqual(self.decide(request, "approve_all", client=self.owner_client).status_code, status.HTTP_200_OK)          # the owner always sees everything

    def test_the_moderator_list_filters_by_status_and_shows_counts(self):
        request, _ = self.make_request()
        listing = self.moderator_client.get(reverse("org-download-request-list", kwargs={"organization_id": self.organization.pk}) + "?status=pending")
        self.assertEqual(listing.data[0]["counts"]["total"], 3)
        self.assertEqual(listing.data[0]["requester"]["username"], "ap-member")
        done = self.moderator_client.get(reverse("org-download-request-list", kwargs={"organization_id": self.organization.pk}) + "?status=approved")
        self.assertEqual(len(done.data), 0)

    def test_a_suspended_organization_accepts_no_request_and_no_decision(self):
        request, _ = self.make_request()
        Organization.objects.filter(pk=self.organization.pk).update(status="suspended")
        self.assertIn(self.decide(request, "approve_all").status_code, (status.HTTP_403_FORBIDDEN,))
        self.assertFalse(DownloadGrant.objects.exists())

    def test_the_requests_listed_to_a_member_are_only_their_own(self):
        mine, _ = self.make_request(self.photos(1))
        theirs, _ = self.make_request(self.photos(1, owner=self.other_member), client=self.authenticated_client(self.other_member))
        rows = self.member_client.get(reverse("downloads:request-list-create")).data
        self.assertEqual([row["id"] for row in rows], [mine.pk])
        self.assertNotEqual(mine.pk, theirs.pk)


class PersonFolderExportTests(ApprovalFixture, TestCase):
    """A person-folder ZIP bundles photographs, so it is refused while one of them is in an organization that requires approval."""

    def make_group_with(self, image):
        pre = image.preprocessing_result
        validation = FaceValidationResult.objects.create(owner=self.member, image=image, preprocessing_result=pre, decision="accepted", face_count=1, faces=[])
        analysis = IdentityAnalysis.objects.create(
            owner=self.member, image=image, preprocessing_result=pre, face_validation=validation, status="completed", provider="test", policy_version="test-v1",
        )
        group = IdentityGroup.objects.create(owner=self.member, display_name="Person 001", member_count=1, protected_centroid=b"c", embedding_dimension=3, provider="test", policy_version="test-v1")
        face = IdentityFace.objects.create(owner=self.member, image=image, analysis=analysis, face_index=0, protected_embedding=b"e", embedding_dimension=3, provider="test")
        IdentityMembership.objects.create(owner=self.member, identity_face=face, identity_group=group, state=IdentityMembership.State.NEW_GROUP, decision_method="test")
        return group

    def test_an_export_of_a_folder_with_such_a_photograph_is_refused(self):
        image = self.make_photo(self.member)
        group = self.make_group_with(image)
        url = reverse("identities:group-export-list-create", kwargs={"group_id": group.pk})
        self.assertNotEqual(self.member_client.post(url, {}, format="json").status_code, status.HTTP_403_FORBIDDEN)       # approval off: the export route behaves as before
        self.require_approval()
        response = self.member_client.post(url, {}, format="json")
        self.assertEqual((response.status_code, response.data["code"]), (status.HTTP_403_FORBIDDEN, "download_approval_required"))

    def test_a_finished_export_cannot_be_downloaded_or_shared_afterwards(self):
        image = self.make_photo(self.member)
        group = self.make_group_with(image)
        export = IdentityGroupExport.objects.create(
            owner=self.member, identity_group=group, status=IdentityGroupExport.Status.READY, display_name="f.zip", file_size=4,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        export.export_file.save("f.zip", ContentFile(b"PK\x05\x06" + b"\0" * 18), save=True)
        self.assertEqual(self.member_client.get(reverse("identities:group-export-download", kwargs={"pk": export.pk})).status_code, status.HTTP_200_OK)
        self.require_approval()
        for name in ("identities:group-export-download", "identities:group-export-share", "identities:group-export-regenerate"):
            call = self.member_client.get if name.endswith("download") else self.member_client.post
            self.assertEqual(call(reverse(name, kwargs={"pk": export.pk})).status_code, status.HTTP_403_FORBIDDEN, name)


# ==============================================================================================================================================================
# Corrections after the first review (C2, C3, C4, P1, P3)
# ==============================================================================================================================================================


class ReviewerAvailabilityTests(ApprovalFixture, TestCase):
    """Approval needs somebody to give it, and nobody approves their own request."""

    def setUp(self):
        super().setUp()
        self.solo_owner = self.make_user("ap-solo-owner")
        self.solo = create_organization(name="Solo Studio", owner=self.solo_owner)
        self.solo_member = self.add_member("ap-solo-member", Role.MEMBER, organization=self.solo)
        self.admin_client = self.authenticated_client(self.make_user("ap-admin-2", role="admin"))

    def enable(self, organization):
        return self.admin_client.patch(
            reverse("organizations:organization-admin-detail", kwargs={"organization_id": organization.pk}), {"download_approval_required": True}, format="json",
        )

    def test_the_administrator_cannot_enable_approval_without_an_active_moderator(self):
        response = self.enable(self.solo)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("moderator", str(response.data))
        self.assertFalse(Organization.objects.get(pk=self.solo.pk).download_approval_required)
        self.add_member("ap-solo-moderator", Role.MODERATOR, organization=self.solo)
        self.assertEqual(self.enable(self.solo).status_code, status.HTTP_200_OK)
        self.assertTrue(Organization.objects.get(pk=self.solo.pk).download_approval_required)

    def test_a_suspended_or_deactivated_moderator_does_not_count(self):
        moderator = self.add_member("ap-solo-moderator", Role.MODERATOR, organization=self.solo)
        type(moderator).objects.filter(pk=moderator.pk).update(is_suspended=True)
        self.assertEqual(self.enable(self.solo).status_code, status.HTTP_400_BAD_REQUEST)
        type(moderator).objects.filter(pk=moderator.pk).update(is_suspended=False, is_active=False)
        self.assertEqual(self.enable(self.solo).status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_owner_has_no_reviewer_when_the_only_moderator_is_gone_and_gets_a_clear_error(self):
        self.require_approval(self.solo)
        photo = self.make_photo(self.solo_owner, organization=self.solo)
        owner_client = self.authenticated_client(self.solo_owner)
        response = self.submit([photo], client=owner_client, organization=self.solo)
        self.assertEqual((response.status_code, response.data["code"]), (status.HTTP_409_CONFLICT, "no_eligible_reviewer"))
        self.assertIn("add a moderator", response.data["detail"])
        self.assertFalse(DownloadRequest.objects.exists())
        check = owner_client.post(reverse("downloads:request-check"), {"organization_id": self.solo.pk, "items": [{"image_id": photo.pk}]}, format="json")
        self.assertFalse(check.data["reviewer_available"])

    def test_a_member_of_the_same_organization_can_still_request_because_the_owner_reviews(self):
        self.require_approval(self.solo)
        photo = self.make_photo(self.solo_member, organization=self.solo)
        client = self.authenticated_client(self.solo_member)
        self.assertEqual(self.submit([photo], client=client, organization=self.solo).status_code, status.HTTP_201_CREATED)
        check = client.post(reverse("downloads:request-check"), {"organization_id": self.solo.pk, "items": [{"image_id": photo.pk}]}, format="json")
        self.assertTrue(check.data["reviewer_available"])

    def test_nobody_gets_a_way_to_approve_their_own_request_and_the_administrator_cannot_decide(self):
        self.require_approval(self.solo)
        self.add_member("ap-solo-moderator", Role.MODERATOR, organization=self.solo)
        photo = self.make_photo(self.solo_member, organization=self.solo)
        with self.captureOnCommitCallbacks(execute=True):
            made = self.submit([photo], client=self.authenticated_client(self.solo_member), organization=self.solo)
        url = reverse("org-download-request-decision", kwargs={"organization_id": self.solo.pk, "request_id": made.data["request"]["id"]})
        self.assertEqual(self.admin_client.post(url, {"action": "approve_all"}, format="json").status_code, status.HTTP_404_NOT_FOUND)       # not a member: no unrestricted admin access
        self.assertEqual(self.authenticated_client(self.solo_member).post(url, {"action": "approve_all"}, format="json").status_code, status.HTTP_403_FORBIDDEN)


class ReviewThrottleTests(ApprovalFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.require_approval()
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)

    def test_decisions_have_their_own_limit_of_120_an_hour(self):
        from django.conf import settings

        from downloads.approval_views import OrganizationDownloadGrantRevokeView, OrganizationDownloadRequestDecisionView

        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        self.assertEqual(rates["download_review"], "120/hour")
        self.assertEqual(OrganizationDownloadRequestDecisionView.throttle_scope, "download_review")
        self.assertEqual(OrganizationDownloadGrantRevokeView.throttle_scope, "download_review")
        self.assertNotEqual(OrganizationDownloadRequestDecisionView.throttle_scope, "org_invite")

    def test_a_moderator_who_reaches_the_limit_is_throttled_and_the_invitation_limit_is_untouched(self):
        from rest_framework.throttling import SimpleRateThrottle

        request, images = self.make_request(self.photos(4))
        ids = [self.item_for(request, image).pk for image in images]
        rates = {**SimpleRateThrottle.THROTTLE_RATES, "download_review": "2/hour"}
        with mock.patch.object(SimpleRateThrottle, "THROTTLE_RATES", rates):
            codes = [self.decide(request, "deny_selected", item_ids=[item_id], reason=self.reasons()).status_code for item_id in ids[:3]]
            invite = self.moderator_client.post(reverse("organizations:organization-invitation-list-create", kwargs={"organization_id": self.organization.pk}), {"email": "new@example.com", "role": "member"}, format="json")
        self.assertEqual(codes, [status.HTTP_200_OK, status.HTTP_200_OK, status.HTTP_429_TOO_MANY_REQUESTS])
        self.assertEqual(invite.status_code, status.HTTP_201_CREATED)                   # decisions did not use up the invitation allowance
        self.assertEqual(request.items.filter(status="denied").count(), 2)


class NotificationReliabilityTests(ApprovalFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.require_approval()

    def test_one_failing_reviewer_does_not_stop_the_others(self):
        from notifications.services import create_user_notification as real

        def flaky(**kwargs):
            if kwargs["recipient"] == self.moderator and kwargs["event_type"] == "download_request_received":
                raise RuntimeError("this moderator's notification fails")
            return real(**kwargs)

        with mock.patch("notifications.services.create_user_notification", side_effect=flaky):
            request, _ = self.make_request()
        self.assertTrue(UserNotification.objects.filter(recipient=self.owner, event_type="download_request_received").exists())
        self.assertFalse(UserNotification.objects.filter(recipient=self.moderator, event_type="download_request_received").exists())
        self.assertTrue(UserNotification.objects.filter(recipient=self.member, event_type="download_request_submitted").exists())
        self.assertEqual(request.status, DownloadRequest.Status.PENDING)

    def test_the_request_stays_in_the_moderator_queue_when_every_notification_fails(self):
        with mock.patch("notifications.services.create_user_notification", side_effect=RuntimeError("down")):
            request, _ = self.make_request()
        listing = self.moderator_client.get(reverse("org-download-request-list", kwargs={"organization_id": self.organization.pk}) + "?status=pending,in_review")
        self.assertEqual([row["id"] for row in listing.data], [request.pk])
        self.assertEqual(self.decide(request, "approve_all").status_code, status.HTTP_200_OK)

    def test_a_review_task_survives_muted_preferences_but_sends_no_e_mail(self):
        from notifications.models import NotificationPreference

        NotificationPreference.objects.update_or_create(user=self.moderator, defaults={"moderator_review_updates": False, "email_notifications": False})
        self.make_request()
        task = UserNotification.objects.get(recipient=self.moderator, event_type="download_request_received")
        self.assertEqual(task.email_status, "skipped")
        self.assertIsNone(task.read_at)

    def test_a_muted_review_preference_alone_keeps_the_task_but_not_the_e_mail(self):
        from notifications.models import NotificationPreference

        NotificationPreference.objects.update_or_create(user=self.moderator, defaults={"moderator_review_updates": False, "email_notifications": True})
        self.make_request()
        self.assertEqual(UserNotification.objects.get(recipient=self.moderator, event_type="download_request_received").email_status, "skipped")
        self.assertIn(UserNotification.objects.get(recipient=self.owner, event_type="download_request_received").email_status, ("queued", "sent"))


class ClientTokenTests(ApprovalFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.require_approval()

    def test_the_same_token_with_a_different_selection_is_a_conflict(self):
        first, second, third = self.photos(3)
        made = self.submit([first, second], client_token="tok-1")
        self.assertEqual(made.status_code, status.HTTP_201_CREATED)
        clash = self.submit([first, third], client_token="tok-1")
        self.assertEqual((clash.status_code, clash.data["code"]), (status.HTTP_409_CONFLICT, "client_token_reused"))
        smaller = self.submit([first], client_token="tok-1")
        self.assertEqual(smaller.data["code"], "client_token_reused")
        self.assertEqual(DownloadRequest.objects.count(), 1)

    def test_the_same_token_with_a_different_file_scope_is_a_conflict(self):
        photo = self.make_photo(self.member, "p.png")
        self.assertEqual(self.submit([photo], client_token="tok-2").status_code, status.HTTP_201_CREATED)
        response = self.member_client.post(reverse("downloads:request-list-create"), {
            "organization_id": self.organization.pk, "client_token": "tok-2", "items": [{"image_id": photo.pk, "artifact_type": "original"}],
        }, format="json")
        self.assertEqual((response.status_code, response.data["code"]), (status.HTTP_409_CONFLICT, "client_token_reused"))

    def test_the_same_token_with_the_same_selection_still_returns_the_request(self):
        photos = self.photos(2)
        first = self.submit(photos, client_token="tok-3")
        again = self.submit(list(reversed(photos)), client_token="tok-3")            # order does not matter
        self.assertEqual((again.status_code, again.data["created"]), (status.HTTP_200_OK, False))
        self.assertEqual(first.data["request"]["id"], again.data["request"]["id"])


class RestrictedPreviewTests(ApprovalFixture, TestCase):
    """Approval controls delivery, and a preview is a copy: while the owner holds no approval they see at most 800 px, watermarked."""

    def setUp(self):
        super().setUp()
        self.big = self.make_photo(self.member, "big.png", size=(1600, 1200))

    def thumbnail(self, image, size=1024, version="original", client=None):
        return (client or self.member_client).get(reverse("image-thumbnail", kwargs={"pk": image.pk}) + f"?size={size}&version={version}")

    def dims(self, response):
        return Image.open(io.BytesIO(self.body(response))).size

    def clean(self, field, size):
        from thumbnails.services import render_thumbnail

        return render_thumbnail(field, size)[0]

    def grant(self, image):
        DownloadGrant.objects.all().delete()
        request = DownloadRequest.objects.create(organization=self.organization, requester=self.member)
        item = Item.objects.create(request=request, image=image, artifact_type="enhanced", artifact_id=image.result.pk)
        return DownloadGrant.objects.create(
            item=item, organization=self.organization, grantee=self.member, image=image, artifact_type="enhanced", artifact_id=image.result.pk,
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def test_without_an_approval_the_preview_is_at_most_800_px_and_watermarked(self):
        self.require_approval()
        for version, field in (("original", self.big.original_file), ("enhanced", self.big.result.enhanced_file)):
            response = self.thumbnail(self.big, 1024, version)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertLessEqual(max(self.dims(response)), 800)
            self.assertEqual(response["X-Preview-Restricted"], "approval-required")
            self.assertNotEqual(self.body(response), self.clean(field, 800))                     # the tiled watermark is in the pixels
            self.assertIn("no-store", response["Cache-Control"])
        self.assertLessEqual(max(self.dims(self.thumbnail(self.big, 256))), 256)                  # a small thumbnail is stamped too

    def test_a_clean_cached_thumbnail_is_never_used_for_it(self):
        self.thumbnail(self.big, 1024)                                                          # approval off: the clean 1024 px thumbnail is made and cached
        self.require_approval()
        response = self.thumbnail(self.big, 1024)
        self.assertLessEqual(max(self.dims(response)), 800)

    def test_with_an_active_approval_the_normal_preview_is_served(self):
        self.require_approval()
        self.grant(self.big)
        response = self.thumbnail(self.big, 1024)
        self.assertEqual(max(self.dims(response)), 1024)
        self.assertNotIn("X-Preview-Restricted", response)
        DownloadGrant.objects.all().update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertLessEqual(max(self.dims(self.thumbnail(self.big, 1024))), 800)                # the approval ended: restricted again

    def test_nothing_changes_when_approval_is_off_or_in_a_personal_workspace(self):
        self.assertEqual(max(self.dims(self.thumbnail(self.big, 1024))), 1024)
        personal = create_personal_organization(self.member)
        Organization.objects.filter(pk=personal.pk).update(download_approval_required=True)
        mine = self.make_photo(self.member, "mine.png", organization=personal, size=(1600, 1200))
        response = self.thumbnail(mine, 1024)
        self.assertEqual(max(self.dims(response)), 1024)
        self.assertNotIn("X-Preview-Restricted", response)

    def test_the_watermark_preview_route_is_restricted_too(self):
        self.require_approval()
        url = reverse("enhancement:result-watermark-preview", kwargs={"pk": self.big.result.pk}) + "?watermark=small"
        response = self.member_client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content[:200])
        self.assertLessEqual(max(self.dims(response)), 800)
        self.assertEqual(response["X-Preview-Restricted"], "approval-required")

    def test_comparison_and_edit_previews_are_restricted(self):
        from enhancement.models import EditPreview, EditPreviewSource, ModelComparison, ModelComparisonOutput

        comparison = ModelComparison.objects.create(owner=self.member, image=self.big, requested_level="auto", profiles=["natural"], status="completed")
        output = ModelComparisonOutput.objects.create(comparison=comparison, profile="natural", outcome="completed")
        output.preview_file.save("c.jpg", ContentFile(png((9, 9, 9), (1600, 1200))), save=True)
        source = EditPreviewSource.objects.create(enhancement_result=self.big.result, source_version="x", width=1600, height=1200, full_width=1600, full_height=1200)
        source.file.save("s.png", ContentFile(png((1, 1, 1), (1600, 1200))), save=True)
        preview = EditPreview.objects.create(
            owner=self.member, enhancement_result=self.big.result, source=source, revision=1, settings_hash="h" * 64, mode="manual", output_format="png", width=1600, height=1200,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        preview.file.save("p.png", ContentFile(png((5, 5, 5), (1600, 1200))), save=True)
        compare_url = reverse("enhancement:comparison-preview", kwargs={"pk": comparison.pk, "profile": "natural"})
        edit_url = reverse("enhancement:preview-image", kwargs={"pk": preview.pk})
        self.assertEqual(Image.open(io.BytesIO(self.body(self.member_client.get(compare_url)))).size, (1600, 1200))          # approval off: as before
        self.assertEqual(Image.open(io.BytesIO(self.body(self.member_client.get(edit_url)))).size, (1600, 1200))
        self.require_approval()
        for url in (compare_url, edit_url):
            response = self.member_client.get(url)
            self.assertLessEqual(max(self.dims(response)), 800, url)
            self.assertEqual(response["X-Preview-Restricted"], "approval-required")

    def test_a_moderator_keeps_the_review_previews_of_other_members_images(self):
        self.require_approval()
        url = reverse("moderation:image-preview", kwargs={"organization_id": self.organization.pk, "image_id": self.big.pk}) + "?variant=original&size=1024"
        response = self.moderator_client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(max(self.dims(response)), 1024)
        self.assertNotIn("X-Preview-Restricted", response)

    def test_a_moderator_looking_at_their_own_image_is_restricted_like_any_owner(self):
        self.require_approval()
        own = self.make_photo(self.moderator, "mod.png", size=(1600, 1200))
        url = reverse("moderation:image-preview", kwargs={"organization_id": self.organization.pk, "image_id": own.pk}) + "?variant=original&size=1024"
        response = self.moderator_client.get(url)
        self.assertLessEqual(max(self.dims(response)), 800)
        self.assertEqual(response["X-Preview-Restricted"], "approval-required")
        enhanced = self.moderator_client.get(url.replace("original", "enhanced"))
        self.assertLessEqual(max(self.dims(enhanced)), 800)

    def test_another_member_still_cannot_see_the_image_at_all(self):
        self.require_approval()
        self.assertEqual(self.thumbnail(self.big, 256, client=self.authenticated_client(self.other_member)).status_code, status.HTTP_404_NOT_FOUND)


class AvatarAndReportPreviewTests(ApprovalFixture, TestCase):
    """Person-folder avatars (96-384 px) and the pictures inside a processing report PDF (160 px) are copies of the image too: while the owner holds no approval they are watermarked."""

    def setUp(self):
        super().setUp()
        self.photo = self.make_photo(self.member, "face.png", size=(200, 150))

    # ---- avatars
    def make_group(self, image, owner=None, name="Person 001"):
        owner = owner or self.member
        pre = image.preprocessing_result
        validation = FaceValidationResult.objects.create(owner=owner, image=image, preprocessing_result=pre, decision="accepted", face_count=1, faces=[])
        analysis = IdentityAnalysis.objects.create(owner=owner, image=image, preprocessing_result=pre, face_validation=validation, status="completed", provider="test", policy_version="v1")
        group = IdentityGroup.objects.create(owner=owner, display_name=name, member_count=1, protected_centroid=b"c", embedding_dimension=3, provider="test", policy_version="v1")
        face = IdentityFace.objects.create(
            owner=owner, image=image, analysis=analysis, face_index=0, protected_embedding=b"e", embedding_dimension=3, provider="test",
            bounding_box={"x": 2, "y": 2, "width": 30, "height": 20}, quality={"confidence": 0.9, "brightness": 90},
        )
        IdentityMembership.objects.create(owner=owner, identity_face=face, identity_group=group, state=IdentityMembership.State.NEW_GROUP, decision_method="test")
        return group, face

    def avatar(self, group, size=384, client=None):
        return (client or self.member_client).get(reverse("identities:group-avatar", kwargs={"pk": group.pk}) + f"?size={size}")

    def clean_avatar(self, face, size):
        from identities.avatars import render_avatar

        return render_avatar(face, size)

    def test_an_avatar_is_watermarked_while_no_approval_is_held(self):
        group, face = self.make_group(self.photo)
        self.require_approval()
        for size in (384, 96):
            response = self.avatar(group, size)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            body = self.body(response)
            self.assertEqual(Image.open(io.BytesIO(body)).size, (size, size))                 # still reduced: never larger than asked, and never above 384 for an avatar
            self.assertEqual(response["X-Preview-Restricted"], "approval-required")
            self.assertIn("no-store", response["Cache-Control"])
            self.assertNotEqual(body, self.clean_avatar(face, size))                           # the tiled watermark is in the pixels

    def test_an_avatar_is_unchanged_with_an_approval_with_approval_off_and_in_a_personal_workspace(self):
        group, face = self.make_group(self.photo)
        clean = self.clean_avatar(face, 384)
        off = self.avatar(group)
        self.assertEqual((self.body(off), "X-Preview-Restricted" in off), (clean, False))
        self.require_approval()
        DownloadGrant.objects.all().delete()
        request = DownloadRequest.objects.create(organization=self.organization, requester=self.member)
        item = Item.objects.create(request=request, image=self.photo, artifact_type="enhanced", artifact_id=self.photo.result.pk)
        grant = DownloadGrant.objects.create(
            item=item, organization=self.organization, grantee=self.member, image=self.photo, artifact_type="enhanced", artifact_id=self.photo.result.pk, expires_at=timezone.now() + timedelta(hours=1),
        )
        with_grant = self.avatar(group)
        self.assertEqual((self.body(with_grant), "X-Preview-Restricted" in with_grant), (clean, False))
        DownloadGrant.objects.filter(pk=grant.pk).update(revoked_at=timezone.now())
        self.assertEqual(self.avatar(group)["X-Preview-Restricted"], "approval-required")          # the approval ended
        personal = create_personal_organization(self.member)
        Organization.objects.filter(pk=personal.pk).update(download_approval_required=True)
        mine, mine_face = self.make_group(self.make_photo(self.member, "mine.png", organization=personal, size=(200, 150)), name="Person 002")
        response = self.avatar(mine)
        self.assertEqual((self.body(response), "X-Preview-Restricted" in response), (self.clean_avatar(mine_face, 384), False))

    def test_a_moderator_is_restricted_only_for_their_own_faces_and_nobody_reads_another_members_folder(self):
        self.require_approval()
        own_group, own_face = self.make_group(self.make_photo(self.moderator, "mod.png", size=(200, 150)), owner=self.moderator)
        self.assertEqual(self.avatar(own_group, client=self.moderator_client)["X-Preview-Restricted"], "approval-required")
        theirs, _ = self.make_group(self.photo)
        self.assertEqual(self.avatar(theirs, client=self.moderator_client).status_code, status.HTTP_404_NOT_FOUND)          # the avatar route is owner-only; moderators review through the review previews

    # ---- report PDF pictures
    def embedded_jpegs(self, pdf):
        """The JPEG of every picture in the PDF, byte for byte (reportlab stores a JPEG as /ASCII85Decode + /DCTDecode)."""
        import base64
        import re

        found = []
        for match in re.finditer(rb"/Subtype /Image.*?stream\r?\n(.*?)endstream", pdf, re.S):
            data = match.group(1).strip()
            found.append(base64.a85decode(data[:-2] if data.endswith(b"~>") else data))
        return found

    def report_pdf(self, viewer, image=None):
        from reports.renderers import render_pdf
        from reports.test_readable_reports import batch_payload, record

        image = image or self.photo
        return render_pdf(batch_payload([record(image.pk, image.original_name)]), viewer)

    def clean_picture(self, image):
        from thumbnails.services import enhanced_result, render_thumbnail

        return render_thumbnail(enhanced_result(image).enhanced_file, 160)[0]

    def test_the_picture_in_a_report_is_watermarked_while_no_approval_is_held(self):
        self.require_approval()
        pictures = self.embedded_jpegs(self.report_pdf(self.member))
        self.assertEqual(len(pictures), 1)
        stamped = Image.open(io.BytesIO(pictures[0]))
        self.assertLessEqual(max(stamped.size), 160)                                      # still the small picture
        self.assertNotEqual(pictures[0], self.clean_picture(self.photo))

    def test_the_picture_in_a_report_is_unchanged_in_every_other_case(self):
        clean = self.clean_picture(self.photo)
        self.assertEqual(self.embedded_jpegs(self.report_pdf(self.member)), [clean])      # approval off
        self.assertEqual(self.embedded_jpegs(self.report_pdf(None)), [clean])             # no viewer given: as before
        self.require_approval()
        self.assertEqual(self.embedded_jpegs(self.report_pdf(self.moderator)), [clean])   # a moderator's report on a member's photo: authorized review, not restricted
        personal = create_personal_organization(self.member)
        Organization.objects.filter(pk=personal.pk).update(download_approval_required=True)
        mine = self.make_photo(self.member, "mine.png", organization=personal, size=(200, 150))
        self.assertEqual(self.embedded_jpegs(self.report_pdf(self.member, mine)), [self.clean_picture(mine)])
        request = DownloadRequest.objects.create(organization=self.organization, requester=self.member)
        item = Item.objects.create(request=request, image=self.photo, artifact_type="enhanced", artifact_id=self.photo.result.pk)
        DownloadGrant.objects.create(
            item=item, organization=self.organization, grantee=self.member, image=self.photo, artifact_type="enhanced", artifact_id=self.photo.result.pk, expires_at=timezone.now() + timedelta(hours=1),
        )
        self.assertEqual(self.embedded_jpegs(self.report_pdf(self.member)), [clean])      # an active approval: the picture is clean

    def test_the_report_task_makes_the_report_for_its_owner(self):
        from reports.models import ProcessingReport
        from reports.tasks import process_processing_report
        from reports.test_readable_reports import batch_payload, record

        self.require_approval()
        report = ProcessingReport.objects.create(owner=self.member, report_type="batch", format="pdf", status="queued", batch=self.photo.batch)
        payload = batch_payload([record(self.photo.pk, self.photo.original_name)])
        with mock.patch("reports.tasks.build_report_payload", return_value=payload):
            process_processing_report(report.pk, "task-1")
        report.refresh_from_db()
        self.assertEqual(report.status, "ready", report.failure_reason)
        pictures = self.embedded_jpegs(report.report_file.read())
        self.assertEqual(len(pictures), 1)
        self.assertNotEqual(pictures[0], self.clean_picture(self.photo))                  # the file the member downloads carries the watermarked picture


# ==============================================================================================================================================================
# Image organization fields and stored report PDFs
# ==============================================================================================================================================================


class ImageOrganizationFieldTests(ApprovalFixture, TestCase):
    """The image and batch APIs say which workspace a photograph is in, from its batch, never from the owner's memberships."""

    def list_rows(self, client=None):
        response = (client or self.member_client).get(reverse("my-image-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        return {row["id"]: row for row in rows}

    def test_an_organization_photograph_reports_its_organization_and_whether_approval_is_required(self):
        photo = self.make_photo(self.member)
        row = self.list_rows()[photo.pk]
        self.assertEqual((row["organization_id"], row["download_approval_required"]), (self.organization.pk, False))
        self.require_approval()
        row = self.list_rows()[photo.pk]
        self.assertEqual((row["organization_id"], row["download_approval_required"]), (self.organization.pk, True))
        detail = self.member_client.get(reverse("image-upload-detail", kwargs={"pk": photo.pk}))
        self.assertEqual((detail.data["organization_id"], detail.data["download_approval_required"]), (self.organization.pk, True))
        plain = self.member_client.get(reverse("image-upload-list"))
        plain_rows = plain.data["results"] if isinstance(plain.data, dict) else plain.data
        self.assertEqual([(r["organization_id"], r["download_approval_required"]) for r in plain_rows if r["id"] == photo.pk], [(self.organization.pk, True)])

    def test_a_personal_workspace_photograph_never_requires_approval_even_if_the_flag_is_set_by_hand(self):
        personal = create_personal_organization(self.member)
        Organization.objects.filter(pk=personal.pk).update(download_approval_required=True)
        mine = self.make_photo(self.member, "mine.png", organization=personal)
        row = self.list_rows()[mine.pk]
        self.assertEqual((row["organization_id"], row["download_approval_required"]), (personal.pk, False))

    def test_the_organization_comes_from_the_batch_not_from_the_owners_memberships(self):
        self.require_approval()
        personal = create_personal_organization(self.member)
        uploaded_to_personal = self.make_photo(self.member, "private.png", organization=personal)        # the member belongs to the organization, but uploaded into their own workspace
        row = self.list_rows()[uploaded_to_personal.pk]
        self.assertEqual((row["organization_id"], row["download_approval_required"]), (personal.pk, False))
        self.assertNotEqual(row["organization_id"], self.organization.pk)

    def test_a_photograph_without_a_workspace_reports_null_and_false(self):
        photo = self.make_photo(self.member)
        UploadBatch.objects.filter(pk=photo.batch_id).update(organization=None)                         # a row from before workspaces existed
        row = self.list_rows()[photo.pk]
        self.assertEqual((row["organization_id"], row["download_approval_required"]), (None, False))

    def test_the_batch_reports_its_organization_and_its_photographs_carry_the_fields(self):
        self.require_approval()
        photo = self.make_photo(self.member)
        response = self.member_client.get(reverse("upload-batch-detail", kwargs={"pk": photo.batch_id}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["organization_id"], self.organization.pk)
        self.assertEqual([(i["organization_id"], i["download_approval_required"]) for i in response.data["images"]], [(self.organization.pk, True)])

    def test_nobody_sees_another_members_photograph_through_these_fields(self):
        theirs = self.make_photo(self.other_member)
        self.assertNotIn(theirs.pk, self.list_rows())

    def test_listing_many_photographs_does_not_look_up_the_organization_for_each(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.require_approval()
        self.photos(1)
        with CaptureQueriesContext(connection) as one:
            self.list_rows()
        self.photos(8)
        with CaptureQueriesContext(connection) as many:
            self.list_rows()

        def organization_selects(captured):
            return [q for q in captured if q["sql"].lstrip().startswith("SELECT") and 'FROM "organizations_organization"' in q["sql"]]

        self.assertLessEqual(len(organization_selects(many)), max(1, len(organization_selects(one))))


def jpegs_in_pdf(pdf):
    """The JPEG of every picture in a PDF, byte for byte (reportlab stores a JPEG as /ASCII85Decode + /DCTDecode)."""
    import base64
    import re

    found = []
    for match in re.finditer(rb"/Subtype /Image.*?stream\r?\n(.*?)endstream", pdf, re.S):
        data = match.group(1).strip()
        found.append(base64.a85decode(data[:-2] if data.endswith(b"~>") else data))
    return found


class StoredReportPdfTests(ApprovalFixture, TestCase):
    """A report file is stored; its pictures were decided when it was made. Downloading it again must follow the CURRENT approval, so a stored PDF cannot outlive an approval."""

    def setUp(self):
        super().setUp()
        self.photo = self.make_photo(self.member, "face.png", size=(200, 150))

    def stored_report(self, *, viewer=None, image=None, report_format="pdf", content=None):
        from reports.models import ProcessingReport
        from reports.renderers import render_pdf
        from reports.test_readable_reports import batch_payload, record

        image = image or self.photo
        payload = batch_payload([record(image.pk, image.original_name)])
        content = content if content is not None else render_pdf(payload, viewer)
        report = ProcessingReport.objects.create(
            owner=self.member, report_type="batch", format=report_format, status="ready", batch=image.batch, payload=payload,
            content_type="application/pdf" if report_format == "pdf" else "text/csv", file_size=len(content),
        )
        report.report_file.save(f"report.{report_format}", ContentFile(content), save=True)
        return report, content

    def download(self, report, client=None):
        return (client or self.member_client).get(reverse("reports:download", kwargs={"pk": report.pk}))

    def clean_picture(self, image):
        from thumbnails.services import enhanced_result, render_thumbnail

        return render_thumbnail(enhanced_result(image).enhanced_file, 160)[0]

    def grant(self, hours=1):
        request = DownloadRequest.objects.create(organization=self.organization, requester=self.member)
        item = Item.objects.create(request=request, image=self.photo, artifact_type="enhanced", artifact_id=self.photo.result.pk)
        return DownloadGrant.objects.create(
            item=item, organization=self.organization, grantee=self.member, image=self.photo, artifact_type="enhanced", artifact_id=self.photo.result.pk,
            expires_at=timezone.now() + timedelta(hours=hours),
        )

    def test_nothing_changes_when_approval_is_off(self):
        report, content = self.stored_report()
        response = self.download(report)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual((self.body(response), "X-Report-Restricted" in response), (content, False))

    def test_a_pdf_made_before_approval_was_switched_on_is_served_as_a_watermarked_copy(self):
        report, content = self.stored_report()
        self.assertEqual(jpegs_in_pdf(content), [self.clean_picture(self.photo)])                 # the stored file shows the picture clean
        self.require_approval()
        response = self.download(report)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = self.body(response)
        self.assertNotEqual(body, content)
        pictures = jpegs_in_pdf(body)
        self.assertEqual(len(pictures), 1)
        self.assertNotEqual(pictures[0], self.clean_picture(self.photo))
        self.assertLessEqual(max(Image.open(io.BytesIO(pictures[0])).size), 160)
        self.assertEqual(response["X-Report-Restricted"], "approval-required")
        self.assertEqual(response["Content-Type"], "application/pdf")
        report.refresh_from_db()
        self.assertEqual(report.report_file.read(), content)                                      # the stored file itself is untouched
        from downloads.models import DownloadEvent

        self.assertEqual(DownloadEvent.objects.filter(owner=self.member, resource_type="processing_report").latest("pk").file_size, len(body))

    def test_a_pdf_made_during_an_approval_cannot_be_downloaded_clean_after_it_expires_or_is_revoked(self):
        self.require_approval()
        grant = self.grant()
        report, content = self.stored_report(viewer=self.member)                                  # made while the approval was active: the picture is clean
        self.assertEqual(jpegs_in_pdf(content), [self.clean_picture(self.photo)])
        active = self.download(report)
        self.assertEqual((self.body(active), "X-Report-Restricted" in active), (content, False))   # the approval is active: the stored file as it is
        DownloadGrant.objects.filter(pk=grant.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        expired = self.download(report)
        self.assertEqual(expired["X-Report-Restricted"], "approval-required")
        self.assertNotEqual(jpegs_in_pdf(self.body(expired)), [self.clean_picture(self.photo)])
        DownloadGrant.objects.filter(pk=grant.pk).update(expires_at=timezone.now() + timedelta(hours=1), revoked_at=timezone.now())
        revoked = self.download(report)
        self.assertEqual(revoked["X-Report-Restricted"], "approval-required")

    def test_a_pdf_made_while_restricted_is_served_as_stored(self):
        self.require_approval()
        report, content = self.stored_report(viewer=self.member)                                  # no approval: its picture was watermarked when it was made
        self.assertNotEqual(jpegs_in_pdf(content), [self.clean_picture(self.photo)])
        self.grant()
        response = self.download(report)
        self.assertEqual(self.body(response), content)

    def test_personal_workspace_and_other_formats_are_unchanged(self):
        personal = create_personal_organization(self.member)
        Organization.objects.filter(pk=personal.pk).update(download_approval_required=True)
        mine = self.make_photo(self.member, "mine.png", organization=personal, size=(200, 150))
        report, content = self.stored_report(image=mine)
        self.assertEqual(self.body(self.download(report)), content)
        self.require_approval()
        csv_report, csv_content = self.stored_report(report_format="csv", content=b"id,name\n1,face.png\n")
        csv_response = self.download(csv_report)
        self.assertEqual((self.body(csv_response), "X-Report-Restricted" in csv_response), (csv_content, False))

    def test_a_pdf_without_pictures_is_served_as_stored(self):
        from reports.models import ProcessingReport

        self.require_approval()
        report, content = self.stored_report()
        ProcessingReport.objects.filter(pk=report.pk).update(payload={"images": []})
        self.assertEqual(self.body(self.download(report)), content)

    def test_only_the_owner_can_download_a_report(self):
        report, _ = self.stored_report()
        self.assertEqual(self.download(report, client=self.authenticated_client(self.other_member)).status_code, status.HTTP_404_NOT_FOUND)
