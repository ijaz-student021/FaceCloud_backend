"""The rendering policy, and the capabilities that were retired from it.

FaceCloud enhances with OpenCV and Pillow only (``lightweight_v1``): exposure,
measured denoising, conditional sharpening, the quality and face-fidelity
guards. YuNet and SFace are not enhancement models; they keep working for face
detection, the face-fidelity check and consented identity features.

The learned enhancement stages that earlier versions could run were retired on
2026-10-15 (see docs/enhancement/retired-capabilities.md). A retired capability
is never silently replaced by a different algorithm and is never a "fallback":
a request that names one is refused with the reason, results made before the
retirement keep the parameters they were stored with, and every new result lists
what is retired in ``applied_parameters.render_policy.retired_capabilities``.
"""
LIGHTWEIGHT = "lightweight_v1"
RETIRED_ON = "2026-10-15"
RETIREMENT_DOCUMENT = "docs/enhancement/retired-capabilities.md"

# key -> (what it was, the settings that switched it on)
RETIRED_CAPABILITIES = {
    "zero_dce": ("Zero-DCE learned exposure curves", ("ZERO_DCE_ENABLED",)),
    "edsr_super_resolution": ("EDSR 2x super-resolution", ("AI_RESTORATION_ENABLED",)),
    "nafnet_deblur": ("NAFNet deblurring", ("NAFNET_DEBLUR_ENABLED", "HD_RESTORATION_ENABLED")),
    "realesrgan_hd": ("Real-ESRGAN HD upscaling", ("HD_RESTORATION_ENABLED",)),
    "gfpgan_face_restoration": ("GFPGAN face restoration", ("GFPGAN_ENABLED", "GFPGAN_QUEUE_ENABLED")),
    "purevision": ("PureVision artefact removal", ("PUREVISION_ENABLED",)),
    "purescale": ("PureScale 2x super-resolution", ("PURESCALE_ENABLED",)),
    "colorization": ("Automatic colourisation", ("COLORIZATION_ENABLED",)),
}

# Environment names that no longer do anything. Setting one is reported at start-up
# (enhancement/checks.py) instead of being ignored in silence.
RETIRED_SETTING_PREFIXES = (
    "ZERO_DCE_", "AI_RESTORATION_", "NAFNET_", "REALESRGAN_", "HD_RESTORATION_", "GFPGAN_",
    "PUREVISION_", "PURESCALE_", "COLORIZATION_",
)
RETIRED_SETTING_NAMES = (
    "ENHANCEMENT_RENDER_POLICY", "ENHANCEMENT_MIN_TILE_SIZE", "ENHANCEMENT_DARK_COLOUR_GAIN_MAX",
    "EXTREME_LOW_LIGHT_MODEL_STRENGTH", "ENHANCEMENT_MODEL_LUMINANCE_THRESHOLD", "ENHANCEMENT_MODEL_LUMINANCE_TARGET",
)

DESCRIPTION = (
    "OpenCV and Pillow only. The learned enhancement stages (Zero-DCE, EDSR, NAFNet, Real-ESRGAN, GFPGAN, "
    "PureVision, PureScale, colourisation) were retired; face detection and identity checks use YuNet and SFace."
)


def active_policy():
    return LIGHTWEIGHT


def retired_capabilities():
    """What was retired, as served to clients and recorded on every result."""
    return [
        {"key": key, "name": name, "retired_on": RETIRED_ON, "status": "retired"}
        for key, (name, _flags) in RETIRED_CAPABILITIES.items()
    ]


def describe():
    """The policy as recorded in every result's parameters."""
    return {
        "name": LIGHTWEIGHT,
        "description": DESCRIPTION,
        "heavy_restoration_available": False,
        "retired_capabilities": retired_capabilities(),
        "documentation": RETIREMENT_DOCUMENT,
    }


def retirement_message(key):
    """The sentence for a request that names a retired capability."""
    name = RETIRED_CAPABILITIES[key][0]
    return f"{name} was retired on {RETIRED_ON} and is no longer available; see {RETIREMENT_DOCUMENT}."
