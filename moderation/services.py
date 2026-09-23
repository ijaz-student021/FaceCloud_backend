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
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

from duplicates.models import DuplicateMatch
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import get_active_membership, record_audit_event
from quality_control.models import ImageQualityDecision
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


# -- Org-scoped reports (T-007 Phase F) --------------------------------------
#
# Only two report types, deliberately: reports/renderers.py's CSV/PDF
# rendering is hard-coded to a per-image row shape, so a report type outside
# that shape needs its own renderer (see moderation/renderers.py) rather than
# reusing reports/ wholesale. review-summary and moderator-activity are the
# two most directly useful given what ModeratorReview/OrganizationAuditEvent
# already track; rejection-reason, quality-score, batch, and
# duplicate-decision reports are explicitly deferred.

_ORGANIZATION_AUDIT_ACTIONS_FOR_ACTIVITY = [
    OrganizationAuditEvent.Action.MODERATOR_REVIEW_DECIDED,
    OrganizationAuditEvent.Action.MODERATOR_REVIEW_ESCALATED,
    OrganizationAuditEvent.Action.MODERATOR_REVIEW_ASSIGNED,
    OrganizationAuditEvent.Action.BATCH_RETRIED_BY_MODERATOR,
    OrganizationAuditEvent.Action.BATCH_CANCELLED_BY_MODERATOR,
]


def build_review_summary_payload(organization):
    reviews = ModeratorReview.objects.filter(organization=organization)
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
        "summary": {
            "total_reviews": reviews.count(),
            "by_status": dict(Counter(reviews.values_list("status", flat=True))),
            "by_source": dict(Counter(reviews.values_list("source", flat=True))),
        },
        "rows": rows,
        "row_count": len(rows),
    }


def build_moderator_activity_payload(organization):
    decided = ModeratorReview.objects.filter(organization=organization, decided_by__isnull=False)
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
        OrganizationAuditEvent.objects.filter(
            organization=organization, action__in=_ORGANIZATION_AUDIT_ACTIONS_FOR_ACTIVITY,
        )
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
        "summary": {"moderators": per_moderator},
        "rows": rows,
        "row_count": len(rows),
    }


_REPORT_PAYLOAD_BUILDERS = {
    ModeratorReport.ReportType.REVIEW_SUMMARY: build_review_summary_payload,
    ModeratorReport.ReportType.MODERATOR_ACTIVITY: build_moderator_activity_payload,
}


def build_moderator_report_payload(report):
    return _REPORT_PAYLOAD_BUILDERS[report.report_type](report.organization)


def moderator_report_expiration_time():
    return timezone.now() + timedelta(hours=settings.MODERATOR_REPORT_TTL_HOURS)


def enqueue_moderator_report(*, organization, requested_by, report_type, report_format):
    from .tasks import generate_moderator_report_task

    active_filters = {
        "organization": organization, "report_type": report_type, "format": report_format,
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
