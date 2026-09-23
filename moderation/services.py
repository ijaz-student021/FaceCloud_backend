"""Moderator review workflow (T-007 / Feature 27 core).

Review rows are created next to the pipeline that produced an uncertain
result (see the create_review_for_* functions, called from
identities/quality_control/duplicates services.py) and decided here. Decisions
always update this app's own ModeratorReview row; where writing the decision
back into the *source* record is safe and side-effect-free (quality,
duplicate), it is also applied there. Identity membership write-back is
intentionally not attempted: the only existing write path,
identities.services.review_membership(), is owner-gated by design and has
cascading group-centroid/member-count side effects that deserve a dedicated
change of their own, not a relaxation of a security-relevant filter under
this phase's scope. The moderator's decision is still fully recorded and
audited on the ModeratorReview row either way.
"""
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

from duplicates.models import DuplicateMatch
from organizations.models import OrganizationAuditEvent
from organizations.services import record_audit_event
from quality_control.models import ImageQualityDecision

from .models import ModeratorReview

_QUALITY_WRITEBACK = {
    "approve": ImageQualityDecision.Decision.ACCEPTED,
    "reject": ImageQualityDecision.Decision.REJECTED,
}
_DUPLICATE_WRITEBACK = {
    "approve": DuplicateMatch.ReviewStatus.CONFIRMED,
    "reject": DuplicateMatch.ReviewStatus.NOT_DUPLICATE,
}
_REVIEW_STATUS_FOR_ACTION = {
    "approve": ModeratorReview.Status.APPROVED,
    "reject": ModeratorReview.Status.REJECTED,
    "correct": ModeratorReview.Status.CORRECTED,
    "keep_in_review": ModeratorReview.Status.IN_REVIEW,
}


class ModerationConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "moderation_conflict"
    default_detail = "This review is no longer open for a decision."


def _organization_for_image(image):
    return image.batch.organization


def _get_or_create_review(*, organization, image, source, ai_recommendation, ai_confidence, **source_kwargs):
    if organization is None:
        return None
    defaults = {
        "organization": organization, "image": image, "source": source,
        "ai_recommendation": ai_recommendation, "ai_confidence": ai_confidence,
    }
    review, _ = ModeratorReview.objects.get_or_create(defaults=defaults, **source_kwargs)
    return review


def create_review_for_quality_decision(decision):
    return _get_or_create_review(
        organization=_organization_for_image(decision.image), image=decision.image,
        source=ModeratorReview.Source.QUALITY,
        ai_recommendation=decision.decision, ai_confidence=decision.overall_score,
        quality_decision=decision,
    )


def create_review_for_duplicate_match(match):
    return _get_or_create_review(
        organization=_organization_for_image(match.source_image), image=match.source_image,
        source=ModeratorReview.Source.DUPLICATE,
        ai_recommendation=match.match_type, ai_confidence=match.similarity_score,
        duplicate_match=match,
    )


def create_review_for_identity_membership(membership):
    image = membership.identity_face.image
    return _get_or_create_review(
        organization=_organization_for_image(image), image=image,
        source=ModeratorReview.Source.IDENTITY,
        ai_recommendation=membership.decision_method, ai_confidence=membership.similarity_score,
        identity_membership=membership,
    )


@transaction.atomic
def decide_review(*, review, actor, action, reason="", notes="", request=None):
    if action not in _REVIEW_STATUS_FOR_ACTION:
        raise ValidationError({"action": "Unknown review action."})
    locked = ModeratorReview.objects.select_for_update().get(pk=review.pk)
    if locked.decided_at is not None:
        raise ModerationConflict("This review has already been decided.")
    locked.status = _REVIEW_STATUS_FOR_ACTION[action]
    locked.assigned_moderator = locked.assigned_moderator or actor
    locked.decision_reason = reason
    locked.decision_notes = notes
    if locked.status != ModeratorReview.Status.IN_REVIEW:
        locked.decided_by = actor
        locked.decided_at = timezone.now()
    locked.save()

    if (
        locked.source == ModeratorReview.Source.QUALITY
        and action in _QUALITY_WRITEBACK and locked.quality_decision_id
    ):
        ImageQualityDecision.objects.filter(pk=locked.quality_decision_id).update(
            decision=_QUALITY_WRITEBACK[action], updated_at=timezone.now(),
        )
    elif (
        locked.source == ModeratorReview.Source.DUPLICATE
        and action in _DUPLICATE_WRITEBACK and locked.duplicate_match_id
    ):
        DuplicateMatch.objects.filter(pk=locked.duplicate_match_id).update(
            review_status=_DUPLICATE_WRITEBACK[action], reviewed_by=actor, reviewed_at=timezone.now(),
        )

    record_audit_event(
        locked.organization, actor, OrganizationAuditEvent.Action.MODERATOR_REVIEW_DECIDED,
        target_type="ModeratorReview", target_id=locked.pk,
        new_value={"action": action, "status": locked.status}, reason=reason, request=request,
    )
    return locked


@transaction.atomic
def escalate_review(*, review, actor, reason="", request=None):
    locked = ModeratorReview.objects.select_for_update().get(pk=review.pk)
    if locked.decided_at is not None:
        raise ModerationConflict("This review has already been decided.")
    locked.status = ModeratorReview.Status.ESCALATED
    locked.assigned_moderator = locked.assigned_moderator or actor
    locked.decision_reason = reason
    locked.save(update_fields=["status", "assigned_moderator", "decision_reason", "updated_at"])
    record_audit_event(
        locked.organization, actor, OrganizationAuditEvent.Action.MODERATOR_REVIEW_ESCALATED,
        target_type="ModeratorReview", target_id=locked.pk, reason=reason, request=request,
    )
    return locked


def dashboard_summary(organization):
    today = timezone.localdate()
    reviews = ModeratorReview.objects.filter(organization=organization)
    return {
        "pending": reviews.filter(status=ModeratorReview.Status.PENDING).count(),
        "in_review": reviews.filter(status=ModeratorReview.Status.IN_REVIEW).count(),
        "escalated": reviews.filter(status=ModeratorReview.Status.ESCALATED).count(),
        "completed_today": reviews.filter(decided_at__date=today).count(),
        "approved_total": reviews.filter(status=ModeratorReview.Status.APPROVED).count(),
        "rejected_total": reviews.filter(status=ModeratorReview.Status.REJECTED).count(),
        "corrected_total": reviews.filter(status=ModeratorReview.Status.CORRECTED).count(),
    }
