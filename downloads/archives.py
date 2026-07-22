import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch
from django.utils import timezone

from enhancement.models import EnhancementStatus, ImageEnhancementResult
from reports.models import ProcessingReport
from reports.renderers import render_report
from reports.services import build_report_payload
from uploads.models import ImageUpload

from .models import DownloadPackage


class PackageBuildError(ValueError):
    pass


def safe_component(value, fallback="file", maximum=100):
    value = Path(str(value or "")).name
    source = Path(value)
    suffix = re.sub(r"[^A-Za-z0-9.]", "", source.suffix.lower())[:16]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip("-._")
    fallback_stem = re.sub(
        r"[^A-Za-z0-9._-]+", "-", Path(fallback).stem
    ).strip("-._") or "file"
    stem_limit = max(1, maximum - len(suffix))
    return f"{(stem or fallback_stem)[:stem_limit]}{suffix}"


def package_display_name(batch, package_type):
    source = batch.display_name or batch.original_archive_name or f"batch-{batch.pk}"
    stem = safe_component(Path(source).stem, fallback=f"batch-{batch.pk}", maximum=70)
    suffix = "accepted-results" if package_type == "accepted_results" else "complete-batch"
    return f"facecloud-{stem}-{suffix}.zip"


def _stored_size(file_field):
    if not file_field or not file_field.name:
        return None
    try:
        if not file_field.storage.exists(file_field.name):
            return None
        return int(file_field.storage.size(file_field.name))
    except (OSError, ValueError, NotImplementedError):
        return None


def _unique_archive_path(folder, filename, used_paths):
    filename = safe_component(filename)
    path = f"{folder}/{filename}"
    if path not in used_paths:
        used_paths.add(path)
        return path
    original = Path(filename)
    counter = 2
    while True:
        candidate = f"{folder}/{original.stem}-{counter}{original.suffix}"
        if candidate not in used_paths:
            used_paths.add(candidate)
            return candidate
        counter += 1


def _copy_stored_file(archive, file_field, archive_path):
    with file_field.open("rb") as source, archive.open(archive_path, "w") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)


def _images_for_package(package):
    enhancements = ImageEnhancementResult.objects.filter(
        status=EnhancementStatus.COMPLETED,
    ).order_by("-created_at")
    return list(
        ImageUpload.objects.filter(
            owner_id=package.owner_id,
            batch_id=package.batch_id,
        )
        .select_related("preprocessing_result", "quality_decision")
        .prefetch_related(Prefetch(
            "enhancement_results",
            queryset=enhancements,
            to_attr="package_enhancement_results",
        ))
        .order_by("pk")
    )


def _report_bytes(package):
    report = ProcessingReport(
        owner_id=package.owner_id,
        report_type=ProcessingReport.ReportType.BATCH,
        format=package.report_format,
        batch_id=package.batch_id,
    )
    payload = build_report_payload(report)
    content, content_type, extension = render_report(payload, package.report_format)
    return content, content_type, extension


def build_package_archive(package):
    images = _images_for_package(package)
    if not images:
        raise PackageBuildError("The batch has no images available for packaging.")

    maximum_files = settings.DOWNLOAD_PACKAGE_MAX_FILES
    maximum_bytes = settings.DOWNLOAD_PACKAGE_MAX_UNCOMPRESSED_SIZE
    compression_level = settings.DOWNLOAD_PACKAGE_COMPRESSION_LEVEL
    used_paths = set()
    candidates = []
    skipped = []

    def add_candidate(image, kind, folder, file_field, display_name):
        size = _stored_size(file_field)
        if size is None:
            skipped.append({
                "image_id": image.pk,
                "resource_type": kind,
                "reason": "Stored file unavailable",
            })
            return
        archive_path = _unique_archive_path(folder, display_name, used_paths)
        candidates.append({
            "image_id": image.pk,
            "resource_type": kind,
            "archive_path": archive_path,
            "size": size,
            "file_field": file_field,
        })

    for image in images:
        enhancement = (
            image.package_enhancement_results[0]
            if image.package_enhancement_results else None
        )
        if package.package_type == DownloadPackage.PackageType.ACCEPTED_RESULTS:
            quality = getattr(image, "quality_decision", None)
            if not quality or quality.status != "completed" or quality.decision != "accepted":
                continue
            if enhancement is None:
                skipped.append({
                    "image_id": image.pk,
                    "resource_type": "enhanced",
                    "reason": "Accepted image has no completed enhancement",
                })
                continue
            add_candidate(
                image, "enhanced", "enhanced", enhancement.enhanced_file,
                enhancement.output_name or f"enhanced-{image.original_name}",
            )
            continue

        if image.original_file:
            add_candidate(
                image, "original", "original", image.original_file,
                image.original_name,
            )
        try:
            preprocessing = image.preprocessing_result
        except ObjectDoesNotExist:
            preprocessing = None
        if preprocessing and preprocessing.status == "completed" and preprocessing.processed_file:
            add_candidate(
                image, "standardized", "standardized", preprocessing.processed_file,
                preprocessing.output_name or f"standardized-{image.original_name}",
            )
        if enhancement is not None:
            add_candidate(
                image, "enhanced", "enhanced", enhancement.enhanced_file,
                enhancement.output_name or f"enhanced-{image.original_name}",
            )

    if package.package_type == DownloadPackage.PackageType.ACCEPTED_RESULTS and not candidates:
        raise PackageBuildError(
            "The batch has no accepted enhanced images ready for download."
        )

    report_content, report_content_type, report_extension = _report_bytes(package)
    report_path = f"reports/batch-report.{report_extension}"
    projected_files = len(candidates) + 2  # report and manifest
    projected_bytes = sum(item["size"] for item in candidates) + len(report_content)
    if projected_files > maximum_files:
        raise PackageBuildError("The package would exceed the configured file-count limit.")
    if projected_bytes > maximum_bytes:
        raise PackageBuildError("The package would exceed the configured size limit.")

    manifest = {
        "schema_version": "download_package_v1",
        "package_id": package.pk,
        "package_type": package.package_type,
        "batch_id": package.batch_id,
        "generated_at": timezone.now().isoformat(),
        "report": {
            "archive_path": report_path,
            "format": package.report_format,
            "content_type": report_content_type,
            "size": len(report_content),
        },
        "files": [
            {key: value for key, value in item.items() if key != "file_field"}
            for item in candidates
        ],
        "skipped_files": skipped,
        "summary": {
            "included_images": len({item["image_id"] for item in candidates}),
            "included_files": len(candidates),
            "skipped_files": len(skipped),
            "uncompressed_bytes": projected_bytes,
        },
    }
    manifest_content = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    if projected_bytes + len(manifest_content) > maximum_bytes:
        raise PackageBuildError("The package would exceed the configured size limit.")

    temporary = tempfile.NamedTemporaryFile(
        prefix="facecloud-package-", suffix=".zip", delete=False
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=compression_level,
            allowZip64=True,
        ) as archive:
            for item in candidates:
                try:
                    _copy_stored_file(
                        archive, item["file_field"], item["archive_path"]
                    )
                except (OSError, ValueError):
                    raise PackageBuildError(
                        "A selected source file became unavailable during packaging."
                    )
            archive.writestr(report_path, report_content)
            archive.writestr("manifest.json", manifest_content)

        checksum = hashlib.sha256()
        with temporary_path.open("rb") as package_stream:
            for chunk in iter(lambda: package_stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        return temporary_path, manifest, temporary_path.stat().st_size, checksum.hexdigest()
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
