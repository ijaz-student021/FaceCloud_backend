"""Cleanup of temporary processing files and orphaned media.

Two kinds of leftovers can hold users' face photos long after the request that
made them:

* **Stale temp archives.** The package and identity-export builders write a ZIP
  to the system temp directory (``delete=False``) and remove it in a
  ``finally`` block. A worker killed mid-build (out of memory, the hard task
  time limit, a crash) never reaches it, so the ZIP stays forever. These are
  deleted by name pattern and age only; nothing else in the temp directory is
  touched.
* **Orphaned media.** A file saved to storage whose database row was never
  committed (a crash between the two) is referenced by nothing, so neither the
  normal delete signals nor account deletion will ever remove it. Every file
  field in the project is checked, and only files older than a grace period in
  the known per-user directories are considered. Orphans are *reported* by
  default; they are only deleted when explicitly enabled, because removing user
  files on a heuristic must never be a surprise.
"""

import fnmatch
import logging
import os
import tempfile
from datetime import timedelta
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import models
from django.utils import timezone

from .models import PrivacyRequest

logger = logging.getLogger(__name__)

TEMP_ARCHIVE_PATTERNS = ("facecloud-package-*.zip", "facecloud-identity-export-*.zip")
# users/<id>/<one of these>/<uuid>.<ext>; a test ties this list to every FileField
# so a new upload path cannot silently be treated as orphaned.
MEDIA_SUBDIRECTORIES = (
    "originals", "preprocessed", "enhanced", "edited",
    "download-packages", "identity-exports", "reports", "comparisons",
)


def stale_temp_archives(directory=None, max_age_hours=None, now=None):
    """``[(path, size)]`` of leftover build archives older than the age limit."""
    directory = Path(directory or tempfile.gettempdir())
    hours = settings.TEMP_FILE_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    cutoff = (now or timezone.now()).timestamp() - hours * 3600
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    found = []
    for entry in entries:
        if not any(fnmatch.fnmatchcase(entry.name, pattern) for pattern in TEMP_ARCHIVE_PATTERNS):
            continue
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            found.append((Path(entry.path), stat.st_size))
    return found


def cleanup_stale_temp_archives(*, dry_run=False, directory=None, max_age_hours=None, now=None):
    found = stale_temp_archives(directory, max_age_hours, now)
    deleted = failed = freed = 0
    if not dry_run:
        for path, size in found:
            try:
                path.unlink()
            except OSError:
                failed += 1
                logger.warning("Could not remove stale temp archive %s.", path.name)
                continue
            deleted += 1
            freed += size
    return {
        "found": len(found),
        "bytes_found": sum(size for _path, size in found),
        "deleted": deleted,
        "bytes_freed": freed,
        "failed": failed,
    }


def referenced_media_names():
    """Every file name any model in the project points at, discovered dynamically."""
    names = set()
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if isinstance(field, models.FileField):
                stored = model._default_manager.exclude(**{field.name: ""}).values_list(field.name, flat=True)
                names.update(name for name in stored.iterator() if name)
    return names


def orphan_media_files(grace_hours=None, now=None):
    """``[(name, size)]`` of unreferenced files past the grace period."""
    hours = settings.ORPHAN_MEDIA_GRACE_HOURS if grace_hours is None else grace_hours
    cutoff = (now or timezone.now()) - timedelta(hours=hours)
    referenced = referenced_media_names()
    try:
        user_directories, _files = default_storage.listdir("users")
    except (FileNotFoundError, OSError):
        return []
    orphans = []
    for user_directory in user_directories:
        for subdirectory in MEDIA_SUBDIRECTORIES:
            directory = f"users/{user_directory}/{subdirectory}"
            try:
                _dirs, files = default_storage.listdir(directory)
            except (FileNotFoundError, OSError):
                continue
            for filename in files:
                name = f"{directory}/{filename}"
                if name in referenced:
                    continue
                try:
                    if default_storage.get_modified_time(name) >= cutoff:
                        continue
                    orphans.append((name, default_storage.size(name)))
                except (OSError, NotImplementedError):
                    continue
    return orphans


def cleanup_orphan_media(*, delete=False, grace_hours=None, now=None):
    orphans = orphan_media_files(grace_hours, now)
    deleted = failed = 0
    if delete:
        for name, _size in orphans:
            try:
                default_storage.delete(name)
            except OSError:
                failed += 1
                logger.warning("Could not remove orphaned media file %s.", name)
                continue
            deleted += 1
    return {
        "found": len(orphans),
        "bytes_found": sum(size for _name, size in orphans),
        "deleted": deleted,
        "failed": failed,
        "delete_enabled": bool(delete),
        "names": [name for name, _size in orphans[:50]],
    }


def cleanup_temporary_files(*, dry_run=False, delete_orphans=None, now=None, record=True):
    """Run both cleanups. Orphans are only deleted when enabled (setting or argument)."""
    if delete_orphans is None:
        delete_orphans = settings.ORPHAN_MEDIA_CLEANUP_ENABLED
    archives = cleanup_stale_temp_archives(dry_run=dry_run, now=now)
    orphans = cleanup_orphan_media(delete=delete_orphans and not dry_run, now=now)
    summary = {"dry_run": dry_run, "temp_archives": archives, "orphaned_media": orphans}
    failed = archives["failed"] + orphans["failed"]
    if record and not dry_run and (archives["deleted"] or orphans["deleted"] or failed):
        stored = {**summary, "orphaned_media": {k: v for k, v in orphans.items() if k != "names"}}
        PrivacyRequest.objects.create(
            user=None,
            kind=PrivacyRequest.Kind.TEMP_CLEANUP,
            trigger=PrivacyRequest.Trigger.OPERATOR,
            status=PrivacyRequest.Status.FAILED if failed else PrivacyRequest.Status.COMPLETED,
            summary=stored,
            verified=failed == 0,
        )
    return summary
