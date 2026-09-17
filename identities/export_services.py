import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import IdentityGroupExport, IdentityMembership


logger = logging.getLogger(__name__)


class IdentityExportNotReadyError(ValueError):
    pass


def ensure_group_exportable(group):
    if group.state != group.State.ACTIVE:
        raise IdentityExportNotReadyError("Only active identity groups can be exported.")
    if not IdentityMembership.objects.filter(
        owner_id=group.owner_id,
        identity_group=group,
        identity_face__owner_id=group.owner_id,
        identity_face__image__owner_id=group.owner_id,
    ).exclude(state__in=[
        IdentityMembership.State.REJECTED,
        IdentityMembership.State.REMOVED,
    ]).exists():
        raise IdentityExportNotReadyError(
            "The identity group has no exportable members."
        )


def enqueue_identity_group_export(*, owner, group, options):
    from .tasks import generate_identity_group_export_task

    if group.owner_id != owner.pk:
        raise IdentityExportNotReadyError("The identity group is unavailable.")
    ensure_group_exportable(group)
    active_filters = {
        "owner": owner,
        "identity_group": group,
        **options,
        "status__in": [
            IdentityGroupExport.Status.QUEUED,
            IdentityGroupExport.Status.GENERATING,
        ],
    }
    try:
        with transaction.atomic():
            active = (
                IdentityGroupExport.objects.select_for_update()
                .filter(**active_filters)
                .first()
            )
            if active:
                return active, False
            task_id = uuid.uuid4().hex
            export = IdentityGroupExport.objects.create(
                owner=owner,
                identity_group=group,
                task_id=task_id,
                status=IdentityGroupExport.Status.QUEUED,
                **options,
            )

            def dispatch():
                try:
                    generate_identity_group_export_task.apply_async(
                        args=[export.pk], task_id=task_id
                    )
                except Exception:
                    logger.exception("Could not queue identity export_id=%s.", export.pk)
                    IdentityGroupExport.objects.filter(pk=export.pk).update(
                        status=IdentityGroupExport.Status.FAILED,
                        failure_reason="The identity-export queue is unavailable.",
                    )

            transaction.on_commit(dispatch)
    except IntegrityError:
        active = IdentityGroupExport.objects.filter(**active_filters).first()
        if active:
            return active, False
        raise
    export.refresh_from_db()
    return export, True


def identity_export_expiration_time():
    return timezone.now() + timedelta(hours=settings.DOWNLOAD_PACKAGE_TTL_HOURS)


def expire_identity_export_if_needed(export, now=None):
    now = now or timezone.now()
    if (
        export.status == IdentityGroupExport.Status.READY
        and export.expires_at and export.expires_at <= now
    ):
        if export.export_file:
            export.export_file.delete(save=False)
        export.status = IdentityGroupExport.Status.EXPIRED
        export.export_file = ""
        export.file_size = 0
        export.file_checksum_sha256 = ""
        export.progress_percentage = 100
        export.save(update_fields=[
            "status", "export_file", "file_size", "file_checksum_sha256",
            "progress_percentage", "updated_at",
        ])
        return True
    return False


def expire_due_identity_exports(*, owner=None, now=None):
    now = now or timezone.now()
    queryset = IdentityGroupExport.objects.filter(
        status=IdentityGroupExport.Status.READY,
        expires_at__lte=now,
    )
    if owner is not None:
        queryset = queryset.filter(owner=owner)
    expired = 0
    for export in queryset.iterator():
        expired += int(expire_identity_export_if_needed(export, now=now))
    return expired

