"""What a member is told when a moderator decides on their photograph.

Only ``message_to_member`` (typed by the moderator, at most 500 characters) is ever shown to the member as free text. The moderator's internal
``reason`` and ``notes`` stay internal. When the moderator typed nothing, a standard sentence for the reason code is used, in plain words.
"""
MAX_MESSAGE_LENGTH = 500

# Reason codes as the quality and face checks produce them (quality_control/services.py, validation/services.py).
REASON_SENTENCES = {
    "face_too_small": "The face in your photo is too small to use. Take the photo again closer, or crop around the face.",
    "group_face_too_small": "Some faces in your group photo are too small to use. Take it again from closer.",
    "severe_blur": "Your photo is too blurry. Take it again holding the camera steady.",
    "borderline_blur": "Your photo is a little blurry. A sharper photo gives better results.",
    "resolution_too_low": "Your photo is too small. Use the original, larger file.",
    "borderline_resolution": "Your photo has a low resolution. A larger file gives better results.",
    "severe_underexposure": "Your photo is too dark. Take it again with more light.",
    "underexposed": "Your photo is rather dark. More light gives better results.",
    "severe_overexposure": "Your photo is too bright. Avoid strong direct light or a bright background.",
    "overexposed": "Your photo is rather bright. Avoid strong direct light.",
    "contrast_too_low": "Your photo looks flat, with almost no difference between light and dark areas.",
    "borderline_contrast": "Your photo has low contrast.",
    "excessive_noise": "Your photo is very grainy, usually from low light.",
    "overall_score_below_acceptance": "The overall quality of your photo is below what the service needs.",
    "no_face_detected": "No clear face was found in your photo. Use a photo where the face is clearly visible and facing the camera.",
    "large_group_detected": "Your photo has more than five faces, which is too many to process reliably.",
    "face_off_center": "The face is too close to the edge of your photo. Centre it in the frame.",
    "face_validation_rejected": "The face check could not accept your photo.",
    "face_validation_required": "The face check was not sure about your photo.",
    "detector_failed": "The face check could not run on your photo. Try uploading it again.",
}

APPROVED_DEFAULT = "A moderator looked at your photo and approved it."
REJECTED_DEFAULT = "A moderator looked at your photo and it could not be accepted."

SOURCE_LEAD = {
    "quality": "",
    "identity": "A moderator checked who is in your photo. ",
    "duplicate": "A moderator checked whether your photo repeats another one. ",
}


def reason_codes(image):
    """The reason codes the checks recorded for ``image``, most important first."""
    codes = []
    quality = getattr(image, "quality_decision", None)
    if quality is not None:
        codes += [code for code in (quality.rejection_reasons or []) if isinstance(code, str)]
    validation = getattr(image, "face_validation", None)
    if validation is not None and getattr(validation, "rejection_reason", ""):
        codes.append(validation.rejection_reason)
    return codes


def standard_sentence(review, outcome):
    """The sentence for a decision when the moderator wrote nothing: the first known reason code for a rejection, else a plain default."""
    if outcome == "approved":
        return SOURCE_LEAD.get(review.source, "") + ("The photo was approved." if review.source != "quality" else APPROVED_DEFAULT)
    for code in reason_codes(review.image):
        if code in REASON_SENTENCES:
            return REASON_SENTENCES[code]
    return SOURCE_LEAD.get(review.source, "") + (REJECTED_DEFAULT if review.source == "quality" else "It could not be accepted.")


def message_for_member(review, outcome):
    """What the member reads: the moderator's own message to them, or the standard sentence."""
    written = (review.message_to_member or "").strip()
    return written[:MAX_MESSAGE_LENGTH] if written else standard_sentence(review, outcome)
