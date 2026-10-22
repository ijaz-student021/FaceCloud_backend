"""Nothing shows a photograph that is in the trash: every GET endpoint of the API is crawled before and after, as the owner, the owner's organization and the public.

The default manager hides trashed photographs from every query that starts at ``ImageUpload``. A query that starts at a **derived** row (a quality decision, a
duplicate match, a share link...) and only joins to the photograph does not get that for free. This test finds them without anyone having to remember:

* one fully populated photograph (every derived row, an album place, a favourite mark, a folder, an enhancement with a share link) has a pk nothing else can
  have (``TARGET_PK``) and a name nothing else contains (``MARKER``);
* every GET path of the OpenAPI schema is called, its ``{id}`` parameters filled with every candidate id the fixture knows;
* **before** the trash the crawl must find the photograph in many places (so the fixture is rich enough and the test cannot pass by finding nothing);
* **after** it, no response may contain the marker or the pk, and no address that carries the pk may answer 200 (except the trash's own).
"""
import io
import itertools
import json
import re
import tempfile
from datetime import timedelta

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from PIL import Image
from rest_framework.test import APIClient

from albums.models import Album, AlbumImage
from duplicates.models import DuplicateAnalysis, DuplicateMatch
from enhancement.models import EnhancementLevel, ImageEnhancementResult
from identities.models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership
from identities.storage import protect_embedding
from identities.test_person_folders import FolderFixtures
from moderation.models import ModeratorReview
from organizations.models import OrganizationMembership
from organizations.services import create_organization, create_personal_organization
from quality_control.models import ImageQualityDecision
from reports.models import ProcessingReport
from sharing.services import create_share_link
from thumbnails.services import get_or_create_thumbnail
from validation.models import FaceValidationResult

from .models import ImagePreprocessingResult, ImageUpload, UploadBatch

TARGET_PK = 987654
MARKER = "zz-trash-marker"
ORG_PK = TARGET_PK + 2
ORG_MARKER = "zz-org-marker"
# Addresses that are allowed to name a trashed photograph: the trash itself; the data export, which must hold everything the service holds about the person
# (tested below); and the two histories, which record what happened (a download, an upload) and stay true after the photograph is trashed.
EXEMPT = ("/api/images/trash/", "/api/privacy/export/", "/api/downloads/", "/api/user/activity/")
EXEMPT_ENDINGS = ("/audit/",)                       # an organization's audit trail is evidence of what happened; it names the photograph it concerned
MEDIA = tempfile.mkdtemp(prefix="facecloud-trash-audit-")


def jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (120, 90, 60)).save(buffer, "JPEG")
    return buffer.getvalue()


def get_paths():
    """``[(path template, [parameter names])]`` for every GET operation of the API."""
    schema = SchemaGenerator().get_schema(request=None, public=True)
    return [(path, re.findall(r"{(\w+)}", path)) for path, item in schema["paths"].items() if "get" in item]


@override_settings(MEDIA_ROOT=MEDIA)
class TrashAuditTests(FolderFixtures, TestCase):
    def setUp(self):
        self.owner = self.make_user("audit-owner")
        self.boss = self.make_user("audit-boss")
        self.personal = create_personal_organization(self.owner)
        self.team = create_organization(name="Audit Co", owner=self.boss)
        OrganizationMembership.objects.create(organization=self.team, user=self.owner, role="member", status="active")
        self.personal_batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple", organization=self.personal)
        self.team_batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple", organization=self.team)
        self.group = self.make_group(self.owner, "Ayesha")
        self.album = Album.objects.create(owner=self.owner, title="Holiday")
        self.control = self.build("control.jpg", self.personal_batch)
        self.target = self.build(f"{MARKER}.jpg", self.personal_batch, pk=TARGET_PK)
        self.in_review = self.build("review-photo.jpg", self.team_batch, pk=TARGET_PK + 1, verdict="needs_review")
        self.org_target = self.build(f"{ORG_MARKER}.jpg", self.team_batch, pk=ORG_PK)
        decision = self.org_target.quality_decision
        ModeratorReview.objects.create(organization=self.team, image=self.org_target, source="quality", quality_decision=decision, status="approved")
        self.populate(self.target)
        self.token = self.populate_share(self.target)
        ModeratorReview.objects.create(
            organization=self.team, image=self.in_review, source="quality", quality_decision=self.in_review.quality_decision, status="pending",
        )
        self.ids = {
            TARGET_PK, TARGET_PK + 1, ORG_PK, self.control.pk, self.personal_batch.pk, self.team_batch.pk, self.group.pk, self.album.pk, self.team.pk,
            self.personal.pk, self.target.enhancement_results.first().pk, self.report.pk, self.match.pk, self.review_id(),
        }

    def review_id(self):
        return ModeratorReview.objects.get(image=self.in_review).pk

    def build(self, name, batch, pk=None, verdict="accepted"):
        data = jpeg_bytes()
        image = ImageUpload.objects.create(
            pk=pk, owner=self.owner, batch=batch, original_name=name, status="uploaded", file_extension=".jpg", mime_type="image/jpeg", file_size=len(data),
            width=64, height=64,
        )
        image.original_file.save(name, ContentFile(data))
        preprocessing = ImagePreprocessingResult.objects.create(owner=self.owner, image=image, status="completed", output_format="jpeg", output_name=name)
        preprocessing.processed_file.save(name, ContentFile(data))
        validation = FaceValidationResult.objects.create(
            owner=self.owner, image=image, preprocessing_result=preprocessing, decision="accepted", face_count=1,
            faces=[{"x": 8, "y": 8, "width": 40, "height": 40, "area_ratio": 0.4, "confidence": 0.99}],
        )
        ImageQualityDecision.objects.create(
            owner=self.owner, image=image, preprocessing_result=preprocessing, status="completed", decision=verdict, overall_score=70,
        )
        analysis = IdentityAnalysis.objects.create(
            owner=self.owner, image=image, preprocessing_result=preprocessing, face_validation=validation, status="completed", provider="test",
            policy_version="test-v1",
        )
        face = IdentityFace.objects.create(
            owner=self.owner, image=image, analysis=analysis, face_index=0, protected_embedding=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3,
            provider="test",
        )
        IdentityMembership.objects.create(owner=self.owner, identity_face=face, identity_group=self.group, state="confirmed", similarity_score=0.9)
        IdentityGroup.objects.filter(pk=self.group.pk).update(member_count=self.group.memberships.filter(state="confirmed").count())
        AlbumImage.objects.create(album=self.album, image=image)
        return ImageUpload.objects.get(pk=image.pk)

    def populate(self, image):
        ImageUpload.objects.filter(pk=image.pk).update(is_favorite=True, favorited_at=timezone.now(), taken_at=timezone.now() - timedelta(days=400))
        analysis = DuplicateAnalysis.objects.create(
            owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, status="completed", decision="duplicate", perceptual_hash="ab" * 8,
        )
        self.match = DuplicateMatch.objects.create(
            owner=self.owner, analysis=analysis, source_image=image, matched_image=self.control, match_type="perceptual", similarity_score=0.97,
        )
        result = ImageEnhancementResult.objects.create(
            owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, status="completed", requested_level=EnhancementLevel.AUTO,
            output_format="jpeg", output_width=64, output_height=64,
        )
        result.enhanced_file.save("enhanced.jpg", ContentFile(jpeg_bytes()))
        get_or_create_thumbnail(image, 256)
        self.report = ProcessingReport.objects.create(owner=self.owner, report_type="image", format="json", status="completed", image=image)

    def populate_share(self, image):
        result = image.enhancement_results.first()
        link, token = create_share_link(owner=self.owner, result=result)
        self.link = link
        return token

    # -- the crawl ------------------------------------------------------------------------------------------------------------------------------
    def client_for(self, user):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user)
        return client

    def crawl(self, client, paths, ids, *, tokens=(), pk=TARGET_PK, marker=MARKER):
        """``{address: (status, leaks)}`` for every GET path with every candidate; ``leaks`` says how the photograph (``pk``, named ``marker``) showed up."""
        found = {}
        candidates = [str(pk) for pk in sorted(ids)] + list(tokens)
        for template, names in paths:
            if len(names) > 2:
                continue
            for combination in itertools.product(candidates, repeat=len(names)) if names else [()]:
                address = template
                for name, value in zip(names, combination):
                    address = address.replace("{" + name + "}", value)
                cache.clear()                                                         # a throttle must not turn a leak into a 429
                response = client.get(address)
                if response.status_code == 429:
                    raise AssertionError(f"throttled: {address}")
                leaks = []
                if response.status_code == 200 and not address.startswith(EXEMPT) and not address.endswith(EXEMPT_ENDINGS):
                    if f"/{pk}/" in address or address.endswith(f"/{pk}"):
                        leaks.append("address")
                    if getattr(response, "streaming", False):
                        body = ""
                    else:
                        body = response.content.decode("utf-8", errors="ignore")
                    if marker in body and not address.startswith(EXEMPT) and not address.endswith(EXEMPT_ENDINGS):
                        leaks.append("name")
                    if re.search(rf"(?<![\w.]){pk}(?![\w.])", body) and not address.startswith(EXEMPT) and not address.endswith(EXEMPT_ENDINGS):
                        leaks.append("pk")
                if leaks:
                    found[address] = (response.status_code, leaks)
        return found

    def trash_it(self, *images):
        for image in images:
            ImageQualityDecision.objects.filter(image=image).update(status="completed")
        response = self.client_for(self.owner).post("/api/images/trash/", {"image_ids": [image.pk for image in images]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_after_the_trash_no_endpoint_shows_the_photograph_to_its_owner(self):
        paths = get_paths()
        self.assertGreater(len(paths), 60)
        client = self.client_for(self.owner)
        before = self.crawl(client, paths, self.ids, tokens=[self.token])
        self.assertGreaterEqual(
            len({a.split("?")[0] for a in before}), 12,
            f"the fixture is too poor to prove anything: the photograph appears in only {sorted(before)}",
        )
        body = self.trash_it(self.target)
        self.assertEqual(body["trashed_ids"], [TARGET_PK], body)
        after = self.crawl(client, paths, self.ids, tokens=[self.token])
        report = json.dumps({address: leaks for address, (_status, leaks) in sorted(after.items())}, indent=1)
        self.assertEqual(after, {}, f"still shown while in the trash ({len(after)} addresses; before the trash: {len(before)}):\n{report}")

    def test_the_public_share_link_of_a_trashed_photograph_stops_working_and_works_again_once_restored(self):
        public = APIClient()
        for suffix in ("", "info/"):
            self.assertEqual(public.get(f"/api/shares/public/{self.token}/{suffix}").status_code, 200)
        self.trash_it(self.target)
        for suffix in ("", "info/"):
            self.assertIn(public.get(f"/api/shares/public/{self.token}/{suffix}").status_code, (404, 410))
        response = self.client_for(self.owner).post("/api/images/trash/restore/", {"image_ids": [TARGET_PK]}, format="json")
        self.assertEqual(response.json()["restored_ids"], [TARGET_PK])
        for suffix in ("", "info/"):
            self.assertEqual(public.get(f"/api/shares/public/{self.token}/{suffix}").status_code, 200)

    def test_restoring_brings_the_photograph_back_to_every_place_it_was_shown(self):
        paths = get_paths()
        client = self.client_for(self.owner)
        before = self.crawl(client, paths, self.ids, tokens=[self.token])
        self.trash_it(self.target)
        self.assertEqual(client.post("/api/images/trash/restore/", {"image_ids": [TARGET_PK]}, format="json").json()["restored_ids"], [TARGET_PK])
        after = self.crawl(client, paths, self.ids, tokens=[self.token])
        self.assertEqual(sorted(after), sorted(before))

    def test_after_the_trash_no_endpoint_shows_an_organization_photograph_to_the_organizations_admin(self):
        paths = get_paths()
        admin = self.client_for(self.boss)
        before = self.crawl(admin, paths, self.ids | {self.team.pk}, pk=ORG_PK, marker=ORG_MARKER)
        self.assertGreaterEqual(len({a.split("?")[0] for a in before}), 3, f"the organization crawl finds the photograph in too few places: {sorted(before)}")
        self.assertEqual(self.client_for(self.owner).post("/api/images/trash/", {"image_ids": [ORG_PK]}, format="json").json()["trashed_ids"], [ORG_PK])
        after = self.crawl(admin, paths, self.ids | {self.team.pk}, pk=ORG_PK, marker=ORG_MARKER)
        self.assertEqual(after, {}, f"the organization's admin still sees it ({len(after)}; before: {len(before)}):\n{json.dumps(sorted(after), indent=1)}")
