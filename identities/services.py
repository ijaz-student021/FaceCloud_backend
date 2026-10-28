import logging
import time
import uuid

import cv2
import numpy as np
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from organizations.services import get_default_organization
from privacy.errors import ConsentRequired
from uploads.models import ImagePreprocessingResult, UploadBatch
from validation.models import FaceValidationResult
from validation.services import get_face_detector

from .models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership
from .providers import get_identity_provider, thresholds_for
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


refresh_group = _refresh_group      # the public name for code outside this module (deleting a photograph recounts its folders)


def _scored_groups(owner, provider, vector):
    """``[(similarity, group)]`` for the owner's candidate folders, best first."""
    groups = IdentityGroup.objects.filter(owner=owner, state=IdentityGroup.State.ACTIVE, provider=provider.name, embedding_dimension=provider.dimension).exclude(protected_centroid=b"").order_by("-updated_at")[:settings.IDENTITY_MAX_CANDIDATE_GROUPS]
    return sorted(((provider.similarity(vector, reveal_embedding(group.protected_centroid, group.embedding_dimension)), group) for group in groups), key=lambda item: item[0], reverse=True)


def _closest_folder(owner, provider, vector):
    """``(score, group id)`` of the existing folder a face resembles most, or ``(None, None)`` when the owner has none."""
    scored = _scored_groups(owner, provider, vector)
    return (scored[0][0], scored[0][1].pk) if scored else (None, None)


def decide_group(scored, limits):
    """The rule that places a face, on its own so that grouping, the evaluation command and the re-grouping command all use the same one.

    ``scored`` is ``[(similarity, folder)]`` best first, ``limits`` the provider's ``{new_group, match, margin}``. Returns
    ``(decision, folder, best_score, second_score)``:

    * ``"new"``: no folder, or the best is below ``new_group``: the face is clearly not like any existing one, so it opens a folder;
    * ``"match"``: the best is at least ``match`` and ``margin`` ahead of the runner-up: it joins that folder;
    * ``"review"``: anything in between (alike, but not sure enough): it waits for a person to decide, suggested for the best folder.
    """
    if not scored or scored[0][0] < limits["new_group"]:
        return "new", None, None, None
    best_score, best_group = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else None
    if best_score >= limits["match"] and (second_score is None or best_score - second_score >= limits["margin"]):
        return "match", best_group, best_score, second_score
    return "review", best_group, best_score, second_score


def _choose_group(owner, provider, vector):
    return decide_group(_scored_groups(owner, provider, vector), thresholds_for(provider))


def face_grouping_check(face, face_count):
    """Whether a detected face is good enough to be grouped, and if not, why: ``(ok, evidence)``.

    The same bar the face check sets for "accepted": the detector's own score floor (the stricter of ``YUNET_SCORE_THRESHOLD`` and
    ``QUALITY_FACE_MIN_CONFIDENCE``) and a usable size (``validation.services._face_is_usable``, with the group allowance when the photograph has
    several faces). A tiny, unsure detection on food or an object therefore never opens a person folder.
    """
    from validation.services import MIN_FACE_AREA_RATIO, MIN_GROUP_FACE_AREA_RATIO, _face_is_usable

    confidence = face.get("confidence")
    floor = max(float(settings.YUNET_SCORE_THRESHOLD), float(settings.QUALITY_FACE_MIN_CONFIDENCE))
    ratio = MIN_GROUP_FACE_AREA_RATIO if face_count > 1 else MIN_FACE_AREA_RATIO
    detail = {
        "confidence": None if confidence is None else round(float(confidence), 3), "score_floor": floor,
        "area_ratio": face.get("area_ratio"), "min_area_ratio": ratio,
    }
    # The face check records both for every face it finds; a face record without them (older data, a stand-in) is judged on what it has.
    if confidence is not None and float(confidence) < floor:
        return False, {**detail, "reason": "detector_score_below_floor"}
    if all(key in face for key in ("area_ratio", "width", "height")) and not _face_is_usable(face, ratio):
        return False, {**detail, "reason": "face_too_small"}
    return True, detail


def photo_is_rejected(image):
    """Whether the checks rejected this photograph (refused at upload, by the face check or by the quality decision): it belongs in no person folder."""
    from enhancement.eligibility import rejection_of

    return rejection_of(image) is not None


def folder_is_renamed(group):
    """Whether the owner named this folder themselves (a rename is recorded, or the name is no longer the automatic ``Person 001``)."""
    import re

    from .models import IdentityChange

    if not re.fullmatch(r"Person \d{3,}", group.display_name):
        return True
    return IdentityChange.objects.filter(group=group, action=IdentityChange.Action.RENAMED).exists()


def _folder_is_empty(group):
    return not group.memberships.filter(state__in=[*_confirmed_states(), IdentityMembership.State.NEEDS_REVIEW]).exists()


FOLDER_EMPTY_REASON = "The folder was left empty."
TRASH_EMPTY_REASON = "The folder was left empty by moving its last photo to the trash."          # uploads/trash.py puts the folder back when a photograph returns


@transaction.atomic
def remove_photo_from_folders(image, *, reason="The photo was rejected by the checks.", archive_reason=FOLDER_EMPTY_REASON):
    """Take a photograph out of every person folder: its memberships become REMOVED, the folders are refreshed and a folder left empty is archived.

    A folder the owner named themselves is only refreshed (it is theirs to delete). Each change is written to the folder's history and to the owner's
    activity log, and the folder keeps its id, so a link to it still resolves. A moderator review of the photograph's identity that is still open is closed
    (``moderation.services.close_open_identity_reviews``). Returns ``{"memberships": n, "archived": [folder ids], "closed_reviews": n}``.
    """
    from activity.services import record_activity_event

    from .models import IdentityChange

    memberships = list(
        IdentityMembership.objects.select_for_update(of=("self",))
        .filter(identity_face__image=image).exclude(state__in=[IdentityMembership.State.REMOVED, IdentityMembership.State.REJECTED])
        .select_related("identity_group")
    )
    groups = {}
    for membership in memberships:
        group = membership.identity_group
        before = membership.state
        if group is not None and group.profile_membership_id == membership.pk:
            group.profile_membership = None
            group.save(update_fields=["profile_membership", "updated_at"])
        # A suggestion with no folder (``needs_review``, group optional) cannot become ``removed``: the database requires a folder for that state. ``rejected`` is
        # the inactive state that needs none.
        membership.state = IdentityMembership.State.REMOVED if group is not None else IdentityMembership.State.REJECTED
        membership.save(update_fields=["state", "updated_at"])
        if group is not None:
            groups[group.pk] = group
        IdentityChange.objects.create(
            owner=membership.owner, actor=None, group=group, membership=membership, action=IdentityChange.Action.REMOVED,
            before={"state": before}, after={"state": membership.state}, reason=reason[:240],
        )
    archived = []
    for group in groups.values():
        _refresh_group(group)
        if group.state == IdentityGroup.State.ACTIVE and _folder_is_empty(group) and not folder_is_renamed(group):
            group.state = IdentityGroup.State.ARCHIVED
            group.save(update_fields=["state", "updated_at"])
            archived.append(group.pk)
            IdentityChange.objects.create(
                owner=group.owner, actor=None, group=group, action=IdentityChange.Action.ARCHIVED,
                before={"state": IdentityGroup.State.ACTIVE}, after={"state": group.state}, reason=archive_reason,
            )
    from moderation.services import close_open_identity_reviews

    closed = close_open_identity_reviews(image)          # a review of a membership that no longer exists has nothing left to decide
    if memberships or closed:
        record_activity_event(
            image.owner, "group", "photo_removed_from_folders", object_type="ImageUpload", object_id=image.pk,
            details={"memberships": len(memberships), "archived_folders": archived, "closed_reviews": closed, "reason": reason},
        )
    return {"memberships": len(memberships), "archived": archived, "closed_reviews": closed}


def group_member_photos(group):
    """The owner's uploaded photos currently in ``group`` (the same membership states its centroid uses)."""
    from uploads.models import ImageUpload

    face_ids = group.memberships.filter(state__in=_confirmed_states()).values_list("identity_face__image_id", flat=True)
    return ImageUpload.objects.filter(pk__in=face_ids, owner=group.owner_id, status="uploaded").order_by("-created_at")


def _batch_cancellation_requested(batch_id):
    return UploadBatch.objects.filter(pk=batch_id, cancellation_requested=True).exists()


def _largest_face(image, detector):
    """The biggest confident, alignable face in ``image``, or ``None``."""
    detection = detector.detect(image)
    candidates = [
        (box[2] * box[3], box, landmarks)
        for box, landmarks in zip(detection.boxes, detection.landmarks)
        if landmarks and len(landmarks) == 5
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _area, box, landmarks = candidates[0]
    return box, landmarks


def search_by_face(owner, uploaded_file):
    """Feature 50: the owner's own photos that contain the same face as a query photo.

    Computing an embedding from a freshly uploaded query image is new biometric
    processing (Feature 41), so it is gated behind the same consent check identity
    analysis uses -- not a separate flag. The embedding is computed, compared, and
    discarded within this call: it is never written to a ``protected_*`` field or any
    other persistent store, only consented to as a one-time comparison.

    Reuses ``_choose_group``'s exact similarity function and ``IDENTITY_MATCH_THRESHOLD``
    (not a new algorithm or a new threshold), so "found a match" here means the same
    thing it does everywhere else in this app. Only the owner's own ACTIVE groups are
    ever considered -- never another user's.

    Returns ``{"faces_detected": bool, "matches": [{"group": IdentityGroup, "similarity": float}, ...]}``,
    matches best first, for every group scoring at or above the match threshold
    (usually zero or one group; more than one means the owner's own faces were split
    across groups that have not been merged). ``faces_detected`` is ``False`` only
    when no face was found in the query photo at all, distinct from a face being
    found but matching nothing.
    """
    from privacy.services import biometric_processing_allowed

    if not biometric_processing_allowed(owner):
        raise ConsentRequired("Biometric consent is required before face search.")
    provider = get_identity_provider()
    provider.healthcheck()
    detector = get_face_detector()

    payload = uploaded_file.read()
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValidationError({"query_image": "The image could not be decoded safely."})
    found = _largest_face(image, detector)
    if found is None:
        return {"faces_detected": False, "matches": []}
    box, landmarks = found
    x, y, width, height = box
    crop = provider.prepare_face(image, {"x": x, "y": y, "width": width, "height": height}, landmarks)
    vector = provider.validate_embedding(provider.embed(crop))

    groups = (
        IdentityGroup.objects.filter(
            owner=owner, state=IdentityGroup.State.ACTIVE,
            provider=provider.name, embedding_dimension=provider.dimension,
        )
        .exclude(protected_centroid=b"")
        .order_by("-updated_at")[: settings.IDENTITY_MAX_CANDIDATE_GROUPS]
    )
    scored = sorted(
        (
            (provider.similarity(vector, reveal_embedding(group.protected_centroid, group.embedding_dimension)), group)
            for group in groups
        ),
        key=lambda item: item[0], reverse=True,
    )
    match_threshold = thresholds_for(provider)["match"]
    matches = [{"group": group, "similarity": score} for score, group in scored if score >= match_threshold]
    return {"faces_detected": True, "matches": matches}


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
        if photo_is_rejected(analysis.image):
            # The quality check can finish after this analysis was queued. A rejected photo is not grouped, and one that already was is taken out.
            analysis.status = IdentityAnalysis.Status.CANCELLED
            analysis.failure_reason = "The photo was rejected by the checks, so it is not grouped into a person folder."
            analysis.finished_at = timezone.now()
            analysis.save(update_fields=["status", "failure_reason", "finished_at", "updated_at"])
            remove_photo_from_folders(analysis.image)
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
        prepared, skipped = [], []
        detected = list(analysis.face_validation.faces)
        for index, face in enumerate(detected):
            grouped, why = face_grouping_check(face, len(detected))
            if not grouped:
                skipped.append({"face_index": index, **why})           # too small or too unsure to be a person: no folder
                continue
            if len(prepared) >= settings.IDENTITY_MAX_FACES_PER_IMAGE:
                skipped.append({"face_index": index, "reason": "too_many_faces", "limit": settings.IDENTITY_MAX_FACES_PER_IMAGE})      # the faces are sorted largest first
                continue
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
        # A photo with no detectable face is a normal result, not an error: the
        # analysis completes with zero faces (and clears any faces it held before).

        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=analysis.owner_id)
            locked = IdentityAnalysis.objects.select_for_update().get(pk=analysis.pk)
            if locked.task_id != analysis.task_id:
                return locked
            limits = thresholds_for(provider)
            old_group_ids = list(locked.faces.values_list("memberships__identity_group_id", flat=True))
            locked.faces.all().delete()
            assigned = review = 0
            for index, face_data, vector in prepared:
                face = IdentityFace.objects.create(owner=locked.owner, image=locked.image, analysis=locked, face_index=index, bounding_box=face_data.get("standardized_box") or face_data, landmarks=face_data.get("landmarks") or [], protected_embedding=protect_embedding(vector), embedding_dimension=provider.dimension, provider=provider.name, model_checksum=provider.model_checksum, quality={key: face_data[key] for key in ("confidence", "sharpness", "brightness") if key in face_data})
                decision, group, best, second = _choose_group(locked.owner, provider, vector)
                closest_score = closest_group_id = None
                if decision == "new":
                    # Why a new folder: how close the nearest existing one was (below the new-folder threshold). Kept in evidence only; the
                    # membership's own similarity_score stays empty because the face is not being compared with the folder it starts.
                    closest_score, closest_group_id = _closest_folder(locked.owner, provider, vector)
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
                membership = IdentityMembership.objects.create(owner=locked.owner, identity_face=face, identity_group=group, similarity_score=best, matching_distance=(1 - best if best is not None else None), state=state, decision_method=method, evidence={"best_score": best if best is not None else closest_score, "second_best_score": second, "closest_group_id": closest_group_id, "threshold": limits["match"], "new_group_threshold": limits["new_group"], "ambiguity_margin": limits["margin"]})
                if state == IdentityMembership.State.NEEDS_REVIEW:
                    from moderation.services import create_review_for_identity_membership
                    create_review_for_identity_membership(membership)
                if state in _confirmed_states():
                    _refresh_group(group)
            for group in IdentityGroup.objects.filter(pk__in=[pk for pk in old_group_ids if pk]):
                _refresh_group(group)
            locked.face_count = len(prepared)
            locked.evidence = {"faces_detected": len(detected), "faces_grouped": len(prepared), "skipped_faces": skipped, "limits": limits}
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
    if photo_is_rejected(image):                # refused at upload or rejected by the quality decision: never grouped
        raise ValidationError("A photo the checks rejected is not grouped into a person folder.")
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
        raise ConsentRequired("Biometric consent is required before identity analysis.")
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
