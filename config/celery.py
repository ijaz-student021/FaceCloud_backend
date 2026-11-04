import logging
import os

from celery import Celery
from celery.signals import worker_ready

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("facecloud")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@worker_ready.connect
def log_pipeline_stages(sender=None, **_):
    """One line when a worker is ready: which enhancement stages are on and which are off and why (the same list as GET /api/system/health/ "stages")."""
    try:
        from operations.services import check_models

        stages = check_models().get("stages") or []
        listed = "; ".join(f"{s['stage']}={'on (' + s['model'] + ')' if s['active'] else 'OFF (' + s['reason'] + ')'}" for s in stages)
        logging.getLogger("facecloud.worker").info("Enhancement stages: %s", listed)
    except Exception:  # a start-up note must never stop a worker
        logging.getLogger("facecloud.worker").exception("Could not list the enhancement stages.")
