"""Moderator previews of images submitted to an organization."""
import io
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from PIL import Image

from enhancement.models import ImageEnhancementResult
from organizations.models import Organization, OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .tests import ModerationFixtureMixin

MEDIA = tempfile.mkdtemp(prefix="facecloud-preview-")


def jpeg(colour=(120, 90, 60), size=(640, 480)):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "JPEG")
    return buffer.getvalue()


@override_settings(MEDIA_ROOT=MEDIA, THUMBNAIL_PREGENERATE=False)
class ModeratorImagePreviewTests(ModerationFixtureMixin, TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = self.make_user("preview-owner")
        self.organization = create_organization(name="Preview Co", owner=self.owner)
        self.moderator = self.make_user("preview-mod")
        OrganizationMembership.objects.create(organization=self.organization, user=self.moderator,
                                              role=OrganizationMembership.Role.MODERATOR)
        self.member = self.make_user("preview-member")
        OrganizationMembership.objects.create(organization=self.organization, user=self.member)
        self.image = self.upload(self.member, self.organization)

    def upload(self, owner, organization, status="uploaded"):
        batch = UploadBatch.objects.create(owner=owner, organization=organization, upload_type="single")
        image = ImageUpload.objects.create(owner=owner, batch=batch, original_name="photo.jpg", status=status)
        if status == "uploaded":
            image.original_file.save("photo.jpg", ContentFile(jpeg()))
        return image

    def personal_space(self, user):
        existing = Organization.objects.filter(memberships__user=user, is_personal=True).first()
        return existing or create_organization(name="Personal", owner=user, is_personal=True)

    def url(self, image=None, organization=None, query=""):
        organization = organization or self.organization
        return f"/api/organizations/{organization.pk}/moderator/images/{(image or self.image).pk}/preview/{query}"

    def test_owners_and_moderators_get_a_thumbnail(self):
        for user in (self.owner, self.moderator):
            with self.subTest(user=user.username):
                response = self.authenticated_client(user).get(self.url())
                self.assertEqual((response.status_code, response["Content-Type"]), (200, "image/jpeg"))
                preview = Image.open(io.BytesIO(b"".join(response.streaming_content)))
                self.assertLessEqual(max(preview.size), 256)

    def test_the_enhanced_variant_is_the_latest_completed_output(self):
        client = self.authenticated_client(self.moderator)
        self.assertEqual(client.get(self.url(query="?variant=enhanced")).status_code, 404)
        preprocessing = ImagePreprocessingResult.objects.create(owner=self.member, image=self.image, status="completed")
        result = ImageEnhancementResult.objects.create(owner=self.member, image=self.image,
                                                       preprocessing_result=preprocessing, status="completed")
        result.enhanced_file.save("out.jpg", ContentFile(jpeg((10, 200, 30))))
        response = client.get(self.url(query="?variant=enhanced&size=1024"))
        self.assertEqual(response.status_code, 200)
        pixel = Image.open(io.BytesIO(response.content)).convert("RGB").getpixel((5, 5))
        self.assertGreater(pixel[1], 150)  # the green enhanced output, not the brown original

    def test_every_preview_is_audited(self):
        self.authenticated_client(self.moderator).get(self.url())
        event = OrganizationAuditEvent.objects.get(action="moderator_image_previewed")
        self.assertEqual((event.actor, event.target_id, event.new_value["variant"]),
                         (self.moderator, self.image.pk, "original"))

    def test_members_outsiders_and_other_organizations_are_refused(self):
        self.assertEqual(self.authenticated_client(self.member).get(self.url()).status_code, 403)
        outsider = self.make_user("preview-outsider")
        self.assertEqual(self.authenticated_client(outsider).get(self.url()).status_code, 404)
        other_owner = self.make_user("other-preview-owner")
        other = create_organization(name="Other Preview", owner=other_owner)
        foreign = self.upload(other_owner, other)
        self.assertEqual(self.authenticated_client(self.owner).get(self.url(image=foreign)).status_code, 404)
        self.assertFalse(OrganizationAuditEvent.objects.filter(action="moderator_image_previewed").exists())

    def test_personal_images_are_never_previewed_here(self):
        personal = self.personal_space(self.member)
        private = self.upload(self.member, personal)
        response = self.authenticated_client(self.member).get(self.url(image=private, organization=personal))
        self.assertEqual(response.status_code, 404)

    def test_bad_parameters_and_rejected_uploads(self):
        client = self.authenticated_client(self.owner)
        self.assertEqual(client.get(self.url(query="?variant=raw")).status_code, 400)
        self.assertEqual(client.get(self.url(query="?size=999")).status_code, 400)
        rejected = self.upload(self.member, self.organization, status="rejected")
        self.assertEqual(client.get(self.url(image=rejected)).status_code, 404)
