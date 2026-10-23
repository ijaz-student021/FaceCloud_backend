import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from enhancement.models import EnhancementStatus, ImageEnhancementResult

from .models import DownloadEvent, DownloadPackage

logger = logging.getLogger(__name__)

TERMINAL_BATCH_STATUSES = {"completed", "partial", "failed", "cancelled"}


class PackageNotReadyError(ValueError):
    """The package cannot be made yet. ``code`` and ``extra`` let a client decide what to do next."""

    def __init__(self, message, code="package_not_ready", **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def ensure_package_ready(batch, package_type):
    if batch.processing_status not in TERMINAL_BATCH_STATUSES:
        raise PackageNotReadyError(
            "Batch processing must reach a terminal state before packaging.",
            code="batch_still_processing",
        )
    if not batch.images.exists():
        raise PackageNotReadyError(
            "The batch has no images available for packaging.", code="batch_empty",
        )
    if package_type == DownloadPackage.PackageType.ACCEPTED_RESULTS:
        available = ImageEnhancementResult.objects.filter(
            owner_id=batch.owner_id,
            image__batch=batch,
            image__quality_decision__status="completed",
            image__quality_decision__decision="accepted",
            status=EnhancementStatus.COMPLETED,
        ).exists()
        if not available:
            # Photos that passed the checks but were never enhanced can be fixed by the caller:
            # POST /api/enhancements/batches/{id}/ and ask again.
            awaiting = batch.images.filter(
                owner_id=batch.owner_id,
                quality_decision__status="completed",
                quality_decision__decision="accepted",
            ).count()
            if awaiting and batch.auto_enhance:
                return  # an automatic enhancement was asked for and none finished (skipped or failed): the package carries the originals
            if awaiting:
                raise PackageNotReadyError(
                    "The accepted photos have not been enhanced yet. Enhance the batch, then ask for the package again.",
                    code="enhancement_required", accepted_images=awaiting,
                )
            raise PackageNotReadyError(
                "The batch has no accepted enhanced images ready for download.",
                code="no_accepted_images",
            )


def enqueue_download_package(*, owner, batch, package_type, report_format):
    from .tasks import generate_download_package_task

    ensure_package_ready(batch, package_type)
    active_filters = {
        "owner": owner,
        "batch": batch,
        "package_type": package_type,
        "report_format": report_format,
        "status__in": [
            DownloadPackage.Status.QUEUED,
            DownloadPackage.Status.GENERATING,
        ],
    }
    try:
        with transaction.atomic():
            active = (
                DownloadPackage.objects.select_for_update()
                .filter(**active_filters)
                .first()
            )
            if active:
                return active, False
            task_id = uuid.uuid4().hex
            package = DownloadPackage.objects.create(
                owner=owner,
                batch=batch,
                package_type=package_type,
                report_format=report_format,
                status=DownloadPackage.Status.QUEUED,
                task_id=task_id,
            )

            def dispatch():
                try:
                    generate_download_package_task.apply_async(
                        args=[package.pk], task_id=task_id
                    )
                except Exception:
                    logger.exception(
                        "Could not queue download package_id=%s.", package.pk
                    )
                    DownloadPackage.objects.filter(pk=package.pk).update(
                        status=DownloadPackage.Status.FAILED,
                        failure_reason="The download-package queue is unavailable.",
                    )

            transaction.on_commit(dispatch)
    except IntegrityError:
        active = DownloadPackage.objects.filter(**active_filters).first()
        if active:
            return active, False
        raise
    package.refresh_from_db()
    return package, True


def expire_package_if_needed(package, now=None):
    now = now or timezone.now()
    if (
        package.status == DownloadPackage.Status.READY
        and package.expires_at
        and package.expires_at <= now
    ):
        if package.package_file:
            package.package_file.delete(save=False)
        package.status = DownloadPackage.Status.EXPIRED
        package.package_file = ""
        package.file_size = 0
        package.file_checksum_sha256 = ""
        package.save(update_fields=[
            "status", "package_file", "file_size", "file_checksum_sha256",
            "updated_at",
        ])
        return True
    return False


def expire_due_packages(*, owner=None, now=None):
    now = now or timezone.now()
    queryset = DownloadPackage.objects.filter(
        status=DownloadPackage.Status.READY,
        expires_at__lte=now,
    )
    if owner is not None:
        queryset = queryset.filter(owner=owner)
    expired = 0
    for package in queryset.iterator():
        expired += int(expire_package_if_needed(package, now=now))
    return expired


def package_expiration_time():
    return timezone.now() + timedelta(hours=settings.DOWNLOAD_PACKAGE_TTL_HOURS)


def record_download(
    *, owner, resource_type, resource_id, filename, file_size=0,
    content_type="", batch_id=None, image_id=None,
):
    return DownloadEvent.objects.create(
        owner=owner,
        resource_type=resource_type,
        resource_id=resource_id,
        batch_id=batch_id,
        image_id=image_id,
        filename=filename,
        file_size=max(0, int(file_size or 0)),
        content_type=content_type or "",
    )
