"""Moderator review workflow (T-007 / Feature 27 core).

Review rows are created next to the pipeline that produced an uncertain
result (see the create_review_for_* functions, called from
identities/quality_control/duplicates services.py) and decided here. Decisions
always update this app's own ModeratorReview row; where writing the decision
back into the *source* record is safe, it is also applied there -- including
identity membership decisions (identities.services.review_membership()),
which was deferred when this app first shipped because that function's
owner=reviewer lookup is a real authorization gate. It still is: this module
never relaxes it. Instead review_membership() now accepts the membership's
actual owner as ``reviewer`` (so group ownership, candidate-group scoping and
IdentityChange.owner all stay correctly attributed to the content's real
owner) and the deciding moderator as a separate ``actor`` (used for
reviewed_by and IdentityChange.actor). See review_membership()'s own
docstring for the full reasoning.
"""
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

from duplicates.models import DuplicateMatch
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import get_active_membership, record_audit_event
from quality_control.models import ImageQualityDecision
from uploads.services import cancel_batch, retry_batch

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


def _identity_review_action(action, group_id):
    """The identities.services.review_membership() action for a moderator decision.

    "approve" confirms whatever group _choose_group already suggested;
    "reject" rejects the assignment outright; "correct" picks a specific
    different group when one is given, or starts a brand-new group
    otherwise. "keep_in_review" has no membership-level effect -- it only
    holds the ModeratorReview open.
    """
    if action == "approve":
        return "confirm"
    if action == "reject":
        return "reject"
    if action == "correct":
        return "select_group" if group_id else "new_group"
    return None


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
def decide_review(*, review, actor, action, reason="", notes="", group_id=None, request=None):
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
    elif locked.source == ModeratorReview.Source.IDENTITY and locked.identity_membership_id:
        membership_action = _identity_review_action(action, group_id)
        if membership_action is not None:
            from identities.services import review_membership
            membership = locked.identity_membership
            review_membership(membership, membership.owner, membership_action, group_id, actor=actor)

    record_audit_event(
        locked.organization, actor, OrganizationAuditEvent.Action.MODERATOR_REVIEW_DECIDED,
        target_type="ModeratorReview", target_id=locked.pk,
        new_value={"action": action, "status": locked.status}, reason=reason, request=request,
    )
    return locked


@transaction.atomic
def escalate_review(*, review, actor, reason="", request=None):
    from notifications.events import notify_moderator_review_escalated

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
    transaction.on_commit(lambda: notify_moderator_review_escalated(locked))
    return locked


@transaction.atomic
def assign_review(*, review, actor, moderator, request=None):
    """Assign a review to a specific org member with the owner/moderator role.

    Separate from the self-assignment that already happens as a side effect
    of decide_review()/escalate_review() -- this is the explicit "give this
    review to someone" action, which is also what makes a "you were
    assigned a review" notification meaningful (notifying yourself that you
    picked something up is not).
    """
    from notifications.events import notify_moderator_review_assigned

    locked = ModeratorReview.objects.select_for_update().get(pk=review.pk)
    if locked.decided_at is not None:
        raise ModerationConflict("This review has already been decided.")
    membership = get_active_membership(moderator, locked.organization_id)
    if membership is None or membership.role not in (
        OrganizationMembership.Role.OWNER, OrganizationMembership.Role.MODERATOR,
    ):
        raise ValidationError({"moderator_id": "This user is not an owner or moderator of this organization."})
    locked.assigned_moderator = moderator
    locked.save(update_fields=["assigned_moderator", "updated_at"])
    record_audit_event(
        locked.organization, actor, OrganizationAuditEvent.Action.MODERATOR_REVIEW_ASSIGNED,
        target_type="ModeratorReview", target_id=locked.pk,
        new_value={"assigned_moderator": moderator.pk}, request=request,
    )
    transaction.on_commit(lambda: notify_moderator_review_assigned(locked))
    return locked


def retry_batch_for_organization(*, batch, actor, request=None):
    """Retry a batch on a moderator's behalf, reusing uploads.services.retry_batch exactly.

    Raises BatchHasNoFailedImages/BatchRetryLimitReached (both ValidationError
    subclasses) unchanged -- the caller (the view) decides how to present
    those, same as the existing admin retry endpoint.
    """
    updated = retry_batch(batch)
    record_audit_event(
        updated.organization, actor, OrganizationAuditEvent.Action.BATCH_RETRIED_BY_MODERATOR,
        target_type="UploadBatch", target_id=updated.pk, request=request,
    )
    return updated


def cancel_batch_for_organization(*, batch, actor, request=None):
    updated = cancel_batch(batch)
    record_audit_event(
        updated.organization, actor, OrganizationAuditEvent.Action.BATCH_CANCELLED_BY_MODERATOR,
        target_type="UploadBatch", target_id=updated.pk, request=request,
    )
    return updated


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
