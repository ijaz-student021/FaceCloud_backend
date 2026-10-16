"""The extra facts the Downloads screen shows for each history row (kind, expiry, size of the set, live link).

Everything is looked up for a whole page at once -- a handful of queries however many rows there are --
and only within the caller's own records.
"""
from django.db.models import Count, Q
from django.utils import timezone

from .models import DownloadEvent

Type = DownloadEvent.ResourceType

KIND_BY_RESOURCE = {
    Type.ORIGINAL_IMAGE: "single",
    Type.STANDARDIZED_IMAGE: "single",
    Type.ENHANCED_IMAGE: "single",
    Type.EDITED_IMAGE: "single",
    Type.BATCH_PACKAGE: "batch",
    Type.IDENTITY_GROUP_PACKAGE: "batch",
    Type.PROCESSING_REPORT: "report",
}


def _ids(events, resource_type):
    return {event.resource_id for event in events if event.resource_type == resource_type}


def build_extras(owner, events):
    """``{event id: {"expires_at", "image_count", "has_active_link"}}`` for the given history rows."""
    from identities.models import IdentityGroupExport
    from reports.models import ProcessingReport
    from sharing.models import ShareLink

    from .models import DownloadPackage

    now = timezone.now()
    package_ids = _ids(events, Type.BATCH_PACKAGE)
    export_ids = _ids(events, Type.IDENTITY_GROUP_PACKAGE)
    report_ids = _ids(events, Type.PROCESSING_REPORT)
    result_ids = _ids(events, Type.ENHANCED_IMAGE)
    edit_ids = _ids(events, Type.EDITED_IMAGE)

    packages = {
        row.pk: row for row in DownloadPackage.objects.filter(owner=owner, pk__in=package_ids).only("id", "expires_at", "manifest")
    } if package_ids else {}
    exports = {
        row.pk: row for row in IdentityGroupExport.objects.filter(owner=owner, pk__in=export_ids).only("id", "expires_at", "manifest")
    } if export_ids else {}
    reports = {
        row["id"]: row
        for row in ProcessingReport.objects.filter(owner=owner, pk__in=report_ids)
        .annotate(batch_images=Count("batch__images")).values("id", "report_type", "batch_images")
    } if report_ids else {}

    # Live share links, keyed by what they point at. The token is never selected.
    links = {"result": {}, "edit": {}, "export": {}}
    if result_ids or edit_ids or export_ids:
        rows = ShareLink.objects.filter(owner=owner, revoked_at__isnull=True, expires_at__gt=now).filter(
            Q(enhancement_result_id__in=result_ids, edit_version__isnull=True)
            | Q(edit_version_id__in=edit_ids)
            | Q(identity_export_id__in=export_ids)
        ).values("enhancement_result_id", "edit_version_id", "identity_export_id", "expires_at")
        for row in rows:
            if row["identity_export_id"]:
                bucket, key = links["export"], row["identity_export_id"]
            elif row["edit_version_id"]:
                bucket, key = links["edit"], row["edit_version_id"]
            else:
                bucket, key = links["result"], row["enhancement_result_id"]
            bucket[key] = max(bucket.get(key, row["expires_at"]), row["expires_at"])

    extras = {}
    for event in events:
        expires_at, image_count, link_expiry = None, None, None
        if event.resource_type == Type.BATCH_PACKAGE and event.resource_id in packages:
            package = packages[event.resource_id]
            expires_at = package.expires_at
            image_count = (package.manifest.get("summary") or {}).get("included_images")
        elif event.resource_type == Type.IDENTITY_GROUP_PACKAGE and event.resource_id in exports:
            export = exports[event.resource_id]
            expires_at = export.expires_at
            image_count = (export.manifest.get("summary") or {}).get("unique_images")
            link_expiry = links["export"].get(event.resource_id)
        elif event.resource_type == Type.PROCESSING_REPORT and event.resource_id in reports:
            report = reports[event.resource_id]
            image_count = report["batch_images"] if report["report_type"] == "batch" else None
        elif event.resource_type == Type.ENHANCED_IMAGE:
            link_expiry = links["result"].get(event.resource_id)
        elif event.resource_type == Type.EDITED_IMAGE:
            link_expiry = links["edit"].get(event.resource_id)
        extras[event.pk] = {
            # The file's own expiry when it has one, else when the live share link ends, else nothing.
            "expires_at": expires_at or link_expiry,
            "image_count": image_count,
            "has_active_link": link_expiry is not None,
        }
    return extras
