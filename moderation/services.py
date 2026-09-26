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
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

from duplicates.models import DuplicateMatch
from organizations.models import OrganizationAuditEvent, OrganizationMembership
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
    """
    queryset = ModeratorReview.objects.filter(
        organization=organization, assigned_moderator=user, status__in=OPEN_REVIEW_STATUSES,
    )
    now = timezone.now()
    with transaction.atomic():
        ModeratorReview.objects.filter(
            pk__in=queryset.filter(status=ModeratorReview.Status.IN_REVIEW).values("pk"),
        ).update(status=ModeratorReview.Status.PENDING, updated_at=now)
        return queryset.update(assigned_moderator=None, updated_at=now)


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


def _report_envelope(organization, report_type, summary, rows):
    return {
        "format_version": 1,
        "report_type": report_type,
        "generated_at": timezone.now().isoformat(),
        "organization": {"id": organization.pk, "name": organization.name},
        "summary": summary,
        "rows": rows,
        "row_count": len(rows),
    }


def _organization_quality_decisions(organization):
    return ImageQualityDecision.objects.filter(
        image__batch__organization=organization, status=ImageQualityDecision.Status.COMPLETED,
    )


def build_rejection_reasons_payload(organization):
    """Why photos were rejected or sent to review, most common reason first."""
    flagged = _organization_quality_decisions(organization).filter(
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
    return _report_envelope(organization, ModeratorReport.ReportType.REJECTION_REASONS, summary, rows)


QUALITY_SCORE_BUCKETS = ((0, 20), (20, 40), (40, 60), (60, 80), (80, 101))
QUALITY_SUBSCORES = (
    "brightness_score", "sharpness_score", "contrast_score", "noise_score", "exposure_score", "resolution_score",
)


def _rounded(value):
    return round(value, 1) if value is not None else None


def build_quality_scores_payload(organization):
    """How good the organization's photos are: averages and a score distribution."""
    decisions = _organization_quality_decisions(organization).filter(overall_score__isnull=False)
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
    return _report_envelope(organization, ModeratorReport.ReportType.QUALITY_SCORES, summary, rows)


def build_batches_payload(organization):
    """The organization's upload batches and how they finished."""
    batches = UploadBatch.objects.filter(organization=organization)
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
    return _report_envelope(organization, ModeratorReport.ReportType.BATCHES, summary, rows)


def build_duplicate_decisions_payload(organization):
    """Suspected duplicates and what was decided about them (hash-based, no faces)."""
    matches = DuplicateMatch.objects.filter(source_image__batch__organization=organization)
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
    return _report_envelope(organization, ModeratorReport.ReportType.DUPLICATE_DECISIONS, summary, rows)


_REPORT_PAYLOAD_BUILDERS = {
    ModeratorReport.ReportType.REVIEW_SUMMARY: build_review_summary_payload,
    ModeratorReport.ReportType.MODERATOR_ACTIVITY: build_moderator_activity_payload,
    ModeratorReport.ReportType.REJECTION_REASONS: build_rejection_reasons_payload,
    ModeratorReport.ReportType.QUALITY_SCORES: build_quality_scores_payload,
    ModeratorReport.ReportType.BATCHES: build_batches_payload,
    ModeratorReport.ReportType.DUPLICATE_DECISIONS: build_duplicate_decisions_payload,
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
