import logging
import time
import uuid

import cv2
import numpy as np
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from organizations.services import get_default_organization
from uploads.models import ImagePreprocessingResult, UploadBatch
from validation.models import FaceValidationResult

from .models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership
from .providers import get_identity_provider
from .storage import protect_embedding, reveal_embedding

logger = logging.getLogger(__name__)


def _read_image(preprocessing):
    if preprocessing.status != "completed" or not preprocessing.processed_file:
        raise ValidationError("Preprocessing must complete before identity analysis.")
    preprocessing.processed_file.open("rb")
    try:
        payload = preprocessing.processed_file.read()
    finally:
        preprocessing.processed_file.close()
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValidationError("The standardized image cannot be decoded safely.")
    return image


def _next_group_name(owner):
    used = set(IdentityGroup.objects.filter(owner=owner, display_name__startswith="Person ").values_list("display_name", flat=True))
    number = 1
    while f"Person {number:03d}" in used:
        number += 1
    return f"Person {number:03d}"


def _confirmed_states():
    return [IdentityMembership.State.AUTO_MATCHED, IdentityMembership.State.NEW_GROUP, IdentityMembership.State.CONFIRMED]


def _refresh_group(group):
    members = list(group.memberships.filter(state__in=_confirmed_states()).select_related("identity_face"))
    if not members:
        group.member_count = 0
        group.protected_centroid = b""
    else:
        vectors = [reveal_embedding(row.identity_face.protected_embedding, row.identity_face.embedding_dimension) for row in members]
        centroid = np.mean(vectors, axis=0)
        centroid /= np.linalg.norm(centroid)
        group.member_count = len(members)
        group.protected_centroid = protect_embedding(centroid)
    group.save(update_fields=["member_count", "protected_centroid", "updated_at"])


def _choose_group(owner, provider, vector):
    groups = IdentityGroup.objects.filter(owner=owner, state=IdentityGroup.State.ACTIVE, provider=provider.name, embedding_dimension=provider.dimension).exclude(protected_centroid=b"").order_by("-updated_at")[:settings.IDENTITY_MAX_CANDIDATE_GROUPS]
    scored = sorted(((provider.similarity(vector, reveal_embedding(group.protected_centroid, group.embedding_dimension)), group) for group in groups), key=lambda item: item[0], reverse=True)
    if not scored or scored[0][0] < settings.IDENTITY_NEW_GROUP_THRESHOLD:
        return "new", None, None, None
    best_score, best_group = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else None
    if best_score >= settings.IDENTITY_MATCH_THRESHOLD and (second_score is None or best_score - second_score >= settings.IDENTITY_AMBIGUITY_MARGIN):
        return "match", best_group, best_score, second_score
    return "review", best_group, best_score, second_score


def _batch_cancellation_requested(batch_id):
    return UploadBatch.objects.filter(pk=batch_id, cancellation_requested=True).exists()


def process_identity_analysis(analysis_id):
    with transaction.atomic():
        analysis = IdentityAnalysis.objects.select_for_update().select_related("image__batch", "preprocessing_result", "face_validation").get(pk=analysis_id)
        if analysis.status in (IdentityAnalysis.Status.COMPLETED, IdentityAnalysis.Status.NEEDS_REVIEW):
            return analysis
        if analysis.image.batch.cancellation_requested:
            analysis.status = IdentityAnalysis.Status.CANCELLED
            analysis.finished_at = timezone.now()
            analysis.save(update_fields=["status", "finished_at", "updated_at"])
            return analysis
        from privacy.services import biometric_processing_allowed
        if not biometric_processing_allowed(analysis.owner):
            # Consent can be withdrawn between queueing and processing.
            analysis.status = IdentityAnalysis.Status.FAILED
            analysis.failure_reason = "Biometric consent is not active."
            analysis.finished_at = timezone.now()
            analysis.save(update_fields=["status", "failure_reason", "finished_at", "updated_at"])
            return analysis
        analysis.status = IdentityAnalysis.Status.PROCESSING
        analysis.started_at = analysis.started_at or timezone.now()
        analysis.failure_reason = ""
        analysis.save(update_fields=["status", "started_at", "failure_reason", "updated_at"])
    started = time.perf_counter()
    try:
        provider = get_identity_provider()
        provider.healthcheck()
        image = _read_image(analysis.preprocessing_result)
        prepared = []
        for index, face in enumerate(analysis.face_validation.faces):
            if _batch_cancellation_requested(analysis.image.batch_id):
                IdentityAnalysis.objects.filter(pk=analysis_id).update(
                    status=IdentityAnalysis.Status.CANCELLED,
                    processing_duration_ms=max(1, round((time.perf_counter() - started) * 1000)),
                    finished_at=timezone.now(),
                )
                return IdentityAnalysis.objects.get(pk=analysis_id)
            box = face.get("standardized_box") or face
            landmarks = face.get("standardized_landmarks") or face.get("landmarks")
            crop = provider.prepare_face(image, box, landmarks)
            vector = provider.validate_embedding(provider.embed(crop))
            prepared.append((index, face, vector))
        if not prepared:
            raise ValidationError("No validated faces are available for identity analysis.")

        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=analysis.owner_id)
            locked = IdentityAnalysis.objects.select_for_update().get(pk=analysis.pk)
            if locked.task_id != analysis.task_id:
                return locked
            old_group_ids = list(locked.faces.values_list("memberships__identity_group_id", flat=True))
            locked.faces.all().delete()
            assigned = review = 0
            for index, face_data, vector in prepared:
                face = IdentityFace.objects.create(owner=locked.owner, image=locked.image, analysis=locked, face_index=index, bounding_box=face_data.get("standardized_box") or face_data, landmarks=face_data.get("landmarks") or [], protected_embedding=protect_embedding(vector), embedding_dimension=provider.dimension, provider=provider.name, model_checksum=provider.model_checksum, quality={key: face_data[key] for key in ("confidence", "sharpness", "brightness") if key in face_data})
                decision, group, best, second = _choose_group(locked.owner, provider, vector)
                if decision == "new":
                    group = IdentityGroup.objects.create(owner=locked.owner, organization=get_default_organization(locked.owner), display_name=_next_group_name(locked.owner), embedding_dimension=provider.dimension, provider=provider.name, model_checksum=provider.model_checksum, policy_version=settings.IDENTITY_POLICY_VERSION)
                    state = IdentityMembership.State.NEW_GROUP
                    method = "new_group_boundary_v1"
                    assigned += 1
                elif decision == "match":
                    state = IdentityMembership.State.AUTO_MATCHED
                    method = "centroid_cosine_v1"
                    assigned += 1
                else:
                    state = IdentityMembership.State.NEEDS_REVIEW
                    method = "ambiguous_centroid_cosine_v1"
                    review += 1
                membership = IdentityMembership.objects.create(owner=locked.owner, identity_face=face, identity_group=group, similarity_score=best, matching_distance=(1 - best if best is not None else None), state=state, decision_method=method, evidence={"best_score": best, "second_best_score": second, "threshold": settings.IDENTITY_MATCH_THRESHOLD, "ambiguity_margin": settings.IDENTITY_AMBIGUITY_MARGIN})
                if state == IdentityMembership.State.NEEDS_REVIEW:
                    from moderation.services import create_review_for_identity_membership
                    create_review_for_identity_membership(membership)
                if state in _confirmed_states():
                    _refresh_group(group)
            for group in IdentityGroup.objects.filter(pk__in=[pk for pk in old_group_ids if pk]):
                _refresh_group(group)
            locked.face_count = len(prepared)
            locked.assigned_count = assigned
            locked.review_count = review
            locked.status = IdentityAnalysis.Status.NEEDS_REVIEW if review else IdentityAnalysis.Status.COMPLETED
            locked.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
            locked.finished_at = timezone.now()
            locked.failure_reason = ""
            locked.save()
            return locked
    except Exception:
        logger.exception("Identity analysis failed safely for analysis_id=%s.", analysis_id)
        IdentityAnalysis.objects.filter(pk=analysis_id).update(status=IdentityAnalysis.Status.FAILED, failure_reason="Identity analysis could not be completed safely.", processing_duration_ms=max(1, round((time.perf_counter() - started) * 1000)), finished_at=timezone.now())
        return IdentityAnalysis.objects.get(pk=analysis_id)


def create_or_reset_identity_analysis(image, force=False):
    try:
        preprocessing = image.preprocessing_result
        validation = image.face_validation
    except (ImagePreprocessingResult.DoesNotExist, FaceValidationResult.DoesNotExist) as error:
        raise ValidationError("Preprocessing and face validation must complete first.") from error
    if preprocessing.status != "completed" or validation.decision not in (FaceValidationResult.Decision.ACCEPTED, FaceValidationResult.Decision.NEEDS_REVIEW):
        raise ValidationError("The image is not eligible for identity analysis.")
    analysis, created = IdentityAnalysis.objects.get_or_create(image=image, defaults={"owner": image.owner, "preprocessing_result": preprocessing, "face_validation": validation, "provider": settings.IDENTITY_PROVIDER, "model_checksum": settings.IDENTITY_MODEL_SHA256, "policy_version": settings.IDENTITY_POLICY_VERSION})
    if not created and analysis.status in (IdentityAnalysis.Status.COMPLETED, IdentityAnalysis.Status.NEEDS_REVIEW) and not force:
        return analysis, False
    analysis.owner = image.owner
    analysis.preprocessing_result = preprocessing
    analysis.face_validation = validation
    analysis.provider = settings.IDENTITY_PROVIDER
    analysis.model_checksum = settings.IDENTITY_MODEL_SHA256
    analysis.policy_version = settings.IDENTITY_POLICY_VERSION
    analysis.status = IdentityAnalysis.Status.PENDING
    analysis.task_id = ""
    analysis.failure_reason = ""
    analysis.started_at = analysis.finished_at = None
    analysis.save()
    return analysis, True


def enqueue_identity_analysis(image, force=False):
    from privacy.services import biometric_processing_allowed
    from .tasks import analyze_image_identity_task
    if not biometric_processing_allowed(image.owner):
        raise ValidationError("Biometric consent is required before identity analysis.")
    provider = get_identity_provider()
    provider.healthcheck()
    analysis, should_queue = create_or_reset_identity_analysis(image, force)
    if not should_queue:
        return analysis
    task_id = uuid.uuid4().hex
    analysis.status = IdentityAnalysis.Status.QUEUED
    analysis.task_id = task_id
    analysis.save(update_fields=["status", "task_id", "updated_at"])
    def dispatch():
        try:
            analyze_image_identity_task.apply_async(args=[analysis.pk], task_id=task_id)
        except Exception:
            logger.exception("Could not queue identity analysis_id=%s.", analysis.pk)
            IdentityAnalysis.objects.filter(pk=analysis.pk).update(status=IdentityAnalysis.Status.FAILED, failure_reason="The identity-analysis queue is unavailable.", finished_at=timezone.now())
    transaction.on_commit(dispatch)
    return analysis


@transaction.atomic
def review_membership(membership, reviewer, action, group_id=None, *, actor=None):
    """Decide an uncertain identity assignment.

    ``reviewer`` is always the membership's own owner -- the internal
    ``owner=reviewer`` lookup below is a real authorization gate, not
    loosened here. A moderator deciding on someone else's behalf (T-007's
    org-scoped review workflow) passes the membership's actual owner as
    ``reviewer`` and themself as ``actor``: group ownership, the
    owner-scoped candidate-group lookup, and IdentityChange.owner all stay
    attributed to the real content owner, while ``reviewed_by`` and
    IdentityChange.actor record who actually made the call. The self-review
    endpoint is unaffected -- it always passes the same user as both, so
    ``actor`` defaults to ``reviewer``.
    """
    actor = actor or reviewer
    from .models import IdentityChange
    membership = IdentityMembership.objects.select_for_update(of=("self",)).select_related("identity_face", "identity_group").get(pk=membership.pk, owner=reviewer)
    if membership.state != IdentityMembership.State.NEEDS_REVIEW:
        raise ValidationError("Only uncertain assignments can be reviewed.")
    old_group = membership.identity_group
    if action == "select_group":
        membership.identity_group = IdentityGroup.objects.filter(pk=group_id, owner=reviewer, state=IdentityGroup.State.ACTIVE).first()
        if membership.identity_group is None:
            raise ValidationError("The selected person folder is unavailable.")
    elif action == "new_group":
        face = membership.identity_face
        membership.identity_group = IdentityGroup.objects.create(owner=reviewer, organization=get_default_organization(reviewer), display_name=_next_group_name(reviewer), embedding_dimension=face.embedding_dimension, provider=face.provider, model_checksum=face.model_checksum, policy_version=settings.IDENTITY_POLICY_VERSION)
    elif action == "reject":
        membership.state = IdentityMembership.State.REJECTED
    if action != "reject":
        if membership.identity_group_id is None:
            raise ValidationError("No suggested group is available to confirm.")
        membership.state = IdentityMembership.State.CONFIRMED
    membership.reviewed_by = actor
    membership.reviewed_at = timezone.now()
    membership.save()
    if old_group:
        _refresh_group(old_group)
    if membership.identity_group and membership.identity_group_id != getattr(old_group, "pk", None):
        _refresh_group(membership.identity_group)
    analysis = membership.identity_face.analysis
    analysis.review_count = analysis.faces.filter(memberships__state=IdentityMembership.State.NEEDS_REVIEW).count()
    analysis.assigned_count = analysis.faces.filter(memberships__state__in=_confirmed_states()).count()
    analysis.status = IdentityAnalysis.Status.NEEDS_REVIEW if analysis.review_count else IdentityAnalysis.Status.COMPLETED
    analysis.save(update_fields=["review_count", "assigned_count", "status", "updated_at"])
    IdentityChange.objects.create(owner=reviewer, actor=actor, group=membership.identity_group, membership=membership, action=IdentityChange.Action.REVIEWED, before={"state": IdentityMembership.State.NEEDS_REVIEW}, after={"state": membership.state, "group_id": membership.identity_group_id})
    return membership
