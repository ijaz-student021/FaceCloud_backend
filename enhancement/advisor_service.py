"""The cloud advisor's workflow: consent, budgets, one independent job per image, safe fallback.

What this module guarantees (each point has a test):

* Nothing is sent unless the advisor is enabled and configured *and* the person
  has an up-to-date, separate consent. Consent is checked when a job is requested
  and again just before the request leaves, so a withdrawal stops queued work.
* One job per image. A failed image never fails its siblings; jobs are claimed by
  one worker, run with a bounded number at once, a deadline, a capped number of
  attempts, and daily request and token budgets counted from a ledger.
* No database transaction is open while the provider is called.
* The same image, source version, intent, model and policy never pays twice:
  an identical active or finished recommendation is returned instead.
* A reply is never trusted: it is validated, held to the automatic ranges and the
  chosen intent, and rendered once on the bounded preview through the renderer's
  own quality guards; a result the guards cut too far is rejected.
* Whatever goes wrong (refusal, invalid reply, timeout, rate limit, missing
  configuration, exhausted budget, withdrawn consent, deadline) the person gets a
  safe result: the local conservative recipe, marked as a fallback with the reason.
* A recommendation is usable only while it is current: same source version, no
  newer edit, not cancelled, not expired.
"""
import base64
import hashlib
import logging
import random
import time
import uuid
from datetime import timedelta, timezone as dt_timezone

import cv2
import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from . import advisor_client
from . import policy as rendering_policy
from .advisor_schema import (
    INTENT_LOCKS, InvalidAdvice, build_user_text, diagnostics_from_quality, validate_advice,
)
from .controls import CONTROL_NAMES, INTENTS, RENDERER_VERSION, recommend_local_adjustments
from .editor_source import decode_with_alpha, resize_to_edge, source_version
from .models import (
    ADVISOR_ACTIVE_STATUSES, AdvisorApiCall, AdvisorConsent, AdvisorRecommendation, AdvisorRun, AdvisorStatus,
    EnhancementStatus, ImageEditVersion, ImageEnhancementResult,
)

logger = logging.getLogger(__name__)

ORIGIN_OPENAI = "openai"
ORIGIN_LOCAL = "local_fallback"
MIN_ACCEPTED_GUARD_STRENGTH = 0.35  # the renderer's guards cut the suggestion below this: reject it
JPEG_QUALITY = 90
NEUTRAL_GREY = 128


class AdvisorNotAvailable(APIException):
    status_code = 503
    default_code = "advisor_unavailable"

    def __init__(self, reason):
        super().__init__("The cloud advisor is not available.")
        self.detail = {"detail": "The cloud advisor is not available.", "code": "advisor_unavailable", "reason": reason}


class ConsentRequired(APIException):
    status_code = 403
    default_code = "advisor_consent_required"

    def __init__(self):
        super().__init__("Permission to send a preview to the cloud advisor is required.")
        self.detail = {
            "detail": "Permission to send a preview of this image to an external service is required.",
            "code": "advisor_consent_required", "policy_version": settings.OPENAI_ADVISOR_POLICY_VERSION,
        }


class RetryLater(Exception):
    """The job should be run again after ``countdown`` seconds (queued behind the concurrency limit, or a transient error)."""

    def __init__(self, countdown, reason=""):
        super().__init__(reason)
        self.countdown = countdown
        self.reason = reason


# -- consent -----------------------------------------------------------------

def consent_state(user):
    latest = AdvisorConsent.objects.filter(user=user).first()
    current = settings.OPENAI_ADVISOR_POLICY_VERSION
    granted = bool(latest and latest.granted and latest.policy_version == current)
    return {
        "granted": granted,
        "policy_version": latest.policy_version if latest else None,
        "current_policy_version": current,
        "needs_renewal": bool(latest and latest.granted and latest.policy_version != current),
        "recorded_at": latest.created_at if latest else None,
    }


def has_consent(user):
    return consent_state(user)["granted"]


def record_consent(user, granted, policy_version):
    """Append a decision. A grant must name the current policy version, so it is made against what was disclosed."""
    if granted and policy_version != settings.OPENAI_ADVISOR_POLICY_VERSION:
        raise ValidationError({"policy_version": f"Accept the current policy ({settings.OPENAI_ADVISOR_POLICY_VERSION})."})
    AdvisorConsent.objects.create(user=user, granted=bool(granted), policy_version=policy_version)
    if not granted:
        # Withdrawal stops work that has not been sent yet.
        for recommendation in AdvisorRecommendation.objects.filter(owner=user, status__in=ADVISOR_ACTIVE_STATUSES):
            cancel_recommendation(recommendation)
    return consent_state(user)


# -- budgets and bounded concurrency -----------------------------------------

def _day_start(now=None):
    now = (now or timezone.now()).astimezone(dt_timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def budget_state(user):
    """Today's (UTC) paid requests and tokens, against the configured limits."""
    calls = AdvisorApiCall.objects.filter(started_at__gte=_day_start())
    totals = calls.aggregate(inputs=Sum("input_tokens"), outputs=Sum("output_tokens"))
    used_requests = calls.count()
    user_requests = calls.filter(owner=user).count() if user is not None else 0
    used_tokens = int(totals["inputs"] or 0) + int(totals["outputs"] or 0)
    reason = ""
    if user is not None and user_requests >= settings.OPENAI_ADVISOR_DAILY_REQUESTS_PER_USER:
        reason = "user_daily_limit"
    elif used_requests >= settings.OPENAI_ADVISOR_DAILY_REQUESTS_TOTAL:
        reason = "daily_request_budget"
    elif used_tokens >= settings.OPENAI_ADVISOR_DAILY_TOKENS_TOTAL:
        reason = "daily_token_budget"
    return {
        "exhausted": bool(reason), "reason": reason,
        "user_requests_today": user_requests, "user_requests_limit": settings.OPENAI_ADVISOR_DAILY_REQUESTS_PER_USER,
        "requests_today": used_requests, "requests_limit": settings.OPENAI_ADVISOR_DAILY_REQUESTS_TOTAL,
        "tokens_today": used_tokens, "tokens_limit": settings.OPENAI_ADVISOR_DAILY_TOKENS_TOTAL,
    }


def _slot_key(index):
    return f"enhancement:advisor:slot:{index}"


def acquire_slot(token):
    """One of OPENAI_ADVISOR_MAX_CONCURRENCY shared slots, or None. A crashed holder frees its slot by timeout."""
    lifetime = int(settings.OPENAI_ADVISOR_TIMEOUT_SECONDS) + 30
    for index in range(settings.OPENAI_ADVISOR_MAX_CONCURRENCY):
        if cache.add(_slot_key(index), token, timeout=lifetime):
            return index
    return None


def release_slot(index, token):
    if index is not None and cache.get(_slot_key(index)) == token:
        cache.delete(_slot_key(index))


# -- requests ----------------------------------------------------------------

def request_key(result, version, intent, model):
    basis = "|".join(str(part) for part in (
        result.pk, version, intent, model, settings.OPENAI_ADVISOR_POLICY_VERSION, RENDERER_VERSION,
        rendering_policy.active_policy(),
    ))
    return hashlib.sha256(basis.encode()).hexdigest()


def _latest_edit_id(result):
    return ImageEditVersion.objects.filter(enhancement_result=result).order_by("-id").values_list("id", flat=True).first() or 0


def staleness(recommendation):
    """Why this recommendation can no longer be applied, or "" when it can."""
    if recommendation.status == AdvisorStatus.CANCELLED:
        return "cancelled"
    if recommendation.status == AdvisorStatus.SUPERSEDED:
        return "superseded"
    result = recommendation.enhancement_result
    if result.status != EnhancementStatus.COMPLETED or not result.enhanced_file:
        return "source_unavailable"
    if source_version(result) != recommendation.source_version:
        return "source_changed"
    if _latest_edit_id(result) > recommendation.baseline_edit_id:
        return "edited_since"
    if recommendation.expires_at <= timezone.now():
        return "expired"
    return ""


def create_recommendation(user, result, intent, run=None):
    """``(recommendation, created)`` for one image; an identical one is returned instead of paying again.

    Raises ConsentRequired / AdvisorNotAvailable (nothing is queued), or ValidationError when a
    different recommendation for this image is still being produced.
    """
    state = advisor_client.availability()
    if not state["available"]:
        raise AdvisorNotAvailable(state["reason"])
    if not has_consent(user):
        raise ConsentRequired()
    if intent not in INTENTS:
        raise ValidationError({"intent": f"Choose one of: {', '.join(INTENTS)}."})
    version = source_version(result)
    key = request_key(result, version, intent, settings.OPENAI_ADVISOR_MODEL)
    now = timezone.now()
    with transaction.atomic():
        ImageEnhancementResult.objects.select_for_update().only("pk").get(pk=result.pk)
        active = AdvisorRecommendation.objects.filter(
            enhancement_result=result, status__in=ADVISOR_ACTIVE_STATUSES).first()
        if active is not None:
            if active.request_key == key:
                return active, False
            raise ValidationError("A recommendation for this image is already being prepared.")
        finished = AdvisorRecommendation.objects.filter(
            enhancement_result=result, request_key=key, status=AdvisorStatus.COMPLETED, origin=ORIGIN_OPENAI,
        ).first()
        if finished is not None and not staleness(finished):
            return finished, False
        try:
            recommendation = AdvisorRecommendation.objects.create(
                owner=user, enhancement_result=result, run=run, intent=intent,
                model=settings.OPENAI_ADVISOR_MODEL, policy_version=settings.OPENAI_ADVISOR_POLICY_VERSION,
                renderer_version=RENDERER_VERSION, render_policy=rendering_policy.active_policy(),
                source_version=version, baseline_edit_id=_latest_edit_id(result), request_key=key,
                deadline_at=now + timedelta(seconds=settings.OPENAI_ADVISOR_DEADLINE_SECONDS),
                expires_at=now + timedelta(hours=settings.OPENAI_ADVISOR_RECOMMENDATION_TTL_HOURS),
            )
        except IntegrityError as error:
            raise ValidationError("A recommendation for this image is already being prepared.") from error
    return recommendation, True


def enqueue(recommendation):
    """Send the job to the queue after the row is committed; a queue failure is recorded on the row."""
    from .tasks import advisor_recommend

    task_id = uuid.uuid4().hex
    AdvisorRecommendation.objects.filter(pk=recommendation.pk).update(task_id=task_id)
    options = {"task_id": task_id}
    if settings.OPENAI_ADVISOR_QUEUE_NAME:
        options["queue"] = settings.OPENAI_ADVISOR_QUEUE_NAME
    try:
        advisor_recommend.apply_async(args=[recommendation.pk], **options)
    except Exception:
        logger.exception("Advisor job %s could not be queued.", recommendation.pk)
        AdvisorRecommendation.objects.filter(pk=recommendation.pk, status=AdvisorStatus.QUEUED).update(
            status=AdvisorStatus.FAILED, failure_reason="The background queue is unavailable.",
            finished_at=timezone.now(),
        )
    recommendation.refresh_from_db()
    return recommendation


def create_run(user, results, intent):
    """One independent job per image. Returns ``(run, items)``; items say what happened to each image."""
    if len(results) > settings.OPENAI_ADVISOR_MAX_IMAGES_PER_RUN:
        raise ValidationError({"result_ids": f"At most {settings.OPENAI_ADVISOR_MAX_IMAGES_PER_RUN} images per request."})
    state = advisor_client.availability()
    if not state["available"]:
        raise AdvisorNotAvailable(state["reason"])
    if not has_consent(user):
        raise ConsentRequired()
    if intent not in INTENTS:
        raise ValidationError({"intent": f"Choose one of: {', '.join(INTENTS)}."})
    run = AdvisorRun.objects.create(owner=user, intent=intent)
    items = []
    for result in results:
        try:
            recommendation, created = create_recommendation(user, result, intent, run=run)
        except ValidationError as error:
            items.append({"result_id": result.pk, "state": "skipped", "reason": "already_in_progress",
                          "detail": error.detail})
            continue
        if created:
            recommendation = enqueue(recommendation)
            items.append({"result_id": result.pk, "state": "queued", "recommendation": recommendation})
        else:
            items.append({"result_id": result.pk, "state": "reused", "recommendation": recommendation})
    return run, items


def cancel_recommendation(recommendation):
    """Stop a recommendation that has not finished. A request already sent cannot be recalled; its answer is discarded."""
    with transaction.atomic():
        locked = AdvisorRecommendation.objects.select_for_update().filter(pk=recommendation.pk).first()
        if locked is not None and locked.status in ADVISOR_ACTIVE_STATUSES:
            locked.status = AdvisorStatus.CANCELLED
            locked.claim_token = ""
            locked.finished_at = timezone.now()
            locked.save(update_fields=["status", "claim_token", "finished_at", "updated_at"])
        return locked or recommendation


def cancel_run(run):
    run.cancelled_at = run.cancelled_at or timezone.now()
    run.save(update_fields=["cancelled_at"])
    for recommendation in run.recommendations.filter(status__in=ADVISOR_ACTIVE_STATUSES):
        cancel_recommendation(recommendation)
    return run


# -- the image and the guards -------------------------------------------------

def prepare_advisor_image(bgr, alpha):
    """``(bgr uint8 bounded, jpeg bytes, had_transparency)`` from a decoded image and its alpha.

    Shrunk to OPENAI_ADVISOR_IMAGE_EDGE; transparent areas are shown on neutral grey; the JPEG re-encode
    carries no EXIF, ICC profile or filename. Pure: no database, no storage.
    """
    bgr, alpha = resize_to_edge(bgr, alpha, settings.OPENAI_ADVISOR_IMAGE_EDGE)
    had_transparency = alpha is not None
    if had_transparency:
        weight = (alpha.astype(np.float32) / 255.0)[:, :, None]
        bgr = np.clip(bgr.astype(np.float32) * weight + NEUTRAL_GREY * (1.0 - weight), 0, 255).round().astype(np.uint8)
    ok, encoded = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise ValueError("preview could not be encoded")
    return bgr, encoded.tobytes(), had_transparency


def build_advisor_image(result):
    """The only pixels that ever leave, from the bounded lossless editor copy of the enhanced image.

    That copy was decoded with colour management and orientation already applied.
    """
    from .previews import ensure_source

    source = ensure_source(result, source_version(result))
    source.file.open("rb")
    try:
        payload = source.file.read()
    finally:
        source.file.close()
    bgr, alpha = decode_with_alpha(payload)
    return prepare_advisor_image(bgr, alpha)


def guard_suggestion(bgr, quality, adjustments, intent):
    """Render the suggestion once on the bounded image through the renderer's guards.

    Returns ``(final_adjustments, guard_info, rejected_reason)``. The renderer already reduces a
    suggestion that would clip highlights or flatten contrast; here a suggestion that needed to be cut
    below MIN_ACCEPTED_GUARD_STRENGTH is rejected, and sharpening that is unsafe (visible noise, or
    noise that grows) is removed.
    """
    from .services import apply_gallery_adjustments

    controls = dict(adjustments)
    info = {"reductions": [], "intent_locks": list(INTENT_LOCKS[intent])}
    if controls.get("sharpness") and "visible_noise" in quality.get("defects", ()):
        controls["sharpness"] = 0.0
        info["reductions"].append({"control": "sharpness", "reason": "noise_present"})
    rendered, metadata = apply_gallery_adjustments(bgr, "manual", controls)
    before_noise = metadata["quality_before"]["estimated_noise"]
    if controls.get("sharpness") and metadata["quality_after"]["estimated_noise"] > max(5.0, before_noise * 1.35):
        controls["sharpness"] = 0.0
        info["reductions"].append({"control": "sharpness", "reason": "noise_increased"})
        rendered, metadata = apply_gallery_adjustments(bgr, "manual", controls)
    info["safety_strength"] = metadata["safety_strength"]
    info["guard_reductions"] = metadata["guard_reductions"]
    info["safety_reasons"] = metadata["safety_reasons"]
    if metadata["safety_strength"] < MIN_ACCEPTED_GUARD_STRENGTH:
        return None, info, "guard_rejected"
    final = {name: round(float(metadata["effective"][name]), 2) for name in CONTROL_NAMES}
    return final, info, ""


# -- running one job -----------------------------------------------------------

def _claim(recommendation_id, token):
    """Take the job for this execution, or return the reason it should not run."""
    lease = timedelta(seconds=settings.OPENAI_ADVISOR_TIMEOUT_SECONDS + 30)
    with transaction.atomic():
        row = AdvisorRecommendation.objects.select_for_update().select_related("enhancement_result").filter(
            pk=recommendation_id).first()
        if row is None:
            return None, "missing"
        if row.status not in ADVISOR_ACTIVE_STATUSES:
            return None, row.status
        now = timezone.now()
        if row.status == AdvisorStatus.PROCESSING and row.claim_token and row.started_at and now - row.updated_at < lease:
            return None, "in_progress"
        row.status = AdvisorStatus.PROCESSING
        row.claim_token = token
        row.started_at = row.started_at or now
        row.save(update_fields=["status", "claim_token", "started_at", "updated_at"])
        return row, ""


def _publish(recommendation_id, token, **fields):
    """Store the outcome only if this execution still owns the job and the image is unchanged."""
    with transaction.atomic():
        row = AdvisorRecommendation.objects.select_for_update().select_related("enhancement_result").filter(
            pk=recommendation_id).first()
        if row is None or row.claim_token != token or row.status != AdvisorStatus.PROCESSING:
            return None  # cancelled, superseded or taken over: the outcome is discarded
        if source_version(row.enhancement_result) != row.source_version or _latest_edit_id(
                row.enhancement_result) > row.baseline_edit_id:
            row.status = AdvisorStatus.SUPERSEDED
            row.claim_token = ""
            row.finished_at = timezone.now()
            row.save(update_fields=["status", "claim_token", "finished_at", "updated_at"])
            return row
        for name, value in fields.items():
            setattr(row, name, value)
        row.status = fields.get("status", AdvisorStatus.COMPLETED)
        row.claim_token = ""
        row.finished_at = timezone.now()
        row.save()
        return row


def _finish_fallback(row, token, reason, *, local=None, guard=None, suggested=None, tokens=(0, 0), latency=0):
    """Publish the conservative local result, marked as a fallback with its reason."""
    local = local if local is not None else row.local_recipe or None
    if local:
        fields = {
            "action": local["action"], "adjustments": local["controls"],
            "rationale": " ".join(local["rationale"])[:500], "caution_flags": local["caution_flags"],
        }
    else:
        fields = {"action": "no_change", "adjustments": {name: 0.0 for name in CONTROL_NAMES},
                  "rationale": "The advisor was unavailable and no local suggestion could be made; nothing was changed.",
                  "caution_flags": []}
    return _publish(
        row.pk, token, origin=ORIGIN_LOCAL, fallback_reason=reason, local_recipe=local or {},
        guard=guard or {}, suggested=suggested or {}, input_tokens=row.input_tokens + tokens[0],
        output_tokens=row.output_tokens + tokens[1], latency_ms=row.latency_ms + latency, **fields,
    )


def _release_to_queue(row_id, token):
    AdvisorRecommendation.objects.filter(pk=row_id, claim_token=token, status=AdvisorStatus.PROCESSING).update(
        status=AdvisorStatus.QUEUED, claim_token="", updated_at=timezone.now())


def _backoff(attempts, retry_after):
    delay = min(2 ** max(attempts, 1) * 2.0, 30.0) + random.uniform(0, 1.5)
    if retry_after:
        delay = max(delay, min(float(retry_after), 30.0))
    return delay


def run_recommendation(recommendation_id):
    """Execute one image's job. Returns a short status; raises RetryLater to be run again later."""
    token = uuid.uuid4().hex
    row, reason = _claim(recommendation_id, token)
    if row is None:
        return reason
    try:
        return _run_claimed(row, token)
    except RetryLater:
        raise
    except Exception:
        logger.exception("Advisor job %s failed unexpectedly.", recommendation_id)
        return _outcome(_finish_fallback(row, token, "internal_error"))


def _run_claimed(row, token):
    now = timezone.now()
    stale = staleness(row)
    if stale in ("source_changed", "edited_since", "source_unavailable"):
        AdvisorRecommendation.objects.filter(pk=row.pk, claim_token=token).update(
            status=AdvisorStatus.SUPERSEDED, claim_token="", finished_at=now)
        return "superseded"

    # Everything below can fall back, so build the local suggestion first; it needs the bounded image.
    try:
        bgr, jpeg, had_transparency = build_advisor_image(row.enhancement_result)
    except Exception:
        logger.exception("Advisor image for job %s could not be built.", row.pk)
        return _outcome(_finish_fallback(row, token, "image_unavailable", local=None))
    from .services import analyze_image_quality, measure_compression_artifacts

    quality = analyze_image_quality(bgr)
    local = recommend_local_adjustments(
        quality, row.intent, blockiness_detected=measure_compression_artifacts(bgr)["detected"])
    row.local_recipe = local

    if now >= row.deadline_at:
        return _outcome(_finish_fallback(row, token, "deadline_exceeded", local=local))
    state = advisor_client.availability()
    if not state["available"]:
        return _outcome(_finish_fallback(row, token, state["reason"], local=local))
    if not has_consent(row.owner):
        return _outcome(_finish_fallback(row, token, "consent_missing", local=local))
    if row.attempts >= settings.OPENAI_ADVISOR_MAX_ATTEMPTS:
        return _outcome(_finish_fallback(row, token, "retries_exhausted", local=local))
    budget = budget_state(row.owner)
    if budget["exhausted"]:
        return _outcome(_finish_fallback(row, token, "budget_exhausted", local=local))

    slot = acquire_slot(token)
    if slot is None:
        _release_to_queue(row.pk, token)
        raise RetryLater(random.uniform(2.0, 6.0), "concurrency_limit")
    call = None
    started = time.perf_counter()
    try:
        client = advisor_client.build_client()
        call = AdvisorApiCall.objects.create(
            recommendation=row, owner=row.owner, model=settings.OPENAI_ADVISOR_MODEL)
        AdvisorRecommendation.objects.filter(pk=row.pk).update(attempts=F("attempts") + 1)
        row.attempts += 1
        # No transaction is open from here until the provider has answered.
        user_text = build_user_text(row.intent, diagnostics_from_quality(quality), local, had_transparency=had_transparency)
        data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        reply = advisor_client.request_advice(client, image_data_url=data_url, user_text=user_text)
    except advisor_client.AdvisorUnavailable as error:
        _close_call(call, error.reason, started)
        return _outcome(_finish_fallback(row, token, error.reason, local=local))
    except advisor_client.TransientProviderError as error:
        _close_call(call, error.reason, started)
        delay = _backoff(row.attempts, error.retry_after)
        if row.attempts >= settings.OPENAI_ADVISOR_MAX_ATTEMPTS or timezone.now() + timedelta(seconds=delay) >= row.deadline_at:
            return _outcome(_finish_fallback(row, token, error.reason, local=local))
        _release_to_queue(row.pk, token)
        raise RetryLater(delay, error.reason) from None
    except advisor_client.ProviderError as error:
        _close_call(call, error.reason, started)
        return _outcome(_finish_fallback(row, token, error.reason, local=local))
    except advisor_client.ReplyRefused as error:
        _close_call(call, "refused", started, getattr(error, "tokens", (0, 0)))
        return _outcome(_finish_fallback(row, token, "refused", local=local, tokens=getattr(error, "tokens", (0, 0))))
    except advisor_client.ReplyUnusable as error:
        _close_call(call, error.reason, started, getattr(error, "tokens", (0, 0)))
        return _outcome(_finish_fallback(row, token, error.reason, local=local, tokens=getattr(error, "tokens", (0, 0))))
    finally:
        release_slot(slot, token)

    latency = max(1, round((time.perf_counter() - started) * 1000))
    tokens = (reply.input_tokens, reply.output_tokens)
    _close_call(call, "ok", started, tokens)
    try:
        advice = validate_advice(reply.payload, row.intent)
    except InvalidAdvice as error:
        return _outcome(_finish_fallback(
            row, token, error.reason, local=local, suggested={"invalid": True}, tokens=tokens, latency=latency))

    guard = {"clamps_and_locks": advice["notes"]}
    adjustments = advice["adjustments"]
    if advice["action"] == "apply":
        final, info, rejected = guard_suggestion(bgr, quality, adjustments, row.intent)
        guard.update(info)
        if rejected:
            return _outcome(_finish_fallback(
                row, token, rejected, local=local, guard=guard, suggested=advice, tokens=tokens, latency=latency))
        adjustments = final
        if not any(adjustments.values()):
            advice["action"] = "no_change"
    published = _publish(
        row.pk, token, origin=ORIGIN_OPENAI, action=advice["action"], suggested=advice, adjustments=adjustments,
        rationale=advice["rationale"], caution_flags=advice["caution_flags"], guard=guard, local_recipe=local,
        input_tokens=row.input_tokens + tokens[0], output_tokens=row.output_tokens + tokens[1],
        latency_ms=row.latency_ms + latency,
    )
    return _outcome(published)


def _outcome(published):
    return published.status if published else "discarded"


def _close_call(call, outcome, started, tokens=(0, 0)):
    if call is None:
        return
    AdvisorApiCall.objects.filter(pk=call.pk).update(
        outcome=outcome, input_tokens=tokens[0], output_tokens=tokens[1],
        latency_ms=max(1, round((time.perf_counter() - started) * 1000)),
    )


def cleanup_records(now=None):
    """Fail jobs that outlived their deadline without a worker finishing them; delete old records."""
    now = now or timezone.now()
    grace = timedelta(seconds=settings.OPENAI_ADVISOR_DEADLINE_SECONDS)
    closed = AdvisorRecommendation.objects.filter(
        status__in=ADVISOR_ACTIVE_STATUSES, deadline_at__lt=now - grace,
    ).update(
        status=AdvisorStatus.FAILED, claim_token="", finished_at=now,
        failure_reason="No worker finished this job before its deadline.",
    )
    horizon = now - timedelta(days=settings.OPENAI_ADVISOR_RECORD_RETENTION_DAYS)
    calls = AdvisorApiCall.objects.filter(started_at__lt=horizon).delete()[0]
    recommendations = AdvisorRecommendation.objects.filter(created_at__lt=horizon).exclude(
        status__in=ADVISOR_ACTIVE_STATUSES).delete()[0]
    runs = AdvisorRun.objects.filter(created_at__lt=horizon, recommendations__isnull=True).delete()[0]
    return {"closed": closed, "api_calls": calls, "recommendations": recommendations, "runs": runs}
