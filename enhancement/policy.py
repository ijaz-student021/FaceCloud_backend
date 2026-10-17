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
BALANCED = "balanced_v1"
LEARNED = "learned_v1"
ADAPTIVE = "adaptive_v1"
POLICIES = (LIGHTWEIGHT, BALANCED, LEARNED, ADAPTIVE)

# Per-model setting -> the stage it switches, as reported to users.
LEARNED_STAGES = {
    "ZERO_DCE_ENABLED": "zero_dce",
    "RETINEXFORMER_ENABLED": "retinexformer",
    "HD_RESTORATION_ENABLED": "hd_restoration",
    "NAFNET_DEBLUR_ENABLED": "nafnet_deblur",
    "NAFNET_DENOISE_ENABLED": "nafnet_denoise",
    "GFPGAN_ENABLED": "gfpgan_face_restoration",
    "CODEFORMER_ENABLED": "codeformer_face_restoration",
}

# Which learned stages each policy may run (a stage also needs its own setting on). ``balanced_v1`` allows
# only Zero-DCE, which runs on the CPU through OpenCV (no PyTorch, no GPU); it is used only when a
# lift was asked for (an explicit level or style), never by automatic mode.
ALLOWED_STAGES = {
    LIGHTWEIGHT: frozenset(),
    BALANCED: frozenset({"ZERO_DCE_ENABLED"}),
    LEARNED: frozenset(LEARNED_STAGES),
    ADAPTIVE: frozenset(LEARNED_STAGES),
}

DESCRIPTIONS = {
    LIGHTWEIGHT: (
        "OpenCV and Pillow only. No learned enhancement model runs: no Zero-DCE, "
        "Retinexformer, NAFNet, Real-ESRGAN, GFPGAN or CodeFormer."
    ),
    BALANCED: (
        "OpenCV and Pillow, plus Zero-DCE (CPU, OpenCV DNN) when a lift is requested by an explicit level or "
        "style. Automatic mode still never lifts a dark photo. No PyTorch model runs."
    ),
    LEARNED: "Each learned model (Retinexformer, Zero-DCE, NAFNet, Real-ESRGAN, GFPGAN, CodeFormer) runs when its own setting is on.",
    ADAPTIVE: (
        "The adaptive engine (enhancement/adaptive/): the photograph is analysed, only the operations its defects call for "
        "run, each candidate is validated (quality, colour, artifacts, identity), and one final image is selected or the "
        "original is kept. Uses the models whose own setting is on."
    ),
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
    return flag in ALLOWED_STAGES[active_policy()] and bool(getattr(settings, flag))


def disabled_by_policy():
    """Stage names whose setting is on but which the active policy keeps off."""
    allowed = ALLOWED_STAGES[active_policy()]
    return sorted(stage for flag, stage in LEARNED_STAGES.items() if flag not in allowed and getattr(settings, flag))


def describe():
    """The policy as recorded in every result's parameters."""
    name = active_policy()
    return {
        "name": name,
        "description": DESCRIPTIONS[name],
        "learned_stages_allowed": bool(ALLOWED_STAGES[name]),
        "allowed_stages": sorted(LEARNED_STAGES[flag] for flag in ALLOWED_STAGES[name]),
        "learned_stages": sorted(LEARNED_STAGES.values()),
        "configured_on_but_disabled_by_policy": disabled_by_policy(),
        "heavy_restoration_available": name in (LEARNED, ADAPTIVE) and any(
            bool(getattr(settings, flag)) for flag in LEARNED_STAGES
        ),
    }


def disabled_reason(stage):
    """The sentence for a stage that is off: the policy, or its own setting."""
    if active_policy() in (LIGHTWEIGHT, BALANCED):
        return f"{stage} is disabled by the {active_policy()} rendering policy."
    return f"{stage} is disabled."
