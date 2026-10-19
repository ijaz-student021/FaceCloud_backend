"""Person folders: faces of one person join one folder, photos the checks rejected leave folders, a detection on food opens none, and the re-grouping commands."""
import math
from io import StringIO
from unittest.mock import patch

import numpy as np
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from privacy.testing import grant_face_consent
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import IdentityAnalysis, IdentityChange, IdentityFace, IdentityGroup, IdentityMembership
from .providers import IdentityEmbeddingProvider, SFaceOnnxProvider
from .regrouping import apply_regroup, plan_regroup, public
from .services import face_grouping_check, process_identity_analysis, remove_photo_from_folders
from .storage import protect_embedding

User = get_user_model()
State = IdentityMembership.State
LIMITS = dict(new_group_threshold=0.30, match_threshold=0.45, ambiguity_margin=0.06, guard_min_similarity=0.80)


def vec(x=0.0, y=0.0, z=0.0):
    vector = np.asarray([x, y, z], dtype=np.float32)
    return vector / np.linalg.norm(vector)


def angle(degrees):
    radians = math.radians(degrees)
    return vec(math.cos(radians), math.sin(radians), 0.0)


class Provider(IdentityEmbeddingProvider):
    """Three-dimensional stand-in with the SFace limits; ``Provider.vectors`` maps a face's box ``x`` to its embedding."""

    name, dimension, model_checksum = "stand_in_sface", 3, "abc"
    vectors = {}

    def __init__(self):
        for key, value in LIMITS.items():
            setattr(self, key, value)

    def healthcheck(self):
        return True

    def prepare_face(self, image, box, landmarks=None):
        return Provider.vectors[box["x"]]

    def embed(self, prepared_face):
        return prepared_face


def a_face(x, **extra):
    """A face record as the face check writes it (large, confident), identified by its ``x``."""
    return {"x": x, "y": 10, "width": 300, "height": 300, "area_ratio": 0.30, "confidence": 0.95, "landmarks": [{"x": 1, "y": 1}] * 5, **extra}


@override_settings(IDENTITY_EMBEDDING_KEY="regroup-tests-key", YUNET_SCORE_THRESHOLD=0.6)
class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="regroup-owner", email="regroup-owner@example.com", password="Password!123", is_email_verified=True)
        grant_face_consent(self.owner)
        self.provider = Provider()
        self.batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple")
        self.count = 0
        Provider.vectors = {}

    def photo(self, faces=None, decision="accepted", quality=None, status="uploaded"):
        self.count += 1
        image = ImageUpload.objects.create(owner=self.owner, batch=self.batch, original_name=f"photo-{self.count}.jpg", status=status, width=1000, height=1000)
        preprocessing = ImagePreprocessingResult.objects.create(owner=self.owner, image=image, status="completed")
        validation = FaceValidationResult.objects.create(
            owner=self.owner, image=image, preprocessing_result=preprocessing, decision=decision, face_count=len(faces or []), faces=faces or [],
        )
        if quality:
            ImageQualityDecision.objects.create(owner=self.owner, image=image, preprocessing_result=preprocessing, face_validation=validation, status="completed", decision=quality)
        return image

    def group(self, name=None, **extra):
        return IdentityGroup.objects.create(
            owner=self.owner, display_name=name or f"Person {IdentityGroup.objects.count() + 1:03d}", embedding_dimension=3, provider=self.provider.name,
            model_checksum="abc", policy_version="test", **extra,
        )

    def member(self, image, vector, group, state=State.NEW_GROUP, reviewed=False, index=0):
        analysis, _ = IdentityAnalysis.objects.get_or_create(
            image=image, defaults={"owner": self.owner, "preprocessing_result": image.preprocessing_result, "face_validation": image.face_validation,
                                   "status": "completed", "provider": self.provider.name, "policy_version": "test"},
        )
        face = IdentityFace.objects.create(
            owner=self.owner, image=image, analysis=analysis, face_index=index, protected_embedding=protect_embedding(vector), embedding_dimension=3, provider=self.provider.name,
            model_checksum="abc",
        )
        return IdentityMembership.objects.create(
            owner=self.owner, identity_face=face, identity_group=group, state=state, decision_method="test", reviewed_by=self.owner if reviewed else None,
        )

    def analyse(self, image):
        analysis, _ = IdentityAnalysis.objects.get_or_create(
            image=image, defaults={"owner": self.owner, "preprocessing_result": image.preprocessing_result, "face_validation": image.face_validation,
                                   "provider": self.provider.name, "policy_version": "test"},
        )
        with patch("identities.services.get_identity_provider", return_value=self.provider), patch("identities.services._read_image", return_value=np.zeros((10, 10, 3), np.uint8)):
            return process_identity_analysis(analysis.pk)

    def refresh(self, *groups):
        from .services import _refresh_group

        for group in groups:
            _refresh_group(group)


class SamePersonJoinsOneFolderTests(Base):
    def test_photos_between_the_new_and_the_old_new_group_limit_now_join_the_folder(self):
        Provider.vectors = {1: angle(0), 2: angle(56), 3: angle(100), 4: vec(0, 0, 1)}        # cosines to the first: 0.56, -0.17, 0.0
        first, second, other_person = self.photo([a_face(1)]), self.photo([a_face(2)]), self.photo([a_face(4)])
        self.assertEqual(self.analyse(first).status, "completed")
        self.assertEqual(self.analyse(second).status, "completed")                              # 0.56: above 0.45, below the old new-group limit of 0.68
        self.assertEqual(IdentityGroup.objects.count(), 1)
        self.assertEqual(IdentityMembership.objects.get(identity_face__image=second).state, State.AUTO_MATCHED)
        self.assertEqual(self.analyse(other_person).status, "completed")                        # a different person: its own folder
        self.assertEqual(IdentityGroup.objects.count(), 2)

    def test_the_old_limits_split_the_same_photos_into_two_folders(self):
        Provider.vectors = {1: angle(0), 2: angle(56)}
        with patch.multiple(self.provider, new_group_threshold=0.68, match_threshold=0.82):
            self.analyse(self.photo([a_face(1)]))
            self.analyse(self.photo([a_face(2)]))
        self.assertEqual(IdentityGroup.objects.count(), 2)

    def test_a_face_between_the_two_limits_waits_for_review_and_a_clearly_other_face_opens_a_folder(self):
        Provider.vectors = {1: angle(0), 2: angle(68), 3: angle(80)}                            # 0.37 (between 0.30 and 0.45) and 0.17 (below 0.30)
        self.analyse(self.photo([a_face(1)]))
        uncertain, stranger = self.photo([a_face(2)]), self.photo([a_face(3)])
        self.assertEqual(self.analyse(uncertain).status, "needs_review")
        waiting = IdentityMembership.objects.get(identity_face__image=uncertain)
        self.assertEqual(waiting.state, State.NEEDS_REVIEW)
        self.assertEqual(waiting.evidence["threshold"], 0.45)                                  # the provider's own limits are what the evidence records
        self.assertEqual(waiting.evidence["new_group_threshold"], 0.30)
        self.analyse(stranger)
        self.assertEqual(IdentityGroup.objects.count(), 2)


class NotEveryDetectionIsAPersonTests(Base):
    def test_a_small_unsure_detection_on_food_opens_no_folder_and_the_analysis_says_why(self):
        Provider.vectors = {1: vec(1, 0, 0), 2: vec(0, 1, 0), 3: vec(0, 0, 1)}
        food = self.photo([
            a_face(1, width=70, height=70, area_ratio=0.004),                      # large enough in confidence, far too small
            a_face(2, confidence=0.55),                                            # big, but the detector itself is not sure
        ], decision="needs_review")
        analysis = self.analyse(food)
        self.assertEqual(analysis.status, "completed")
        self.assertEqual(analysis.face_count, 0)
        self.assertEqual(IdentityGroup.objects.count(), 0)
        self.assertEqual(IdentityFace.objects.count(), 0)
        reasons = {item["face_index"]: item["reason"] for item in analysis.evidence["skipped_faces"]}
        self.assertEqual(reasons, {0: "face_too_small", 1: "detector_score_below_floor"})
        self.assertEqual((analysis.evidence["faces_detected"], analysis.evidence["faces_grouped"]), (2, 0))

    def test_a_real_face_in_the_same_photograph_still_gets_its_folder(self):
        Provider.vectors = {1: vec(1, 0, 0), 2: vec(0, 1, 0)}
        photo = self.photo([a_face(1, width=70, height=70, area_ratio=0.004), a_face(2)], decision="needs_review")
        analysis = self.analyse(photo)
        self.assertEqual((analysis.face_count, IdentityGroup.objects.count()), (1, 1))
        self.assertEqual(analysis.evidence["skipped_faces"][0]["face_index"], 0)
        self.assertEqual(analysis.evidence["limits"]["match"], 0.45)

    def test_the_bar_is_the_one_the_face_check_sets_for_accepted(self):
        ok, _ = face_grouping_check(a_face(1), 1)
        self.assertTrue(ok)
        self.assertFalse(face_grouping_check(a_face(1, area_ratio=0.01, width=100, height=100), 1)[0])          # 1 % of the frame, under the 6 % single-face rule
        self.assertTrue(face_grouping_check(a_face(1, area_ratio=0.02, width=100, height=100), 3)[0])           # but fine as one of a group (1.2 % rule)
        self.assertTrue(face_grouping_check(a_face(1, area_ratio=0.01, width=200, height=200), 1)[0])           # a big face in a huge frame (pixels count too)
        self.assertTrue(face_grouping_check({"x": 0, "y": 0, "width": 10, "height": 10}, 1)[0])                 # an old record without score or ratio is judged on what it has


class RejectedPhotosLeaveFoldersTests(Base):
    def folder_with(self, image, vector=None, **group_extra):
        group = self.group(**group_extra)
        membership = self.member(image, vector if vector is not None else vec(1, 0, 0), group)
        self.refresh(group)
        return group, membership

    def test_a_photo_rejected_by_the_quality_check_is_taken_out_and_its_empty_folder_archived(self):
        photo = self.photo([a_face(1)])
        group, membership = self.folder_with(photo)
        ImageQualityDecision.objects.create(
            owner=self.owner, image=photo, preprocessing_result=photo.preprocessing_result, face_validation=photo.face_validation, status="completed", decision="rejected",
        )
        membership.refresh_from_db()
        group.refresh_from_db()
        self.assertEqual(membership.state, State.REMOVED)
        self.assertEqual((group.state, group.member_count, bytes(group.protected_centroid)), ("archived", 0, b""))
        change_actions = list(IdentityChange.objects.filter(group=group).order_by("pk").values_list("action", flat=True))
        self.assertEqual(change_actions, ["removed", "archived"])                              # both written to the folder's history
        self.assertTrue(IdentityGroup.objects.filter(pk=group.pk).exists())                     # archived, not deleted: its id still resolves

    def test_a_photo_the_face_check_rejects_leaves_too(self):
        photo = self.photo([a_face(1)])
        group, membership = self.folder_with(photo)
        FaceValidationResult.objects.filter(image=photo).update(decision="needs_review")
        validation = FaceValidationResult.objects.get(image=photo)
        validation.decision = "rejected"
        validation.save()
        membership.refresh_from_db()
        self.assertEqual(membership.state, State.REMOVED)

    def test_a_folder_that_still_has_other_photos_is_refreshed_not_archived(self):
        keep, rejected = self.photo([a_face(1)]), self.photo([a_face(2)])
        group = self.group()
        self.member(keep, vec(1, 0, 0), group)
        self.member(rejected, vec(0.9, 0.1, 0), group, state=State.AUTO_MATCHED)
        self.refresh(group)
        self.assertEqual(IdentityGroup.objects.get(pk=group.pk).member_count, 2)
        ImageQualityDecision.objects.create(
            owner=self.owner, image=rejected, preprocessing_result=rejected.preprocessing_result, face_validation=rejected.face_validation, status="completed", decision="rejected",
        )
        group.refresh_from_db()
        self.assertEqual((group.state, group.member_count), ("active", 1))

    def test_a_folder_the_owner_named_is_never_archived_even_when_it_is_left_empty(self):
        photo = self.photo([a_face(1)])
        group, _membership = self.folder_with(photo, name="Grandfather")
        remove_photo_from_folders(photo)
        group.refresh_from_db()
        self.assertEqual((group.state, group.member_count), ("active", 0))

    def test_a_photo_that_is_not_rejected_is_not_touched(self):
        photo = self.photo([a_face(1)])
        group, membership = self.folder_with(photo)
        ImageQualityDecision.objects.create(
            owner=self.owner, image=photo, preprocessing_result=photo.preprocessing_result, face_validation=photo.face_validation, status="completed", decision="needs_review",
        )
        membership.refresh_from_db()
        self.assertEqual(membership.state, State.NEW_GROUP)

    def test_a_rejected_photo_is_not_grouped_in_the_first_place(self):
        Provider.vectors = {1: vec(1, 0, 0)}
        photo = self.photo([a_face(1)], quality="rejected")
        analysis = IdentityAnalysis.objects.create(
            owner=self.owner, image=photo, preprocessing_result=photo.preprocessing_result, face_validation=photo.face_validation, status="queued", provider=self.provider.name, policy_version="test",
        )
        with patch("identities.services.get_identity_provider", return_value=self.provider):
            result = process_identity_analysis(analysis.pk)
        self.assertEqual(result.status, "cancelled")
        self.assertIn("rejected", result.failure_reason)
        self.assertEqual(IdentityGroup.objects.count(), 0)
        from rest_framework.exceptions import ValidationError

        from .services import create_or_reset_identity_analysis

        other = self.photo([a_face(1)], quality="rejected")
        with self.assertRaisesMessage(ValidationError, "not grouped"):
            create_or_reset_identity_analysis(other)

    def test_the_cleanup_command_repairs_data_from_before_and_a_dry_run_changes_nothing(self):
        food = self.photo([a_face(1)], quality="rejected")                                      # the rejected food photograph that sat in a folder
        keeper = self.photo([a_face(2)])
        old_folder = self.group()
        IdentityGroup.objects.filter(pk=old_folder.pk).update(created_at=timezone.make_aware(timezone.datetime(2026, 10, 17, 12, 0)))
        self.member(food, vec(1, 0, 0), old_folder)
        busy = self.group()
        self.member(keeper, vec(0, 1, 0), busy)
        self.refresh(old_folder, busy)

        out = StringIO()
        call_command("cleanup_rejected_folder_members", stdout=out)
        self.assertIn("Dry run", out.getvalue())
        self.assertIn(f"folder {old_folder.pk}", out.getvalue())
        self.assertIn("17 Oct 2026", out.getvalue())
        self.assertEqual(IdentityMembership.objects.filter(state=State.NEW_GROUP).count(), 2)       # nothing changed
        self.assertEqual(IdentityGroup.objects.get(pk=old_folder.pk).state, "active")

        out = StringIO()
        call_command("cleanup_rejected_folder_members", "--apply", stdout=out)
        self.assertIn("1 memberships removed", out.getvalue())
        self.assertEqual(IdentityMembership.objects.get(identity_face__image=food).state, State.REMOVED)
        self.assertEqual(IdentityGroup.objects.get(pk=old_folder.pk).state, "archived")
        self.assertEqual(IdentityGroup.objects.get(pk=busy.pk).state, "active")                     # the photo that was not rejected stays where it is
        self.assertEqual(IdentityMembership.objects.get(identity_face__image=keeper).state, State.NEW_GROUP)

    def test_the_folder_list_no_longer_shows_the_rejected_photo(self):
        photo = self.photo([a_face(1)])
        self.folder_with(photo)
        client = APIClient()
        client.force_authenticate(self.owner)
        before = client.get("/api/identities/groups/").json()
        before = before["results"] if isinstance(before, dict) else before
        self.assertEqual(before[0]["confirmed_count"], 1)
        ImageQualityDecision.objects.create(
            owner=self.owner, image=photo, preprocessing_result=photo.preprocessing_result, face_validation=photo.face_validation, status="completed", decision="rejected",
        )
        after = client.get("/api/identities/groups/").json()
        after = after["results"] if isinstance(after, dict) else after
        self.assertEqual(after, [])                                                              # the archived folder is not listed


class FolderCountsTests(Base):
    def test_confirmed_and_suggested_are_counted_apart_and_the_old_fields_remain(self):
        group = self.group()
        for number in range(2):
            self.member(self.photo([a_face(number)]), vec(1, 0, 0), group, state=State.AUTO_MATCHED)
        suggested = self.photo([a_face(5)])
        self.member(suggested, vec(0.5, 0.5, 0), group, state=State.NEEDS_REVIEW)
        self.member(self.photo([a_face(6)]), vec(0, 1, 0), group, state=State.REMOVED)
        self.refresh(group)
        client = APIClient()
        client.force_authenticate(self.owner)
        body = client.get("/api/identities/groups/").json()
        row = (body["results"] if isinstance(body, dict) else body)[0]
        self.assertEqual((row["member_count"], row["confirmed_count"], row["suggested_count"]), (2, 2, 1))
        for field in ("id", "display_name", "state", "member_count", "preview_image", "cover_images", "matching_confidence", "created_at", "updated_at"):
            self.assertIn(field, row)
        detail = client.get(f"/api/identities/groups/{group.pk}/").json()
        self.assertEqual((detail["confirmed_count"], detail["suggested_count"]), (2, 1))
        photos = client.get(f"/api/identities/groups/{group.pk}/images/").json()
        photos = photos["results"] if isinstance(photos, dict) else photos
        self.assertEqual(len(photos), 3)                                                          # the photo list shows the suggested one too: now the counts say why
        self.assertEqual({photo["membership_state"] for photo in photos}, {"auto_matched", "needs_review"})


class RegroupTests(Base):
    """One man split over four folders by the old limits, another man in his own, and the owner's own decisions."""

    def fragmented(self):
        self.old = []
        angles = [0, 25, 45, 60]
        for number, degrees in enumerate(angles):
            group = self.group()
            self.member(self.photo([a_face(number)]), angle(degrees), group)
            self.old.append(group)
        self.other = self.group()
        self.member(self.photo([a_face(10)]), vec(0, 0, 1), self.other)
        self.refresh(*self.old, self.other)

    def snapshot(self):
        return (
            sorted(IdentityGroup.objects.values_list("pk", "state", "member_count", "display_name")),
            sorted(IdentityMembership.objects.values_list("pk", "identity_group_id", "state")),
            IdentityChange.objects.count(),
        )

    def test_a_dry_run_reports_and_changes_nothing(self):
        self.fragmented()
        before = self.snapshot()
        plan = public(plan_regroup(self.owner, self.provider))
        self.assertEqual(plan["before"]["folders"], 5)
        self.assertEqual(plan["after"]["folders"], 2)
        self.assertEqual(plan["limits"]["match"], 0.45)
        self.assertEqual(len(plan["moves"]), 3)
        self.assertEqual({entry["folder"] for entry in plan["merge_or_archive"]}, {self.old[1].pk, self.old[2].pk, self.old[3].pk})
        self.assertEqual(self.snapshot(), before)

        out = StringIO()
        with patch("identities.management.commands.regroup_identities.get_identity_provider", return_value=self.provider):
            call_command("regroup_identities", "--owner", str(self.owner.pk), "--dry-run", "--verbose", stdout=out)
            call_command("regroup_identities", "--owner", str(self.owner.pk), stdout=out)            # the default is a dry run too
        self.assertIn("Dry run: nothing was changed", out.getvalue())
        self.assertIn("Folders before: 5", out.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_apply_merges_the_fragments_keeps_every_id_resolvable_and_audits_each_move(self):
        self.fragmented()
        with patch("identities.management.commands.regroup_identities.get_identity_provider", return_value=self.provider):
            out = StringIO()
            call_command("regroup_identities", "--owner", str(self.owner.pk), "--apply", stdout=out)
        self.assertIn("3 faces moved", out.getvalue())
        survivor = IdentityGroup.objects.get(pk=self.old[0].pk)
        self.assertEqual((survivor.state, survivor.member_count), ("active", 4))
        for group in self.old[1:]:
            group.refresh_from_db()
            self.assertEqual(group.state, "merged")                                              # merged, not deleted
        self.assertEqual(IdentityGroup.objects.get(pk=self.other.pk).state, "active")
        client = APIClient()
        client.force_authenticate(self.owner)
        for group in self.old:
            self.assertEqual(client.get(f"/api/identities/groups/{group.pk}/").status_code, 200)   # every old id still resolves
        moved = IdentityChange.objects.filter(action="moved", reason__startswith="Regrouped")
        self.assertEqual(moved.count(), 3)
        self.assertTrue(all(change.actor_id is None for change in moved))
        self.assertTrue(IdentityChange.objects.filter(action="merged").exists())
        from activity.models import ActivityEvent

        self.assertTrue(ActivityEvent.objects.filter(user=self.owner, event="identities_regrouped").exists())
        self.assertEqual(IdentityMembership.objects.filter(decision_method="regroup_v1", identity_group=survivor).count(), 3)
        self.assertEqual(plan_regroup(self.owner, self.provider)["moves"], [])                   # a second run has nothing left to do

    def test_a_folder_the_owner_renamed_and_a_face_they_confirmed_are_never_touched(self):
        self.fragmented()
        IdentityGroup.objects.filter(pk=self.old[3].pk).update(display_name="Dad")
        confirmed_group = IdentityGroup.objects.get(pk=self.old[2].pk)
        IdentityMembership.objects.filter(identity_group=confirmed_group).update(state=State.CONFIRMED, reviewed_by=self.owner)
        before_dad = list(IdentityMembership.objects.filter(identity_group=self.old[3]).values_list("pk", "identity_group_id", "state"))
        before_confirmed = list(IdentityMembership.objects.filter(identity_group=self.old[2]).values_list("pk", "identity_group_id", "state"))
        plan = public(plan_regroup(self.owner, self.provider))
        left = {entry["id"]: entry["reason"] for entry in plan["left_alone"]}
        self.assertEqual(set(left), {self.old[2].pk, self.old[3].pk})
        self.assertIn("renamed", left[self.old[3].pk])
        self.assertIn("confirmed", left[self.old[2].pk])
        apply_regroup(plan_regroup(self.owner, self.provider), self.owner)
        self.assertEqual(list(IdentityMembership.objects.filter(identity_group=self.old[3]).values_list("pk", "identity_group_id", "state")), before_dad)
        self.assertEqual(list(IdentityMembership.objects.filter(identity_group=self.old[2]).values_list("pk", "identity_group_id", "state")), before_confirmed)
        self.assertEqual(IdentityGroup.objects.get(pk=self.old[3].pk).display_name, "Dad")
        self.assertEqual(IdentityGroup.objects.get(pk=self.old[2].pk).state, "active")
        self.assertEqual(IdentityGroup.objects.get(pk=self.old[0].pk).member_count, 2)              # the two folders left to regroup became one

    def test_a_face_the_old_limits_parked_for_review_is_placed_and_other_owners_are_not_looked_at(self):
        group = self.group()
        self.member(self.photo([a_face(1)]), angle(0), group)
        parked = self.member(self.photo([a_face(2)]), angle(50), None, state=State.NEEDS_REVIEW)
        self.refresh(group)
        stranger = User.objects.create_user(username="someone-else", email="someone-else@example.com", password="Password!123")
        other_group = IdentityGroup.objects.create(owner=stranger, display_name="Person 001", embedding_dimension=3, provider=self.provider.name, policy_version="t")
        apply_regroup(plan_regroup(self.owner, self.provider), self.owner)
        parked.refresh_from_db()
        self.assertEqual((parked.state, parked.identity_group_id), (State.AUTO_MATCHED, group.pk))
        other_group.refresh_from_db()
        self.assertEqual(other_group.state, "active")
        with self.assertRaisesMessage(CommandError, "There is no user with id 999999"):
            call_command("regroup_identities", "--owner", "999999")

    def test_a_decision_made_between_the_plan_and_the_apply_is_respected(self):
        self.fragmented()
        plan = plan_regroup(self.owner, self.provider)
        moving = plan["moves"][0]["membership_id"]
        IdentityMembership.objects.filter(pk=moving).update(state=State.CONFIRMED, reviewed_by=self.owner)       # the owner decided in the meantime
        group_before = IdentityMembership.objects.get(pk=moving).identity_group_id
        apply_regroup(plan, self.owner)
        membership = IdentityMembership.objects.get(pk=moving)
        self.assertEqual((membership.identity_group_id, membership.state), (group_before, State.CONFIRMED))


class SfaceProviderSubclassTests(TestCase):
    def test_the_stand_in_used_here_matches_the_real_providers_limits(self):
        self.assertEqual({key: getattr(Provider(), key) for key in LIMITS}, {key: getattr(SFaceOnnxProvider(), key) for key in LIMITS})
