import logging
import time
import uuid

import cv2
import numpy as np
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from uploads.models import ImagePreprocessingResult, ImageUpload
from validation.models import FaceValidationResult

from .models import DuplicateAnalysis, DuplicateMatch
from .providers import get_face_fingerprint_provider


HASH_BITS = 64
logger = logging.getLogger(__name__)


def perceptual_hash(image):
    if image is None or image.size == 0:
        raise ValidationError("The image cannot be fingerprinted safely.")
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    low_frequency = cv2.dct(resized)[:8, :8]
    median = float(np.median(low_frequency.flatten()[1:]))
    bits = low_frequency.flatten() > median
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def hamming_distance(first_hash, second_hash):
    try:
        return (int(first_hash, 16) ^ int(second_hash, 16)).bit_count()
    except (TypeError, ValueError) as error:
        raise ValidationError("A stored image fingerprint is invalid.") from error


def similarity_from_distance(distance):
    return round(max(0.0, (HASH_BITS - distance) / HASH_BITS * 100), 2)


def _read_standardized(preprocessing):
    if preprocessing.status != "completed" or not preprocessing.processed_file:
        raise ValidationError("Preprocessing must complete before duplicate analysis.")
    preprocessing.processed_file.open("rb")
    try:
        payload = preprocessing.processed_file.read()
    finally:
        preprocessing.processed_file.close()
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValidationError("The standardized image cannot be decoded safely.")
    return image


def _face_fingerprints(image, image_upload):
    try:
        validation = image_upload.face_validation
    except FaceValidationResult.DoesNotExist:
        return "perceptual_face_region_v1", []
    provider = get_face_fingerprint_provider(perceptual_hash)
    return provider.name, provider.fingerprints(image, validation.faces)


def _best_face_match(source_fingerprints, candidate_fingerprints):
    best = None
    for source in source_fingerprints:
        for candidate in candidate_fingerprints:
            try:
                distance = hamming_distance(
                    source["fingerprint"], candidate["fingerprint"]
                )
            except (KeyError, TypeError, ValidationError):
                continue
            if best is None or distance < best["distance"]:
                best = {
                    "distance": distance,
                    "source_face_index": source.get("face_index"),
                    "candidate_face_index": candidate.get("face_index"),
                }
    return best


def _stored_pair(owner_id, first_image_id, second_image_id, match_type):
    """The stored match for these two photographs and this kind of match, whichever of them is stored as the source.

    A pair is the same pair seen from either photograph: the check of the older photograph and the check of the newer one find it as (newer, older) or (older,
    newer) depending on which photograph had a finished check at the time, and the person who decided it does not know or care which one is the "source".
    """
    return (
        DuplicateMatch.objects.filter(owner_id=owner_id, match_type=match_type)
        .filter(
            Q(source_image_id=first_image_id, matched_image_id=second_image_id)
            | Q(source_image_id=second_image_id, matched_image_id=first_image_id)
        )
        .order_by("pk")
        .first()
    )


def _create_match(
    *, analysis, candidate, match_type, similarity_score,
    hash_distance=None, face_similarity=None, evidence=None, confirmed=False,
):
    """Record that ``analysis.image`` and ``candidate`` match, and return the stored row.

    A pair that is already stored keeps its row: its id and its ``review_status``, ``reviewed_by`` and ``reviewed_at`` are somebody's decision, and finding the
    pair again must not undo it. Only what the engine measures (score, distances, evidence) is refreshed, and only when it changed. A new pair is created
    ``suspected``, or ``confirmed`` for an exact checksum match.
    """
    measured = {
        "similarity_score": similarity_score,
        "hash_distance": hash_distance,
        "face_similarity": face_similarity,
        "evidence": evidence or {},
    }
    match = _stored_pair(analysis.owner_id, analysis.image_id, candidate.pk, match_type)
    if match is None:
        target_analysis = analysis
        source_image = analysis.image
        matched_image = candidate
        if candidate.pk > analysis.image_id:
            candidate_analysis = DuplicateAnalysis.objects.filter(
                owner_id=analysis.owner_id,
                image=candidate,
                status=DuplicateAnalysis.Status.COMPLETED,
            ).first()
            if candidate_analysis is not None:
                target_analysis = candidate_analysis
                source_image = candidate
                matched_image = analysis.image
        match = DuplicateMatch.objects.create(
            owner_id=analysis.owner_id,
            analysis=target_analysis,
            source_image=source_image,
            matched_image=matched_image,
            match_type=match_type,
            review_status=(
                DuplicateMatch.ReviewStatus.CONFIRMED
                if confirmed else DuplicateMatch.ReviewStatus.SUSPECTED
            ),
            **measured,
        )
    else:
        changed = []
        for name, value in measured.items():
            if getattr(match, name) != value:
                setattr(match, name, value)
                changed.append(name)
        if confirmed and match.review_status != DuplicateMatch.ReviewStatus.CONFIRMED:
            # An exact checksum match is always confirmed (it cannot be dismissed), so a stored exact pair that says otherwise is put right.
            match.review_status = DuplicateMatch.ReviewStatus.CONFIRMED
            match.reviewed_by = None
            match.reviewed_at = None
            changed += ["review_status", "reviewed_by", "reviewed_at"]
        if changed:
            # Only these columns are written, so a decision made at the same moment cannot be overwritten with the value read a moment ago.
            match.save(update_fields=[*changed, "updated_at"])
    if match.review_status == DuplicateMatch.ReviewStatus.SUSPECTED:
        # A pair somebody already decided does not go back to the moderators' queue.
        from moderation.services import create_review_for_duplicate_match
        create_review_for_duplicate_match(match)
    return match


def _remove_pairs_no_longer_found(analysis, *, found_match_ids, looked_at):
    """Delete the stored pairs of this photograph that the check has just stopped finding; return the other photographs of the pairs it deleted.

    A pair is deleted only when the check really looked for it: its other photograph was one of the photographs this check compared with (``looked_at``, per kind
    of match), and the pair was not found. A pair is therefore left alone when the other photograph's own check is not finished (it is being checked again at the same
    moment, or its check failed), or when it fell outside the ``DUPLICATE_MAX_CANDIDATES`` window: the check cannot say that such a pair is gone, and deleting
    it would throw away a decision for nothing. A pair with a photograph in the trash is never deleted either: restoring the photograph brings the pair back as it was.
    """
    stale = (
        DuplicateMatch.objects.filter(owner_id=analysis.owner_id)
        .filter(Q(source_image_id=analysis.image_id) | Q(matched_image_id=analysis.image_id))
        .exclude(pk__in=found_match_ids)
        .filter(source_image__trashed_at__isnull=True, matched_image__trashed_at__isnull=True)
        .order_by()
        .values_list("pk", "source_image_id", "matched_image_id", "match_type")
    )
    gone_ids = []
    other_image_ids = set()
    for match_id, source_image_id, matched_image_id, match_type in stale:
        other_image_id = matched_image_id if source_image_id == analysis.image_id else source_image_id
        if other_image_id in looked_at.get(match_type, ()):
            gone_ids.append(match_id)
            other_image_ids.add(other_image_id)
    if gone_ids:
        DuplicateMatch.objects.filter(pk__in=gone_ids).delete()
    return other_image_ids


def _refresh_analysis_summary(image_id):
    analysis = DuplicateAnalysis.objects.filter(image_id=image_id).first()
    if analysis is None or analysis.status != DuplicateAnalysis.Status.COMPLETED:
        return
    active_matches = DuplicateMatch.objects.filter(
        owner=analysis.owner,
    ).filter(
        Q(source_image_id=image_id) | Q(matched_image_id=image_id)
    ).exclude(review_status=DuplicateMatch.ReviewStatus.NOT_DUPLICATE)
    counterpart_ids = set()
    statuses = set()
    for match in active_matches.only(
        "source_image_id", "matched_image_id", "review_status"
    ):
        counterpart_ids.add(
            match.matched_image_id
            if match.source_image_id == image_id
            else match.source_image_id
        )
        statuses.add(match.review_status)
    if DuplicateMatch.ReviewStatus.CONFIRMED in statuses:
        decision = DuplicateAnalysis.Decision.DUPLICATE
    elif DuplicateMatch.ReviewStatus.SUSPECTED in statuses:
        decision = DuplicateAnalysis.Decision.NEEDS_REVIEW
    else:
        decision = DuplicateAnalysis.Decision.NO_DUPLICATE
    analysis.decision = decision
    analysis.match_count = len(counterpart_ids)
    analysis.save(update_fields=["decision", "match_count", "updated_at"])


def process_duplicate_analysis(analysis_id):
    with transaction.atomic():
        analysis = (
            DuplicateAnalysis.objects.select_for_update(of=("self",))
            .select_related(
                "image__batch", "image__owner", "image__face_validation",
                "preprocessing_result",
            )
            .get(pk=analysis_id)
        )
        if analysis.status == DuplicateAnalysis.Status.COMPLETED:
            return analysis
        if analysis.image.batch.cancellation_requested:
            analysis.status = DuplicateAnalysis.Status.CANCELLED
            analysis.finished_at = timezone.now()
            analysis.save(update_fields=["status", "finished_at", "updated_at"])
            return analysis
        analysis.status = DuplicateAnalysis.Status.PROCESSING
        analysis.started_at = analysis.started_at or timezone.now()
        analysis.finished_at = None
        analysis.failure_reason = ""
        analysis.save(
            update_fields=[
                "status", "started_at", "finished_at", "failure_reason", "updated_at",
            ]
        )

    started = time.perf_counter()
    try:
        image = _read_standardized(analysis.preprocessing_result)
        image_hash = perceptual_hash(image)
        provider_name, face_fingerprints = _face_fingerprints(image, analysis.image)
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=analysis.owner_id)
            locked = DuplicateAnalysis.objects.select_for_update().get(pk=analysis.pk)
            exact_candidates = list(
                ImageUpload.objects.filter(
                    owner=locked.owner,
                    status="uploaded",
                    checksum_sha256=locked.image.checksum_sha256,
                )
                .exclude(checksum_sha256="")
                .exclude(pk=locked.image_id)
                .order_by("-pk")[: settings.DUPLICATE_MAX_CANDIDATES]
            )
            exact_candidate_ids = {candidate.pk for candidate in exact_candidates}
            previous_analyses = list(
                DuplicateAnalysis.objects.filter(
                    owner=locked.owner,
                    status=DuplicateAnalysis.Status.COMPLETED,
                )
                .exclude(image_id=locked.image_id)
                .exclude(perceptual_hash="")
                .select_related("image")
                .order_by("-image_id")[: settings.DUPLICATE_MAX_CANDIDATES]
            )
            # The stored pairs of this photograph are reconciled with what this check finds, not wiped and rebuilt: a pair found again keeps its row and so the
            # decision somebody made about it (_create_match), and only the pairs that are no longer found are deleted afterwards.
            matched_image_ids = set()
            found_match_ids = set()
            for candidate in exact_candidates:
                match = _create_match(
                    analysis=locked,
                    candidate=candidate,
                    match_type=DuplicateMatch.MatchType.EXACT,
                    similarity_score=100.0,
                    hash_distance=0,
                    evidence={"method": "sha256_exact_v1"},
                    confirmed=True,
                )
                found_match_ids.add(match.pk)
                matched_image_ids.add(candidate.pk)

            for candidate_analysis in previous_analyses:
                candidate = candidate_analysis.image
                if candidate.pk in exact_candidate_ids:
                    continue
                distance = hamming_distance(image_hash, candidate_analysis.perceptual_hash)
                if distance <= settings.DUPLICATE_PERCEPTUAL_HASH_DISTANCE:
                    match = _create_match(
                        analysis=locked,
                        candidate=candidate,
                        match_type=DuplicateMatch.MatchType.PERCEPTUAL,
                        similarity_score=similarity_from_distance(distance),
                        hash_distance=distance,
                        evidence={"method": "phash_dct_64_v1"},
                    )
                    found_match_ids.add(match.pk)
                    matched_image_ids.add(candidate.pk)

                face_match = _best_face_match(
                    face_fingerprints, candidate_analysis.face_fingerprints
                )
                if (
                    face_match is not None
                    and face_match["distance"] <= settings.DUPLICATE_FACE_HASH_DISTANCE
                ):
                    face_similarity = similarity_from_distance(face_match["distance"])
                    match = _create_match(
                        analysis=locked,
                        candidate=candidate,
                        match_type=DuplicateMatch.MatchType.FACE_REGION,
                        similarity_score=face_similarity,
                        hash_distance=face_match["distance"],
                        face_similarity=face_similarity,
                        evidence={
                            "method": provider_name,
                            "source_face_index": face_match["source_face_index"],
                            "candidate_face_index": face_match["candidate_face_index"],
                        },
                    )
                    found_match_ids.add(match.pk)
                    matched_image_ids.add(candidate.pk)

            visual_image_ids = {candidate_analysis.image_id for candidate_analysis in previous_analyses}
            removed_image_ids = _remove_pairs_no_longer_found(
                locked,
                found_match_ids=found_match_ids,
                looked_at={
                    DuplicateMatch.MatchType.EXACT: exact_candidate_ids,
                    DuplicateMatch.MatchType.PERCEPTUAL: visual_image_ids,
                    DuplicateMatch.MatchType.FACE_REGION: visual_image_ids,
                },
            )

            locked.status = DuplicateAnalysis.Status.COMPLETED
            locked.decision = DuplicateAnalysis.Decision.NO_DUPLICATE
            locked.perceptual_hash = image_hash
            locked.face_fingerprints = face_fingerprints
            locked.fingerprint_provider = provider_name
            locked.match_count = len(matched_image_ids)
            locked.failure_reason = ""
            locked.processing_duration_ms = max(
                1, round((time.perf_counter() - started) * 1000)
            )
            locked.finished_at = timezone.now()
            locked.save()
            # The photographs of the pairs that were deleted need their summary recounted too, or they would keep reporting a match that is gone.
            affected_image_ids = matched_image_ids | removed_image_ids | {locked.image_id}
            for affected_image_id in affected_image_ids:
                _refresh_analysis_summary(affected_image_id)
        return DuplicateAnalysis.objects.get(pk=analysis.pk)
    except Exception:
        logger.exception(
            "Duplicate analysis failed safely for analysis_id=%s.",
            analysis.pk,
        )
        DuplicateAnalysis.objects.filter(pk=analysis.pk).update(
            status=DuplicateAnalysis.Status.FAILED,
            decision="",
            failure_reason="Duplicate analysis could not be completed safely.",
            processing_duration_ms=max(1, round((time.perf_counter() - started) * 1000)),
            finished_at=timezone.now(),
        )
        return DuplicateAnalysis.objects.get(pk=analysis.pk)


def create_or_reset_duplicate_analysis(image, *, force=False):
    try:
        preprocessing = image.preprocessing_result
    except ImagePreprocessingResult.DoesNotExist as error:
        raise ValidationError(
            "Preprocessing must complete before duplicate analysis."
        ) from error
    if preprocessing.status != "completed" or not preprocessing.processed_file:
        raise ValidationError("Preprocessing must complete before duplicate analysis.")
    analysis, created = DuplicateAnalysis.objects.get_or_create(
        image=image,
        defaults={
            "owner": image.owner,
            "preprocessing_result": preprocessing,
        },
    )
    if analysis.owner_id != image.owner_id:
        raise ValidationError("The duplicate-analysis owner does not match the image owner.")
    if not created and analysis.status == DuplicateAnalysis.Status.COMPLETED and not force:
        return analysis, False
    analysis.owner = image.owner
    analysis.preprocessing_result = preprocessing
    analysis.status = DuplicateAnalysis.Status.PENDING
    analysis.decision = ""
    analysis.match_count = 0
    analysis.task_id = ""
    analysis.failure_reason = ""
    analysis.started_at = None
    analysis.finished_at = None
    analysis.save()
    # A forced check does not delete the photograph's matches: it is run again from scratch, and process_duplicate_analysis reconciles the stored pairs with
    # what it finds, keeping each pair that is found again together with the decision somebody made about it. Deleting them here, before the check runs,
    # threw every confirm and dismiss away even for a pair the check finds again.
    return analysis, True


def enqueue_duplicate_analysis(image, *, force=False):
    from .tasks import analyze_image_duplicates_task

    analysis, should_queue = create_or_reset_duplicate_analysis(image, force=force)
    if not should_queue:
        return analysis
    task_id = uuid.uuid4().hex
    analysis.status = DuplicateAnalysis.Status.QUEUED
    analysis.task_id = task_id
    analysis.save(update_fields=["status", "task_id", "updated_at"])

    def dispatch():
        try:
            analyze_image_duplicates_task.apply_async(
                args=[analysis.pk],
                task_id=task_id,
            )
        except Exception:
            logger.exception(
                "Could not queue duplicate analysis_id=%s.",
                analysis.pk,
            )
            DuplicateAnalysis.objects.filter(pk=analysis.pk).update(
                status=DuplicateAnalysis.Status.FAILED,
                failure_reason="The duplicate-analysis queue is unavailable.",
                finished_at=timezone.now(),
            )

    transaction.on_commit(dispatch)
    analysis.refresh_from_db()
    return analysis


@transaction.atomic
def review_duplicate_match(match, reviewer, review_status):
    match = DuplicateMatch.objects.select_for_update().select_related("analysis").get(
        pk=match.pk,
        owner=reviewer,
    )
    if match.match_type == DuplicateMatch.MatchType.EXACT:
        if review_status != DuplicateMatch.ReviewStatus.CONFIRMED:
            raise ValidationError("An exact checksum match cannot be dismissed.")
    match.review_status = review_status
    match.reviewed_by = reviewer
    match.reviewed_at = timezone.now()
    match.save(
        update_fields=["review_status", "reviewed_by", "reviewed_at", "updated_at"]
    )
    _refresh_analysis_summary(match.source_image_id)
    _refresh_analysis_summary(match.matched_image_id)
    return match
