"""Feature 45: load/performance testing against a running FaceCloud server.

Run with the isolated loadtest environment (never the main venv -- Locust
pulls in gevent and its own web UI, unrelated to the Django app runtime):

    python -m venv .runtime/venvs/loadtest
    .runtime/venvs/loadtest/Scripts/pip install -r requirements-loadtest.txt
    venv/Scripts/python manage.py seed_loadtest_users --count 20   # main venv, DEBUG only
    .runtime/venvs/loadtest/Scripts/locust -f loadtest/locustfile.py --host http://127.0.0.1:8000

Never point this at a real deployment: ``seed_loadtest_users`` accounts share
one public password and this file logs in as them by a predictable username.
"""
import io
import os
import random

from locust import HttpUser, between, task
from PIL import Image

LOADTEST_PASSWORD = "LoadTest!Pass123"
USER_COUNT = int(os.environ.get("LOADTEST_USER_COUNT", "20"))


def _sample_jpeg_bytes():
    color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    image = Image.new("RGB", (64, 64), color=color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


class AuthenticatedUser(HttpUser):
    """Base: log in once per simulated user, keep the access token for every request."""

    abstract = True

    def login(self, username):
        response = self.client.post(
            "/api/auth/login/",
            json={"identifier": username, "password": LOADTEST_PASSWORD},
            name="/api/auth/login/",
        )
        response.raise_for_status()
        self.token = response.json()["access"]
        self.client.headers.update({"Authorization": f"Bearer {self.token}"})


class RegularUser(AuthenticatedUser):
    """The common case: a member browsing and occasionally uploading."""

    weight = 10
    wait_time = between(1, 3)

    def on_start(self):
        self.login(f"loadtest-user-{random.randint(1, USER_COUNT)}")
        self.batch_ids = []

    @task(4)
    def list_batches(self):
        response = self.client.get("/api/images/batches/", name="/api/images/batches/")
        if response.ok:
            payload = response.json()
            results = payload["results"] if isinstance(payload, dict) else payload
            self.batch_ids = [row["id"] for row in results]

    @task(4)
    def list_my_images(self):
        self.client.get("/api/images/my/", name="/api/images/my/")

    @task(2)
    def view_batch_progress(self):
        if not self.batch_ids:
            return
        batch_id = random.choice(self.batch_ids)
        self.client.get(
            f"/api/images/batches/{batch_id}/progress/", name="/api/images/batches/[id]/progress/",
        )

    @task(1)
    def upload_single_image(self):
        files = {"file": ("sample.jpg", _sample_jpeg_bytes(), "image/jpeg")}
        self.client.post(
            "/api/images/upload/single/", files=files, name="/api/images/upload/single/",
        )


class AdminUser(AuthenticatedUser):
    """Rare in practice: an administrator checking platform-wide views."""

    weight = 1
    wait_time = between(2, 5)

    def on_start(self):
        self.login("loadtest-admin")

    @task(2)
    def system_health(self):
        self.client.get("/api/system/health/", name="/api/system/health/")

    @task(2)
    def analytics(self):
        self.client.get("/api/system/analytics/", name="/api/system/analytics/")

    @task(2)
    def moderation_overview(self):
        self.client.get("/api/system/moderation/", name="/api/system/moderation/")

    @task(1)
    def audit_feed(self):
        self.client.get("/api/system/audit/", name="/api/system/audit/")

    @task(1)
    def queue_status(self):
        self.client.get("/api/system/queue/", name="/api/system/queue/")
