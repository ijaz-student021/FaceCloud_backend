import csv
import hashlib
import io
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from django.conf import settings
from django.db.models import Prefetch
from django.utils import timezone

from enhancement.models import EnhancementStatus, ImageEnhancementResult

from .models import IdentityGroupExport, IdentityMembership


class IdentityExportBuildError(ValueError):
    pass


def safe_component(value, fallback="file", maximum=100):
    value = Path(str(value or "")).name
    source = Path(value)
    suffix = re.sub(r"[^A-Za-z0-9.]", "", source.suffix.lower())[:16]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip("-._")
    fallback_stem = re.sub(
        r"[^A-Za-z0-9._-]+", "-", Path(fallback).stem
    ).strip("-._") or "file"
    return f"{(stem or fallback_stem)[:max(1, maximum - len(suffix))]}{suffix}"


def identity_export_display_name(export):
    group_name = safe_component(
        export.identity_group.display_name,
        fallback=f"identity-group-{export.identity_group_id}",
        maximum=70,
    )
    return f"facecloud-{group_name}-images.zip"


def _stored_size(file_field):
    if not file_field or not file_field.name:
        return None
    try:
        if not file_field.storage.exists(file_field.name):
            return None
        return int(file_field.storage.size(file_field.name))
    except (OSError, ValueError, NotImplementedError):
        return None


def _unique_path(folder, filename, used_paths):
    filename = safe_component(filename)
    candidate = f"{folder}/{filename}"
    if candidate not in used_paths:
        used_paths.add(candidate)
        return candidate
    source = Path(filename)
    counter = 2
    while True:
        candidate = f"{folder}/{source.stem}-{counter}{source.suffix}"
        if candidate not in used_paths:
            used_paths.add(candidate)
            return candidate
        counter += 1


def _copy_stored_file(archive, file_field, archive_path):
    with file_field.open("rb") as source, archive.open(archive_path, "w") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)


def _members_for_export(export):
    enhancements = ImageEnhancementResult.objects.filter(
        owner_id=export.owner_id,
        status=EnhancementStatus.COMPLETED,
    ).order_by("-created_at")
    return list(
        IdentityMembership.objects.filter(
            owner_id=export.owner_id,
            identity_group_id=export.identity_group_id,
            identity_group__owner_id=export.owner_id,
            identity_face__owner_id=export.owner_id,
            identity_face__image__owner_id=export.owner_id,
        )
        .exclude(state__in=[
            IdentityMembership.State.REJECTED,
            IdentityMembership.State.REMOVED,
        ])
        .select_related(
            "identity_face__image__preprocessing_result",
            "identity_group",
        )
        .prefetch_related(Prefetch(
            "identity_face__image__enhancement_results",
            queryset=enhancements,
            to_attr="identity_export_enhancements",
        ))
        .order_by("identity_face__image_id", "identity_face__face_index", "pk")
    )


def _report_content(export, members, image_ids):
    rows = [
        {
            "membership_id": member.pk,
            "image_id": member.identity_face.image_id,
            "face_index": member.identity_face.face_index,
            "membership_state": member.state,
        }
        for member in members
    ]
    if export.report_format == IdentityGroupExport.ReportFormat.CSV:
        output = io.StringIO(newline="")
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "membership_id", "image_id", "face_index", "membership_state",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue().encode("utf-8"), "csv"
    payload = {
        "schema_version": "identity_group_export_v1",
        "group_id": export.identity_group_id,
        "group_name": export.identity_group.display_name,
        "member_count": len(members),
        "unique_image_count": len(image_ids),
        "memberships": rows,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"), "json"


def build_identity_export_archive(export):
    members = _members_for_export(export)
    if not members:
        raise IdentityExportBuildError("The identity group has no exportable members.")

    maximum_files = settings.DOWNLOAD_PACKAGE_MAX_FILES
    maximum_bytes = settings.DOWNLOAD_PACKAGE_MAX_UNCOMPRESSED_SIZE
    used_paths = set()
    candidates = []
    skipped = []
    images = {}
    for member in members:
        image = member.identity_face.image
        images.setdefault(image.pk, image)

    def add_candidate(image, kind, folder, file_field, display_name):
        size = _stored_size(file_field)
        if size is None:
            skipped.append({
                "image_id": image.pk,
                "resource_type": kind,
                "reason": "Stored file unavailable",
            })
            return
        candidates.append({
            "image_id": image.pk,
            "resource_type": kind,
            "archive_path": _unique_path(folder, display_name, used_paths),
            "size": size,
            "file_field": file_field,
        })

    for image in images.values():
        if export.include_originals and image.original_file:
            add_candidate(
                image, "original", "originals", image.original_file,
                image.original_name,
            )
        preprocessing = getattr(image, "preprocessing_result", None)
        if (
            export.include_preprocessed and preprocessing
            and preprocessing.status == "completed" and preprocessing.processed_file
        ):
            add_candidate(
                image, "preprocessed", "preprocessed", preprocessing.processed_file,
                preprocessing.output_name or f"preprocessed-{image.original_name}",
            )
        enhancements = getattr(image, "identity_export_enhancements", [])
        if export.include_enhanced:
            if enhancements:
                enhancement = enhancements[0]
                add_candidate(
                    image, "enhanced", "enhanced", enhancement.enhanced_file,
                    enhancement.output_name or f"enhanced-{image.original_name}",
                )
            else:
                skipped.append({
                    "image_id": image.pk,
                    "resource_type": "enhanced",
                    "reason": "No completed enhancement",
                })

    report_content = b""
    report_path = None
    if export.include_report:
        report_content, extension = _report_content(export, members, set(images))
        report_path = f"report/identity-group.{extension}"

    if not candidates and not report_content:
        raise IdentityExportBuildError("No requested files are available for export.")

    projected_files = len(candidates) + int(bool(report_content)) + 1
    projected_bytes = sum(item["size"] for item in candidates) + len(report_content)
    if projected_files > maximum_files:
        raise IdentityExportBuildError("The export exceeds the configured file-count limit.")
    if projected_bytes > maximum_bytes:
        raise IdentityExportBuildError("The export exceeds the configured size limit.")

    manifest = {
        "schema_version": "identity_group_export_v1",
        "export_id": export.pk,
        "group_id": export.identity_group_id,
        "group_name": export.identity_group.display_name,
        "generated_at": timezone.now().isoformat(),
        "options": {
            "include_originals": export.include_originals,
            "include_preprocessed": export.include_preprocessed,
            "include_enhanced": export.include_enhanced,
            "include_report": export.include_report,
            "report_format": export.report_format,
        },
        "files": [
            {key: value for key, value in item.items() if key != "file_field"}
            for item in candidates
        ],
        "skipped_files": skipped,
        "summary": {
            "memberships": len(members),
            "unique_images": len(images),
            "included_files": len(candidates),
            "skipped_files": len(skipped),
            "uncompressed_bytes": projected_bytes,
        },
    }
    manifest_content = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    if projected_bytes + len(manifest_content) > maximum_bytes:
        raise IdentityExportBuildError("The export exceeds the configured size limit.")

    temporary = tempfile.NamedTemporaryFile(
        prefix="facecloud-identity-export-", suffix=".zip", delete=False
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=settings.DOWNLOAD_PACKAGE_COMPRESSION_LEVEL,
            allowZip64=True,
        ) as archive:
            for item in candidates:
                try:
                    _copy_stored_file(archive, item["file_field"], item["archive_path"])
                except (OSError, ValueError) as error:
                    raise IdentityExportBuildError(
                        "A selected source file became unavailable during export."
                    ) from error
            if report_content:
                archive.writestr(report_path, report_content)
            archive.writestr("manifest.json", manifest_content)

        checksum = hashlib.sha256()
        with temporary_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        return temporary_path, manifest, temporary_path.stat().st_size, checksum.hexdigest()
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

