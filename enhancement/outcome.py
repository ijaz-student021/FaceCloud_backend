"""What an enhancement ended as, in words a screen can show: shared by the result serializer and the batch progress."""

FAILED_SUMMARY = "The photograph could not be enhanced. Your original photograph is unchanged."


def result_outcome(result):
    """``enhanced`` / ``partially_enhanced`` / ``original_preserved`` (the adaptive engine), ``failed``, or ``None`` when the staged pipeline made the result."""
    if result.status == "failed":
        return "failed"
    parameters = result.applied_parameters if isinstance(result.applied_parameters, dict) else {}
    adaptive = parameters.get("adaptive")
    return adaptive.get("outcome") if isinstance(adaptive, dict) else None


def result_summary(result):
    """One plain sentence on the outcome, safe to show a person."""
    if result.status == "failed":
        return FAILED_SUMMARY
    parameters = result.applied_parameters if isinstance(result.applied_parameters, dict) else {}
    adaptive = parameters.get("adaptive")
    return str(adaptive.get("summary", "")) if isinstance(adaptive, dict) else ""
