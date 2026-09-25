"""One realistic pass through the whole product, over the real HTTP API.

Registration by emailed code, login, upload, progress polling, thumbnails, an
encrypted round trip, events, enhancement, a collage and notifications, in the
order a frontend would call them. Background jobs run in-process
(``CELERY_TASK_ALWAYS_EAGER``) against a throwaway database, so it needs no
Redis and never touches development data.

Tagged ``slow`` (it runs the real enhancement models and needs the sample photos
in ``Dataset/`` and ``People/``), so CI skips it. Run it with::

    python manage.py test config.test_end_to_end --tag slow
"""
import re
import time
import unittest
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings
from django.core import mail
from django.test import TransactionTestCase, override_settings, tag
from rest_framework.test import APIClient

ROOT = Path(settings.BASE_DIR)
DARK_PHOTOS = [ROOT / "Dataset" / "image" / name for name in ("1.png", "10.png", "100.png")]
FACE_PHOTO = next(iter(sorted((ROOT / "People").glob("*.jpg"))), None) if (ROOT / "People").exists() else None
TERMINAL = {"completed", "partial", "failed", "cancelled"}
PASSWORD = "ForestRiver!4821"


def luminance(payload):
    decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    assert decoded is not None, "the response is not a decodable image"
    return float(decoded.mean())


@tag("slow")
@unittest.skipUnless(all(path.exists() for path in DARK_PHOTOS) and FACE_PHOTO, "sample photos are not available")
@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    BIOMETRIC_CONSENT_REQUIRED=False,
)
class EndToEndFlowTests(TransactionTestCase):
    def body(self, response):
        return b"".join(response.streaming_content) if getattr(response, "streaming", False) else response.content

    def call(self, method, url, expected, **kwargs):
        response = getattr(self.client, method)(url, **kwargs)
        self.assertEqual(response.status_code, expected, f"{method.upper()} {url}: {getattr(response, 'data', '')}")
        return response

    def poll_progress(self, batch_id):
        for _ in range(60):
            data = self.call("get", f"/api/images/batches/{batch_id}/progress/", 200).data
            if data["status"] in TERMINAL:
                return data
            time.sleep(0.5)
        self.fail(f"batch {batch_id} never reached a terminal status: {data['status']} / {data['stage']}")

    def test_the_whole_journey(self):
        self.client = APIClient()

        # 1. Sign up with the emailed code, then log in.
        registered = self.call("post", "/api/auth/register/", 202, data={
            "username": "e2e-user", "email": "e2e@example.com", "password": PASSWORD,
        }, format="json")
        code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
        self.call("post", "/api/auth/verify-email/", 201, data={
            "registration_id": registered.data["registration_id"], "code": code,
        }, format="json")
        login = self.call("post", "/api/auth/login/", 200, data={"identifier": "e2e@example.com", "password": PASSWORD}, format="json")
        self.assertEqual(login.data["role"], "user")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")

        # 2. Upload three dark photos and one face photo as a batch.
        paths = DARK_PHOTOS + [FACE_PHOTO]
        originals = {path.name: path.read_bytes() for path in paths}
        files = [open(path, "rb") for path in paths]
        try:
            uploaded = self.call("post", "/api/images/upload/multiple/", 201, data={"files": files}, format="multipart")
        finally:
            for handle in files:
                handle.close()
        batch_id = uploaded.data["id"]
        self.assertEqual(uploaded.data["accepted_files"], 4, uploaded.data)

        # 3. Follow progress until the batch is finished; a whole-batch failure is a pipeline bug.
        progress = self.poll_progress(batch_id)
        self.assertIn(progress["status"], {"completed", "partial"}, progress)
        for image in progress["images"]:
            self.assertNotEqual(image["status"], "failed", f"{image['filename']}: {image['failure_reason']}")

        # 4. The gallery lists them with working thumbnails, and downloads are byte-identical
        #    to what was uploaded even though everything is encrypted at rest.
        gallery = self.call("get", "/api/images/my/", 200).data
        rows = gallery["results"]
        self.assertEqual(len(rows), 4)
        for row in rows:
            # A photo with no detectable face is analysed as "completed, zero faces", never "failed".
            analysis = row["identity_analysis"]
            self.assertTrue(analysis is None or analysis["status"] != "failed", (row["original_name"], analysis))
            self.assertTrue(row["thumbnail_url"], row["original_name"])
            thumb = self.call("get", row["thumbnail_url"], 200)
            self.assertEqual(thumb["Content-Type"], "image/jpeg")
            self.assertLess(len(self.body(thumb)), 200_000)
            download = self.call("get", row["download_url"], 200)
            self.assertEqual(self.body(download), originals[row["original_name"]], row["original_name"])

        # 5. Events group the four uploads together and expose a cover thumbnail.
        events = self.call("get", "/api/events/", 200).data
        self.assertEqual(events["count"], 1)
        event = events["results"][0]
        self.assertEqual(event["photo_count"], 4)
        self.assertEqual(self.call("get", event["cover_thumbnail_url"], 200)["Content-Type"], "image/jpeg")
        photos = self.call("get", f"/api/events/{event['id']}/photos/", 200).data["results"]
        self.assertEqual(len(photos), 4)

        # 6. Enhance a dark photo with the real pipeline and check it really got brighter.
        dark_row = next(row for row in rows if row["original_name"] == DARK_PHOTOS[0].name)
        queued = self.call("post", f"/api/enhancements/images/{dark_row['id']}/", 202, data={"level": "auto", "style": "natural"}, format="json")
        result = self.call("get", f"/api/enhancements/results/{queued.data['id']}/", 200).data
        self.assertEqual(result["status"], "completed", result.get("failure_reason"))
        enhanced = self.body(self.call("get", result["download_url"], 200))
        self.assertGreater(luminance(enhanced), luminance(originals[DARK_PHOTOS[0].name]) + 20)

        # 7. Make a collage from the event and check the file.
        started = self.call("post", "/api/collages/", 202, data={"source_type": "event", "event_id": event["id"]}, format="json")
        collage = self.call("get", f"/api/collages/{started.data['id']}/", 200).data
        self.assertEqual(collage["status"], "ready", collage.get("failure_reason"))
        picture = cv2.imdecode(np.frombuffer(self.body(self.call("get", collage["download_url"], 200)), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(picture.shape[:2], (1200, 1600))

        # 8. The user was told the batch finished, and only ever sees their own data.
        notifications = self.call("get", "/api/notifications/", 200).data
        kinds = {item["event_type"] for item in notifications["results"]}
        self.assertIn("batch_completed", kinds)
        stranger = APIClient()
        stranger_tokens = None
        registered = stranger.post("/api/auth/register/", {"username": "e2e-other", "email": "other@example.com", "password": PASSWORD}, format="json")
        stranger.post("/api/auth/verify-email/", {
            "registration_id": registered.data["registration_id"], "code": re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1),
        }, format="json")
        stranger_tokens = stranger.post("/api/auth/login/", {"identifier": "other@example.com", "password": PASSWORD}, format="json").data
        stranger.credentials(HTTP_AUTHORIZATION=f"Bearer {stranger_tokens['access']}")
        for url in (f"/api/images/{dark_row['id']}/", dark_row["thumbnail_url"], dark_row["download_url"], result["download_url"]):
            self.assertEqual(stranger.get(url).status_code, 404, url)
        self.assertEqual(stranger.get("/api/images/my/").data["count"], 0)
