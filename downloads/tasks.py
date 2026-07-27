import logging

from celery import shared_task
from django.core.files import File
from django.db import transaction
from django.utils import timezone

from .archives import build_package_archive, package_display_name
from .models import DownloadPackage
from .services import expire_due_packages, package_expiration_time

logger = logging.getLogger(__name__)


def process_download_package(package_id, task_id=""):
    with transaction.atomic():
        package = DownloadPackage.objects.select_for_update().filter(pk=package_id).first()
        if package is None:
            return None
        if package.status == DownloadPackage.Status.READY:
            return package
        if package.task_id and task_id and package.task_id != task_id:
            return package
        package.status = DownloadPackage.Status.GENERATING
        package.failure_reason = ""
        package.save(update_fields=["status", "failure_reason", "updated_at"])

    temporary_path = None
    try:
        package = DownloadPackage.objects.select_related("batch").get(pk=package_id)
        temporary_path, manifest, file_size, checksum = build_package_archive(package)
        display_name = package_display_name(package.batch, package.package_type)
        with transaction.atomic():
            locked = DownloadPackage.objects.select_for_update().filter(pk=package_id).first()
            if locked is None:
                return None
            if locked.task_id and task_id and locked.task_id != task_id:
                return locked
            if locked.package_file:
                locked.package_file.delete(save=False)
            with temporary_path.open("rb") as stream:
                locked.package_file.save(display_name, File(stream), save=False)
            locked.display_name = display_name
            locked.manifest = manifest
            locked.file_size = file_size
            locked.file_checksum_sha256 = checksum
            locked.status = DownloadPackage.Status.READY
            locked.failure_reason = ""
            locked.generated_at = timezone.now()
            locked.expires_at = package_expiration_time()
            locked.save()
            return locked
    except Exception:
        logger.exception("Could not generate download package_id=%s.", package_id)
        with transaction.atomic():
            failed = DownloadPackage.objects.select_for_update().filter(pk=package_id).first()
            if failed is None:
                return None
            if failed.package_file:
                failed.package_file.delete(save=False)
            failed.package_file = ""
            failed.status = DownloadPackage.Status.FAILED
            failed.failure_reason = "The download package could not be generated safely."
            failed.file_size = 0
            failed.file_checksum_sha256 = ""
            failed.expires_at = None
            failed.save()
            return failed
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@shared_task(bind=True, name="downloads.generate_package", acks_late=True)
def generate_download_package_task(self, package_id):
    package = DownloadPackage.objects.filter(pk=package_id).first()
    if package is None:
        return {"package_id": package_id, "status": "deleted"}
    if package.task_id and self.request.id and package.task_id != self.request.id:
        return {"package_id": package_id, "status": "superseded"}
    package = process_download_package(package_id, self.request.id or "")
    if package is not None:
        from notifications.events import notify_download_result
        notify_download_result(package)
    return {
        "package_id": package_id,
        "status": package.status if package is not None else "deleted",
    }


@shared_task(name="downloads.cleanup_expired")
def cleanup_expired_download_packages():
    return {"expired_packages": expire_due_packages()}
