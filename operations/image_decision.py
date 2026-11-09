"""An administrator's decision on a photograph: ``POST /api/system/images/{id}/decision/`` (``operations/admin_images.py`` is the HTTP side).

The moderator's decisions need an organization membership and act on a review; a platform administrator is in no organization and the photograph may have no review at
all (the checks accepted it on their own, or it is in a personal workspace). This settles the photograph itself, with the moderator's own words and effects:

| action           | the quality decision becomes | then |
|------------------|------------------------------|------|
| ``approve``      | ``accepted``     | what the pipeline does after any accepted verdict: the automatic enhancement, then the person folders; the owner is told |
| ``reject``       | ``rejected``     | out of every person folder (never grouped, never enhanced automatically); the owner is told the reason (required) |
| ``keep_in_review`` | ``needs_review`` | in a team the photograph is in the moderators' queue again (its review is opened, or reopened if a moderator had decided it); the owner is not told |

* **An open quality review is decided by the same ``moderation.services.decide_review`` a moderator uses**, with the administrator as ``decided_by``, so the queue, the decision log (the team's
  audit trail) and the overview figures count it like any decision. Without one (the checks decided on their own, a moderator already decided it, a personal workspace),
  ``apply_quality_verdict`` (the write-back and hooks ``decide_review`` itself uses) does the same, and in a team the change is written to the team's audit trail as an event about the photograph,
  so a moderator reading the log sees that an administrator changed it. Reviews about other questions (a duplicate, who is in the photo) are not touched: this decides the quality verdict.
  A live claim by another moderator is respected (409, as for a moderator): it ends by itself. ``keep_in_review`` never closes a review: it opens one for a team's photograph that has
  none, and reopens (``moderation.services.reopen_review``) one a moderator had decided, since a photograph has one quality review for good.
* **The member is told once per real change**: through ``decide_review`` when it closed a review, else by ``notifications.events.notify_member_of_image_decision`` (the same message, one
  notification per audit row). The administrator's reason is the message. Deciding what is already decided changes nothing: no write, no notification, no audit row.
* **Audit**: one ``admin`` activity event ``admin_image_decided`` (so it shows in ``/api/system/audit/`` next to ``admin_image_viewed``) naming the photograph, its owner, the action, the previous
  decision and the reason.
* **Refused with 409** (``ImageNotDecidable``, a sentence says which): a photograph refused at upload (its file was never stored), one in the trash (an approval would queue work for a photograph
  its owner has put away), and one whose quality check has no verdict yet. 409 rather than 400 because the request is well formed and the photograph's state is what forbids it (the
  moderator's decisions answer 409 for a review that is no longer open); 400 stays for a request that is wrong whatever the photograph is.
"""
import logging
from functools import partial

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from activity.services import record_activity_event
from moderation.member_messages import ADMIN_APPROVED_DEFAULT
from moderation.models import ModeratorReview
from moderation.services import (
    ModerationConflict,
    apply_quality_verdict,
    create_review_for_quality_decision,
    decide_review,
    reopen_review,
)
from notifications.events import notify_member_of_image_decision
from organizations.models import OrganizationAuditEvent
from organizations.services import record_audit_event
from quality_control.effective import moderator_can_decide
from quality_control.models import ImageQualityDecision

logger = logging.getLogger(__name__)

Decision = ImageQualityDecision.Decision
# The action names are the moderator's (``ModeratorDecisionSerializer``); "correct" is left out: it picks a person folder, which has no meaning for a photograph.
DECISION_FOR_ACTION = {"approve": Decision.ACCEPTED.value, "reject": Decision.REJECTED.value, "keep_in_review": Decision.NEEDS_REVIEW.value}
OUTCOME_FOR_ACTION = {"approve": "approved", "reject": "rejected"}
BY = "an administrator"
REASON_REQUIRED = "Give a reason for rejecting the photo: the owner is told it."


class ImageNotDecidable(ModerationConflict):
    """409: the photograph cannot be decided now. The message says why, in a sentence for the administrator."""

    default_code = "image_not_decidable"
    default_detail = "This photo cannot be decided."


def _refuse_if_undecidable(image, quality):
    if image.status != "uploaded":
        raise ImageNotDecidable("This photo was refused at upload and its file was never stored, so there is nothing to decide.")
    if image.trashed_at is not None:
        raise ImageNotDecidable("This photo is in its owner's trash. Decide it after it is restored.")
    if quality is not None and quality.status == ImageQualityDecision.Status.FAILED:
        raise ImageNotDecidable("The quality check of this photo failed, so it has no verdict to change. Retry its upload batch first.")
    if quality is None or quality.status != ImageQualityDecision.Status.COMPLETED or not quality.decision:
        raise ImageNotDecidable("The quality check of this photo has not finished, so it cannot be decided yet.")


@transaction.atomic
def decide_image(*, image, actor, action, reason="", request=None):
    """Apply ``action`` (``approve``, ``reject`` or ``keep_in_review``) to ``image`` as ``actor``. Returns whether anything changed: ``False`` when the photograph already had this decision and
    no review was open, so nothing was written, told or recorded. See the module text for the rules.

    ``ValidationError`` for an unknown action or a rejection without a reason; ``ImageNotDecidable`` (409) for a photograph that cannot be decided; ``ReviewClaimedByAnother`` (409) while
    another moderator holds the review. Nothing is written when it raises.
    """
    if action not in DECISION_FOR_ACTION:
        raise ValidationError({"action": ["Choose approve, reject or keep_in_review."]})
    reason = (reason or "").strip()
    if action == "reject" and not reason:
        raise ValidationError({"reason": [REASON_REQUIRED]})
    # Locked in the order a moderator's decision takes them (the review, then the quality decision), so two decisions on one photograph wait for each other instead of deadlocking.
    review = ModeratorReview.objects.select_for_update().filter(image_id=image.pk, source=ModeratorReview.Source.QUALITY).order_by("-pk").first()
    quality = ImageQualityDecision.objects.select_for_update().filter(image_id=image.pk).first()
    _refuse_if_undecidable(image, quality)

    previous, target = quality.decision, DECISION_FOR_ACTION[action]
    in_a_team = moderator_can_decide(image)               # a moderator can decide it: it is filed under an organization that is not a personal workspace
    open_review = review if review is not None and review.decided_at is None else None
    if action == "keep_in_review":
        review_change, review = _put_under_review(image, quality, review, open_review, in_a_team, actor, reason, request)
        changed = previous != target or review_change is not None
    else:
        review_change = "closed" if open_review is not None else None
        changed = previous != target or open_review is not None
        if changed:
            message = reason or ADMIN_APPROVED_DEFAULT
            if open_review is not None:
                decide_review(
                    review=open_review, actor=actor, action=action, reason=reason, message_to_member=message, request=request, rejected_by=BY,
                )
            else:
                apply_quality_verdict(image, quality.pk, action, by=BY)
    if not changed:
        return False

    event = record_activity_event(
        actor, "admin", "admin_image_decided", object_type="ImageUpload", object_id=image.pk,
        details={
            "owner_id": image.owner_id, "action": action, "previous_decision": previous, "decision": target, "reason": reason,
            "review_id": review.pk if review is not None else None,
        },
    )
    if in_a_team and review_change not in ("closed", "reopened"):
        # decide_review and reopen_review wrote the team's audit event themselves; any other change to a team's photograph is written here.
        record_audit_event(
            image.batch.organization, actor, OrganizationAuditEvent.Action.MODERATOR_REVIEW_DECIDED,
            target_type="ImageUpload", target_id=image.pk,
            previous_value={"decision": previous}, new_value={"action": action, "decision": target}, reason=reason, request=request,
        )
    if action in OUTCOME_FOR_ACTION and review_change != "closed":
        transaction.on_commit(partial(
            _tell_owner, image, actor, OUTCOME_FOR_ACTION[action], reason or ADMIN_APPROVED_DEFAULT, f"image:{image.pk}:admin_decision:{event.pk}",
        ))
    return True


def _put_under_review(image, quality, review, open_review, in_a_team, actor, reason, request):
    """``keep_in_review``: the quality decision becomes ``needs_review`` and, in a team, a moderator can see the photograph in the queue.

    Returns ``(what happened to the review, the review)``: ``opened`` (it had none), ``reopened`` (a moderator had decided it) or ``None`` (it was open, or this is no team's photograph).
    """
    review_change = None
    if in_a_team:
        if review is None:
            # Before the verdict changes, so the recommendation it records is the checks' own verdict. The administrator's note is the review's note, as an escalation's reason is.
            review = create_review_for_quality_decision(quality)
            if review is not None:
                review_change = "opened"
                if reason:
                    ModeratorReview.objects.filter(pk=review.pk).update(decision_reason=reason)
        elif open_review is None:
            reopen_review(review=review, actor=actor, reason=reason, request=request)
            review_change = "reopened"
    if quality.decision != Decision.NEEDS_REVIEW:
        ImageQualityDecision.objects.filter(pk=quality.pk).update(decision=Decision.NEEDS_REVIEW, updated_at=timezone.now())
    return review_change, review


def _tell_owner(image, actor, outcome, message, idempotency_key):
    try:
        notify_member_of_image_decision(image, actor, outcome, message, idempotency_key)
    except Exception:
        # The decision is made and committed: failing to tell the member must not turn it into an error for the administrator.
        logger.exception("Could not tell the owner of image_id=%s about the administrator's decision.", image.pk)
