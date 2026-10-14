"""Live camera sessions: metadata only, plus links to snapshots the user saved.

No video is received or stored. A snapshot exists only when the user sends one
(an explicit action) and is then an ordinary upload, processed and kept like
any other image. Session records (and their snapshot links, not the images)
are deleted CAMERA_SESSION_RETENTION_DAYS after the session started.
"""
import uuid

from django.conf import settings
from django.db import models


class CameraSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="camera_sessions")
    device_label = models.CharField(max_length=80, blank=True)
    settings = models.JSONField(default=dict, blank=True, help_text="Client preferences, e.g. resolution, overlay.")
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at"]


class CameraSnapshot(models.Model):
    session = models.ForeignKey(CameraSession, on_delete=models.CASCADE, related_name="snapshots")
    image = models.OneToOneField("uploads.ImageUpload", on_delete=models.CASCADE, related_name="camera_snapshot")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
