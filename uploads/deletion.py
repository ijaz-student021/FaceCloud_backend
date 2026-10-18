"""Deleting photographs, and everything that would otherwise still show them.

Removing an ``ImageUpload`` already removes its files, thumbnails, quality and face records, enhanced copies and the share links that point at
those copies (they are deleted with the photo). Three things do not follow by themselves, and are done here:

* **Person folders.** A folder keeps a member count and an average face (its centroid) that the matching uses. Both are recomputed for every folder
  the photograph was in, so the folder no longer counts it and no longer matches faces against it. A folder left with no photographs stays, empty,
  until its owner deletes it (it may have a name worth keeping); its cover photo is cleared by the database.
* **Ready ZIPs and folder exports that contain a copy of the photograph.** A package is built per upload batch and a folder export per folder; either
  holds a copy of the photograph, so it is expired now (its file deleted, its secure links answering 410) instead of lingering until its own expiry.
  The owner can build a new one.
* **Everything in one transaction.** Either the photographs and these effects are all gone or none is.
"""
import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def delete_images(images):
    """Delete ``images`` (a list of ``ImageUpload``) with the effects above; returns what was done."""
    from downloads.models import DownloadPackage
    from downloads.services import expire_package_if_needed
    from identities.export_services import expire_identity_export_if_needed
    from identities.models import IdentityGroup, IdentityGroupExport, IdentityMembership
    from identities.services import refresh_group

    images = list(images)
    if not images:
        return {"deleted": 0, "folders_updated": 0, "packages_expired": 0, "exports_expired": 0}
    image_ids = [image.pk for image in images]
    batch_ids = {image.batch_id for image in images}
    now = timezone.now()
    with transaction.atomic():
        group_ids = set(
            IdentityMembership.objects.filter(identity_face__image_id__in=image_ids).values_list("identity_group_id", flat=True)
        )
        packages = DownloadPackage.objects.filter(batch_id__in=batch_ids, status=DownloadPackage.Status.READY)
        packages_expired = 0
        for package in packages:
            package.expires_at = now
            packages_expired += int(expire_package_if_needed(package, now=now))
        exports_expired = 0
        for export in IdentityGroupExport.objects.filter(identity_group_id__in=group_ids, status=IdentityGroupExport.Status.READY):
            export.expires_at = now
            exports_expired += int(expire_identity_export_if_needed(export, now=now))
        for image in images:
            image.delete()
        for group in IdentityGroup.objects.filter(pk__in=group_ids):
            refresh_group(group)
    logger.info("Deleted %d photo(s); %d folder(s) recounted, %d package(s) and %d folder export(s) expired.",
                len(images), len(group_ids), packages_expired, exports_expired)
    return {
        "deleted": len(images), "folders_updated": len(group_ids),
        "packages_expired": packages_expired, "exports_expired": exports_expired,
    }
