from celery import shared_task

from .services import purge_old_sessions


@shared_task(name="camera.purge_old_sessions")
def purge_old_camera_sessions():
    return {"deleted": purge_old_sessions()}
