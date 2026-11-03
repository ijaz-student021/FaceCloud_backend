"""Person folders are made after the quality verdict, and after the photograph's automatic enhancement.

A photograph used to be filed into a person folder as soon as it was preprocessed, before the quality check had said whether the photograph is usable. A
photo the quality check then rejected had already opened a folder, moved its average face and changed its cover and count, and was taken out again a moment
later. Now the identity analysis is queued from where the quality verdict is saved (``quality_control.services.process_quality_decision``), by
``group_after_verdict``:

| Quality verdict | Identity analysis |
|---|---|
| ``accepted`` | now |
| ``rejected`` | never |
| ``needs_review``, the photo is in a personal workspace (or in none): no moderator can ever decide | now |
| ``needs_review``, the photo was submitted to an organization | after a moderator approves it (``moderation.services.decide_review``, action ``approve``) |

Since 2026-11-03 the order is: quality verdict, then the automatic enhancement, then the person folders. ``group_when_enhancement_ends`` is the one entry point: it queues the analysis at once
when no automatic enhancement is coming (it is switched off, the verdict does not allow it) and otherwise when that enhancement reaches an end state (completed, failed, cancelled, skipped), called from
``enhancement.auto`` wherever one ends. The analysis still embeds ``preprocessing.processed_file``, the standardised ORIGINAL, never an enhanced file, so an enhancement can never change who a face is
grouped with; only the time at which the analysis starts moved. A photograph never waits longer than ``GROUPING_MAX_WAIT_FOR_ENHANCEMENT_SECONDS`` for its enhancement.

A photograph whose face check was rejected, or that was refused at upload, is never grouped either. The manual analysis
(``POST /api/identities/images/{id}/``) is unchanged, and so is the safety net that takes a rejected photo out of its folders (``identities.signals``).
"""
import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import ValidationError

from quality_control.models import ImageQualityDecision
from uploads.models import ImageUpload
from validation.models import FaceValidationResult

from .models import IdentityAnalysis

logger = logging.getLogger(__name__)

ACCEPTED = ImageQualityDecision.Decision.ACCEPTED
REJECTED = ImageQualityDecision.Decision.REJECTED
NEEDS_REVIEW = ImageQualityDecision.Decision.NEEDS_REVIEW

# Why a photograph is (not yet) grouped.
GROUP_NOW = "accepted"
GROUP_NOW_PERSONAL = "personal_workspace"
NEVER = "rejected"
WAIT_FOR_VERDICT = "quality_verdict"
WAIT_FOR_MODERATOR = "moderator_approval"
WAIT_FOR_ENHANCEMENT = "enhancement"

QUALITY_IN_PROGRESS = (ImageQualityDecision.Status.PENDING, ImageQualityDecision.Status.QUEUED, ImageQualityDecision.Status.PROCESSING)
IN_PROGRESS = (IdentityAnalysis.Status.PENDING, IdentityAnalysis.Status.QUEUED, IdentityAnalysis.Status.PROCESSING)
STATUS_WORDS = {
    IdentityAnalysis.Status.PENDING: "queued", IdentityAnalysis.Status.QUEUED: "queued", IdentityAnalysis.Status.PROCESSING: "processing",
    IdentityAnalysis.Status.COMPLETED: "completed", IdentityAnalysis.Status.NEEDS_REVIEW: "needs_review",
    IdentityAnalysis.Status.FAILED: "failed", IdentityAnalysis.Status.CANCELLED: "cancelled",
}


def no_moderator_can_decide(image):
    """Whether the photograph is in a personal workspace (or in no organization): nobody reviews it, so a ``needs_review`` verdict is final."""
    organization = image.batch.organization
    return organization is None or organization.is_personal


def verdict_allows_grouping(image, verdict=None):
    """``(allowed, why)``: whether ``image`` may be grouped into person folders now, and the reason (see the module table).

    ``verdict`` is the quality decision (``accepted`` / ``rejected`` / ``needs_review``) when the caller has just decided it; otherwise the saved one is read.
    ``why`` is ``quality_verdict`` or ``moderator_approval`` when the photograph is waiting for one of those, ``rejected`` when it never will be grouped.
    """
    if image.status == "rejected":
        return False, NEVER
    validation = getattr(image, "face_validation", None)
    if validation is not None and validation.decision == FaceValidationResult.Decision.REJECTED:
        return False, NEVER
    if verdict is None:
        quality = getattr(image, "quality_decision", None)
        if quality is None or quality.status != ImageQualityDecision.Status.COMPLETED:
            return False, WAIT_FOR_VERDICT
        verdict = quality.decision
    if verdict == ACCEPTED:
        return True, GROUP_NOW
    if verdict == REJECTED:
        return False, NEVER
    if verdict == NEEDS_REVIEW:
        return (True, GROUP_NOW_PERSONAL) if no_moderator_can_decide(image) else (False, WAIT_FOR_MODERATOR)
    return False, WAIT_FOR_VERDICT


def group_after_verdict(image, verdict=None):
    """Queue the identity analysis of ``image`` if its quality verdict allows it; returns the analysis, or ``None`` when it did not (or could not).

    Never raises: grouping is best effort and must not undo the verdict or the moderator's decision that called it (it runs in a savepoint). An analysis
    that already ran is left as it is, so calling this twice, or after an approval that follows an earlier grouping, changes nothing.
    """
    from .services import enqueue_identity_analysis

    if not settings.IDENTITY_AUTO_ANALYZE:
        return None
    try:
        with transaction.atomic():
            allowed, _why = verdict_allows_grouping(image, verdict)
            return enqueue_identity_analysis(image) if allowed else None
    except ValidationError as error:
        # Not eligible (the face check did not accept it, no consent, no usable face): an expected outcome, not a failure of the verdict.
        logger.info("Image %s is not grouped into a person folder: %s", image.pk, error.detail)
    except Exception:
        logger.exception("Could not queue the identity analysis of image_id=%s.", image.pk)
    return None


def group_when_enhancement_ends(image, verdict=None):
    """Queue the identity analysis of ``image`` unless its automatic enhancement is still to come or running (its end calls this again); returns the analysis or ``None``.

    Idempotent and never raises (``group_after_verdict`` is a best-effort savepoint). A person's OWN enhancement request neither delays nor triggers grouping: only the photograph's
    ``AutoEnhancement`` record does.
    """
    from enhancement.auto import enhancement_pending

    try:
        if enhancement_pending(image):
            return None
    except Exception:
        logger.exception("Could not tell whether the automatic enhancement of image_id=%s is pending; grouping it now.", image.pk)
    return group_after_verdict(image, verdict=verdict)


def grouping_expected_soon(image, seconds=120):
    """Whether ``image`` is about to be grouped: its automatic enhancement ended in the last ``seconds`` seconds, its verdict allows grouping, a face was found and the owner consented, but
    the analysis is not queued yet. Used only so that the progress never shows a finished photograph in the instant between the two stages."""
    from datetime import timedelta

    from django.utils import timezone

    from enhancement.models import AutoEnhancement
    from privacy.services import biometric_processing_allowed

    if not settings.IDENTITY_AUTO_ANALYZE or IdentityAnalysis.objects.filter(image_id=image.pk).exists():
        return False
    record = AutoEnhancement.objects.filter(image_id=image.pk).first()
    if record is None or record.state == AutoEnhancement.State.WAITING or timezone.now() - record.updated_at > timedelta(seconds=seconds):
        return False
    validation = getattr(image, "face_validation", None)
    if validation is None or not (validation.face_count or 0) or validation.decision == FaceValidationResult.Decision.REJECTED:
        return False
    if not verdict_allows_grouping(image)[0]:
        return False
    return bool(biometric_processing_allowed(image.owner))


def grouping_status(image, *, personal):
    """How far this photograph is in being grouped, for the batch progress: ``{"status": ..., "waiting_for": ...}``.

    ``status`` is ``queued`` / ``processing`` / ``completed`` / ``needs_review`` / ``failed`` / ``cancelled`` (the analysis), ``skipped`` (rejected: it is never
    grouped) or ``not_applicable`` (grouping is off, or this photograph will not be grouped). A photograph still waiting is ``queued`` and says what for in
    ``waiting_for``: ``quality_verdict``, ``moderator_approval`` or ``enhancement`` (its automatic enhancement has not ended); otherwise it is empty.
    """
    if image.status == "rejected":
        return {"status": "skipped", "waiting_for": ""}
    validation = getattr(image, "face_validation", None)
    quality = getattr(image, "quality_decision", None)
    rejected = (validation is not None and validation.decision == FaceValidationResult.Decision.REJECTED) or (
        quality is not None and quality.decision == REJECTED
    )
    if rejected:
        return {"status": "skipped", "waiting_for": ""}
    analysis = getattr(image, "identity_analysis", None)
    if analysis is not None:
        return {"status": STATUS_WORDS.get(analysis.status, analysis.status), "waiting_for": ""}
    if not settings.IDENTITY_AUTO_ANALYZE:
        return {"status": "not_applicable", "waiting_for": ""}
    if quality is None or quality.status != ImageQualityDecision.Status.COMPLETED:
        return {"status": "queued", "waiting_for": WAIT_FOR_VERDICT}
    if quality.decision == NEEDS_REVIEW and not personal:
        return {"status": "queued", "waiting_for": WAIT_FOR_MODERATOR}
    from enhancement.auto import enhancement_pending

    if enhancement_pending(image):
        return {"status": "queued", "waiting_for": WAIT_FOR_ENHANCEMENT}
    return {"status": "not_applicable", "waiting_for": ""}


def batch_grouping_finished(batch):
    """Whether every photograph of ``batch`` that will be grouped has been: some analysis ran, none is still running, none is still waiting for its verdict.

    A photograph waiting for a *moderator* does not hold the batch back (like ``needs_review`` in the batch status, it can take days), and neither does one
    that was rejected, whose checks failed or that was never preprocessed successfully.
    """
    if not IdentityAnalysis.objects.filter(image__batch=batch).exists():
        return False
    if IdentityAnalysis.objects.filter(image__batch=batch, status__in=IN_PROGRESS).exists():
        return False
    from enhancement.auto import enhancement_pending

    if any(enhancement_pending(image) for image in ImageUpload.objects.filter(batch=batch, status="uploaded", identity_analysis__isnull=True, auto_enhancement__isnull=False)):
        return False        # a photograph still waiting for its enhancement is not grouped yet
    waiting = (
        ImageUpload.objects.filter(batch=batch, status="uploaded")
        .exclude(face_validation__decision=FaceValidationResult.Decision.REJECTED)
        .exclude(preprocessing_result__status="failed")
        .filter(Q(quality_decision__isnull=True) | Q(quality_decision__status__in=QUALITY_IN_PROGRESS))
    )
    return bool(batch.cancellation_requested) or not waiting.exists()
