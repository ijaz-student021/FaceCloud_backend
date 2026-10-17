from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import CameraSession


def purge_old_sessions(now=None):
    """Delete session records older than the retention period (the images stay)."""
    cutoff = (now or timezone.now()) - timedelta(days=settings.CAMERA_SESSION_RETENTION_DAYS)
    return CameraSession.objects.filter(started_at__lt=cutoff).delete()[0]
