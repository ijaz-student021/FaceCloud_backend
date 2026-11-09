"""Memories: worked out from a person's own photographs, never from anyone else's, never showing a rejected photograph, deterministic."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from duplicates.models import DuplicateAnalysis, DuplicateMatch
from identities.models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .services import (
    MIN_EVENT, MIN_ON_THIS_DAY, MIN_PERSON, build_memories, cluster, memory_by_id, on_this_day, photo_rows, recent_events, select_highlights,
)

User = get_user_model()


class MemoryBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="mem-owner", email="mem-owner@example.com", password="Password!123", is_email_verified=True)
        self.stranger = User.objects.create_user(username="mem-other", email="mem-other@example.com", password="Password!123", is_email_verified=True)
        self.batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple")
        self.now = timezone.now()
        self.today = timezone.localdate()
        self.count = 0

    def photo(self, taken, *, score=70.0, faces=1, favorite=False, verdict="accepted", owner=None, batch=None, status="uploaded"):
        self.count += 1
        owner = owner or self.owner
        image = ImageUpload.objects.create(
            owner=owner, batch=batch or (self.batch if owner == self.owner else UploadBatch.objects.create(owner=owner, upload_type="single")), original_name=f"m-{self.count}.jpg",
            status=status, taken_at=taken, is_favorite=favorite,
        )
        preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
        FaceValidationResult.objects.create(owner=owner, image=image, preprocessing_result=preprocessing, decision="accepted", face_count=faces, faces=[])
        ImageQualityDecision.objects.create(owner=owner, image=image, preprocessing_result=preprocessing, status="completed", decision=verdict, overall_score=score)
        return image

    def burst(self, start, count, step=timedelta(minutes=10), **kwargs):
        return [self.photo(start + step * index, **kwargs) for index in range(count)]

    def api(self, user=None):
        user = user or self.owner
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        return client

    def years_ago(self, years, hour=10):
        try:
            day = self.today.replace(year=self.today.year - years)
        except ValueError:
            day = self.today.replace(year=self.today.year - years, day=28)
        return timezone.make_aware(timezone.datetime(day.year, day.month, day.day, hour, 0, 0))


@override_settings(QUALITY_CHECKS_REJECT=True)      # these tests are about photographs the checks refused; the product default no longer refuses (quality_control/test_never_reject.py)
class OnThisDayTests(MemoryBase):
    def test_a_year_with_enough_photographs_becomes_a_card(self):
        self.burst(self.years_ago(2), MIN_ON_THIS_DAY)
        self.burst(self.years_ago(3), MIN_ON_THIS_DAY - 1)                                              # too few
        (memory,) = on_this_day(self.owner, self.today)
        self.assertEqual((memory.kind, memory.title, len(memory.rows)), ("on_this_day", "2 years ago today", MIN_ON_THIS_DAY))
        self.assertEqual(memory.id, f"otd:{self.today.year - 2}")
        self.assertEqual(memory.filter, {"taken_from": memory.rows[0]["captured"].date().isoformat(), "taken_to": memory.rows[0]["captured"].date().isoformat()})

    def test_one_year_ago_is_singular_and_this_years_photographs_are_not_a_memory(self):
        self.burst(self.years_ago(1), 4)
        self.burst(timezone.now().replace(hour=1, minute=0), 4, step=timedelta(minutes=1))
        titles = [memory.title for memory in on_this_day(self.owner, self.today)]
        self.assertEqual(titles, ["1 year ago today"])

    def test_rejected_photographs_and_other_peoples_do_not_count(self):
        self.burst(self.years_ago(2), 2)
        self.photo(self.years_ago(2, hour=12), verdict="rejected")
        self.burst(self.years_ago(2, hour=13), 5, owner=self.stranger)
        self.assertEqual(on_this_day(self.owner, self.today), [])

    def test_the_newest_year_comes_first_and_at_most_three_are_shown(self):
        for years in (2, 3, 4, 5):
            self.burst(self.years_ago(years), 3)
        self.assertEqual([memory.id for memory in on_this_day(self.owner, self.today)], [f"otd:{self.today.year - years}" for years in (2, 3, 4)])


class EventTests(MemoryBase):
    def test_runs_of_photographs_close_in_time_are_clustered_and_small_ones_ignored(self):
        start = self.now - timedelta(days=10)
        big = self.burst(start, MIN_EVENT + 1)
        small = self.burst(start - timedelta(days=3), MIN_EVENT - 1)
        events = recent_events(self.owner, self.now)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].kind, events[0].id, len(events[0].rows)), ("event", f"event:{big[0].pk}", MIN_EVENT + 1))
        self.assertTrue(events[0].title.startswith("Moments from "))
        self.assertNotIn(small[0].pk, [row["id"] for row in events[0].rows])

    def test_a_gap_of_more_than_six_hours_splits_an_event_and_newest_comes_first(self):
        start = self.now - timedelta(days=20)
        first = self.burst(start, MIN_EVENT)
        second = self.burst(start + timedelta(hours=9), MIN_EVENT)
        newer = self.burst(self.now - timedelta(days=2), MIN_EVENT)
        events = recent_events(self.owner, self.now)
        self.assertEqual([event.id for event in events], [f"event:{newer[0].pk}", f"event:{second[0].pk}", f"event:{first[0].pk}"])

    def test_photographs_older_than_the_recent_window_are_not_listed_but_the_card_can_still_be_opened(self):
        old = self.burst(self.now - timedelta(days=400), MIN_EVENT)
        self.assertEqual(recent_events(self.owner, self.now), [])
        memory = memory_by_id(self.owner, f"event:{old[0].pk}", self.now)
        self.assertEqual(len(memory.rows), MIN_EVENT)

    def test_a_stale_event_id_is_gone(self):
        photos = self.burst(self.now - timedelta(days=10), MIN_EVENT)
        stale = f"event:{photos[2].pk}"                                                                 # not the first photograph of its run
        self.assertIsNone(memory_by_id(self.owner, stale, self.now))
        self.assertIsNone(memory_by_id(self.owner, "event:999999", self.now))

    def test_the_title_says_the_day_or_the_days(self):
        same_day = self.burst(self.now - timedelta(days=10), MIN_EVENT, step=timedelta(minutes=5))
        title = recent_events(self.owner, self.now)[0].title
        self.assertRegex(title, r"^Moments from \d{1,2} [A-Z][a-z]{2}$")
        del same_day
        runs = cluster([{"id": 1, "captured": self.now - timedelta(days=3)}, {"id": 2, "captured": self.now - timedelta(days=3, hours=-1)}])
        self.assertEqual(len(runs), 1)


class PersonTests(MemoryBase):
    def person(self, name, photos):
        group = IdentityGroup.objects.create(owner=self.owner, display_name=name, member_count=len(photos), embedding_dimension=3, provider="t", policy_version="v1", protected_centroid=b"x")
        for image in photos:
            analysis = IdentityAnalysis.objects.create(owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, face_validation=image.face_validation, status="completed", provider="t", policy_version="v1")
            face = IdentityFace.objects.create(owner=self.owner, image=image, analysis=analysis, face_index=0, protected_embedding=b"\0", embedding_dimension=3, provider="t")
            IdentityMembership.objects.create(owner=self.owner, identity_face=face, identity_group=group, state="confirmed", decision_method="test")
        return group

    def test_a_named_person_with_enough_photographs_gets_a_card(self):
        photos = self.burst(self.now - timedelta(days=40), MIN_PERSON, step=timedelta(hours=7))
        group = self.person("Ayesha", photos)
        card = next(memory for memory in build_memories(self.owner, 10, self.now) if memory.kind == "person")
        self.assertEqual((card.title, card.id, len(card.rows), card.filter), ("With Ayesha", f"person:{group.pk}:{timezone.localtime(self.now):%Y-%m}", MIN_PERSON, {"person": group.pk}))

    def test_unnamed_and_small_folders_get_no_card(self):
        self.person("Person 001", self.burst(self.now - timedelta(days=40), MIN_PERSON, step=timedelta(hours=7)))
        self.person("Bilal", self.burst(self.now - timedelta(days=80), MIN_PERSON - 1, step=timedelta(hours=7)))
        self.assertEqual([memory for memory in build_memories(self.owner, 10, self.now) if memory.kind == "person"], [])

    def test_the_card_can_be_opened_by_its_id(self):
        group = self.person("Ayesha", self.burst(self.now - timedelta(days=40), MIN_PERSON, step=timedelta(hours=7)))
        memory = memory_by_id(self.owner, f"person:{group.pk}:2020-01", self.now)
        self.assertEqual((memory.title, len(memory.rows)), ("With Ayesha", MIN_PERSON))
        other = IdentityGroup.objects.create(owner=self.stranger, display_name="Theirs", member_count=0, embedding_dimension=3, provider="t", policy_version="v1", protected_centroid=b"x")
        self.assertIsNone(memory_by_id(self.owner, f"person:{other.pk}:2020-01", self.now))


class SelectionTests(MemoryBase):
    def rows(self, images):
        return photo_rows(self.owner, ids=[image.pk for image in images])

    def test_favourites_first_then_quality_then_a_face_then_the_earlier_one(self):
        start = self.now - timedelta(days=5)
        plain = self.photo(start, score=60)
        better = self.photo(start + timedelta(hours=1), score=90)
        no_face = self.photo(start + timedelta(hours=2), score=90, faces=0)
        favourite = self.photo(start + timedelta(hours=3), score=10, favorite=True)
        chosen = select_highlights(self.owner, self.rows([plain, better, no_face, favourite]), 4)
        self.assertEqual([row["id"] for row in chosen], [favourite.pk, better.pk, no_face.pk, plain.pk])

    def test_a_burst_gives_one_photograph_the_best_of_it(self):
        start = self.now - timedelta(days=5)
        shots = [self.photo(start + timedelta(seconds=10 * index), score=50 + index) for index in range(6)]
        elsewhere = self.photo(start + timedelta(hours=2), score=40)
        chosen = select_highlights(self.owner, self.rows([*shots, elsewhere]), 10)
        self.assertEqual({row["id"] for row in chosen}, {shots[-1].pk, elsewhere.pk})

    def test_a_duplicate_of_one_already_chosen_is_skipped_unless_it_was_said_not_to_be(self):
        start = self.now - timedelta(days=5)
        original, copy, innocent = self.photo(start, score=90), self.photo(start + timedelta(hours=1), score=80), self.photo(start + timedelta(hours=2), score=70)
        analysis = DuplicateAnalysis.objects.create(owner=self.owner, image=copy, preprocessing_result=copy.preprocessing_result, status="completed")
        DuplicateMatch.objects.create(owner=self.owner, analysis=analysis, source_image=copy, matched_image=original, match_type="perceptual", similarity_score=99.0)
        analysis_two = DuplicateAnalysis.objects.create(owner=self.owner, image=innocent, preprocessing_result=innocent.preprocessing_result, status="completed")
        DuplicateMatch.objects.create(owner=self.owner, analysis=analysis_two, source_image=innocent, matched_image=original, match_type="perceptual", similarity_score=90.0, review_status="not_duplicate")
        chosen = select_highlights(self.owner, self.rows([original, copy, innocent]), 10)
        self.assertEqual({row["id"] for row in chosen}, {original.pk, innocent.pk})

    def test_the_limit_and_the_order_are_deterministic_whatever_the_insertion_order(self):
        start = self.now - timedelta(days=5)
        images = [self.photo(start + timedelta(hours=index), score=50 + (index * 7) % 40, favorite=index % 5 == 0) for index in range(12)]
        first = [row["id"] for row in select_highlights(self.owner, self.rows(images), 5)]
        again = [row["id"] for row in select_highlights(self.owner, list(reversed(self.rows(images))), 5)]
        self.assertEqual(first, again)
        self.assertEqual(len(first), 5)

    def test_nothing_in_nothing_out(self):
        self.assertEqual(select_highlights(self.owner, [], 5), [])


@override_settings(QUALITY_CHECKS_REJECT=True)      # these tests are about photographs the checks refused; the product default no longer refuses (quality_control/test_never_reject.py)
class EndpointTests(MemoryBase):
    def test_the_list_has_the_cards_with_a_cover_and_previews(self):
        photos = self.burst(self.now - timedelta(days=10), 8, score=60)
        best = self.photo(self.now - timedelta(days=10, hours=-3), score=95)
        body = self.api().get(reverse("memories:memory-list")).json()
        (card,) = [row for row in body if row["kind"] == "event"]
        self.assertEqual(set(card), {"id", "kind", "title", "subtitle", "start", "end", "photo_count", "cover_thumbnail_url", "preview_thumbnail_urls", "filter"})
        self.assertEqual(card["photo_count"], 9)
        self.assertTrue(card["cover_thumbnail_url"].endswith(f"/api/images/{best.pk}/thumbnail/"))              # the best photograph is the cover
        previews = [int(url.split("/images/")[1].split("/")[0]) for url in card["preview_thumbnail_urls"]]
        self.assertEqual(previews, [best.pk, photos[0].pk, photos[1].pk, photos[2].pk])                       # the best, then the earliest of the equally good ones

    def test_the_list_never_shows_another_persons_photographs(self):
        self.burst(self.now - timedelta(days=10), 8, owner=self.stranger)
        self.assertEqual(self.api().get(reverse("memories:memory-list")).json(), [])
        self.assertTrue(self.api(self.stranger).get(reverse("memories:memory-list")).json())

    def test_the_limit_is_validated_and_capped(self):
        client = self.api()
        for bad in ("0", "x", "-1"):
            self.assertEqual(client.get(reverse("memories:memory-list"), {"limit": bad}).status_code, 400)
        self.assertEqual(client.get(reverse("memories:memory-list"), {"limit": "1000"}).status_code, 200)
        self.assertEqual(APIClient().get(reverse("memories:memory-list")).status_code, 401)

    def test_the_detail_has_the_best_photographs_as_gallery_rows_oldest_first_and_a_filter_for_all_of_them(self):
        start = self.now - timedelta(days=10)
        photos = self.burst(start, MIN_EVENT + 4)
        client = self.api()
        (card,) = [row for row in client.get(reverse("memories:memory-list")).json() if row["kind"] == "event"]
        detail = client.get(reverse("memories:memory-detail", kwargs={"memory_id": card["id"]})).json()
        ids = [row["id"] for row in detail["images"]]
        self.assertEqual(ids, sorted(ids, key=lambda pk: next(p.taken_at for p in photos if p.pk == pk)))
        self.assertTrue(set(ids) <= {p.pk for p in photos})
        for key in ("id", "display_status", "thumbnail_url", "is_favorite", "taken_at"):
            self.assertIn(key, detail["images"][0])
        everything = client.get(reverse("my-image-list"), card["filter"]).json()
        self.assertEqual(everything["count"], card["photo_count"])

    def test_unknown_foreign_and_malformed_ids_are_404(self):
        theirs = self.burst(self.now - timedelta(days=10), MIN_EVENT, owner=self.stranger)
        client = self.api()
        for memory_id in (f"event:{theirs[0].pk}", "event:999999", "event:abc", "otd:abc", "otd:1999", "person:999:2020-01", "person:x", "nonsense", "event:"):
            with self.subTest(memory_id=memory_id):
                self.assertEqual(client.get(reverse("memories:memory-detail", kwargs={"memory_id": memory_id})).status_code, 404)

    def test_a_rejected_photograph_is_never_in_a_memory(self):
        start = self.now - timedelta(days=10)
        kept = self.burst(start, MIN_EVENT)
        self.photo(start + timedelta(minutes=3), verdict="rejected", score=99)
        self.photo(start + timedelta(minutes=4), status="rejected")
        (card,) = [row for row in self.api().get(reverse("memories:memory-list")).json() if row["kind"] == "event"]
        self.assertEqual(card["photo_count"], len(kept))
