"""How an organization audit event reads on the moderator's Audit and Moderation Logs screens: a sentence, the thing it was about, and old and new status.

The stored record (action, target, previous and new value, reason) is unchanged; these are derived when it is listed. The labels of the things events were
about are looked up for a whole page at once, so a page costs a fixed number of queries.
"""
from django.db.models import Q

from .models import OrganizationAuditEvent

Action = OrganizationAuditEvent.Action

# {label} is the photo, upload or folder the event was about ("a photo" when it is gone or the event names none).
SENTENCES = {
    Action.ORGANIZATION_CREATED: "Created the organization.",
    Action.MEMBER_INVITED: "Invited a member.",
    Action.INVITATION_RESENT: "Resent an invitation.",
    Action.INVITATION_CANCELLED: "Cancelled an invitation.",
    Action.INVITATION_ACCEPTED: "Accepted an invitation.",
    Action.MEMBER_ROLE_CHANGED: "Changed a member's role.",
    Action.MEMBER_STATUS_CHANGED: "Changed a member's status.",
    Action.MEMBER_REMOVED: "Removed a member.",
    Action.MODERATOR_REVIEW_DECIDED: "{decision} {label}.",
    Action.MODERATOR_REVIEW_ESCALATED: "Escalated the review of {label}.",
    Action.MODERATOR_REVIEW_ASSIGNED: "Assigned the review of {label}.",
    Action.MODERATOR_REVIEW_CLAIMED: "Took the review of {label}.",
    Action.MODERATOR_REVIEW_RELEASED: "Released the review of {label}.",
    Action.MODERATOR_BULK_DECISION: "Decided several reviews at once.",
    Action.MODERATOR_IMAGE_PREVIEWED: "Looked at {label}.",
    Action.BATCH_RETRIED_BY_MODERATOR: "Retried {label}.",
    Action.BATCH_CANCELLED_BY_MODERATOR: "Cancelled {label}.",
    Action.BATCH_REPROCESSED_BY_MODERATOR: "Reprocessed {label}.",
    Action.IDENTITY_GROUPS_MERGED_BY_MODERATOR: "Merged person folders into {label}.",
    Action.ORGANIZATION_DATA_EXPORTED: "Exported the organization's data.",
    Action.MEMBER_NOTIFIED_OF_REVIEW: "Told the member about the decision on {label}.",
}
DECISION_WORDS = {
    "approve": "Approved", "reject": "Rejected", "correct": "Corrected", "escalate": "Escalated", "hold": "Held", "keep": "Kept",
}


def labels_for(events):
    """``{(target_type, target_id): label}`` for the reviews, uploads, photos and folders the events were about (one query each)."""
    from identities.models import IdentityGroup
    from moderation.models import ModeratorReview
    from uploads.models import ImageUpload, UploadBatch

    wanted = {}
    for event in events:
        if event.target_id:
            wanted.setdefault(event.target_type, set()).add(event.target_id)
    labels = {}
    if "ModeratorReview" in wanted:
        for pk, name in ModeratorReview.objects.filter(pk__in=wanted["ModeratorReview"]).values_list("pk", "image__original_name"):
            labels[("ModeratorReview", pk)] = name
    if "ImageUpload" in wanted:
        for pk, name in ImageUpload.objects.filter(pk__in=wanted["ImageUpload"]).values_list("pk", "original_name"):
            labels[("ImageUpload", pk)] = name
    if "UploadBatch" in wanted:
        for batch in UploadBatch.objects.filter(pk__in=wanted["UploadBatch"]):
            labels[("UploadBatch", batch.pk)] = batch.display_name or batch.original_archive_name or f"upload {batch.pk}"
    if "IdentityGroup" in wanted:
        for pk, name in IdentityGroup.objects.filter(pk__in=wanted["IdentityGroup"]).values_list("pk", "display_name"):
            labels[("IdentityGroup", pk)] = name
    return labels


def describe_event(event, labels):
    """``(message, target_label, from_status, to_status)`` for one stored event."""
    label = labels.get((event.target_type, event.target_id))
    decision = (event.new_value or {}).get("action", "")
    template = SENTENCES.get(event.action) or event.get_action_display() + "."
    message = template.format(
        label=label or {"UploadBatch": "an upload", "IdentityGroup": "a person folder"}.get(event.target_type, "a photo"),
        decision=DECISION_WORDS.get(decision, "Decided"),
    )
    return message, label, (event.previous_value or {}).get("status"), (event.new_value or {}).get("status")


def _search_filter(text):
    """What ``?q=`` keeps: the actor (username or email), the reason, the action (its stored name, or its sentence), a decision's wording
    ("approved", "rejected", ...), or the name of the photo, upload or folder the event was about."""
    import re

    from identities.models import IdentityGroup
    from moderation.models import ModeratorReview
    from uploads.models import ImageUpload, UploadBatch

    lowered = text.lower()
    # A sentence matches on its fixed words: "{label}" and "{decision}" are filled in per event, so they are not searched as text.
    sentences = [action for action, template in SENTENCES.items() if lowered in re.sub(r"\{\w+\}", "", template).lower()]
    decisions = [word for word, label in DECISION_WORDS.items() if lowered in label.lower()]
    found = (
        Q(actor__username__icontains=text) | Q(actor__email__icontains=text) | Q(reason__icontains=text)
        | Q(action__icontains=lowered.replace(" ", "_")) | Q(action__in=sentences)
        | Q(target_type="ModeratorReview", target_id__in=ModeratorReview.objects.filter(image__original_name__icontains=text).values("pk"))
        | Q(target_type="ImageUpload", target_id__in=ImageUpload.objects.filter(original_name__icontains=text).values("pk"))
        | Q(target_type="UploadBatch", target_id__in=UploadBatch.objects.filter(
            Q(display_name__icontains=text) | Q(original_archive_name__icontains=text)).values("pk"))
        | Q(target_type="IdentityGroup", target_id__in=IdentityGroup.objects.filter(display_name__icontains=text).values("pk"))
    )
    if decisions:
        found |= Q(action=Action.MODERATOR_REVIEW_DECIDED, new_value__action__in=decisions)
    return found


def filter_events(queryset, params):
    """Apply ``?action=``, ``?actor=``, ``?q=``, ``?date_from=``, ``?date_to=``, ``?target_type=`` and ``?target_id=``; ``ValueError`` for a bad value."""
    from django.utils import timezone

    action = params.get("action")
    if action:
        if action not in Action.values:
            raise ValueError(f"action must be one of: {', '.join(Action.values)}.")
        queryset = queryset.filter(action=action)
    actor = params.get("actor")
    if actor:
        if not actor.isdigit():
            raise ValueError("actor must be a user id.")
        queryset = queryset.filter(actor_id=int(actor))
    target_id = params.get("target_id")
    if target_id:
        if not target_id.isdigit():
            raise ValueError("target_id must be a number.")
        queryset = queryset.filter(target_id=int(target_id))
    if params.get("target_type"):
        queryset = queryset.filter(target_type=params["target_type"])
    for name, lookup, end in (("date_from", "created_at__gte", False), ("date_to", "created_at__lte", True)):
        value = params.get(name)
        if value:
            try:
                moment = timezone.datetime.fromisoformat(value)
            except ValueError as error:
                raise ValueError(f"{name} must be an ISO-8601 date.") from error
            if timezone.is_naive(moment):
                moment = timezone.make_aware(moment)
            if end and len(value) == 10:
                moment += timezone.timedelta(days=1) - timezone.timedelta(microseconds=1)
            queryset = queryset.filter(**{lookup: moment})
    text = (params.get("q") or "").strip()
    if text:
        if len(text) > 100:
            raise ValueError("q must be at most 100 characters.")
        queryset = queryset.filter(_search_filter(text))
    return queryset
