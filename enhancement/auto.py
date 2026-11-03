"""Automatic enhancement after upload.

A photograph that passes the checks is enhanced without anyone asking. It is started from the same places that queue the person folders
(``quality_control.services.process_quality_decision``, a moderator's approval in ``moderation.services.decide_review``, and the repeat of an
already-decided photograph in ``uploads.services.preprocess_image``), so a photograph is considered exactly when its quality verdict is known:

| Quality verdict | Automatic enhancement |
|---|---|
| ``accepted`` | now |
| ``rejected`` (also an upload or face check that rejected it) | never (``enhancement.eligibility``) |
| ``needs_review``, the photo is in a personal workspace (or in none): no moderator can ever decide | now |
| ``needs_review``, the photo was submitted to an organization | after a moderator approves it |

It never makes a second one: a photograph has one ``AutoEnhancement`` record, and none is made when it already has a result of any kind (a person asked
first, or a retry or a re-analysis calls this again). It runs level ``auto`` and style ``natural`` under ``AUTO_ENHANCEMENT_RENDER_POLICY`` (the adaptive
engine by default: a clean photograph ends as ``original_preserved``, which is a successful outcome); a person's own request keeps
``ENHANCEMENT_RENDER_POLICY``. Every safety of the engine applies unchanged, and no model setting is touched.

* A person can switch it off: ``auto_enhance`` on the account (the notification-preferences endpoint) and ``auto_enhance`` on an upload; the answer is
  stored on the batch (``UploadBatch.auto_enhance``) when it is uploaded. ``AUTO_ENHANCEMENT_ENABLED`` is the operator's switch.
* The owner's limit of enhancements in progress (``ensure_enhancement_capacity``) is respected: a photograph over it is ``waiting`` and starts as slots
  free up, or is ``skipped`` (``capacity``) after ``AUTO_ENHANCEMENT_CAPACITY_WAIT_MINUTES``. Nothing here ever fails an upload.
* The owner gets one notification per batch ("N photos enhanced"), none per photograph.
* Person folders come AFTER the enhancement (identities/verdict.py ``group_when_enhancement_ends``): wherever an automatic enhancement ends (``after_result``, ``_skip``,
  ``_record_failure_to_start``) the photograph's identity analysis is queued, at once when no automatic enhancement is coming; ``enhancement_pending`` says whether one is.
"""
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from uploads.models import ImageUpload, UploadBatch

from .models import AutoEnhancement, EnhancementLevel, EnhancementStatus, EnhancementStyle, EnhancementTrigger, ImageEnhancementResult
from .outcome import result_outcome, result_summary

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (EnhancementStatus.PENDING, EnhancementStatus.QUEUED, EnhancementStatus.PROCESSING)
REJECTED = "rejected"

# Why a photograph is (not yet) enhanced automatically: ``verdict_allows_enhancement`` and the ``waiting_for`` of the progress payload.
NOW = "accepted"
NOW_PERSONAL = "personal_workspace"
NEVER = "rejected"
WAIT_FOR_VERDICT = "quality_verdict"
WAIT_FOR_MODERATOR = "moderator_approval"
WAIT_FOR_CAPACITY = "capacity"

# The words of a photograph's enhancement stage (``enhancement_state``).
QUEUED, PROCESSING, COMPLETED, SKIPPED, FAILED, NOT_APPLICABLE = "queued", "processing", "completed", "skipped", "failed", "not_applicable"

TRUE_WORDS = {"true", "1", "yes", "on"}
FALSE_WORDS = {"false", "0", "no", "off"}
QUALITY_IN_PROGRESS = ("pending", "queued", "processing")
ENHANCED_OUTCOMES = ("enhanced", "partially_enhanced")


def parse_auto_enhance(value):
    """The ``auto_enhance`` form field: ``None`` when absent or empty, else ``True`` / ``False``; ``ValueError`` for anything else."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    raise ValueError("auto_enhance must be true or false.")


def resolve_auto_enhance(user, requested=None):
    """Whether a new batch is enhanced automatically: the upload's own choice when it made one, else the account setting; never when the operator turned it off."""
    if not settings.AUTO_ENHANCEMENT_ENABLED:
        return False
    if requested is not None:
        return bool(requested)
    from notifications.services import get_notification_preferences

    return bool(get_notification_preferences(user).auto_enhance)


# -- when ----------------------------------------------------------------------------------------------------------------------------------------
def _in_personal_workspace(image):
    organization = image.batch.organization
    return organization is None or organization.is_personal


def verdict_allows_enhancement(image, verdict=None):
    """``(allowed, why)``: whether ``image`` may be enhanced automatically now, and the reason (see the module table).

    ``verdict`` is the quality decision when the caller has just decided it; otherwise the saved one is read. ``why`` is ``quality_verdict`` or
    ``moderator_approval`` when the photograph is waiting for one of those, ``rejected`` when it never will be enhanced automatically.
    """
    if image.status == REJECTED:
        return False, NEVER
    validation = getattr(image, "face_validation", None)
    if validation is not None and validation.decision == REJECTED:
        return False, NEVER
    if verdict is None:
        quality = getattr(image, "quality_decision", None)
        if quality is None or quality.status != "completed":
            return False, WAIT_FOR_VERDICT
        verdict = quality.decision
    if verdict == REJECTED and not settings.QUALITY_CHECKS_REJECT:
        verdict = "needs_review"             # a row from before quality checks stopped refusing photographs
    if verdict == "accepted":
        return True, NOW
    if verdict == REJECTED:
        return False, NEVER
    if verdict == "needs_review":
        return (True, NOW_PERSONAL) if _in_personal_workspace(image) else (False, WAIT_FOR_MODERATOR)
    return False, WAIT_FOR_VERDICT


def enhance_after_verdict(image, verdict=None):
    """Start the automatic enhancement of ``image`` if its batch asked for one and its quality verdict allows it. Returns the record, or ``None``.

    Never raises: this runs inside the transaction that saves a verdict (or a moderator's decision), which an enhancement problem must not undo. It is
    idempotent: a photograph that already has a record, or any enhancement result, is left alone.
    """
    batch_id = image.batch_id
    try:
        if not settings.AUTO_ENHANCEMENT_ENABLED:
            return None
        with transaction.atomic():
            # of=("self",): lock the photograph's own row only (PostgreSQL refuses FOR UPDATE on the nullable side of the organization join).
            locked = ImageUpload.objects.select_for_update(of=("self",)).select_related("batch__organization", "owner").get(pk=image.pk)
            if not locked.batch.auto_enhance or locked.batch.cancellation_requested:
                return None
            allowed, _why = verdict_allows_enhancement(locked, verdict)
            record = _start(locked) if allowed else None
    except Exception:
        logger.exception("Could not start the automatic enhancement of image_id=%s.", image.pk)
        _record_failure_to_start(image)
        record = None
    # A photograph that will not be enhanced can be the last thing a batch was waiting for: look once the verdict is committed.
    transaction.on_commit(lambda: notify_batch_if_finished(batch_id))
    return record


def _record_failure_to_start(image):
    """A photograph whose start failed unexpectedly is marked skipped, so it never holds its batch back."""
    try:
        with transaction.atomic():
            AutoEnhancement.objects.get_or_create(image_id=image.pk, defaults={
                "owner_id": image.owner_id, "batch_id": image.batch_id, "state": AutoEnhancement.State.SKIPPED, "skip_reason": "error",
            })
    except Exception:
        logger.exception("Could not record the failed automatic enhancement of image_id=%s.", image.pk)
    _group_now(image)


def _start(image):
    if AutoEnhancement.objects.filter(image=image).exists():
        return None  # already started, waiting or skipped: calling this again changes nothing
    if ImageEnhancementResult.objects.filter(image=image).exists():
        return None  # a result of any kind exists (a person asked first, or an earlier run): nothing to start
    record = AutoEnhancement.objects.create(
        image=image, owner=image.owner, batch=image.batch, state=AutoEnhancement.State.WAITING, waiting_since=timezone.now(),
    )
    _release(record)
    return record


# -- capacity ------------------------------------------------------------------------------------------------------------------------------------
STARTED_NOW, CAPACITY_FULL, SKIPPED_NOW, HELD = "started", "full", "skipped", "held"


def _skip(record, reason):
    record.state = AutoEnhancement.State.SKIPPED
    record.skip_reason = reason
    record.waiting_since = None
    record.save(update_fields=["state", "skip_reason", "waiting_since", "updated_at"])
    _group_now(record.image)          # it will not be enhanced: its person folders do not wait for it
    return SKIPPED_NOW


def _release(record):
    """Try to start a ``waiting`` record: ``started``, ``full`` (the owner is at the limit; it stays waiting), ``held`` (its quality verdict is being decided
    again; it stays waiting) or ``skipped`` (it will not be enhanced)."""
    from .services import TooManyActiveJobs, create_enhancement_result, ensure_enhancement_capacity

    image, batch = record.image, record.batch
    if batch.cancellation_requested:
        return _skip(record, "cancelled")
    allowed, why = verdict_allows_enhancement(image)
    if not allowed:
        return _skip(record, "not_possible") if why == NEVER else HELD
    if ImageEnhancementResult.objects.filter(image=image, status__in=ACTIVE_STATUSES).exists():
        return _skip(record, "already_requested")
    try:
        ensure_enhancement_capacity(record.owner, batch=False)
    except TooManyActiveJobs:
        waited = timezone.now() - (record.waiting_since or record.created_at)
        if waited >= timedelta(minutes=settings.AUTO_ENHANCEMENT_CAPACITY_WAIT_MINUTES):
            return _skip(record, "capacity")
        return CAPACITY_FULL
    try:
        with transaction.atomic():
            result = create_enhancement_result(
                image, EnhancementLevel.AUTO, requested_style=EnhancementStyle.NATURAL, trigger=EnhancementTrigger.AUTO,
            )
            task_id = uuid.uuid4().hex
            result.status = EnhancementStatus.QUEUED
            result.task_id = task_id
            result.save(update_fields=["status", "task_id", "updated_at"])
            record.state = AutoEnhancement.State.STARTED
            record.result = result
            record.waiting_since = None
            record.save(update_fields=["state", "result", "waiting_since", "updated_at"])
    except (ValidationError, IntegrityError):
        return _skip(record, "not_possible")
    transaction.on_commit(lambda: _dispatch(result.pk, task_id))
    return STARTED_NOW


def _dispatch(result_id, task_id):
    from .tasks import process_enhancement_image

    try:
        process_enhancement_image.apply_async(args=[result_id], task_id=task_id)
    except Exception:
        logger.exception("Automatic enhancement result %s could not be queued.", result_id)
        ImageEnhancementResult.objects.filter(pk=result_id, status=EnhancementStatus.QUEUED).update(
            status=EnhancementStatus.FAILED, failure_reason="The background enhancement queue is unavailable.", finished_at=timezone.now(),
        )
        result = ImageEnhancementResult.objects.filter(pk=result_id).select_related("image", "owner").first()
        if result is not None:
            after_result(result)


def enhancement_pending(image):
    """Whether an automatic enhancement of ``image`` is still to come or running, so that its person folders wait for it.

    Only the photograph's own ``AutoEnhancement`` record counts (a person's own request, ``POST /api/enhancements/images/{id}/``, neither delays nor triggers grouping), and only for
    ``GROUPING_MAX_WAIT_FOR_ENHANCEMENT_SECONDS`` from the moment that record was made: a lost task or an owner stuck at the limit never leaves a photograph ungrouped.
    """
    record = AutoEnhancement.objects.select_related("result").filter(image_id=image.pk).first()
    if record is None:
        return False
    if timezone.now() - record.created_at > timedelta(seconds=settings.GROUPING_MAX_WAIT_FOR_ENHANCEMENT_SECONDS):
        return False
    if record.state == AutoEnhancement.State.WAITING:
        return True
    return record.state == AutoEnhancement.State.STARTED and record.result is not None and record.result.status in ACTIVE_STATUSES


def _group_now(image):
    """An automatic enhancement of ``image`` ended (or none is coming): queue its person folders. Idempotent; never raises into the enhancement code."""
    try:
        from identities.verdict import group_when_enhancement_ends

        group_when_enhancement_ends(image)
    except Exception:
        logger.exception("Could not queue the person folders of image_id=%s after its enhancement.", image.pk)


def release_overdue_grouping(limit=200):
    """Group the photographs that waited the maximum for an enhancement that never ended (a worker died, an owner stuck at the limit); returns how many were tried.

    Run by the scheduler with ``release_waiting``. Cheap: only photographs with an automatic enhancement, a face and no identity analysis yet, whose enhancement is older than the maximum wait and
    younger than six hours, are looked at; ``group_when_enhancement_ends`` does nothing for one that is not eligible.
    """
    now = timezone.now()
    images = (
        ImageUpload.objects.filter(
            status="uploaded", identity_analysis__isnull=True, face_validation__face_count__gt=0,
            auto_enhancement__created_at__lte=now - timedelta(seconds=settings.GROUPING_MAX_WAIT_FOR_ENHANCEMENT_SECONDS),
            auto_enhancement__created_at__gte=now - timedelta(hours=6),
        ).order_by("pk")[:limit]
    )
    tried = 0
    for image in images:
        _group_now(image)
        tried += 1
    return tried


def release_waiting(owner=None):
    """Start the waiting photographs, oldest first, while their owners have free slots; returns how many were started.

    Called when an automatic enhancement ends (its slot is free) and every few minutes by the scheduler (``enhancement.release_waiting_auto``), so a photograph
    that waited is never forgotten. An owner at the limit is passed over, not waited for.
    """
    waiting = AutoEnhancement.objects.filter(state=AutoEnhancement.State.WAITING)
    if owner is not None:
        waiting = waiting.filter(owner=owner)
    started, full_owners, touched_batches = 0, set(), set()
    for pk, owner_id in list(waiting.order_by("waiting_since", "pk").values_list("pk", "owner_id")[:500]):
        if owner_id in full_owners:
            continue
        with transaction.atomic():
            record = (
                AutoEnhancement.objects.select_for_update(skip_locked=True, of=("self",)).select_related("image", "batch", "owner")
                .filter(pk=pk, state=AutoEnhancement.State.WAITING).first()
            )
            if record is None:
                continue
            outcome = _release(record)
        if outcome == STARTED_NOW:
            started += 1
        elif outcome == CAPACITY_FULL:
            full_owners.add(owner_id)
        elif outcome == SKIPPED_NOW:
            touched_batches.add(record.batch_id)
    if owner is None:
        # The scheduler's pass also looks at batches whose last enhancement ended without telling anyone (a stuck job closed by the reaper, a cancelled batch).
        touched_batches.update(
            UploadBatch.objects.filter(auto_enhance=True, auto_enhance_notified_at__isnull=True, auto_enhancements__isnull=False)
            .values_list("pk", flat=True).distinct()[:200]
        )
    if owner is None:
        release_overdue_grouping()
    for batch_id in touched_batches:
        notify_batch_if_finished(batch_id)
    return started


def after_result(result):
    """An automatic enhancement ended: free its slot for the next waiting photograph, and tell the owner once when the whole batch is done. Never raises."""
    _group_now(result.image)          # first: the identity analysis is queued before anything reports this photograph as finished
    try:
        release_waiting(owner=result.owner)
        notify_batch_if_finished(result.image.batch_id)
    except Exception:
        logger.exception("Could not finish the bookkeeping of automatic enhancement result %s.", result.pk)


# -- where a photograph is, for the progress payload ---------------------------------------------------------------------------------------------
def _rejected_by_the_checks(image):
    if image.status == REJECTED:
        return True
    validation = getattr(image, "face_validation", None)
    quality = getattr(image, "quality_decision", None)
    return (validation is not None and validation.decision == REJECTED) or (quality is not None and quality.decision == REJECTED and settings.QUALITY_CHECKS_REJECT)


def _latest_result(image):
    from uploads.display_status import latest_result

    return latest_result(image)


def enhancement_state(image, *, personal=None):
    """The enhancement stage of one photograph: ``{"status", "outcome", "summary", "waiting_for", "skipped_reason"}``.

    ``status`` is ``queued`` / ``processing`` / ``completed`` / ``skipped`` / ``failed`` / ``not_applicable``. A photograph still waiting is ``queued`` and says
    what for in ``waiting_for``: ``quality_verdict``, ``moderator_approval`` or ``capacity``; otherwise it is empty. ``outcome`` and ``summary`` are the
    enhancement's own (``enhanced`` / ``partially_enhanced`` / ``original_preserved`` / ``failed``), empty until it has ended.
    """
    state = {"status": NOT_APPLICABLE, "outcome": None, "summary": "", "waiting_for": "", "skipped_reason": ""}
    try:
        record = image.auto_enhancement
    except ObjectDoesNotExist:
        record = None
    result = _latest_result(image)
    if result is not None:
        if result.status in ACTIVE_STATUSES:
            state["status"] = PROCESSING if result.status == EnhancementStatus.PROCESSING else QUEUED
        elif result.status in (EnhancementStatus.COMPLETED, EnhancementStatus.PARTIAL):
            state.update(status=COMPLETED, outcome=result_outcome(result), summary=result_summary(result))
        elif result.status == EnhancementStatus.FAILED:
            state.update(status=FAILED, outcome="failed", summary=result_summary(result))
        else:
            state.update(status=SKIPPED, skipped_reason="cancelled")
        return state
    if record is not None and record.state == AutoEnhancement.State.SKIPPED:
        state.update(status=SKIPPED, skipped_reason=record.skip_reason)
        return state
    if record is not None and record.state == AutoEnhancement.State.WAITING:
        state.update(status=QUEUED, waiting_for=WAIT_FOR_CAPACITY)
        return state
    if record is not None:
        return state  # started, but its result was deleted since: nothing is running for this photograph
    if not image.batch.auto_enhance or _rejected_by_the_checks(image):
        return state
    if personal is None:
        personal = _in_personal_workspace(image)
    quality = getattr(image, "quality_decision", None)
    if quality is None or quality.status != "completed":
        state.update(status=QUEUED, waiting_for=WAIT_FOR_VERDICT)
    elif quality.decision == "needs_review" and not personal:
        state.update(status=QUEUED, waiting_for=WAIT_FOR_MODERATOR)
    return state


def waiting_in_queue(image):
    """Whether ``image`` has an automatic enhancement that is waiting for a free slot (it has no result yet, so the photograph is not finished)."""
    try:
        return image.auto_enhancement.state == AutoEnhancement.State.WAITING
    except ObjectDoesNotExist:
        return False


# -- the batch -----------------------------------------------------------------------------------------------------------------------------------
def batch_enhancement_finished(batch):
    """Whether every photograph of ``batch`` that will be enhanced automatically has been, or was skipped.

    A photograph waiting for a *moderator* does not hold the batch back (it can take days, like ``needs_review`` in the batch status); one still waiting for
    its quality verdict does. ``False`` for a batch that never asked for automatic enhancement or has nothing to enhance.
    """
    if not batch.auto_enhance:
        return False
    if not AutoEnhancement.objects.filter(batch=batch).exists():
        return False
    if AutoEnhancement.objects.filter(batch=batch, state=AutoEnhancement.State.WAITING).exists():
        return False
    if ImageEnhancementResult.objects.filter(image__batch=batch, trigger=EnhancementTrigger.AUTO, status__in=ACTIVE_STATUSES).exists():
        return False
    waiting_for_verdict = (
        ImageUpload.objects.filter(batch=batch, status="uploaded")
        .exclude(face_validation__decision=REJECTED)
        .exclude(preprocessing_result__status="failed")
        .filter(Q(quality_decision__isnull=True) | Q(quality_decision__status__in=QUALITY_IN_PROGRESS))
    )
    if waiting_for_verdict.exists():
        return False
    # The person folders come after the enhancement, so the batch is not finished while one is still being made.
    from identities.models import IdentityAnalysis
    from identities.verdict import IN_PROGRESS as GROUPING_IN_PROGRESS

    return not IdentityAnalysis.objects.filter(image__batch=batch, status__in=GROUPING_IN_PROGRESS).exists()


def batch_counts(batch):
    """What the batch's automatic enhancements came to: ``{"enhanced", "kept", "failed", "skipped", "total"}``."""
    results = ImageEnhancementResult.objects.filter(image__batch=batch, trigger=EnhancementTrigger.AUTO)
    completed = [result for result in results.filter(status__in=(EnhancementStatus.COMPLETED, EnhancementStatus.PARTIAL))]
    enhanced = sum(1 for result in completed if result_outcome(result) in ENHANCED_OUTCOMES or result_outcome(result) is None)
    failed = results.filter(status=EnhancementStatus.FAILED).count()
    skipped = AutoEnhancement.objects.filter(batch=batch, state=AutoEnhancement.State.SKIPPED).count() + results.filter(status=EnhancementStatus.CANCELLED).count()
    return {"enhanced": enhanced, "kept": len(completed) - enhanced, "failed": failed, "skipped": skipped, "total": len(completed) + failed + skipped}


def notify_batch_if_finished(batch_id):
    """Tell the owner once, when the batch's automatic enhancements are all finished: ``"N photos enhanced"``. Never raises; returns the notification or ``None``."""
    try:
        batch = UploadBatch.objects.select_related("owner").filter(pk=batch_id).first()
        if batch is None or not batch_enhancement_finished(batch):
            return None
        claimed = UploadBatch.objects.filter(pk=batch.pk, auto_enhance_notified_at__isnull=True).update(auto_enhance_notified_at=timezone.now())
        if not claimed:
            return None
        from notifications.events import notify_batch_enhanced

        return notify_batch_enhanced(batch.pk, batch_counts(batch))
    except Exception:
        logger.exception("Could not notify batch_id=%s that its enhancements finished.", batch_id)
        return None
