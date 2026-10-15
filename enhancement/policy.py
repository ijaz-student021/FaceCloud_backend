"""The rendering policy: which enhancement stages may run.

``lightweight_v1`` (the default) renders with OpenCV/Pillow only: exposure,
measured denoising, conditional sharpening, the quality and face-fidelity
guards. It never runs a learned enhancement model, whatever the per-model
settings say. ``learned_v1`` is the earlier behaviour, where each model's own
setting (ZERO_DCE_ENABLED, ...) decides; it is kept so the change is
reversible, and is selected only by ENHANCEMENT_RENDER_POLICY.

Learned stages are never a silent fallback: under ``lightweight_v1`` a stage
that would have run reports ``disabled by rendering policy``, and the output's
parameters name the policy, so a stored result always says what could have run.

YuNet and SFace are not enhancement models. They keep working for face
detection, the face-fidelity check and consented identity features.
"""
from django.conf import settings

LIGHTWEIGHT = "lightweight_v1"
LEARNED = "learned_v1"
POLICIES = (LIGHTWEIGHT, LEARNED)

# Per-model setting -> the stage it switches, as reported to users.
LEARNED_STAGES = {
    "ZERO_DCE_ENABLED": "zero_dce",
    "AI_RESTORATION_ENABLED": "edsr_super_resolution",
    "HD_RESTORATION_ENABLED": "hd_restoration",
    "NAFNET_DEBLUR_ENABLED": "nafnet_deblur",
    "PURESCALE_ENABLED": "purescale",
    "PUREVISION_ENABLED": "purevision",
    "GFPGAN_ENABLED": "gfpgan_face_restoration",
    "COLORIZATION_ENABLED": "colorization",
}

DESCRIPTIONS = {
    LIGHTWEIGHT: (
        "OpenCV and Pillow only. No learned enhancement model runs: no Zero-DCE, EDSR, "
        "NAFNet, Real-ESRGAN, GFPGAN, PureVision, PureScale or colourisation."
    ),
    LEARNED: "Each learned model runs when its own setting is on (earlier behaviour).",
}


def active_policy():
    name = getattr(settings, "ENHANCEMENT_RENDER_POLICY", LIGHTWEIGHT)
    if name not in POLICIES:
        raise ValueError(f"ENHANCEMENT_RENDER_POLICY must be one of {POLICIES}, not {name!r}.")
    return name


def learned_stage_enabled(flag):
    """Whether a learned stage may run: the policy allows it *and* its setting is on."""
    if flag not in LEARNED_STAGES:
        raise KeyError(f"{flag} is not a learned-stage setting.")
    return active_policy() == LEARNED and bool(getattr(settings, flag))


def disabled_by_policy():
    """Stage names whose setting is on but which the active policy keeps off."""
    if active_policy() == LEARNED:
        return []
    return sorted(stage for flag, stage in LEARNED_STAGES.items() if getattr(settings, flag))


def describe():
    """The policy as recorded in every result's parameters."""
    name = active_policy()
    return {
        "name": name,
        "description": DESCRIPTIONS[name],
        "learned_stages_allowed": name == LEARNED,
        "learned_stages": sorted(LEARNED_STAGES.values()),
        "configured_on_but_disabled_by_policy": disabled_by_policy(),
        "heavy_restoration_available": name == LEARNED and any(
            bool(getattr(settings, flag)) for flag in LEARNED_STAGES
        ),
    }


def disabled_reason(stage):
    """The sentence for a stage that is off: the policy, or its own setting."""
    if active_policy() == LIGHTWEIGHT:
        return f"{stage} is disabled by the {LIGHTWEIGHT} rendering policy."
    return f"{stage} is disabled."
