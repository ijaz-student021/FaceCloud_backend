"""Bounded, authenticated, revision-specific editor previews.

How a preview request is handled:

1. The settings are validated exactly as for a saved edit (controls.py).
2. A bounded lossless copy of the enhanced image (``EditPreviewSource``) is
   made once per source version and reused, so moving a slider never decodes
   the full-resolution file.
3. The preview is rendered with the same renderer as the full-resolution save
   (``apply_gallery_adjustments``), on that bounded copy, so it is a faithful
   small version of what a save produces (the tests check the two agree).
4. It is stored under its own id, never over an earlier one: its file is
   written first and its row is added under a lock on the result, and if the
   row cannot be added the file is removed. A late answer to an older revision
   is stored and reported as not latest; it never replaces a newer preview.

A client closing the connection does not cancel anything: the render finishes
and is stored, and repeating the same revision with the same settings returns
that stored preview instead of rendering again.
"""
import hashlib
import json
from datetime import timedelta

import cv2
import numpy as np
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

from . import policy as rendering_policy
from .controls import RENDERER_VERSION
from .editor_source import (
    attach, decode_with_alpha, read_enhanced_with_alpha, resize_to_edge, restore_hidden, source_version,
    visible_for_measurement,
)
from .models import EditPreview, EditPreviewSource, ImageEnhancementResult


class StaleSource(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "stale_source"

    def __init__(self, current):
        super().__init__("The image changed since this preview was requested.")
        self.detail = {"detail": self.detail, "code": "stale_source", "source_version": current}


class RevisionConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "revision_conflict"

    def __init__(self, revision):
        super().__init__("This revision was already used with different settings; use a higher revision.")
        self.detail = {"detail": self.detail, "code": "revision_conflict", "revision": revision}


def clamp_edge(requested):
    edge = int(requested or settings.ENHANCEMENT_PREVIEW_DEFAULT_EDGE)
    if not 128 <= edge <= settings.ENHANCEMENT_PREVIEW_MAX_EDGE:
        raise ValidationError({"size": f"Choose a size between 128 and {settings.ENHANCEMENT_PREVIEW_MAX_EDGE}."})
    return edge


def settings_hash(mode, adjustments, edge, version):
    basis = json.dumps(
        {"mode": mode, "adjustments": adjustments, "edge": edge, "source": version,
         "renderer": RENDERER_VERSION, "policy": rendering_policy.active_policy()},
        sort_keys=True, default=float,
    )
    return hashlib.sha256(basis.encode()).hexdigest()


def _png(image):
    return cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 3])[1].tobytes()


def _read(file_field):
    file_field.open("rb")
    try:
        return file_field.read()
    finally:
        file_field.close()


def _source_edge():
    return settings.ENHANCEMENT_PREVIEW_MAX_EDGE


def ensure_source(result, version):
    """The bounded cached copy of this version of the enhanced image."""
    existing = EditPreviewSource.objects.filter(enhancement_result=result, source_version=version).first()
    if existing is not None:
        return existing
    bgr, alpha = read_enhanced_with_alpha(result)
    full_height, full_width = bgr.shape[:2]
    bgr, alpha = resize_to_edge(bgr, alpha, _source_edge())
    source = EditPreviewSource(
        enhancement_result=result, source_version=version, width=bgr.shape[1], height=bgr.shape[0],
        has_alpha=alpha is not None, full_width=full_width, full_height=full_height,
    )
    source.file.save("source.png", ContentFile(_png(attach(bgr, alpha))), save=False)
    try:
        with transaction.atomic():
            source.save()
    except IntegrityError:  # another request made it first
        source.file.storage.delete(source.file.name)
        return EditPreviewSource.objects.get(enhancement_result=result, source_version=version)
    return source


def _baseline(source, result):
    """The unenhanced photo at the source's size, for the intensity blend; cached on first use."""
    if source.baseline_file:
        return decode_with_alpha(_read(source.baseline_file))[0]
    from .services import _read_preprocessed_baseline

    full = _read_preprocessed_baseline(result.preprocessing_result)
    if full is None:
        return None
    baseline = cv2.resize(full, (source.width, source.height), interpolation=cv2.INTER_AREA)
    source.baseline_file.save("baseline.png", ContentFile(_png(baseline)), save=False)
    EditPreviewSource.objects.filter(pk=source.pk, baseline_file="").update(baseline_file=source.baseline_file.name)
    return baseline


def _encode(image, has_alpha):
    if has_alpha:
        return _png(image), "png"
    params = [cv2.IMWRITE_JPEG_QUALITY, settings.ENHANCEMENT_PREVIEW_JPEG_QUALITY]
    if hasattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR") and hasattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR_444"):
        params += [cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444]
    ok, encoded = cv2.imencode(".jpg", image, params)
    if not ok:
        raise ValidationError("The preview could not be encoded.")
    return encoded.tobytes(), "jpeg"


def _latest_revision(owner, result):
    return EditPreview.objects.filter(owner=owner, enhancement_result=result).order_by("-revision").values_list(
        "revision", flat=True).first()


def create_preview(result, owner, *, revision, mode, adjustments, size=None, claimed_version=""):
    """``(preview, created)``. Raises StaleSource or RevisionConflict (409), ValidationError (400)."""
    from .services import apply_gallery_adjustments

    version = source_version(result)
    if claimed_version and claimed_version != version:
        raise StaleSource(version)
    edge = clamp_edge(size)
    digest = settings_hash(mode, adjustments, edge, version)

    existing = EditPreview.objects.filter(owner=owner, enhancement_result=result, revision=revision).first()
    if existing is not None:
        if existing.settings_hash == digest:
            return existing, False
        raise RevisionConflict(revision)

    source = ensure_source(result, version)
    bgr, alpha = decode_with_alpha(_read(source.file))
    bgr, alpha = resize_to_edge(bgr, alpha, edge)
    wants_blend = mode == "manual" and float(adjustments.get("intensity", 100.0)) < 100.0
    baseline = None
    if wants_blend:
        baseline = _baseline(source, result)
        if baseline is not None and baseline.shape[:2] != bgr.shape[:2]:
            baseline = cv2.resize(baseline, (bgr.shape[1], bgr.shape[0]), interpolation=cv2.INTER_AREA)
    rendered, metadata = apply_gallery_adjustments(visible_for_measurement(bgr, alpha), mode, adjustments, baseline=baseline)
    rendered = restore_hidden(rendered, bgr, alpha)
    image = attach(rendered, alpha)
    payload, output_format = _encode(image, alpha is not None)
    metadata["preview"] = {
        "edge": edge, "source_edge": max(source.width, source.height), "source_version": version,
        "full_resolution": [source.full_width, source.full_height],
    }

    preview = EditPreview(
        owner=owner, enhancement_result=result, source=source, revision=revision, settings_hash=digest, mode=mode,
        requested_adjustments=adjustments, applied_adjustments=metadata["effective"], render_metadata=metadata,
        output_format=output_format, width=int(rendered.shape[1]), height=int(rendered.shape[0]),
        file_size=len(payload), expires_at=timezone.now() + timedelta(hours=settings.ENHANCEMENT_PREVIEW_RETENTION_HOURS),
    )
    preview.file.save(f"preview.{'png' if output_format == 'png' else 'jpg'}", ContentFile(payload), save=False)
    try:
        with transaction.atomic():
            # Serialise writers of one result's previews; the render above did not hold the lock.
            ImageEnhancementResult.objects.select_for_update().only("pk").get(pk=result.pk)
            raced = EditPreview.objects.filter(owner=owner, enhancement_result=result, revision=revision).first()
            if raced is not None:
                preview.file.storage.delete(preview.file.name)
                if raced.settings_hash == digest:
                    return raced, False
                raise RevisionConflict(revision)
            preview.save()
            _trim(owner, result)
    except Exception:
        if preview.pk is None:
            preview.file.storage.delete(preview.file.name)
        raise
    return preview, True


def _trim(owner, result):
    """Keep only the newest previews of one result."""
    keep = settings.ENHANCEMENT_PREVIEW_KEEP_PER_RESULT
    stale = list(
        EditPreview.objects.filter(owner=owner, enhancement_result=result).order_by("-revision")
        .values_list("pk", flat=True)[keep:]
    )
    if stale:
        for preview in EditPreview.objects.filter(pk__in=stale):
            preview.delete()  # post_delete removes its file


def describe(preview):
    """The preview's place among its siblings."""
    latest = _latest_revision(preview.owner, preview.enhancement_result)
    return {"latest_revision": latest, "is_latest": preview.revision == latest}


def cleanup_expired(now=None):
    """Delete expired previews and cached sources nothing refers to. Independent of originals and saved edits."""
    now = now or timezone.now()
    previews = 0
    for preview in EditPreview.objects.filter(expires_at__lte=now):
        preview.delete()
        previews += 1
    horizon = now - timedelta(hours=settings.ENHANCEMENT_PREVIEW_RETENTION_HOURS)
    sources = 0
    for source in EditPreviewSource.objects.filter(created_at__lte=horizon, previews__isnull=True):
        source.delete()
        sources += 1
    return {"previews": previews, "sources": sources}
