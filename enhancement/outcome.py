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


DARK_EXPOSURES = ("low_light", "low_light_subject", "dark_scene")          # the analyzer's exposure statuses that make a photograph a "dark photo"


def result_lighting(result):
    """How the engine saw the photograph's lighting: ``{detected, exposure, scene_median, brightened}``, or ``None`` when there is no adaptive report.

    ``detected`` is ``dark`` or ``light`` (a normal, over-exposed or back-lit photograph is ``light``), ``exposure`` the analyzer's own status,
    ``scene_median`` the median grey level of the frame (0-255) and ``brightened`` whether a brightening step really changed the photograph: a dark
    photograph whose brightening was not safe to apply is ``dark`` with ``brightened`` false.
    """
    parameters = result.applied_parameters if isinstance(result.applied_parameters, dict) else {}
    adaptive = parameters.get("adaptive")
    report = adaptive.get("quality_report") if isinstance(adaptive, dict) else None
    exposure = report.get("exposure") if isinstance(report, dict) else None
    if not isinstance(exposure, dict) or not exposure.get("status"):
        return None
    status = str(exposure["status"])
    return {
        "detected": "dark" if status in DARK_EXPOSURES else "light",
        "exposure": status,
        "scene_median": exposure.get("scene_median"),
        "brightened": "low_light_enhancement" in (adaptive.get("operations_applied") or []),
    }
