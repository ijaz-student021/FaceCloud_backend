"""Search by what is in the photograph: indexing, ranking, owner scope, the trash, the switches, a missing model, and what the vectors are not.

A fake embedder stands in for the model (solid-colour photographs, colour words), so CI needs no model; the real model's quality is measured separately
(``scripts/evaluate_semantic_search.py``, ``docs/evaluation/2026-10-22-semantic-search.md``).
"""
import io
import sys
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.checks import run_checks
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient

from config.production_guard import model_rule
from identities.models import IdentityFace
from organizations.models import OrganizationMembership
from organizations.services import create_organization, create_personal_organization
from quality_control.models import ImageQualityDecision
from uploads.deletion import delete_images
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from uploads.trash import move_to_trash, restore

from .checks import semantic_search_checks
from .models import ImageEmbedding
from .semantic import service
from .semantic.embedders import FakeEmbedder, OpenClipEmbedder, SemanticSearchUnavailable, decode_vectors, encode_vector

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix="facecloud-semantic-")
COLOURS = {"red": (220, 30, 30), "green": (30, 200, 30), "blue": (30, 30, 220), "yellow": (230, 220, 30)}


def solid(colour):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), COLOURS[colour]).save(buffer, "JPEG")
    return buffer.getvalue()


@override_settings(MEDIA_ROOT=MEDIA, SEMANTIC_SEARCH_ENABLED=True, SEMANTIC_SEARCH_MIN_SCORE=0.0)
class SemanticBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="sem-owner", email="sem-owner@example.com", password="Password!123", is_email_verified=True)
        self.other = User.objects.create_user(username="sem-other", email="sem-other@example.com", password="Password!123", is_email_verified=True)
        self.personal = create_personal_organization(self.owner)
        self.embedder = FakeEmbedder()
        service.set_embedder_for_tests(self.embedder)
        self.addCleanup(service.set_embedder_for_tests, None)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.count = 0

    def photo(self, colour, *, owner=None, verdict="accepted", batch=None, index=True):
        self.count += 1
        owner = owner or self.owner
        batch = batch or UploadBatch.objects.create(owner=owner, upload_type="single", organization=self.personal if owner == self.owner else None)
        image = ImageUpload.objects.create(owner=owner, batch=batch, original_name=f"{colour}-{self.count}.jpg", status="uploaded", file_extension=".jpg", mime_type="image/jpeg")
        image.original_file.save(image.original_name, ContentFile(solid(colour)))
        preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
        if verdict:
            ImageQualityDecision.objects.create(owner=owner, image=image, preprocessing_result=preprocessing, status="completed", decision=verdict, overall_score=70)
        image = ImageUpload.objects.get(pk=image.pk)
        if index:
            service.index_images([image], self.embedder)
        return image

    def search(self, query, **extra):
        return self.client.get(reverse("search-photos"), {"q": query, **extra})

    def ids(self, query, **extra):
        return [row["photo"]["id"] for row in self.search(query, **extra).json()["results"]]


class RankingTests(SemanticBase):
    def test_the_words_find_the_photographs_that_show_them_best_first(self):
        red, green, blue = self.photo("red"), self.photo("green"), self.photo("blue")
        body = self.search("a red thing").json()
        self.assertEqual((body["enabled"], body["language"], body["model"]), (True, "en", "fake-colours"))
        self.assertEqual([row["photo"]["id"] for row in body["results"]][0], red.pk)
        scores = [row["score"] for row in body["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(self.ids("blue")[0], blue.pk)
        self.assertEqual(self.ids("green")[0], green.pk)
        self.assertEqual(len(body["results"]), 3)                                                  # with a floor of 0 every indexed photograph is a candidate

    def test_a_result_is_a_gallery_row_with_a_score(self):
        red = self.photo("red")
        row = self.search("red").json()["results"][0]
        self.assertEqual(set(row), {"score", "photo"})
        self.assertEqual((row["photo"]["id"], row["photo"]["original_name"]), (red.pk, red.original_name))
        self.assertTrue(row["photo"]["thumbnail_url"].endswith(f"/api/images/{red.pk}/thumbnail/"))

    def test_the_limit_and_the_floor_cut_the_list(self):
        for colour in ("red", "red", "red", "blue"):
            self.photo(colour)
        self.assertEqual(len(self.ids("red", limit=2)), 2)
        with override_settings(SEMANTIC_SEARCH_MIN_SCORE=0.9):
            hits = self.ids("red")
        self.assertEqual(len(hits), 3)                                                              # only the red ones are close enough; the blue one is below the floor
        with override_settings(SEMANTIC_SEARCH_MAX_RESULTS=2, SEMANTIC_SEARCH_DEFAULT_RESULTS=1):
            self.assertEqual(len(self.ids("red")), 1)
            self.assertEqual(len(self.ids("red", limit=50)), 2)

    def test_only_the_callers_own_photographs(self):
        mine, theirs = self.photo("red"), self.photo("red", owner=self.other)
        self.assertEqual(self.ids("red"), [mine.pk])
        stranger = APIClient()
        stranger.force_authenticate(self.other)
        self.assertEqual([row["photo"]["id"] for row in stranger.get(reverse("search-photos"), {"q": "red"}).json()["results"]], [theirs.pk])

    def test_only_photographs_that_are_accepted_or_effectively_accepted(self):
        accepted = self.photo("red")
        self.photo("red", verdict="rejected")
        self.photo("red", verdict=None)
        personal_review = self.photo("red", verdict="needs_review")                                 # a personal workspace has no moderator: it counts as accepted
        team = create_organization(name="Sem Co", owner=User.objects.create_user(username="sem-boss", email="sem-boss@example.com", password="Password!123"))
        OrganizationMembership.objects.create(organization=team, user=self.owner, role="member", status="active")
        self.photo("red", verdict="needs_review", batch=UploadBatch.objects.create(owner=self.owner, upload_type="single", organization=team))
        self.assertEqual(sorted(self.ids("red")), sorted([accepted.pk, personal_review.pk]))       # all five are indexed here; the answer filters by the verdict as it is now
        eligible = {image.pk for image in service.pending_images(self.embedder, reindex=True)}
        self.assertEqual(eligible, {accepted.pk, personal_review.pk})                                # and the indexer would only ever have taken these two
        ImageQualityDecision.objects.filter(image=accepted).update(decision="rejected")             # a verdict that changes later also changes the answer
        self.assertEqual(self.ids("red"), [personal_review.pk])

    def test_a_photograph_in_the_trash_is_not_found_and_is_found_again_once_restored(self):
        gone, kept = self.photo("red"), self.photo("red")
        move_to_trash([gone])
        self.assertEqual(self.ids("red"), [kept.pk])
        restore([ImageUpload.all_objects.get(pk=gone.pk)])
        self.assertEqual(sorted(self.ids("red")), sorted([gone.pk, kept.pk]))

    def test_the_top_bar_search_carries_the_same_photographs_when_it_is_on(self):
        red = self.photo("red")
        body = self.client.get(reverse("search"), {"q": "red"}).json()
        self.assertEqual([row["id"] for row in body["photos"]], [red.pk])
        self.assertEqual(set(body["photos"][0]), {"id", "name", "thumbnail_url", "status", "created_at", "score"})
        self.assertIn("images", body)                                                               # the name search is untouched

    def test_the_query_is_validated(self):
        for query in ("", "a", "x" * 201):
            self.assertEqual(self.client.get(reverse("search-photos"), {"q": query}).status_code, 400, query)
        self.assertEqual(self.client.get(reverse("search-photos")).status_code, 400)
        self.assertEqual(self.client.get(reverse("search-photos"), {"q": "red", "limit": "0"}).status_code, 400)
        self.assertEqual(APIClient().get(reverse("search-photos"), {"q": "red"}).status_code, 401)


class IndexingTests(SemanticBase):
    def test_indexing_is_idempotent_and_a_new_model_is_a_new_index(self):
        image = self.photo("red", index=False)
        first = service.index_images([image], self.embedder)
        again = service.index_images([image], self.embedder)
        self.assertEqual((first["indexed"], again["indexed"], again["skipped"]), (1, 0, 1))
        self.assertEqual(self.embedder.calls["images"], 1)
        self.assertEqual(ImageEmbedding.objects.filter(image=image).count(), 1)
        other = FakeEmbedder(checksum="a" * 64)
        self.assertEqual(list(service.pending_images(other)), [image])                              # the old vector belongs to another model
        self.assertEqual(service.rank(self.owner, "red", embedder=other), [])                       # and is never compared with a query
        service.index_images([image], other)
        self.assertEqual(ImageEmbedding.objects.get(image=image).model_checksum, "a" * 64)
        self.assertEqual(ImageEmbedding.objects.filter(image=image).count(), 1)                     # replaced, not duplicated

    def test_a_photograph_that_cannot_be_read_is_reported_not_fatal(self):
        broken = self.photo("red", index=False)
        ImageUpload.objects.filter(pk=broken.pk).update(original_file="users/1/originals/missing.jpg")
        fine = self.photo("blue", index=False)
        result = service.index_images([ImageUpload.objects.get(pk=broken.pk), fine], self.embedder)
        self.assertEqual((result["indexed"], result["failed"]), (1, 1))

    def test_a_verdict_queues_the_indexing_and_the_task_indexes_only_what_is_eligible(self):
        from .tasks import index_image_task

        image = self.photo("red", index=False)
        with self.captureOnCommitCallbacks(execute=True):
            service.index_after_verdict(image)
        self.assertTrue(ImageEmbedding.objects.filter(image=image).exists())
        rejected = self.photo("red", verdict="rejected", index=False)
        self.assertEqual(index_image_task(rejected.pk), {"indexed": 0})
        self.assertEqual(index_image_task(999999), {"indexed": 0})

    def test_the_catch_up_task_and_command_index_what_is_missing(self):
        from .tasks import index_pending_task

        first, second = self.photo("red", index=False), self.photo("blue", index=False)
        self.assertEqual(index_pending_task(limit=1)["indexed"], 1)
        out = io.StringIO()
        call_command("index_semantic_search", stdout=out)
        self.assertEqual(ImageEmbedding.objects.filter(image__in=[first, second]).count(), 2)
        self.assertIn("Indexed 1", out.getvalue())
        calls = self.embedder.calls["images"]
        call_command("index_semantic_search", stdout=io.StringIO())
        self.assertEqual(self.embedder.calls["images"], calls)                                      # nothing pending: no model call
        call_command("index_semantic_search", "--reindex", stdout=io.StringIO())
        self.assertEqual(self.embedder.calls["images"], calls + 2)

    def test_the_vectors_survive_float16_storage(self):
        vectors = self.embedder.embed_images([Image.new("RGB", (8, 8), COLOURS["red"]), Image.new("RGB", (8, 8), COLOURS["blue"])])
        stored = decode_vectors([encode_vector(vector) for vector in vectors], vectors.shape[1])
        self.assertEqual(stored.shape, vectors.shape)
        self.assertLess(abs(stored - vectors).max(), 1e-3)
        self.assertEqual(decode_vectors([], 8).shape, (0, 8))


class SwitchTests(SemanticBase):
    def test_switched_off_the_feature_does_nothing_and_says_so(self):
        red = self.photo("red")
        with override_settings(SEMANTIC_SEARCH_ENABLED=False):
            body = self.search("red").json()
            self.assertEqual((body["enabled"], body["results"], body["model"]), (False, [], None))
            self.assertNotIn("photos", self.client.get(reverse("search"), {"q": "red"}).json())
            fresh = self.photo("blue", index=False)
            with self.captureOnCommitCallbacks(execute=True):
                service.index_after_verdict(fresh)
            self.assertFalse(ImageEmbedding.objects.filter(image=fresh).exists())
            from .tasks import index_image_task

            self.assertEqual(index_image_task(fresh.pk), {"indexed": 0})
            with self.assertRaises(CommandError):
                call_command("index_semantic_search", stdout=io.StringIO())
            with self.assertRaises(SemanticSearchUnavailable):
                service.get_embedder()
        self.assertTrue(ImageEmbedding.objects.filter(image=red).exists())

    def test_the_default_configuration_is_off(self):
        from importlib import import_module

        with open(import_module("config.settings").__file__) as handle:                          # the settings of this machine may be changed by its .env; the shipped default is what is pinned
            self.assertIn('SEMANTIC_SEARCH_ENABLED = env.bool("SEMANTIC_SEARCH_ENABLED", default=False)', handle.read())

    def test_switched_on_without_a_usable_model_the_endpoint_says_503_and_the_top_bar_still_works(self):
        service.set_embedder_for_tests(None)
        with override_settings(SEMANTIC_SEARCH_MODEL_DIR=tempfile.mkdtemp(), SEMANTIC_SEARCH_MODEL_SHA256="0" * 64):
            service._configured_embedder.cache_clear()
            response = self.search("red")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["code"], "semantic_search_unavailable")
            top_bar = self.client.get(reverse("search"), {"q": "red"})
            self.assertEqual(top_bar.status_code, 200)
            self.assertNotIn("photos", top_bar.json())
        service._configured_embedder.cache_clear()


class ModelLoaderTests(TestCase):
    """The real loader refuses anything that is not the approved model, before any machine-learning library is imported."""

    def make_dir(self, content=b"weights"):
        folder = tempfile.mkdtemp()
        with open(f"{folder}/open_clip_model.safetensors", "wb") as handle:
            handle.write(content)
        return folder

    def test_a_missing_folder_or_file_is_unavailable(self):
        for model_dir in (None, "/nonexistent/folder", tempfile.mkdtemp()):
            with self.assertRaises(SemanticSearchUnavailable):
                OpenClipEmbedder(model_dir, "0" * 64, "x").embed_texts(["red"])

    def test_weights_without_a_recorded_checksum_or_with_a_different_one_are_refused(self):
        folder = self.make_dir()
        with self.assertRaisesMessage(SemanticSearchUnavailable, "SEMANTIC_SEARCH_MODEL_SHA256 is not set"):
            OpenClipEmbedder(folder, "", "x").embed_texts(["red"])
        with self.assertRaisesMessage(SemanticSearchUnavailable, "checksum differs"):
            OpenClipEmbedder(folder, "1" * 64, "x").embed_texts(["red"])
        self.assertNotIn("open_clip", sys.modules)                                                  # nothing heavy was imported to find that out

    def test_the_libraries_missing_is_unavailable_not_a_crash(self):
        import hashlib

        folder = self.make_dir()
        digest = hashlib.sha256(b"weights").hexdigest()
        with mock.patch.dict(sys.modules, {"open_clip": None, "torch": None}):
            with self.assertRaisesMessage(SemanticSearchUnavailable, "libraries"):
                OpenClipEmbedder(folder, digest, "x").embed_texts(["red"])


class PrivacyTests(SemanticBase):
    def test_the_vectors_are_not_face_data_and_stay_out_of_the_export(self):
        self.photo("red")
        export = self.client.get("/api/privacy/export/")
        self.assertEqual(export.status_code, 200)
        def keys(value):
            if isinstance(value, dict):
                for key, inner in value.items():
                    yield key
                    yield from keys(inner)
            elif isinstance(value, list):
                for inner in value:
                    yield from keys(inner)

        names = {key.lower() for key in keys(export.json())}
        self.assertFalse({name for name in names if "vector" in name or "semantic" in name or name in {"embedding", "embeddings"}})
        self.assertEqual(IdentityFace.objects.count(), 0)                                           # indexing never creates a face template

    def test_a_vector_goes_with_its_photograph_and_with_the_account(self):
        image, other = self.photo("red"), self.photo("blue")
        delete_images([image])
        self.assertFalse(ImageEmbedding.objects.filter(image_id=image.pk).exists())
        self.owner.delete()
        self.assertFalse(ImageEmbedding.objects.filter(image_id=other.pk).exists())

    def test_the_query_text_is_never_logged(self):
        self.photo("red")
        with self.assertNoLogs(level="DEBUG"):
            self.search("my secret query about red")


class ChecksTests(TestCase):
    def test_nothing_is_asked_while_the_switch_is_off(self):
        with override_settings(SEMANTIC_SEARCH_ENABLED=False):
            self.assertEqual(semantic_search_checks(None), [])

    def test_the_model_its_checksum_and_the_licence_record_are_required_when_on(self):
        with override_settings(SEMANTIC_SEARCH_ENABLED=True, SEMANTIC_SEARCH_MODEL_DIR="/nonexistent", SEMANTIC_SEARCH_MODEL_SHA256="", SEMANTIC_SEARCH_LICENSE_RECORD_PATH="/nonexistent.md"):
            self.assertEqual(sorted(error.id for error in semantic_search_checks(None)), ["search.E001", "search.E002", "search.E003"])
            self.assertIn("search.E001", {error.id for error in run_checks()})
        folder = tempfile.mkdtemp()
        open(f"{folder}/open_clip_model.safetensors", "wb").close()

        def record(text):
            handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
            handle.write(text)
            handle.close()
            return handle.name

        def problems(text):
            with override_settings(SEMANTIC_SEARCH_ENABLED=True, SEMANTIC_SEARCH_MODEL_DIR=folder, SEMANTIC_SEARCH_MODEL_SHA256="a" * 64, SEMANTIC_SEARCH_LICENSE_RECORD_PATH=record(text)):
                return sorted(error.id for error in semantic_search_checks(None))

        self.assertEqual(problems(f"Owner approval: approved\n\nweights {'a' * 64}\n"), [])
        self.assertEqual(problems(f"Owner approval: pending\n\nweights {'a' * 64}\n"), ["search.E005"])                 # read but not decided: still off
        self.assertEqual(problems(f"Owner approval: approved\n\nweights {'b' * 64}\n"), ["search.E004"])                # approved for other weights
        self.assertEqual(problems("nothing useful"), ["search.E004", "search.E005"])
        from pathlib import Path

        from django.conf import settings

        shipped = (Path(settings.BASE_DIR) / "docs" / "model-licenses" / "semantic-search-model.md").read_text()
        self.assertRegex(shipped, r"(?m)^Owner approval: (pending|approved)$")                                               # the decision is the owner's, in this one line
        self.assertIn("81942ea8c09b9e41963357cca5d2682118bf7eb7491c2ae51a28bc4f4f95b194", shipped)                           # and the record names the weights it is about

    def test_production_refuses_the_switch_without_the_checksum_of_the_approved_weights(self):
        base = {"SEMANTIC_SEARCH_ENABLED": True, "SEMANTIC_SEARCH_MODEL_DIR": "/models/siglip"}
        self.assertTrue(any("SEMANTIC_SEARCH_MODEL_SHA256" in problem for problem in model_rule(base)))
        self.assertEqual(model_rule({**base, "SEMANTIC_SEARCH_MODEL_SHA256": "a" * 64}), [])
        self.assertEqual(model_rule({"SEMANTIC_SEARCH_ENABLED": False}), [])
