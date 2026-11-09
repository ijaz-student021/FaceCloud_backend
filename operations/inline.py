"""Whether background jobs run inside the web request.

With ``CELERY_TASK_ALWAYS_EAGER`` on (the development default when ``DJANGO_DEBUG`` is on and the variable is not set) every "background" task runs in the
process that queued it: an upload is processed inside the HTTP request, which then returns a batch that is already finished. The progress screens never
see anything happen, which looks like a bug. This is made visible in four places: a warning when the server starts (``warn_if_inline``) and as a system check
(``operations.W001``), ``GET /api/system/health/`` (``background_jobs_inline`` and the celery service's detail ``eager``), and ``processing_mode`` in the
answer to every upload.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

INLINE = "inline"
BACKGROUND = "background"
WARNING_TEXT = (
    "Background jobs run inside the request (CELERY_TASK_ALWAYS_EAGER is on): uploads are processed before the request returns and the screens show no progress. "
    "Start a Celery worker and set CELERY_TASK_ALWAYS_EAGER=False to see progress."
)


def background_jobs_inline():
    return bool(settings.CELERY_TASK_ALWAYS_EAGER)


def processing_mode():
    """``"inline"`` when tasks run inside the request, else ``"background"`` (on a Celery worker)."""
    return INLINE if background_jobs_inline() else BACKGROUND


def warn_if_inline():
    """Log the warning once per server process; called when the web application is loaded (``config/wsgi.py``, ``config/asgi.py``)."""
    if background_jobs_inline():
        logger.warning(WARNING_TEXT)
    return background_jobs_inline()
