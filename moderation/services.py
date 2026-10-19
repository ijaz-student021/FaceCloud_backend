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
import uuid
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Max, Min, Q, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

from duplicates.models import DuplicateMatch
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.scope import visible_audit_events
from organizations.services import get_active_membership, record_audit_event
from quality_control.models import ImageQualityDecision
from uploads.models import UploadBatch
from uploads.services import cancel_batch, retry_batch

from .models import ModeratorReport, ModeratorReview

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


class ReviewClaimedByAnother(ModerationConflict):
    default_code = "review_claimed"
    default_detail = "Another moderator is reviewing this right now."


def _reject_if_held_by_another(locked, actor):
    """Stop one moderator writing a decision over another's open claim.

    Only a live claim blocks: an expired one is no claim at all, so work never
    becomes permanently unreachable because somebody forgot to release it.
    """
    if locked.is_claimed_by_other(actor):
        raise ReviewClaimedByAnother(
            f"{locked.claimed_by.username} is reviewing this right now. "
            "Their claim expires shortly if they do not finish."
        )


def _organization_for_image(image):
    return image.batch.organization


def review_deadline(from_time=None):
    """When a review created now must be decided by.

    Computed here rather than as a model default so that the deadline of an
    existing row never moves: a review keeps the deadline it was given even if
    MODERATION_SLA_HOURS is changed afterwards.
    """
    return (from_time or timezone.now()) + timedelta(hours=settings.MODERATION_SLA_HOURS)


def _get_or_create_review(*, organization, image, source, ai_recommendation, ai_confidence, **source_kwargs):
    if organization is None:
        return None
    defaults = {
        "organization": organization, "image": image, "source": source,
        "ai_recommendation": ai_recommendation, "ai_confidence": ai_confidence,
        "due_at": review_deadline(),
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


def _as_percentage(fraction):
    """An identity similarity (0-1) as the percentage ModeratorReview stores.

    Quality scores and duplicate similarities are already 0-100; identity
    matching works in fractions. Converting here keeps ModeratorReview's
    ai_confidence one unit, so a queue can render it without first asking which
    of the three sources it came from.
    """
    return None if fraction is None else fraction * 100


def create_review_for_identity_membership(membership):
    image = membership.identity_face.image
    return _get_or_create_review(
        organization=_organization_for_image(image), image=image,
        source=ModeratorReview.Source.IDENTITY,
        ai_recommendation=membership.decision_method,
        ai_confidence=_as_percentage(membership.similarity_score),
        identity_membership=membership,
    )


@transaction.atomic
def decide_review(*, review, actor, action, reason="", notes="", group_id=None, request=None):
    if action not in _REVIEW_STATUS_FOR_ACTION:
        raise ValidationError({"action": "Unknown review action."})
    locked = ModeratorReview.objects.select_for_update().get(pk=review.pk)
    if locked.decided_at is not None:
        raise ModerationConflict("This review has already been decided.")
    _reject_if_held_by_another(locked, actor)
    previous_status = locked.status
    locked.status = _REVIEW_STATUS_FOR_ACTION[action]
    locked.assigned_moderator = locked.assigned_moderator or actor
    locked.decision_reason = reason
    locked.decision_notes = notes
    if locked.status != ModeratorReview.Status.IN_REVIEW:
        locked.decided_by = actor
        locked.decided_at = timezone.now()
        # A decided review is nobody's work in progress any more. "Hold" keeps
        # the claim, because holding it is the point of that action.
        locked.claimed_by = None
        locked.claimed_at = None
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
        previous_value={"status": previous_status},
        new_value={"action": action, "status": locked.status}, reason=reason, request=request,
    )
    return locked


OPEN_REVIEW_STATUSES = (
    ModeratorReview.Status.PENDING, ModeratorReview.Status.IN_REVIEW, ModeratorReview.Status.ESCALATED,
)


def release_open_reviews(*, organization, user):
    """Take a member's undecided reviews back into the queue; return how many.

    Called when someone stops being able to moderate (removed, suspended or
    demoted), so their reviews are not left assigned to a person who can no
    longer act on them. A review that was being worked on returns to
    ``pending``; an escalated one keeps its status so it still reaches an owner.
    Decided reviews are history and are never touched.

    Any claim the same person was holding is dropped as well, including on
    reviews that were never assigned to them -- a claim would otherwise keep
    other moderators off a review for its full lifetime even though the person
    holding it can no longer act on it.
    """
    queryset = ModeratorReview.objects.filter(
        organization=organization, assigned_moderator=user, status__in=OPEN_REVIEW_STATUSES,
    )
    now = timezone.now()
    with transaction.atomic():
        ModeratorReview.objects.filter(
            pk__in=queryset.filter(status=ModeratorReview.Status.IN_REVIEW).values("pk"),
        ).update(status=ModeratorReview.Status.PENDING, updated_at=now)
        ModeratorReview.objects.filter(
            organization=organization, claimed_by=user, status__in=OPEN_REVIEW_STATUSES,
        ).update(claimed_by=None, claimed_at=None, updated_at=now)
        return queryset.update(assigned_moderator=None, updated_at=now)


@transaction.atomic
def escalate_review(*, review, actor, reason="", request=None):
    from notifications.events import notify_moderator_review_escalated

    locked = ModeratorReview.objects.select_for_update().get(pk=review.pk)
    if locked.decided_at is not None:
        raise ModerationConflict("This review has already been decided.")
    _reject_if_held_by_another(locked, actor)
    locked.status = ModeratorReview.Status.ESCALATED
    locked.assigned_moderator = locked.assigned_moderator or actor
    locked.decision_reason = reason
    # Escalating hands the review on to an owner, so it stops being held here.
    locked.claimed_by = None
    locked.claimed_at = None
    locked.save(update_fields=[
        "status", "assigned_moderator", "decision_reason", "claimed_by", "claimed_at", "updated_at",
    ])
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
    from organizations.scope import DataScope

    if not DataScope(locked.organization, moderator, membership).sees(locked.image.owner_id):
        # Giving a review to a moderator who may not see that member's photos would hand them the very data the scope keeps from them.
        raise ValidationError({"moderator_id": "This moderator does not see that member's photographs."})
    locked.assigned_moderator = moderator
    locked.save(update_fields=["assigned_moderator", "updated_at"])
    record_audit_event(
        locked.organization, actor, OrganizationAuditEvent.Action.MODERATOR_REVIEW_ASSIGNED,
        target_type="ModeratorReview", target_id=locked.pk,
        new_value={"assigned_moderator": moderator.pk}, request=request,
    )
    transaction.on_commit(lambda: notify_moderator_review_assigned(locked))
    return locked


@transaction.atomic
def claim_review(*, review, actor, request=None):
    """Take a review as the one you are working on now.

    This is the soft lock that stops two moderators spending their time on the
    same photo and then racing to record contradictory decisions. It is
    deliberately not the same thing as assignment: assignment is a standing
    allocation someone else can make for you, a claim is you saying "I have
    this open" and lasts MODERATION_CLAIM_MINUTES.

    Claiming an already-claimed review is a 409 rather than a silent takeover,
    and re-claiming your own review simply extends it, so returning to a tab
    you left open does not fail.
    """
    locked = ModeratorReview.objects.select_for_update().get(pk=review.pk)
    if locked.decided_at is not None:
        raise ModerationConflict("This review has already been decided.")
    _reject_if_held_by_another(locked, actor)
    locked.claimed_by = actor
    locked.claimed_at = timezone.now()
    locked.assigned_moderator = locked.assigned_moderator or actor
    # Picking a review up is what "in review" means. An escalated review keeps
    # its status so it stays visible to owners as escalated while being worked.
    if locked.status == ModeratorReview.Status.PENDING:
        locked.status = ModeratorReview.Status.IN_REVIEW
    locked.save(update_fields=[
        "claimed_by", "claimed_at", "assigned_moderator", "status", "updated_at",
    ])
    record_audit_event(
        locked.organization, actor, OrganizationAuditEvent.Action.MODERATOR_REVIEW_CLAIMED,
        target_type="ModeratorReview", target_id=locked.pk,
        new_value={"claimed_until": locked.claim_expires_at.isoformat()}, request=request,
    )
    return locked


@transaction.atomic
def release_review_claim(*, review, actor, is_owner=False, request=None):
    """Give a claimed review back to the queue.

    The moderator holding it can always release it. An owner can release
    somebody else's claim too, which is the manual way out of the case the
    expiry already handles automatically. Anyone else gets a 409 rather than
    silently doing nothing, so a stale screen does not look like it worked.
    """
    locked = ModeratorReview.objects.select_for_update().get(pk=review.pk)
    if locked.claimed_by_id is None:
        raise ModerationConflict("This review is not claimed.")
    if locked.claimed_by_id != actor.pk and not is_owner:
        raise ReviewClaimedByAnother(
            f"{locked.claimed_by.username} holds this review; only they or an "
            "organization owner can release it."
        )
    previous_holder = locked.claimed_by_id
    locked.claimed_by = None
    locked.claimed_at = None
    if locked.status == ModeratorReview.Status.IN_REVIEW:
        locked.status = ModeratorReview.Status.PENDING
    locked.save(update_fields=["claimed_by", "claimed_at", "status", "updated_at"])
    record_audit_event(
        locked.organization, actor, OrganizationAuditEvent.Action.MODERATOR_REVIEW_RELEASED,
        target_type="ModeratorReview", target_id=locked.pk,
        previous_value={"claimed_by": previous_holder}, request=request,
    )
    return locked


BULK_ACTIONS = ("approve", "reject", "escalate")


def bulk_decide_reviews(*, organization, actor, review_ids, action, reason="", notes="", request=None, scope=None):
    """Apply one decision to many reviews, reporting each one's outcome.

    Only approve, reject and escalate are offered. "Correct" is left out on
    purpose: correcting an identity review means choosing which person folder
    the face belongs to, which is a per-photo judgement and cannot honestly be
    answered once for a selection.

    Each review is decided in its own transaction by the same decide_review()
    the single-review endpoint uses, so write-back, the audit trail and the
    claim lock all behave identically. One review that cannot be decided --
    already decided, or held by another moderator -- is reported in ``failed``
    and does not roll back the rest, because a moderator who selected thirty
    photos should not lose twenty-nine of them to one stale row.
    """
    if action not in BULK_ACTIONS:
        raise ValidationError({"action": f"Bulk actions are limited to {', '.join(BULK_ACTIONS)}."})
    unique_ids = list(dict.fromkeys(review_ids))
    if not unique_ids:
        raise ValidationError({"review_ids": "Select at least one review."})
    if len(unique_ids) > settings.MODERATION_BULK_MAX_REVIEWS:
        raise ValidationError({
            "review_ids": f"At most {settings.MODERATION_BULK_MAX_REVIEWS} reviews can be decided at once.",
        })

    found = {
        review.pk: review
        for review in organization_reviews(organization, scope).filter(pk__in=unique_ids)
    }
    succeeded, failed = [], []
    for review_id in unique_ids:
        review = found.get(review_id)
        if review is None:
            # Not 404: the request as a whole is valid, this one row is not
            # part of this organization (or no longer exists at all).
            failed.append({"review_id": review_id, "detail": "No such review in this organization."})
            continue
        try:
            if action == "escalate":
                decided = escalate_review(review=review, actor=actor, reason=reason, request=request)
            else:
                decided = decide_review(
                    review=review, actor=actor, action=action, reason=reason, notes=notes, request=request,
                )
        except (ModerationConflict, ValidationError) as error:
            failed.append({"review_id": review_id, "detail": _error_detail(error)})
        else:
            succeeded.append(decided.pk)

    record_audit_event(
        organization, actor, OrganizationAuditEvent.Action.MODERATOR_BULK_DECISION,
        target_type="ModeratorReview",
        new_value={
            "action": action, "requested": len(unique_ids),
            "succeeded": succeeded, "failed_count": len(failed),
        },
        reason=reason, request=request,
    )
    return {
        "action": action,
        "requested": len(unique_ids),
        "succeeded": succeeded,
        "succeeded_count": len(succeeded),
        "failed": failed,
        "failed_count": len(failed),
    }


def _error_detail(error):
    """One readable sentence out of a DRF exception, whatever shape it carries."""
    detail = getattr(error, "detail", None)
    if isinstance(detail, dict):
        for value in detail.values():
            if isinstance(value, list) and value:
                return str(value[0])
            return str(value)
    if isinstance(detail, list) and detail:
        return str(detail[0])
    return str(detail) if detail else str(error)


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


def organization_reviews(organization, scope=None):
    """The organization's reviews, without those of members ``scope`` (a ``DataScope``) says the viewer may not see."""
    reviews = ModeratorReview.objects.filter(organization=organization)
    return scope.exclude(reviews, "image__owner_id") if scope is not None else reviews


def open_reviews(organization, scope=None):
    """Every review in the organization still waiting for a decision (that the viewer may see)."""
    return organization_reviews(organization, scope).filter(
        status__in=OPEN_REVIEW_STATUSES, decided_at__isnull=True,
    )


def overdue_reviews(organization, now=None, scope=None):
    now = now or timezone.now()
    return open_reviews(organization, scope).filter(due_at__isnull=False, due_at__lt=now)


def dashboard_summary(organization, scope=None):
    today = timezone.localdate()
    now = timezone.now()
    reviews = organization_reviews(organization, scope)
    waiting = open_reviews(organization, scope)
    due_soon_cutoff = now + timedelta(hours=settings.MODERATION_SLA_DUE_SOON_HOURS)
    oldest_created = waiting.aggregate(oldest=Min("created_at"))["oldest"]
    return {
        "pending": reviews.filter(status=ModeratorReview.Status.PENDING).count(),
        "in_review": reviews.filter(status=ModeratorReview.Status.IN_REVIEW).count(),
        "escalated": reviews.filter(status=ModeratorReview.Status.ESCALATED).count(),
        "completed_today": reviews.filter(decided_at__date=today).count(),
        "approved_total": reviews.filter(status=ModeratorReview.Status.APPROVED).count(),
        "rejected_total": reviews.filter(status=ModeratorReview.Status.REJECTED).count(),
        "corrected_total": reviews.filter(status=ModeratorReview.Status.CORRECTED).count(),
        # Service levels. "Due soon" excludes anything already overdue so the
        # two numbers can be read side by side without double-counting.
        "overdue": waiting.filter(due_at__isnull=False, due_at__lt=now).count(),
        "due_soon": waiting.filter(
            due_at__isnull=False, due_at__gte=now, due_at__lt=due_soon_cutoff,
        ).count(),
        "unassigned": waiting.filter(assigned_moderator__isnull=True).count(),
        "claimed_now": waiting.filter(
            claimed_by__isnull=False,
            claimed_at__gt=now - timedelta(minutes=settings.MODERATION_CLAIM_MINUTES),
        ).count(),
        "oldest_open_age_hours": (
            round((now - oldest_created).total_seconds() / 3600, 2) if oldest_created else None
        ),
        "sla_hours": settings.MODERATION_SLA_HOURS,
    }


# -- Per-moderator performance (T-009) ---------------------------------------


def _decision_latency_expression():
    """How long a review took, as a database-side duration.

    Measured from creation rather than from assignment or claim: what matters
    to whoever uploaded the photo is how long they waited, not how much of
    that wait was queueing.
    """
    return ExpressionWrapper(F("decided_at") - F("created_at"), output_field=DurationField())


def _hours(duration):
    return round(duration.total_seconds() / 3600, 2) if duration is not None else None


def moderator_metrics(organization, days=30, now=None, scope=None):
    """Throughput, speed and timeliness per moderator over a window.

    Counts only decided reviews, because an undecided one says nothing about
    the person it is assigned to yet; the open queue is the dashboard's job.
    ``on_time`` means decided before the review's deadline -- a review with no
    deadline (created before deadlines existed) counts as neither on time nor
    late, so the two never add up to more than the total but are never wrong.
    """
    now = now or timezone.now()
    since = now - timedelta(days=days)
    decided = organization_reviews(organization, scope).filter(decided_by__isnull=False, decided_at__gte=since)
    latency = _decision_latency_expression()
    rows = []
    per_moderator = (
        decided.values("decided_by_id", "decided_by__username")
        .annotate(
            decided_count=Count("id"),
            approved=Count("id", filter=Q(status=ModeratorReview.Status.APPROVED)),
            rejected=Count("id", filter=Q(status=ModeratorReview.Status.REJECTED)),
            corrected=Count("id", filter=Q(status=ModeratorReview.Status.CORRECTED)),
            on_time=Count("id", filter=Q(due_at__isnull=False, decided_at__lte=F("due_at"))),
            late=Count("id", filter=Q(due_at__isnull=False, decided_at__gt=F("due_at"))),
            average_decision_time=Avg(latency),
            slowest_decision_time=Max(latency),
        )
        .order_by("-decided_count", "decided_by__username")
    )
    for row in per_moderator:
        measured = row["on_time"] + row["late"]
        rows.append({
            "moderator_id": row["decided_by_id"],
            "username": row["decided_by__username"],
            "decided_count": row["decided_count"],
            "approved": row["approved"],
            "rejected": row["rejected"],
            "corrected": row["corrected"],
            "on_time": row["on_time"],
            "late": row["late"],
            "on_time_rate": round(row["on_time"] / measured, 4) if measured else None,
            "average_decision_hours": _hours(row["average_decision_time"]),
            "slowest_decision_hours": _hours(row["slowest_decision_time"]),
        })

    totals = decided.aggregate(
        decided_count=Count("id"),
        on_time=Count("id", filter=Q(due_at__isnull=False, decided_at__lte=F("due_at"))),
        late=Count("id", filter=Q(due_at__isnull=False, decided_at__gt=F("due_at"))),
        average_decision_time=Avg(latency),
    )
    measured_total = totals["on_time"] + totals["late"]
    return {
        "window_days": days,
        "since": since.isoformat(),
        "sla_hours": settings.MODERATION_SLA_HOURS,
        "moderators": rows,
        "totals": {
            "decided_count": totals["decided_count"],
            "on_time": totals["on_time"],
            "late": totals["late"],
            "on_time_rate": round(totals["on_time"] / measured_total, 4) if measured_total else None,
            "average_decision_hours": _hours(totals["average_decision_time"]),
            "open_now": open_reviews(organization, scope).count(),
            "overdue_now": overdue_reviews(organization, now, scope).count(),
        },
    }


def flag_overdue_reviews(now=None):
    """Report every open review that has passed its deadline, once each.

    ``sla_breached_at`` is stamped in the same pass, so a review that stays
    late for days alerts on the first run and then stops. Run by the
    moderation.flag_sla_breaches beat task.
    """
    from notifications.events import notify_moderator_review_overdue

    now = now or timezone.now()
    breached = ModeratorReview.objects.filter(
        status__in=OPEN_REVIEW_STATUSES, decided_at__isnull=True,
        due_at__isnull=False, due_at__lt=now, sla_breached_at__isnull=True,
    ).select_related("assigned_moderator")
    flagged = 0
    for review in breached.iterator():
        # Stamped first, and only the still-unstamped row is updated, so two
        # overlapping runs of the beat task cannot both notify for one review.
        claimed = ModeratorReview.objects.filter(
            pk=review.pk, sla_breached_at__isnull=True,
        ).update(sla_breached_at=now, updated_at=now)
        if not claimed:
            continue
        review.sla_breached_at = now
        notify_moderator_review_overdue(review)
        flagged += 1
    return flagged


# -- Org-scoped reports (T-007 Phase F) --------------------------------------
#
# reports/renderers.py's CSV/PDF rendering is hard-coded to a per-image row
# shape, so these reports have their own renderer (moderation/renderers.py) and
# share one {summary, rows} shape. Six types: review summary, moderator
# activity, rejection reasons, quality scores, batches and duplicate decisions.
# None of them reads facial data: they use decisions, scores, counts and the
# hash-based duplicate match type, never embeddings, identity groups or
# face-similarity values.

REPORT_ROW_LIMIT = 200

_ORGANIZATION_AUDIT_ACTIONS_FOR_ACTIVITY = [
    OrganizationAuditEvent.Action.MODERATOR_REVIEW_DECIDED,
    OrganizationAuditEvent.Action.MODERATOR_REVIEW_ESCALATED,
    OrganizationAuditEvent.Action.MODERATOR_REVIEW_ASSIGNED,
    OrganizationAuditEvent.Action.BATCH_RETRIED_BY_MODERATOR,
    OrganizationAuditEvent.Action.BATCH_CANCELLED_BY_MODERATOR,
]


def report_period_key(date_from, date_to):
    """Names a reporting window for deduplication: "all" when unbounded."""
    if date_from is None and date_to is None:
        return "all"
    stamp = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else "-"  # noqa: E731
    return f"{stamp(date_from)}/{stamp(date_to)}"


def _in_period(queryset, field, period):
    """Rows whose ``field`` falls in the half-open UTC window ``period``."""
    if period is None:
        return queryset
    start, end = period
    if start is not None:
        queryset = queryset.filter(**{f"{field}__gte": start})
    if end is not None:
        queryset = queryset.filter(**{f"{field}__lt": end})
    return queryset


def _period_description(period, field):
    start, end = period or (None, None)
    return {
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
        "timezone": "UTC",
        "bounds": "from inclusive, to exclusive",
        "filtered_on": field,
    }


def build_review_summary_payload(organization, period=None, scope=None):
    reviews = _in_period(organization_reviews(organization, scope), "created_at", period)
    rows = list(
        reviews.order_by("-created_at").values(
            "id", "source", "status", "ai_recommendation", "ai_confidence",
            "decision_reason", "decided_at", "created_at",
        )[:200]
    )
    # JSONField has no custom encoder here (matches reports/models.py's
    # ProcessingReport.payload), so every datetime must already be a string
    # before this dict is assigned to a payload field -- reports/services.py's
    # own build_report_payload follows the same .isoformat() convention.
    for row in rows:
        row["decided_at"] = row["decided_at"].isoformat() if row["decided_at"] else None
        row["created_at"] = row["created_at"].isoformat()
    return {
        "format_version": 1,
        "report_type": ModeratorReport.ReportType.REVIEW_SUMMARY,
        "generated_at": timezone.now().isoformat(),
        "organization": {"id": organization.pk, "name": organization.name},
        "period": _period_description(period, "review created_at"),
        "summary": {
            "total_reviews": reviews.count(),
            "by_status": dict(Counter(reviews.values_list("status", flat=True))),
            "by_source": dict(Counter(reviews.values_list("source", flat=True))),
        },
        "rows": rows,
        "row_count": len(rows),
    }


def build_moderator_activity_payload(organization, period=None, scope=None):
    decided = _in_period(
        organization_reviews(organization, scope).filter(decided_by__isnull=False), "decided_at", period,
    )
    per_moderator = list(
        decided.values("decided_by_id", "decided_by__username")
        .annotate(
            total=Count("id"),
            approved=Count("id", filter=Q(status=ModeratorReview.Status.APPROVED)),
            rejected=Count("id", filter=Q(status=ModeratorReview.Status.REJECTED)),
            corrected=Count("id", filter=Q(status=ModeratorReview.Status.CORRECTED)),
        )
        .order_by("-total")
    )
    rows = list(
        _in_period(visible_audit_events(OrganizationAuditEvent.objects.filter(
            organization=organization, action__in=_ORGANIZATION_AUDIT_ACTIONS_FOR_ACTIVITY,
        ), organization, scope), "created_at", period)
        .order_by("-created_at")
        .values("actor_id", "actor__username", "action", "target_type", "target_id", "reason", "created_at")[:200]
    )
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
    return {
        "format_version": 1,
        "report_type": ModeratorReport.ReportType.MODERATOR_ACTIVITY,
        "generated_at": timezone.now().isoformat(),
        "organization": {"id": organization.pk, "name": organization.name},
        "period": _period_description(period, "review decided_at; audit event created_at"),
        "summary": {"moderators": per_moderator},
        "rows": rows,
        "row_count": len(rows),
    }


def _report_envelope(organization, report_type, summary, rows, period=None, filtered_on=""):
    return {
        "format_version": 1,
        "report_type": report_type,
        "generated_at": timezone.now().isoformat(),
        "organization": {"id": organization.pk, "name": organization.name},
        "period": _period_description(period, filtered_on),
        "summary": summary,
        "rows": rows,
        "row_count": len(rows),
    }


def _organization_quality_decisions(organization, scope=None):
    decisions = ImageQualityDecision.objects.filter(
        image__batch__organization=organization, status=ImageQualityDecision.Status.COMPLETED,
    )
    return scope.exclude(decisions, "image__owner_id") if scope is not None else decisions


def build_rejection_reasons_payload(organization, period=None, scope=None):
    """Why photos were rejected or sent to review, most common reason first."""
    flagged = _in_period(_organization_quality_decisions(organization, scope), "updated_at", period).filter(
        decision__in=[ImageQualityDecision.Decision.REJECTED, ImageQualityDecision.Decision.NEEDS_REVIEW],
    )
    reasons = Counter(
        reason
        for reason_list in flagged.values_list("rejection_reasons", flat=True)
        for reason in (reason_list or [])
        if isinstance(reason, str)
    )
    rows = [
        {
            "image_id": row["image_id"], "decision": row["decision"],
            "reasons": ", ".join(str(item) for item in (row["rejection_reasons"] or [])),
            "decided_at": row["updated_at"].isoformat(),
        }
        for row in flagged.order_by("-updated_at").values(
            "image_id", "decision", "rejection_reasons", "updated_at",
        )[:REPORT_ROW_LIMIT]
    ]
    summary = {
        "flagged_photos": flagged.count(),
        "by_decision": dict(Counter(flagged.values_list("decision", flat=True))),
        "reasons": dict(reasons.most_common()),
    }
    return _report_envelope(organization, ModeratorReport.ReportType.REJECTION_REASONS, summary, rows,
                            period, "quality decision updated_at")


QUALITY_SCORE_BUCKETS = ((0, 20), (20, 40), (40, 60), (60, 80), (80, 101))
QUALITY_SUBSCORES = (
    "brightness_score", "sharpness_score", "contrast_score", "noise_score", "exposure_score", "resolution_score",
)


def _rounded(value):
    return round(value, 1) if value is not None else None


def build_quality_scores_payload(organization, period=None, scope=None):
    """How good the organization's photos are: averages and a score distribution."""
    decisions = _in_period(
        _organization_quality_decisions(organization, scope), "updated_at", period,
    ).filter(overall_score__isnull=False)
    averages = decisions.aggregate(overall=Avg("overall_score"), **{name: Avg(name) for name in QUALITY_SUBSCORES})
    buckets = decisions.aggregate(**{
        f"b{low}": Count("id", filter=Q(overall_score__gte=low, overall_score__lt=high))
        for low, high in QUALITY_SCORE_BUCKETS
    })
    rows = [
        {
            "image_id": row["image_id"], "decision": row["decision"],
            **{name: _rounded(row[name]) for name in ("overall_score", *QUALITY_SUBSCORES)},
            "scored_at": row["updated_at"].isoformat(),
        }
        for row in decisions.order_by("-updated_at").values(
            "image_id", "decision", "overall_score", *QUALITY_SUBSCORES, "updated_at",
        )[:REPORT_ROW_LIMIT]
    ]
    summary = {
        "scored_photos": decisions.count(),
        "mean_overall_score": _rounded(averages["overall"]),
        "mean_subscores": {name: _rounded(averages[name]) for name in QUALITY_SUBSCORES},
        "score_distribution": {f"{low}-{min(high, 100)}": buckets[f"b{low}"] for low, high in QUALITY_SCORE_BUCKETS},
        "by_decision": dict(Counter(decisions.values_list("decision", flat=True))),
    }
    return _report_envelope(organization, ModeratorReport.ReportType.QUALITY_SCORES, summary, rows,
                            period, "quality decision updated_at")


def build_batches_payload(organization, period=None, scope=None):
    """The organization's upload batches and how they finished."""
    batches = UploadBatch.objects.filter(organization=organization)
    batches = _in_period(scope.exclude(batches) if scope is not None else batches, "created_at", period)
    totals = batches.aggregate(
        files=Sum("total_files"), accepted=Sum("accepted_files"), rejected=Sum("rejected_files"),
        processing_failed=Sum("processing_failed_files"), retries=Sum("retry_count"),
    )
    rows = [
        {
            "batch_id": row["id"], "upload_type": row["upload_type"], "processing_status": row["processing_status"],
            "total_files": row["total_files"], "accepted_files": row["accepted_files"],
            "rejected_files": row["rejected_files"], "processing_failed_files": row["processing_failed_files"],
            "retry_count": row["retry_count"], "created_at": row["created_at"].isoformat(),
        }
        for row in batches.order_by("-created_at").values(
            "id", "upload_type", "processing_status", "total_files", "accepted_files", "rejected_files",
            "processing_failed_files", "retry_count", "created_at",
        )[:REPORT_ROW_LIMIT]
    ]
    summary = {
        "total_batches": batches.count(),
        "by_processing_status": dict(Counter(batches.values_list("processing_status", flat=True))),
        "total_files": totals["files"] or 0,
        "accepted_files": totals["accepted"] or 0,
        "rejected_files": totals["rejected"] or 0,
        "processing_failed_files": totals["processing_failed"] or 0,
        "retries": totals["retries"] or 0,
    }
    return _report_envelope(organization, ModeratorReport.ReportType.BATCHES, summary, rows,
                            period, "batch created_at")


def build_duplicate_decisions_payload(organization, period=None, scope=None):
    """Suspected duplicates and what was decided about them (hash-based, no faces)."""
    matches = DuplicateMatch.objects.filter(source_image__batch__organization=organization)
    matches = _in_period(scope.exclude(matches, "source_image__owner_id") if scope is not None else matches, "created_at", period)
    rows = [
        {
            "match_id": row["id"], "source_image_id": row["source_image_id"],
            "matched_image_id": row["matched_image_id"], "match_type": row["match_type"],
            "similarity_score": round(row["similarity_score"], 3), "review_status": row["review_status"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in matches.order_by("-created_at").values(
            "id", "source_image_id", "matched_image_id", "match_type", "similarity_score",
            "review_status", "created_at",
        )[:REPORT_ROW_LIMIT]
    ]
    summary = {
        "total_matches": matches.count(),
        "by_review_status": dict(Counter(matches.values_list("review_status", flat=True))),
        "by_match_type": dict(Counter(matches.values_list("match_type", flat=True))),
        "awaiting_decision": matches.filter(review_status=DuplicateMatch.ReviewStatus.SUSPECTED).count(),
    }
    return _report_envelope(organization, ModeratorReport.ReportType.DUPLICATE_DECISIONS, summary, rows,
                            period, "duplicate match created_at")


def build_general_activity_payload(organization, period=None, scope=None):
    """Everything recorded in the organization's audit trail, plus period totals.

    Rows are audit events (who did what to which member, review or batch);
    the summary counts them by action and by actor and adds how many batches,
    images and reviews were created in the window. Names of the organization's
    own members appear, as they do in its audit log; no image content or
    facial data.
    """
    events = _in_period(
        visible_audit_events(OrganizationAuditEvent.objects.filter(organization=organization), organization, scope), "created_at", period,
    )
    rows = [
        {
            "event_id": row["id"], "action": row["action"], "actor_id": row["actor_id"],
            "actor": row["actor__username"] or "", "target_type": row["target_type"],
            "target_id": row["target_id"], "reason": row["reason"], "created_at": row["created_at"].isoformat(),
        }
        for row in events.order_by("-created_at", "-pk").values(
            "id", "action", "actor_id", "actor__username", "target_type", "target_id", "reason", "created_at",
        )[:REPORT_ROW_LIMIT]
    ]
    batches = UploadBatch.objects.filter(organization=organization)
    batches = _in_period(scope.exclude(batches) if scope is not None else batches, "created_at", period)
    summary = {
        "audit_events": events.count(),
        "by_action": dict(Counter(events.values_list("action", flat=True)).most_common()),
        "by_actor": dict(Counter(
            name or "(system)" for name in events.values_list("actor__username", flat=True)
        ).most_common()),
        "batches_created": batches.count(),
        "images_uploaded": sum(batches.values_list("total_files", flat=True)),
        "reviews_created": _in_period(organization_reviews(organization, scope), "created_at", period).count(),
    }
    return _report_envelope(organization, ModeratorReport.ReportType.GENERAL_ACTIVITY, summary, rows,
                            period, "audit event created_at; batch and review created_at")


_REPORT_PAYLOAD_BUILDERS = {
    ModeratorReport.ReportType.REVIEW_SUMMARY: build_review_summary_payload,
    ModeratorReport.ReportType.MODERATOR_ACTIVITY: build_moderator_activity_payload,
    ModeratorReport.ReportType.REJECTION_REASONS: build_rejection_reasons_payload,
    ModeratorReport.ReportType.QUALITY_SCORES: build_quality_scores_payload,
    ModeratorReport.ReportType.BATCHES: build_batches_payload,
    ModeratorReport.ReportType.DUPLICATE_DECISIONS: build_duplicate_decisions_payload,
    ModeratorReport.ReportType.GENERAL_ACTIVITY: build_general_activity_payload,
}


def build_moderator_report_payload(report):
    """The report's data, as the person who asked for it may see it (what ``moderator_data_scope`` allows them, evaluated now)."""
    from organizations.scope import data_scope

    period = None if report.date_from is None and report.date_to is None else (report.date_from, report.date_to)
    scope = data_scope(report.organization, report.requested_by) if report.requested_by_id else None
    return _REPORT_PAYLOAD_BUILDERS[report.report_type](report.organization, period, scope=scope)


def moderator_report_expiration_time():
    return timezone.now() + timedelta(hours=settings.MODERATOR_REPORT_TTL_HOURS)


def enqueue_moderator_report(*, organization, requested_by, report_type, report_format, date_from=None, date_to=None):
    from .tasks import generate_moderator_report_task

    period_key = report_period_key(date_from, date_to)
    active_filters = {
        "organization": organization, "report_type": report_type, "format": report_format,
        "period_key": period_key, "requested_by": requested_by,
        "status__in": [ModeratorReport.Status.QUEUED, ModeratorReport.Status.GENERATING],
    }
    try:
        with transaction.atomic():
            active = ModeratorReport.objects.select_for_update().filter(**active_filters).first()
            if active:
                return active, False
            task_id = uuid.uuid4().hex
            report = ModeratorReport.objects.create(
                organization=organization, requested_by=requested_by, report_type=report_type,
                format=report_format, status=ModeratorReport.Status.QUEUED, task_id=task_id,
                date_from=date_from, date_to=date_to, period_key=period_key,
            )

            def dispatch():
                try:
                    generate_moderator_report_task.apply_async(args=[report.pk], task_id=task_id)
                except Exception:
                    ModeratorReport.objects.filter(pk=report.pk).update(
                        status=ModeratorReport.Status.FAILED,
                        failure_reason="The report queue is unavailable.",
                    )

            transaction.on_commit(dispatch)
    except IntegrityError:
        active = ModeratorReport.objects.filter(**active_filters).first()
        if active:
            return active, False
        raise
    report.refresh_from_db()
    return report, True


def expire_moderator_report_if_needed(report, now=None):
    now = now or timezone.now()
    if (
        report.status == ModeratorReport.Status.READY
        and report.expires_at
        and report.expires_at <= now
    ):
        if report.report_file:
            report.report_file.delete(save=False)
        report.status = ModeratorReport.Status.EXPIRED
        report.report_file = ""
        report.file_size = 0
        report.file_checksum_sha256 = ""
        report.save(update_fields=[
            "status", "report_file", "file_size", "file_checksum_sha256", "updated_at",
        ])
        return True
    return False


def expire_due_moderator_reports(now=None):
    now = now or timezone.now()
    queryset = ModeratorReport.objects.filter(status=ModeratorReport.Status.READY, expires_at__lte=now)
    expired = 0
    for report in queryset.iterator():
        expired += int(expire_moderator_report_if_needed(report, now=now))
    return expired


# -- Duplicate-group merging (T-007 Phase G) ---------------------------------
#
# There is no DuplicateMatch-level merge concept -- the real merge primitive
# is identities.management.merge_groups(), and the link from a duplicate
# review to *which* identity groups are involved is not modeled anywhere, so
# both are resolved here rather than reused wholesale.

_CONFIRMED_MEMBERSHIP_STATES = ["auto_matched", "new_group", "confirmed"]


def groups_for_duplicate_match(match):
    """(source-side groups, matched-side groups) linked to a duplicate match.

    An image can have multiple faces, and thus multiple groups, so each side
    returns a small candidate set rather than assuming exactly one.
    """
    from identities.models import IdentityGroup

    def groups_for_image(image):
        return list(
            IdentityGroup.objects.filter(
                memberships__identity_face__image=image,
                memberships__state__in=_CONFIRMED_MEMBERSHIP_STATES,
                state=IdentityGroup.State.ACTIVE,
            ).distinct().order_by("pk")
        )

    return groups_for_image(match.source_image), groups_for_image(match.matched_image)


def _resolve_merge_candidate(candidates, explicit_id, field_name):
    if explicit_id:
        for group in candidates:
            if group.pk == explicit_id:
                return group
        raise ValidationError({field_name: "That identity group is not linked to this duplicate match."})
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValidationError({field_name: "No identity group is linked to this side of the duplicate match yet."})
    raise ValidationError({
        field_name: "Multiple identity groups are linked to this side; specify which one explicitly.",
        "candidates": [group.pk for group in candidates],
    })


def merge_duplicate_identity_groups(
    *, review, actor, source_group_id=None, target_group_id=None, reason="", request=None,
):
    if review.source != ModeratorReview.Source.DUPLICATE or not review.duplicate_match_id:
        raise ValidationError({"detail": "This review is not a duplicate-match review."})
    from identities.management import merge_groups

    match = review.duplicate_match
    source_candidates, target_candidates = groups_for_duplicate_match(match)
    source_group = _resolve_merge_candidate(source_candidates, source_group_id, "source_group_id")
    target_group = _resolve_merge_candidate(target_candidates, target_group_id, "target_group_id")
    if source_group.pk == target_group.pk:
        raise ValidationError({"detail": "The duplicate match resolves to the same identity group on both sides."})
    if source_group.owner_id != target_group.owner_id:
        raise ValidationError({"detail": "These identity groups belong to different owners and cannot be merged."})
    merged_source, merged_target = merge_groups(
        source_group, target_group, source_group.owner, reason, actor=actor,
    )
    record_audit_event(
        review.organization, actor, OrganizationAuditEvent.Action.IDENTITY_GROUPS_MERGED_BY_MODERATOR,
        target_type="IdentityGroup", target_id=merged_target.pk,
        new_value={"source_group_id": merged_source.pk, "target_group_id": merged_target.pk},
        reason=reason, request=request,
    )
    return merged_source, merged_target
