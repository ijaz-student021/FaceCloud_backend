"""Review only where someone can review.

A quality verdict ``needs_review`` means "a person has to look at this". In an organization that person is a moderator. A photograph in a **personal workspace** (or in no
organization) has no moderator: nobody can ever decide it, so the verdict is information, not a state. What a person sees for such a photograph is ``accepted`` with tips
("a little dim", "no face found"), never "In review".

* ``decision`` on the stored quality decision stays exactly what the checks found (it is evidence, and what an administrator reads).
* ``effective_decision`` is what it means for the person: ``accepted`` for a personal ``needs_review``, otherwise the stored verdict. ``rejected`` is never softened here.
* ``moderator_can_decide(image)`` is the one rule, used by the status word, the progress and batch status, the My Images filter, the dashboard counts, the notification, the activity
  entry, the ``accepted_results`` package and the quality serializers.

The same rule is written as a database condition (``effective_q``) so filters and counts agree with the Python version.
"""
from django.db.models import Q

# Reason codes as the checks write them (quality_control/services.py, validation/services.py), as a tip for a person whose photograph was accepted anyway. They are
# suggestions in plain words: nothing here asks the person to do anything, and nothing says "review".
TIP_SENTENCES = {
    "face_too_small": "The face is small in this photo; a closer shot gives better results.",
    "group_face_too_small": "Some faces are too small to group; a closer shot would include them.",
    "severe_blur": "This photo is blurry; a steadier shot is sharper.",
    "borderline_blur": "This photo is a little blurry.",
    "resolution_too_low": "This photo is small; the original, larger file gives better results.",
    "borderline_resolution": "This photo has a low resolution; a larger file gives better results.",
    "severe_underexposure": "This photo is very dark.",
    "underexposed": "This photo is a little dark; more light gives better results.",
    "severe_overexposure": "This photo is very bright.",
    "overexposed": "This photo is a little bright.",
    "contrast_too_low": "This photo looks flat, with little difference between light and dark.",
    "borderline_contrast": "This photo has low contrast.",
    "excessive_noise": "This photo is grainy, usually from low light.",
    "overall_score_below_acceptance": "The overall quality of this photo is modest.",
    "no_face_detected": "No face was found in this photo, so it is not placed in a person folder.",
    "large_group_detected": "This photo has many faces; the largest are grouped.",
    "face_off_center": "A face is cut off by, or very close to, the edge of this photo.",
    "face_validation_rejected": "The face in this photo was not clear.",
    "face_validation_required": "The face check was not sure about this photo.",
    "detector_failed": "The face check could not run on this photo.",
}


def moderator_can_decide(image):
    """Whether a moderator could ever decide ``image``: it is filed under an organization that is not a personal workspace."""
    organization = image.batch.organization
    return organization is not None and not organization.is_personal


def effective_decision(image, decision=None):
    """What the stored verdict means for the person who owns ``image``. ``decision`` defaults to the photograph's saved quality decision (``""`` when there is none)."""
    if decision is None:
        quality = getattr(image, "quality_decision", None)
        decision = quality.decision if quality is not None else ""
    if decision == "needs_review" and not moderator_can_decide(image):
        return "accepted"
    return decision


def review_required(image, decision=None):
    """Whether the photograph is waiting for a moderator: a ``needs_review`` verdict in an organization."""
    if decision is None:
        quality = getattr(image, "quality_decision", None)
        decision = quality.decision if quality is not None else ""
    return decision == "needs_review" and moderator_can_decide(image)


def tips_for(reasons):
    """Plain sentences for reason codes (unknown codes are skipped), in order, without repeats."""
    seen, tips = set(), []
    for code in reasons or []:
        sentence = TIP_SENTENCES.get(code) if isinstance(code, str) else None
        if sentence and sentence not in seen:
            seen.add(sentence)
            tips.append(sentence)
    return tips


def image_reason_codes(image):
    """The reasons the checks gave for ``image``: the quality reasons, then the face check's when it had a doubt."""
    codes = []
    quality = getattr(image, "quality_decision", None)
    if quality is not None:
        codes += [code for code in (quality.rejection_reasons or []) if isinstance(code, str)]
    validation = getattr(image, "face_validation", None)
    if validation is not None and validation.decision in ("needs_review", "rejected") and validation.rejection_reason:
        codes.append(validation.rejection_reason)
    return codes


def moderated_q(batch_path="batch"):
    """A database condition for "filed under an organization that is not personal" (a moderator can decide it); ``batch_path`` is how the model reaches its batch."""
    return Q(**{f"{batch_path}__organization__isnull": False}) & Q(**{f"{batch_path}__organization__is_personal": False})


def effective_q(wanted, *, decision_path="quality_decision__decision", batch_path="batch"):
    """The condition for "the effective verdict is ``wanted``" on a queryset of photographs (the defaults) or of anything that reaches them (change the paths).

    ``accepted`` also matches a stored ``needs_review`` that no moderator can decide; ``needs_review`` matches only one a moderator can.
    """
    stored = Q(**{decision_path: wanted})
    if wanted == "accepted":
        return stored | (Q(**{decision_path: "needs_review"}) & ~moderated_q(batch_path))
    if wanted == "needs_review":
        return stored & moderated_q(batch_path)
    return stored
