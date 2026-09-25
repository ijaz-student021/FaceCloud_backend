from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from uploads.models import ImageUpload, UploadBatch

from .services import compute_events

User = get_user_model()


class EventTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="event-owner", email="event-owner@example.com", password="pass-12345")
        self.other = User.objects.create_user(username="event-other", email="event-other@example.com", password="pass-12345")
        self.base = timezone.now() - timedelta(days=10)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def photo(self, owner, hours, *, status="uploaded", file="users/1/originals/x.png"):
        batch = UploadBatch.objects.create(owner=owner, upload_type="single")
        image = ImageUpload.objects.create(
            owner=owner, batch=batch, original_name=f"photo-{hours}.png", file_extension=".png",
            mime_type="image/png", width=10, height=10, status=status, original_file=file,
        )
        # created_at is auto_now_add, so set the timeline after creation.
        ImageUpload.objects.filter(pk=image.pk).update(created_at=self.base + timedelta(hours=hours))
        return image


class ComputeEventsTests(EventTestCase):
    def test_photos_within_the_gap_share_an_event_and_a_longer_silence_starts_a_new_one(self):
        first = self.photo(self.owner, 0)
        self.photo(self.owner, 1)
        self.photo(self.owner, 4)
        later = self.photo(self.owner, 40)
        events = compute_events(self.owner, gap_hours=6)
        self.assertEqual([event["photo_count"] for event in events], [1, 3])
        self.assertEqual(events[0]["id"], later.pk)
        self.assertEqual(events[1]["id"], first.pk)

    def test_the_gap_is_measured_between_neighbours_not_from_the_event_start(self):
        for hours in (0, 5, 10, 15):
            self.photo(self.owner, hours)
        self.assertEqual(len(compute_events(self.owner, gap_hours=6)), 1)

    def test_a_photo_exactly_at_the_gap_still_joins(self):
        self.photo(self.owner, 0)
        self.photo(self.owner, 6)
        self.assertEqual(len(compute_events(self.owner, gap_hours=6)), 1)

    def test_only_uploaded_photos_with_a_file_count_and_only_the_owners(self):
        self.photo(self.owner, 0)
        self.photo(self.owner, 1, status="rejected")
        self.photo(self.owner, 2, file="")
        self.photo(self.other, 3)
        events = compute_events(self.owner, gap_hours=6)
        self.assertEqual([event["photo_count"] for event in events], [1])

    def test_appending_a_photo_keeps_the_existing_event_id(self):
        first = self.photo(self.owner, 0)
        before = compute_events(self.owner, gap_hours=6)[0]["id"]
        self.photo(self.owner, 2)
        self.assertEqual(compute_events(self.owner, gap_hours=6)[0]["id"], before)
        self.assertEqual(before, first.pk)

    def test_the_default_gap_comes_from_settings(self):
        self.photo(self.owner, 0)
        self.photo(self.owner, 3)
        with override_settings(EVENT_GAP_HOURS=1):
            self.assertEqual(len(compute_events(self.owner)), 2)
        with override_settings(EVENT_GAP_HOURS=6):
            self.assertEqual(len(compute_events(self.owner)), 1)

    def test_no_photos_means_no_events(self):
        self.assertEqual(compute_events(self.owner), [])


class EventApiTests(EventTestCase):
    def test_login_is_required(self):
        for name, kwargs in (("event-list", {}), ("event-detail", {"event_id": 1}), ("event-photos", {"event_id": 1})):
            response = APIClient().get(reverse(name, kwargs=kwargs))
            self.assertEqual(response.status_code, 401, name)

    def test_list_returns_paginated_events_newest_first_with_a_cover(self):
        first = self.photo(self.owner, 0)
        self.photo(self.owner, 1)
        later = self.photo(self.owner, 40)
        response = self.client.get(reverse("event-list"), {"gap_hours": 6})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        newest, oldest = response.data["results"]
        self.assertEqual(newest["id"], later.pk)
        self.assertEqual(newest["photo_count"], 1)
        self.assertEqual(oldest["id"], first.pk)
        self.assertEqual(oldest["photo_count"], 2)
        self.assertEqual(oldest["cover_image_id"], first.pk)
        self.assertTrue(oldest["cover_download_url"].endswith(f"/api/images/{first.pk}/download/"))
        self.assertLess(oldest["start"], oldest["end"])

    def test_detail_and_photos_use_the_same_gap(self):
        first = self.photo(self.owner, 0)
        second = self.photo(self.owner, 3)
        detail = self.client.get(reverse("event-detail", kwargs={"event_id": first.pk}), {"gap_hours": 6})
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["photo_count"], 2)
        photos = self.client.get(reverse("event-photos", kwargs={"event_id": first.pk}), {"gap_hours": 6})
        self.assertEqual([item["id"] for item in photos.data["results"]], [first.pk, second.pk])
        self.assertTrue(photos.data["results"][0]["download_url"].endswith(f"/api/images/{first.pk}/download/"))
        split = self.client.get(reverse("event-photos", kwargs={"event_id": first.pk}), {"gap_hours": 1})
        self.assertEqual([item["id"] for item in split.data["results"]], [first.pk])

    def test_an_unknown_or_someone_elses_event_is_404(self):
        theirs = self.photo(self.other, 0)
        mine = self.photo(self.owner, 0)
        for name in ("event-detail", "event-photos"):
            self.assertEqual(self.client.get(reverse(name, kwargs={"event_id": theirs.pk})).status_code, 404)
            self.assertEqual(self.client.get(reverse(name, kwargs={"event_id": 999999})).status_code, 404)
            self.assertEqual(self.client.get(reverse(name, kwargs={"event_id": mine.pk})).status_code, 200)

    def test_a_photo_in_the_middle_of_an_event_is_not_an_event_id(self):
        self.photo(self.owner, 0)
        middle = self.photo(self.owner, 1)
        response = self.client.get(reverse("event-detail", kwargs={"event_id": middle.pk}))
        self.assertEqual(response.status_code, 404)

    def test_an_invalid_gap_is_rejected(self):
        for value in ("abc", "0", "0.1", "500", "-3"):
            response = self.client.get(reverse("event-list"), {"gap_hours": value})
            self.assertEqual(response.status_code, 400, value)
            self.assertIn("gap_hours", response.data)

    def test_the_response_carries_no_face_or_identity_fields(self):
        self.photo(self.owner, 0)
        event = self.client.get(reverse("event-list")).data["results"][0]
        self.assertEqual(
            set(event),
            {"id", "start", "end", "photo_count", "cover_image_id", "cover_download_url", "cover_thumbnail_url"},
        )
